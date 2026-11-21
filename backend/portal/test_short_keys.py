from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from django.conf import settings
from django.core.cache import caches
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from .ingest import relay_token
from .models import Channel, Environment, User, SHORT_KEY_ALPHABET, normalize_short_key, short_key_digest


@override_settings(SMA_REALTIME_ENABLED=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ShortKeyTests(TestCase):
    def setUp(self):
        caches['rtmp_auth'].clear()
        self.a = Environment.objects.create(name='SMA A', slug='short-a')
        self.b = Environment.objects.create(name='SMA B', slug='short-b')
        self.ca = Channel(environment=self.a, name='Drone');self.key = self.ca.issue_key();self.ca.save()
        self.cb = Channel(environment=self.b, name='Drone');self.other_key = self.cb.issue_key();self.cb.save()
        self.internal = {'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}
        self.admin = User.objects.create_user(username='short_admin', role='admin', environment=self.a, must_change_password=False)

    def send(self, key=None, action='publish', clientid='12', **extra):
        data = {'name': key or self.key, 'app': 'live', 'clientid': clientid, 'addr': '192.0.2.7',
                'call': {'publish':'publish','update':'update_publish','done':'publish_done'}[action]}
        data.update(extra)
        return self.client.post('/internal/rtmp/'+action+'/', data, **self.internal)

    def relay(self, channel=None, token=None, action='publish'):
        channel = channel or self.ca;channel.refresh_from_db()
        return self.client.post('/internal/relay/'+action+'/', {'name':channel.stream_id,'token':token or relay_token(channel),
            'app':'live','clientid':'88','call':{'publish':'publish','update':'update_publish','done':'publish_done'}[action]}, **self.internal)

    def test_generated_keys_are_twelve_characters_and_not_catalog_identifiers(self):
        self.assertEqual(len(SHORT_KEY_ALPHABET), 32)
        self.assertEqual(len(self.key), 12)
        self.assertTrue(set(self.key) <= set(SHORT_KEY_ALPHABET))
        self.assertNotEqual(self.key, self.other_key)
        self.assertNotEqual(self.key, self.ca.stream_id)
        self.assertEqual(self.ca.key_digest, short_key_digest(self.key))
        self.assertNotIn(self.key, self.ca.key_digest)
        self.assertEqual(self.ca.key_format, 'short')

    def test_lowercase_and_grouped_codes_resolve_to_same_canonical_stream(self):
        forms = [self.key, self.key.lower(), '-'.join(self.key[i:i+4] for i in range(0,12,4)).lower()]
        for code in forms:
            response = self.send(code);self.assertEqual(response.status_code,302)
            self.ca.refresh_from_db()
            url = urlsplit(response['Location'])
            self.assertEqual(url.path, '/live/'+self.ca.stream_id)
            self.assertEqual(url.hostname, '127.0.0.1');self.assertEqual(url.port,1936)
            self.assertEqual(parse_qs(url.query)['token'], [relay_token(self.ca)])
            self.assertNotIn(self.key, response['Location'])
            self.assertEqual(self.send(code, action='update').status_code,204)
            self.assertEqual(self.send(code, action='done').status_code,204)

    def test_public_ingress_rejects_wrong_keys_internal_ids_and_ambiguous_parameters(self):
        wrong = self.key[:-1] + next(c for c in SHORT_KEY_ALPHABET if c != self.key[-1])
        for key in [wrong, self.ca.stream_id, self.key+'A', '000000000000', '../'+self.key, self.key+'?token=x']:
            self.assertEqual(self.send(key).status_code,403)
        self.assertEqual(self.send(token='A'*43).status_code,403)
        self.assertEqual(self.send(name=[self.key,self.other_key]).status_code,403)
        self.assertEqual(self.send(addr=['192.0.2.7','192.0.2.8']).status_code,429)
        self.assertEqual(self.client.post('/internal/rtmp/publish/',{}).status_code,403)

    def test_collision_retries_and_rotation_never_reuses_its_previous_code(self):
        candidate = next(c for c in SHORT_KEY_ALPHABET if c*12 not in [self.key,self.other_key])*12
        with patch('portal.models.secrets.choice',side_effect=list(self.other_key+self.key+candidate)):
            result = self.ca.issue_key()
        self.assertEqual(result,candidate)
        self.assertNotEqual(result,self.key)

    def test_database_rejects_duplicate_short_codes_across_smas(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Channel.objects.create(environment=self.b,name='Duplicate',key_format='short',key_digest=self.ca.key_digest)

    def test_channel_form_issues_short_key_and_complete_drone_url(self):
        self.client.force_login(self.admin)
        response=self.client.post('/canais/novo/',{'name':'Novo drone'})
        self.assertEqual(response.status_code,200)
        key=response.context['stream_key']
        self.assertEqual(len(key),12)
        self.assertContains(response,'value="'+response.context['server_url']+'/'+key+'"')
        self.assertContains(response,'Endereço completo para o drone')
        self.assertNotContains(response,'?token=')

    def test_rotating_legacy_key_in_ui_issues_short_key_and_rejects_old_publishers(self):
        old=self.ca.issue_key(legacy=True);self.ca.save()
        name,token=old.split('?token=')
        self.assertEqual(self.send(name,token=token).status_code,302)
        self.client.force_login(self.admin)
        response=self.client.post('/canais/'+str(self.ca.pk)+'/chave/')
        self.assertEqual(response.status_code,200)
        new=response.context['stream_key'];self.assertEqual(len(new),12)
        self.ca.refresh_from_db();self.assertEqual(self.ca.key_format,'short')
        self.assertEqual(self.send(name,token=token).status_code,403)
        self.assertEqual(self.send(name,token=token,action='update').status_code,403)
        self.assertEqual(self.send(new).status_code,302)

    @patch('portal.ingest.socket.gethostbyname',return_value='172.23.0.5')
    def test_relay_credentials_are_bound_to_channel_key_and_current_publisher(self, resolve):
        self.assertEqual(self.relay().status_code,403)
        self.send();self.ca.refresh_from_db();token=relay_token(self.ca)
        self.assertEqual(self.relay(token=token).status_code,302)
        self.assertEqual(self.relay(self.cb,token=token).status_code,403)
        self.assertEqual(self.relay(token=self.key).status_code,403)
        self.assertEqual(self.send(clientid='15').status_code,302)
        # A second source now has a separate stream and cannot replace the first.
        self.assertEqual(self.relay(token=token).status_code,302)
        self.assertEqual(self.send(action='done',clientid='15').status_code,204)
        self.assertEqual(self.relay(token=token).status_code,302)
        self.ca.refresh_from_db();current=relay_token(self.ca)
        self.ca.issue_key();self.ca.save()
        self.assertEqual(self.relay(token=current).status_code,403)

    def test_disabled_environment_or_stale_source_stops_both_stages(self):
        self.send();self.ca.refresh_from_db();token=relay_token(self.ca)
        self.ca.last_seen=timezone.now()-timedelta(seconds=30);self.ca.save()
        self.assertEqual(self.relay(token=token).status_code,403)
        self.a.active=False;self.a.save()
        self.assertEqual(self.send().status_code,403)
        self.assertEqual(self.send(action='update').status_code,403)

    def test_publish_limit_is_per_origin_and_does_not_block_existing_updates(self):
        for _ in range(60): self.assertEqual(self.send().status_code,302)
        self.assertEqual(self.send().status_code,429)
        self.assertEqual(self.send(action='update').status_code,204)
        self.assertEqual(self.send(addr='192.0.2.8').status_code,302)

    def test_normalization_does_not_accept_spaces_unicode_or_extra_separators(self):
        for code in [' '+self.key,self.key+' ',self.key[:4]+'--'+self.key[4:], 'ſ'*12]:
            self.assertIsNone(normalize_short_key(code))

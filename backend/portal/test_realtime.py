import hashlib
import socket
from unittest.mock import patch
from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from .models import Channel, Environment, User


@override_settings(SMA_REALTIME_ENABLED=True,
    SMA_OWNER_HOST='central.example.test',
    SMA_TENANT_HOSTS={'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'},
    ALLOWED_HOSTS=['testserver', 'backend', 'central.example.test', 'sma1.example.test', 'sma2.example.test'],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RealtimeAuthorizationTests(TestCase):
    def setUp(self):
        patcher = patch('portal.ingest.socket.gethostbyname', return_value='172.19.0.5')
        self.resolve = patcher.start(); self.addCleanup(patcher.stop)
        self.a = Environment.objects.create(name='SMA 1', slug='sma1')
        self.b = Environment.objects.create(name='SMA 2', slug='sma2')
        self.owner = User.objects.create_user(username='rtc_owner', role=User.Role.OWNER, must_change_password=False)
        self.admin = User.objects.create_user(username='rtc_admin', role=User.Role.ADMIN, environment=self.a, must_change_password=False)
        self.viewer = User.objects.create_user(username='rtc_viewer', role=User.Role.VIEWER, environment=self.a, must_change_password=False)
        self.ca = Channel(environment=self.a, name='A'); self.key_a = self.ca.issue_key(legacy=True).split('token=')[1]; self.ca.save()
        self.cb = Channel(environment=self.b, name='B'); self.key_b = self.cb.issue_key(legacy=True).split('token=')[1]; self.cb.save()

    def view(self, user, channel, host, internal=True):
        client = Client()
        if user: client.force_login(user)
        return client.get('/internal/realtime/view/'+channel.stream_id+'/', HTTP_HOST=host, secure=True,
            HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN if internal else '')

    def publish(self, name=None, token=None, internal=True):
        return self.client.post('/internal/realtime/publish/',
            {'name': name or self.ca.stream_id, 'token': token or self.key_a}, HTTP_HOST='backend',
            HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN if internal else '')

    def ingress(self, action='publish', **extra):
        data = {'name': self.ca.stream_id, 'token': self.key_a, 'clientid': '1234', 'app': 'live', 'addr': '192.0.2.11',
                'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action]}
        data.update(extra)
        return self.client.post('/internal/rtmp/'+action+'/', data, HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)

    def test_owner_can_read_both_and_tenant_accounts_only_their_own(self):
        for channel in [self.ca, self.cb]:
            response = self.view(self.owner, channel, 'central.example.test')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'allowed': True, 'online': False})
        for user in [self.admin, self.viewer]:
            self.assertEqual(self.view(user, self.ca, 'sma1.example.test').status_code, 200)
            self.assertEqual(self.view(user, self.cb, 'sma1.example.test').status_code, 403)
            self.assertEqual(self.view(user, self.ca, 'sma2.example.test').status_code, 403)
            self.assertEqual(self.view(user, self.ca, 'central.example.test').status_code, 403)
        self.assertEqual(self.view(self.owner, self.ca, 'sma1.example.test').status_code, 403)

    def test_anonymous_internal_forgery_password_change_and_deactivation(self):
        self.assertEqual(self.view(None, self.ca, 'sma1.example.test').status_code, 401)
        self.assertEqual(self.view(self.viewer, self.ca, 'sma1.example.test', internal=False).status_code, 403)
        self.viewer.must_change_password = True; self.viewer.save()
        self.assertEqual(self.view(self.viewer, self.ca, 'sma1.example.test').status_code, 403)
        self.viewer.must_change_password = False; self.viewer.is_active = False; self.viewer.save()
        self.assertEqual(self.view(self.viewer, self.ca, 'sma1.example.test').status_code, 401)
        self.a.active = False; self.a.save()
        self.assertEqual(self.view(self.owner, self.ca, 'central.example.test').status_code, 403)
        self.cb.active = False; self.cb.save()
        self.assertEqual(self.view(self.owner, self.cb, 'central.example.test').status_code, 403)

    def test_ingress_creates_authenticated_relay_without_changing_key(self):
        before = self.ca.key_digest
        response = self.ingress()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('rtmp://127.0.0.1:1936/live/'+self.ca.stream_id+'?token='))
        self.assertNotIn(self.key_a, response['Location'])
        self.ca.refresh_from_db()
        self.assertEqual(self.ca.key_digest, before)
        self.assertEqual(self.ca.publisher_client_id, '1234')
        self.assertEqual(self.publish().status_code, 204)
        self.ca.refresh_from_db()
        self.assertEqual(self.ca.publisher_client_id, '1234')
        self.assertEqual(self.ingress('update').status_code, 204)
        self.assertEqual(self.ingress('done').status_code, 204)
        self.assertEqual(self.publish().status_code, 403)

    def test_relay_denies_unpublished_wrong_duplicate_rotated_and_disabled_keys(self):
        self.assertEqual(self.publish().status_code, 403)
        self.ingress()
        self.assertEqual(self.publish(internal=False).status_code, 403)
        self.assertEqual(self.publish(token=self.key_b).status_code, 403)
        self.assertEqual(self.publish(name=self.cb.stream_id).status_code, 403)
        self.assertEqual(self.publish(token=[self.key_a, self.key_a]).status_code, 403)
        self.ca.refresh_from_db(); self.ca.issue_key(legacy=True); self.ca.save()
        self.assertEqual(self.publish().status_code, 403)
        self.assertEqual(self.ingress('update').status_code, 403)

    def test_periodic_internal_reader_does_not_accept_external_publication_shape(self):
        url = '/internal/rtmp/update/'
        data = {'name': self.ca.stream_id, 'clientid': '9', 'app': '', 'call': 'update_play'}
        request = lambda value: self.client.post(url, value, HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
        self.assertEqual(request(data).status_code, 204)
        self.assertEqual(request({**data, 'token': self.key_a}).status_code, 403)
        self.assertEqual(request({**data, 'app': 'live'}).status_code, 403)
        self.assertEqual(request({**data, 'clientid': ['1', '2']}).status_code, 403)
        self.assertEqual(request({**data, 'name': '../other'}).status_code, 403)

    @override_settings(SMA_REALTIME_ENABLED=False)
    def test_disabled_integration_preserves_original_ingress(self):
        self.assertEqual(self.ingress().status_code, 302)
        self.assertEqual(self.publish().status_code, 403)
        self.assertEqual(self.view(self.owner, self.ca, 'central.example.test').status_code, 403)

    def test_unavailable_realtime_destination_keeps_authenticated_hls_source(self):
        self.resolve.side_effect = socket.gaierror('test service unavailable')
        self.assertEqual(self.ingress().status_code, 302)
        from .ingest import relay_token
        self.ca.refresh_from_db()
        response = self.client.post('/internal/relay/publish/', {'name':self.ca.stream_id, 'token':relay_token(self.ca),
            'app':'live','call':'publish','clientid':'99'}, HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
        self.assertEqual(response.status_code,204)
        self.ca.refresh_from_db()
        self.assertEqual(self.ca.publisher_client_id, '1234')

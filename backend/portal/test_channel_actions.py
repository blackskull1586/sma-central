"""Credential disclosure and channel deletion preserve tenant isolation."""
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .ingest import relay_token
from .models import AuditEvent, Channel, Environment, User, short_key_digest


TENANT_HOSTS = {f'sma{i}.example.test': f'sma{i}' for i in range(1, 6)}
RTMP_URLS = {f'sma{i}': f'rtmp://sma{i}.example.test:19350/live' for i in range(1, 6)}
OWNER_HOST = 'central.example.test'


@override_settings(SECRET_KEY='channel-credentials-test-primary-secret', SECRET_KEY_FALLBACKS=[])
class ChannelKeyStorageTests(TestCase):
    def setUp(self):
        self.environment = Environment.objects.create(name='Storage A', slug='storage-a')
        self.other_environment = Environment.objects.create(name='Storage B', slug='storage-b')
        self.channel = Channel(environment=self.environment, name='Camera')
        self.key = self.channel.issue_key()
        self.channel.save()

    def test_saved_key_is_recoverable_without_storing_plaintext(self):
        saved = Channel.objects.get(pk=self.channel.pk)
        self.assertEqual(saved.reveal_key(), self.key)
        self.assertTrue(saved.key_ciphertext)
        self.assertNotIn(self.key, saved.key_ciphertext)
        self.assertNotEqual(saved.key_ciphertext, self.key)
        self.assertEqual(saved.key_digest, short_key_digest(self.key))

    def test_ciphertext_corruption_or_missing_ciphertext_returns_no_key(self):
        original = self.channel.key_ciphertext
        for value in ['', 'not-a-fernet-token', original[:-8], '!' + original[1:]]:
            with self.subTest(value_length=len(value)):
                self.channel.key_ciphertext = value
                self.assertIsNone(self.channel.reveal_key())
        self.channel.key_ciphertext = original
        self.assertEqual(self.channel.reveal_key(), self.key)

    def test_encrypted_key_is_bound_to_channel_environment_stream_format_and_digest(self):
        substitutions = {
            'pk': uuid.uuid4(),
            'environment_id': self.other_environment.pk,
            'stream_id': uuid.uuid4().hex,
            'key_format': 'legacy',
            'key_digest': '0' * 64,
        }
        for field, wrong_value in substitutions.items():
            with self.subTest(field=field):
                original = getattr(self.channel, field)
                setattr(self.channel, field, wrong_value)
                try:
                    self.assertIsNone(self.channel.reveal_key())
                finally:
                    setattr(self.channel, field, original)
        self.assertEqual(self.channel.reveal_key(), self.key)

    def test_ciphertext_cannot_be_transplanted_to_another_channel(self):
        other = Channel(environment=self.environment, name='Other camera')
        other.key_ciphertext = self.channel.key_ciphertext
        other.key_format = self.channel.key_format
        other.key_digest = self.channel.key_digest
        other.stream_id = self.channel.stream_id
        self.assertIsNone(other.reveal_key())

    def test_renaming_channel_preserves_its_existing_key(self):
        self.channel.name = 'Renamed camera'
        self.channel.save(update_fields=['name'])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.reveal_key(), self.key)

    def test_rotation_updates_recoverable_key_and_rejects_old_encrypted_value(self):
        old_ciphertext = self.channel.key_ciphertext
        new_key = self.channel.issue_key()
        self.channel.save()
        self.channel.refresh_from_db()
        self.assertNotEqual(new_key, self.key)
        self.assertEqual(self.channel.reveal_key(), new_key)
        self.assertEqual(self.channel.key_digest, short_key_digest(new_key))
        self.channel.key_ciphertext = old_ciphertext
        self.assertIsNone(self.channel.reveal_key())

    def test_secret_key_rotation_can_use_explicit_fallback(self):
        old_secret = settings.SECRET_KEY
        with override_settings(SECRET_KEY='channel-credentials-new-test-secret', SECRET_KEY_FALLBACKS=[old_secret]):
            self.assertEqual(self.channel.reveal_key(), self.key)
            new_key = self.channel.issue_key()
            self.channel.save()
        with override_settings(SECRET_KEY='channel-credentials-new-test-secret', SECRET_KEY_FALLBACKS=[]):
            self.channel.refresh_from_db()
            self.assertEqual(self.channel.reveal_key(), new_key)
        with override_settings(SECRET_KEY=old_secret, SECRET_KEY_FALLBACKS=[]):
            self.assertIsNone(self.channel.reveal_key())

    def test_wrong_secret_does_not_reveal_or_rotate_existing_key(self):
        original = (self.channel.key_digest, self.channel.key_ciphertext)
        with override_settings(SECRET_KEY='unrelated-channel-test-secret', SECRET_KEY_FALLBACKS=[]):
            self.assertIsNone(self.channel.reveal_key())
        self.assertEqual((self.channel.key_digest, self.channel.key_ciphertext), original)
        self.assertEqual(self.channel.reveal_key(), self.key)

    def test_new_legacy_fixture_is_recoverable_but_preexisting_digest_is_not(self):
        key = self.channel.issue_key(legacy=True)
        self.channel.save()
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.reveal_key(), key)
        self.channel.key_ciphertext = ''
        self.channel.save(update_fields=['key_ciphertext'])
        digest = self.channel.key_digest
        self.assertIsNone(self.channel.reveal_key())
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.key_digest, digest)


@override_settings(
    SMA_REALTIME_ENABLED=True,
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    SMA_RTMP_URLS=RTMP_URLS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class ChannelActionAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.environments = []
        cls.admins = []
        cls.channels = []
        cls.keys = []
        for index in range(1, 6):
            environment = Environment.objects.create(name=f'SMA {index}', slug=f'sma{index}')
            admin = User.objects.create_user(username=f'action_admin_{index}', role=User.Role.ADMIN,
                environment=environment, must_change_password=False)
            channel = Channel(environment=environment, name=f'Camera {index}')
            key = channel.issue_key()
            channel.save()
            cls.environments.append(environment)
            cls.admins.append(admin)
            cls.channels.append(channel)
            cls.keys.append(key)
        cls.owner = User.objects.create_user(username='action_owner', role=User.Role.OWNER,
            must_change_password=False)
        cls.viewer = User.objects.create_user(username='action_viewer', role=User.Role.VIEWER,
            environment=cls.environments[0], must_change_password=False)

    def setUp(self):
        caches['rtmp_auth'].clear()
        self.channel = self.channels[0]
        self.host = 'sma1.example.test'
        self.internal = {'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}
        self.client.force_login(self.admins[0])

    def path(self, action, channel=None):
        channel = channel or self.channel
        return f'/canais/{channel.pk}/{action}/'

    def request(self, method, path, data=None, host=None, client=None, **extra):
        return getattr(client or self.client, method)(path, data=data or {},
            HTTP_HOST=host or self.host, secure=True, **extra)

    def notification(self, action='publish'):
        return self.client.post(f'/internal/rtmp/{action}/', {
            'name': self.keys[0], 'app': 'live', 'clientid': '123', 'addr': '192.0.2.18',
            'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action],
        }, HTTP_HOST='backend', **self.internal)

    def test_each_of_five_admins_can_consult_own_credentials_without_mutating_channel(self):
        for index, (admin, channel, key) in enumerate(zip(self.admins, self.channels, self.keys), start=1):
            with self.subTest(sma=index):
                self.client.force_login(admin)
                channel.publisher_client_id = f'{index}23'
                channel.last_seen = timezone.now()
                channel.save(update_fields=['publisher_client_id', 'last_seen'])
                before = Channel.objects.filter(pk=channel.pk).values().get()
                audit_before = AuditEvent.objects.count()
                response = self.request('get', self.path('credenciais', channel), host=f'sma{index}.example.test')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['stream_key'], key)
                self.assertEqual(response.context['server_url'], RTMP_URLS[f'sma{index}'])
                self.assertContains(response, RTMP_URLS[f'sma{index}'] + '/' + key)
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(Channel.objects.filter(pk=channel.pk).values().get(), before)
                self.assertEqual(AuditEvent.objects.count(), audit_before + 1)
                event = AuditEvent.objects.first()
                self.assertEqual(event.actor_id, admin.pk)
                self.assertEqual(event.environment_id, channel.environment_id)
                self.assertNotIn(key, event.target + event.action)
                self.assertNotIn(channel.key_ciphertext, event.target + event.action)

    def test_owner_uses_selected_management_environment_for_both_actions(self):
        self.client.force_login(self.owner)
        for channel in self.channels:
            self.assertEqual(self.request('post', f'/ambientes/{channel.environment_id}/abrir/', host=OWNER_HOST).status_code, 302)
            self.assertEqual(self.request('get', self.path('credenciais', channel), host=OWNER_HOST).status_code, 200)
            self.assertEqual(self.request('get', self.path('excluir', channel), host=OWNER_HOST).status_code, 200)
        # A monitoring filter must not replace the explicit management selection.
        for action in ['credenciais', 'excluir']:
            self.assertEqual(self.request('get', self.path(action, self.channels[0]),
                {'sma': str(self.environments[0].pk)}, host=OWNER_HOST).status_code, 404)

    def test_foreign_channel_and_foreign_domain_cannot_reveal_or_delete(self):
        foreign = self.channels[1]
        for action in ['credenciais', 'excluir']:
            self.assertEqual(self.request('get', self.path(action, foreign)).status_code, 404)
            self.assertEqual(self.request('get', self.path(action), host='sma2.example.test').status_code, 403)
        self.assertEqual(self.request('post', self.path('excluir', foreign), {'confirmation': str(foreign.pk)}).status_code, 404)
        self.assertEqual(self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)},
            host='sma2.example.test').status_code, 403)
        self.assertEqual(Channel.objects.count(), 5)

    def test_viewer_and_anonymous_users_cannot_reveal_or_delete(self):
        self.client.force_login(self.viewer)
        for action in ['credenciais', 'excluir']:
            response = self.request('get', self.path(action))
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(self.keys[0], response.content.decode())
        self.assertEqual(self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)}).status_code, 403)
        self.client.logout()
        for method, action in [('get', 'credenciais'), ('get', 'excluir'), ('post', 'excluir')]:
            response = self.request(method, self.path(action), {'confirmation': str(self.channel.pk)})
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response['Location'])
        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())

    def test_forced_password_change_and_inactive_environment_block_both_actions(self):
        admin = self.admins[0]
        admin.must_change_password = True
        admin.save(update_fields=['must_change_password'])
        for action in ['credenciais', 'excluir']:
            self.assertRedirects(self.request('get', self.path(action)), '/conta/senha/', fetch_redirect_response=False)
        self.assertRedirects(self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)}),
            '/conta/senha/', fetch_redirect_response=False)
        admin.must_change_password = False
        admin.save(update_fields=['must_change_password'])
        self.environments[0].active = False
        self.environments[0].save(update_fields=['active'])
        for action in ['credenciais', 'excluir']:
            self.assertEqual(self.request('get', self.path(action)).status_code, 403)
        self.assertEqual(self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)}).status_code, 403)
        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())

    def test_credential_consultation_rejects_post_and_does_not_rotate_key(self):
        original_digest = self.channel.key_digest
        response = self.request('post', self.path('credenciais'))
        self.assertEqual(response.status_code, 405)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.key_digest, original_digest)
        self.assertEqual(self.channel.reveal_key(), self.keys[0])

    def test_unrecoverable_existing_key_stays_valid_and_is_not_silently_replaced(self):
        for stored_value in ['', 'corrupt-stored-key']:
            with self.subTest(stored_value=stored_value):
                self.channel.key_ciphertext = stored_value
                self.channel.save(update_fields=['key_ciphertext'])
                before = Channel.objects.filter(pk=self.channel.pk).values().get()
                response = self.request('get', self.path('credenciais'))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context['stream_key'])
                self.assertNotContains(response, self.keys[0])
                self.assertNotContains(response, 'value="None"')
                self.assertEqual(Channel.objects.filter(pk=self.channel.pk).values().get(), before)
                self.assertEqual(self.notification().status_code, 302)

    def test_create_rotate_and_consult_keep_returning_current_key(self):
        created = self.request('post', '/canais/novo/', {'name': 'New action camera'})
        self.assertEqual(created.status_code, 200)
        channel = Channel.objects.get(environment=self.environments[0], name='New action camera')
        first_key = created.context['stream_key']
        self.assertEqual(self.request('get', self.path('credenciais', channel)).context['stream_key'], first_key)
        rotated = self.request('post', self.path('chave', channel))
        self.assertEqual(rotated.status_code, 200)
        second_key = rotated.context['stream_key']
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(self.request('get', self.path('credenciais', channel)).context['stream_key'], second_key)
        channel.refresh_from_db()
        self.assertEqual(channel.reveal_key(), second_key)

    def test_dashboard_and_catalogs_expose_actions_without_any_publishing_secret(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(HLS_DIR=Path(directory)):
            self.channel.last_seen = timezone.now()
            self.channel.save(update_fields=['last_seen'])
            (Path(directory) / f'{self.channel.stream_id}.m3u8').write_text('#EXTM3U\n')
            page = self.request('get', '/painel/')
            self.assertContains(page, self.path('credenciais'))
            self.assertContains(page, self.path('excluir'))
            for unchanged_action in ['editar', 'chave', 'estado']:
                self.assertContains(page, self.path(unchanged_action))
            for response in [page, self.request('get', '/streams.json'), self.request('get', '/api/channels/')]:
                self.assertEqual(response.status_code, 200)
                for channel, key in zip(self.channels, self.keys):
                    self.assertNotContains(response, key)
                    self.assertNotContains(response, channel.key_digest)
                    self.assertNotContains(response, channel.key_ciphertext)
                self.assertNotContains(response, 'key_ciphertext')

    def test_get_delete_is_confirmation_only_and_repeated_values_are_rejected(self):
        before = Channel.objects.filter(pk=self.channel.pk).values().get()
        response = self.request('get', self.path('excluir'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.channel.name)
        self.assertNotContains(response, self.keys[0])
        for data in [{}, {'confirmation': 'yes'}, {'confirmation': str(self.channels[1].pk)},
                     {'confirmation': [str(self.channel.pk), str(self.channel.pk)]}]:
            with self.subTest(data=data):
                self.assertEqual(self.request('post', self.path('excluir'), data).status_code, 400)
                self.assertEqual(Channel.objects.filter(pk=self.channel.pk).values().get(), before)
        self.assertEqual(self.request('put', self.path('excluir')).status_code, 405)

    def test_delete_requires_csrf_and_valid_confirmation_then_preserves_other_channels(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admins[0])
        data = {'confirmation': str(self.channel.pk)}
        self.assertEqual(self.request('post', self.path('excluir'), data, client=browser).status_code, 403)
        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())
        confirmation = self.request('get', self.path('excluir'), client=browser)
        self.assertEqual(confirmation.status_code, 200)
        data['csrfmiddlewaretoken'] = browser.cookies[settings.CSRF_COOKIE_NAME].value
        other_channels_before = list(Channel.objects.exclude(pk=self.channel.pk).order_by('pk').values())
        response = self.request('post', self.path('excluir'), data, client=browser,
            HTTP_ORIGIN=f'https://{self.host}', HTTP_REFERER=f'https://{self.host}' + self.path('excluir'))
        self.assertRedirects(response, '/painel/', fetch_redirect_response=False)
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())
        self.assertEqual(list(Channel.objects.order_by('pk').values()), other_channels_before)
        event = AuditEvent.objects.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_id, self.admins[0].pk)
        self.assertEqual(event.environment_id, self.environments[0].pk)
        self.assertNotIn(self.keys[0], event.target + event.action)
        self.assertNotIn(self.channel.key_ciphertext, event.target + event.action)
        self.assertEqual(self.request('get', self.path('credenciais')).status_code, 404)
        self.assertEqual(self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)}).status_code, 404)

    def test_owner_can_delete_in_selected_environment(self):
        self.client.force_login(self.owner)
        channel = self.channels[4]
        self.request('post', f'/ambientes/{channel.environment_id}/abrir/', host=OWNER_HOST)
        response = self.request('post', self.path('excluir', channel), {'confirmation': str(channel.pk)}, host=OWNER_HOST)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Channel.objects.filter(pk=channel.pk).exists())
        self.assertEqual(Channel.objects.count(), 4)

    def test_audit_failure_does_not_commit_channel_deletion(self):
        with patch('portal.views.audit', side_effect=RuntimeError('Simulated audit failure')):
            with self.assertRaises(RuntimeError):
                self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)})
        self.assertTrue(Channel.objects.filter(pk=self.channel.pk).exists())

    def test_deleting_active_channel_revokes_media_authorization_and_publish_callbacks(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(HLS_DIR=Path(directory)):
            self.assertEqual(self.notification().status_code, 302)
            self.channel.refresh_from_db()
            token = relay_token(self.channel)
            playlist = f'/hls/{self.channel.stream_id}.m3u8'
            (Path(directory) / f'{self.channel.stream_id}.m3u8').write_text('#EXTM3U\n')
            rtc_path = f'/internal/realtime/view/{self.channel.stream_id}/'
            self.assertEqual(self.request('get', '/internal/hls/', HTTP_X_ORIGINAL_URI=playlist, **self.internal).status_code, 204)
            self.assertEqual(self.request('get', rtc_path, **self.internal).json(), {'allowed': True, 'online': True})
            response = self.request('post', self.path('excluir'), {'confirmation': str(self.channel.pk)})
            self.assertEqual(response.status_code, 302)
            for suffix in ['.m3u8', '-123.ts']:
                self.assertEqual(self.request('get', '/internal/hls/',
                    HTTP_X_ORIGINAL_URI=f'/hls/{self.channel.stream_id}{suffix}', **self.internal).status_code, 403)
            self.assertEqual(self.request('get', rtc_path, **self.internal).status_code, 403)
            self.assertEqual(self.request('get', '/streams.json').json(), [])
            self.assertEqual(self.request('get', '/api/channels/').json(), [])
            for action in ['publish', 'update', 'done']:
                self.assertEqual(self.notification(action).status_code, 403)
                relay_response = self.client.post(f'/internal/relay/{action}/', {
                    'name': self.channel.stream_id, 'token': token, 'app': 'live', 'clientid': '456',
                    'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action],
                }, HTTP_HOST='backend', **self.internal)
                self.assertEqual(relay_response.status_code, 403)
            self.assertEqual(self.client.post('/internal/realtime/publish/', {
                'name': self.channel.stream_id, 'token': token,
            }, HTTP_HOST='backend', **self.internal).status_code, 403)

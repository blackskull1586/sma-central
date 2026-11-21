import tempfile
import uuid
from pathlib import Path
from django.conf import settings
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .models import User, Environment, Channel


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    ALLOWED_HOSTS=['testserver', 'central.example.test', 'sma1.example.test', 'sma2.example.test'],
    SMA_OWNER_HOST='central.example.test',
    SMA_TENANT_HOSTS={'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'},
)
class OwnerMonitorTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.files = Path(self.directory.name)
        self.override = override_settings(HLS_DIR=self.files)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.a = Environment.objects.create(name='SMA 1', slug='sma1')
        self.b = Environment.objects.create(name='SMA 2', slug='sma2')
        self.owner = User.objects.create_user(username='monitor_owner', password='Test-Monitor-Only-392!',
            role=User.Role.OWNER, must_change_password=False)
        self.admin = User.objects.create_user(username='monitor_admin', password='Test-Monitor-Only-392!',
            role=User.Role.ADMIN, environment=self.a, must_change_password=False)
        self.viewer = User.objects.create_user(username='monitor_viewer', password='Test-Monitor-Only-392!',
            role=User.Role.VIEWER, environment=self.a, must_change_password=False)
        self.ca = self.channel(self.a)
        self.cb = self.channel(self.b)
        self.client.force_login(self.owner)

    def channel(self, environment):
        channel = Channel(environment=environment, name='Câmera de equipe')
        channel.issue_key()
        channel.last_seen = timezone.now()
        channel.save()
        (self.files / f'{channel.stream_id}.m3u8').write_text('#EXTM3U\n')
        return channel

    def get(self, path):
        return self.client.get(path, HTTP_HOST='central.example.test', secure=True)

    def authorize(self, channel):
        return self.client.get('/internal/hls/', secure=True, HTTP_HOST='central.example.test',
            HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN,
            HTTP_X_ORIGINAL_URI=f'/hls/{channel.stream_id}.m3u8')

    def test_owner_opens_monitor_without_selecting_an_environment_and_sees_both_origins(self):
        page = self.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'aria-label="Selecionar SMA"')
        for label in ['Todos os SMA', 'SMA 1', 'SMA 2', 'Monitoramento']:
            self.assertContains(page, label)
        self.assertContains(page, 'data-default-select-all="true"')
        rows = self.get('/streams.json').json()
        self.assertCountEqual([r['id'] for r in rows], [self.ca.stream_id, self.cb.stream_id])
        self.assertCountEqual([r['environment']['name'] for r in rows], ['SMA 1', 'SMA 2'])
        self.assertTrue(all(r['url'] == f"/hls/{r['id']}.m3u8" for r in rows))
        self.assertFalse(any('token' in r['url'] or 'key_digest' in r for r in rows))

    def test_owner_filters_each_sma_and_returns_to_all(self):
        for env, channel in [(self.a, self.ca), (self.b, self.cb)]:
            page = self.get(f'/?sma={env.pk}')
            self.assertEqual(page.context['monitor_environment'], env)
            self.assertContains(page, f'data-catalog-url="/streams.json?sma={env.pk}"')
            self.assertEqual([r['id'] for r in self.get(f'/streams.json?sma={env.pk}').json()], [channel.stream_id])
        self.assertEqual(len(self.get('/streams.json?sma=all').json()), 2)

    def test_monitor_filter_does_not_change_management_selection(self):
        session = self.client.session
        session['environment_id'] = str(self.b.pk)
        session.save()
        self.get(f'/?sma={self.a.pk}')
        self.get('/streams.json?sma=all')
        self.assertEqual(self.client.session['environment_id'], str(self.b.pk))
        self.assertEqual([r['id'] for r in self.get('/api/channels/').json()], [str(self.cb.pk)])

    def test_owner_can_authorize_both_hls_streams_without_management_scope(self):
        self.assertEqual(self.authorize(self.ca).status_code, 204)
        self.assertEqual(self.authorize(self.cb).status_code, 204)
        session = self.client.session
        session['environment_id'] = str(self.a.pk)
        session.save()
        self.assertEqual(self.authorize(self.cb).status_code, 204)

    def test_disabled_environment_and_channel_stay_inaccessible_to_owner(self):
        self.a.active = False
        self.a.save(update_fields=['active'])
        self.cb.active = False
        self.cb.save(update_fields=['active'])
        self.assertEqual(self.get('/streams.json').json(), [])
        self.assertEqual(self.authorize(self.ca).status_code, 403)
        self.assertEqual(self.authorize(self.cb).status_code, 403)
        self.assertEqual(self.get(f'/streams.json?sma={self.a.pk}').status_code, 404)

    def test_admin_and_viewer_cannot_request_all_or_other_sma(self):
        for user in [self.admin, self.viewer]:
            client = Client()
            client.force_login(user)
            own = client.get('/streams.json', HTTP_HOST='sma1.example.test', secure=True)
            self.assertEqual([r['id'] for r in own.json()], [self.ca.stream_id])
            page = client.get('/', HTTP_HOST='sma1.example.test', secure=True)
            self.assertNotContains(page, 'aria-label="Selecionar SMA"')
            for selection in ['all', str(self.b.pk)]:
                for path in ['/', '/streams.json']:
                    self.assertEqual(client.get(path + '?sma=' + selection,
                        HTTP_HOST='sma1.example.test', secure=True).status_code, 403)
            self.assertEqual(client.get('/internal/hls/', HTTP_HOST='sma1.example.test', secure=True,
                HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN,
                HTTP_X_ORIGINAL_URI=f'/hls/{self.cb.stream_id}.m3u8').status_code, 403)

    def test_invalid_filters_fail_cleanly_and_new_active_sma_appears_automatically(self):
        for query in ['sma=invalid', 'sma=', f'sma={uuid.uuid4()}', f'sma={self.a.pk}&sma={self.b.pk}']:
            self.assertEqual(self.get('/streams.json?' + query).status_code, 404)
        c = Environment.objects.create(name='SMA 3', slug='sma3')
        self.assertContains(self.get('/'), f'?sma={c.pk}')
        self.assertEqual(self.get(f'/streams.json?sma={c.pk}').json(), [])

    def test_anonymous_and_owner_initial_password_cannot_use_global_catalog_or_video(self):
        anonymous = Client()
        self.assertEqual(anonymous.get('/streams.json', HTTP_HOST='central.example.test', secure=True).status_code, 401)
        self.owner.must_change_password = True
        self.owner.save(update_fields=['must_change_password'])
        self.assertEqual(self.get('/streams.json').status_code, 403)
        self.assertEqual(self.authorize(self.ca).status_code, 403)
        self.assertRedirects(self.get('/'), '/conta/senha/', fetch_redirect_response=False)

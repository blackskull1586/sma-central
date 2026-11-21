import json
import tempfile
from pathlib import Path
from django.conf import settings
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .models import User, Environment, Channel

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AccessIsolationTests(TestCase):
    def setUp(self):
        self.env_a = Environment.objects.create(name='Ambiente A', slug='teste-a')
        self.env_b = Environment.objects.create(name='Ambiente B', slug='teste-b')
        self.owner = self.user('owner_test', User.Role.OWNER, None)
        self.admin_a = self.user('admin_a', User.Role.ADMIN, self.env_a)
        self.admin_b = self.user('admin_b', User.Role.ADMIN, self.env_b)
        self.viewer_a = self.user('viewer_a', User.Role.VIEWER, self.env_a)
        self.viewer_b = self.user('viewer_b', User.Role.VIEWER, self.env_b)
        self.channel_a, self.key_a = self.channel(self.env_a)
        self.channel_b, self.key_b = self.channel(self.env_b)
        self.internal = {'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}
        self.client.force_login(self.admin_a)

    def user(self, username, role, environment):
        return User.objects.create_user(username=username, password='Strong-Test-Pass-72!',
            role=role, environment=environment, must_change_password=False)

    def channel(self, environment):
        channel = Channel(environment=environment, name='Camera de teste')
        key = channel.issue_key(legacy=True)
        channel.save()
        return channel, key

    def notification(self, action='publish', key=None, **overrides):
        key = key or self.key_a
        name, token = key.split('?token=')
        data = {'app': 'live', 'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action],
                'name': name, 'token': token, 'clientid': '12', 'addr': '192.0.2.10'}
        data.update(overrides)
        return self.client.post(f'/internal/rtmp/{action}/', data, **self.internal)

    def test_administrator_cannot_read_or_modify_foreign_accounts_channels_or_environment(self):
        page = self.client.get('/painel/')
        self.assertContains(page, self.viewer_a.username)
        self.assertNotContains(page, self.viewer_b.username)
        self.assertEqual(self.client.get(f'/usuarios/{self.viewer_b.pk}/senha/').status_code, 404)
        self.assertEqual(self.client.post(f'/usuarios/{self.viewer_b.pk}/estado/', {'active': '0'}).status_code, 404)
        self.assertEqual(self.client.get(f'/canais/{self.channel_b.pk}/editar/').status_code, 404)
        self.assertEqual(self.client.post(f'/canais/{self.channel_b.pk}/chave/').status_code, 404)
        self.assertEqual(self.client.post(f'/canais/{self.channel_b.pk}/estado/', {'active': '0'}).status_code, 404)
        self.assertEqual(self.client.post(f'/ambientes/{self.env_b.pk}/abrir/').status_code, 403)
        self.assertEqual(self.client.get('/ambientes/').status_code, 403)
        self.channel_b.refresh_from_db()
        self.viewer_b.refresh_from_db()
        self.assertTrue(self.channel_b.active and self.viewer_b.is_active)

    def test_viewer_has_no_management_access(self):
        self.client.force_login(self.viewer_a)
        for path in ['/painel/', '/usuarios/novo/', '/canais/novo/', '/api/channels/', '/ambientes/']:
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.post(f'/canais/{self.channel_a.pk}/chave/').status_code, 403)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_account_creation_ignores_foreign_tenant_and_rejects_privilege_escalation(self):
        payload = {'username': 'new_viewer', 'first_name': 'Novo', 'role': 'owner',
                   'password1': 'New-Strong-Pass-456!', 'password2': 'New-Strong-Pass-456!',
                   'environment': str(self.env_b.pk), 'is_superuser': '1'}
        self.assertEqual(self.client.post('/usuarios/novo/', payload).status_code, 400)
        self.assertFalse(User.objects.filter(username='new_viewer').exists())
        payload['role'] = 'viewer'
        self.assertEqual(self.client.post('/usuarios/novo/', payload).status_code, 302)
        created = User.objects.get(username='new_viewer')
        self.assertEqual(created.environment_id, self.env_a.pk)
        self.assertFalse(created.is_superuser or created.is_staff)
        self.assertTrue(created.must_change_password)

    def test_catalog_is_scoped_and_contains_no_publishing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(HLS_DIR=Path(tmp)):
            for channel in [self.channel_a, self.channel_b]:
                channel.last_seen = timezone.now()
                channel.save(update_fields=['last_seen'])
                (Path(tmp) / f'{channel.stream_id}.m3u8').write_text('#EXTM3U\n')
            response = self.client.get('/streams.json', {'environment_id': str(self.env_b.pk)})
            self.assertEqual([row['id'] for row in response.json()], [self.channel_a.stream_id])
            self.assertNotContains(response, 'token')
            self.assertNotContains(response, self.channel_a.key_digest)
            self.assertNotContains(response, self.key_a.split('?token=')[1])
            self.assertIn('no-store', response.headers['Cache-Control'])

    def test_hls_checks_each_manifest_and_segment_and_rejects_path_tricks(self):
        self.client.force_login(self.viewer_a)
        for suffix in ['.m3u8', '-123456789.ts']:
            own = f'/hls/{self.channel_a.stream_id}{suffix}'
            other = f'/hls/{self.channel_b.stream_id}{suffix}'
            self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=own, **self.internal).status_code, 204)
            self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=other, **self.internal).status_code, 403)
            self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=own).status_code, 403)
        for path in ['/hls/../file', '/hls/%2e%2e/file', '/hls/' + self.channel_a.stream_id + '.m3u8.bak', '/assets/streams.json']:
            self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=path, **self.internal).status_code, 403)

    def test_channel_key_rotation_rejects_previous_key_and_update(self):
        self.assertEqual(self.notification().status_code, 302)
        self.assertEqual(self.notification('update').status_code, 204)
        response = self.client.post(f'/canais/{self.channel_a.pk}/chave/')
        self.assertEqual(response.status_code, 200)
        self.channel_a.refresh_from_db()
        self.assertNotIn(self.key_a.split('?token=')[1], response.content.decode())
        self.assertEqual(self.notification().status_code, 403)
        self.assertEqual(self.notification('update').status_code, 403)

    def test_notifications_reject_missing_secret_wrong_key_and_duplicate_parameters(self):
        self.assertEqual(self.client.post('/internal/rtmp/publish/', {}).status_code, 403)
        self.assertEqual(self.notification(token='A' * 43).status_code, 403)
        self.assertEqual(self.notification(app='other').status_code, 403)
        self.assertEqual(self.notification(name='../bad').status_code, 403)
        name, token = self.key_a.split('?token=')
        body = f'app=live&call=publish&name={name}&name={self.channel_b.stream_id}&token={token}&clientid=12'
        self.assertEqual(self.client.post('/internal/rtmp/publish/', body, content_type='application/x-www-form-urlencoded', **self.internal).status_code, 403)
        self.assertEqual(self.notification(clientid=['12', '14']).status_code, 403)

    def test_disabled_environment_blocks_catalog_hls_and_ingest(self):
        self.env_a.active = False
        self.env_a.save(update_fields=['active'])
        self.assertEqual(self.client.get('/streams.json').status_code, 403)
        self.assertEqual(self.notification().status_code, 403)
        self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=f'/hls/{self.channel_a.stream_id}.m3u8', **self.internal).status_code, 403)

    def test_disabled_user_loses_existing_session(self):
        self.client.force_login(self.viewer_a)
        self.viewer_a.is_active = False
        self.viewer_a.save(update_fields=['is_active'])
        self.assertEqual(self.client.get('/streams.json').status_code, 401)
        self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=f'/hls/{self.channel_a.stream_id}.m3u8', **self.internal).status_code, 401)

    def test_initial_password_must_be_changed_before_accessing_videos(self):
        self.viewer_a.must_change_password = True
        self.viewer_a.save(update_fields=['must_change_password'])
        self.client.force_login(self.viewer_a)
        self.assertRedirects(self.client.get('/'), '/conta/senha/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/streams.json').status_code, 403)
        self.assertEqual(self.client.get('/internal/hls/', HTTP_X_ORIGINAL_URI=f'/hls/{self.channel_a.stream_id}.m3u8', **self.internal).status_code, 403)

    def test_reset_password_invalidates_other_sessions(self):
        viewer = Client()
        viewer.force_login(self.viewer_a)
        response = self.client.post(f'/usuarios/{self.viewer_a.pk}/senha/', {
            'new_password1': 'Changed-Strong-Pass-996!', 'new_password2': 'Changed-Strong-Pass-996!'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(viewer.get('/streams.json').status_code, 401)
        self.assertFalse(viewer.login(username=self.viewer_a.username, password='Strong-Test-Pass-72!'))

    def test_browser_mutations_require_csrf_and_post(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin_a)
        self.assertEqual(browser.post(f'/canais/{self.channel_a.pk}/estado/', {'active': '0'}).status_code, 403)
        self.assertEqual(browser.get(f'/canais/{self.channel_a.pk}/estado/').status_code, 405)
        self.assertEqual(browser.get('/sair/').status_code, 405)

    def test_owner_selects_scope_and_last_administrator_is_preserved(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(f'/ambientes/{self.env_b.pk}/abrir/').status_code, 302)
        response = self.client.get('/api/channels/')
        self.assertEqual([item['id'] for item in response.json()], [str(self.channel_b.pk)])
        self.assertEqual(self.client.post(f'/usuarios/{self.admin_b.pk}/estado/', {'active': '0'}).status_code, 400)
        self.assertEqual(self.client.post(f'/usuarios/{self.viewer_a.pk}/estado/', {'active': '0'}).status_code, 404)

    def test_done_callback_clears_only_its_publisher_and_stale_repeat_is_denied(self):
        self.assertEqual(self.notification().status_code, 302)
        self.assertEqual(self.notification(clientid='15').status_code, 302)
        self.assertEqual(self.notification('done', clientid='15').status_code, 204)
        self.assertEqual(self.notification('done', clientid='15').status_code, 403)
        self.channel_a.refresh_from_db()
        self.assertEqual(self.channel_a.publisher_client_id, '12')
        self.assertEqual(self.notification('update', clientid='12').status_code, 204)

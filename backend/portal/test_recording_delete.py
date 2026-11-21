import fcntl
import json
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch
from urllib.parse import urlencode
from django.test import TestCase, Client, override_settings
from .models import AuditEvent, Channel, Environment, User
from .recordings import entries

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], ALLOWED_HOSTS=['central.example.test', 'sma1.example.test', 'testserver'], SMA_OWNER_HOST='central.example.test', SMA_TENANT_HOSTS={'sma1.example.test': 'sma1'})
class RecordingDeleteTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(SMA_RECORDINGS_DIR=self.tmp.name)
        self.settings_override.enable(); self.addCleanup(self.settings_override.disable)
        self.env = Environment.objects.create(name='SMA 1', slug='sma1')
        self.channel = Channel.objects.create(environment=self.env, name='Equipe Alfa')
        self.owner = User.objects.create_user(username='owner_delete', role=User.Role.OWNER, must_change_password=False)
        self.client.force_login(self.owner)
        self.rid = uuid.uuid4()
        self.folder = Path(self.tmp.name)/'sma1'/'2026-10-04'/self.rid.hex
        self.folder.mkdir(parents=True)
        self.data = dict(recording_id=self.rid.hex, channel_id=str(self.channel.pk), environment_id=str(self.env.pk), unit_id=None,
                         environment_name='SMA 1', channel_name='Equipe Alfa', started_at='2026-10-04T11:00:00-04:00', status='complete',
                         duration_seconds=60, video_file='Equipe-Alfa__04102026_110000-UTC-04.mp4', slot=1,
                         stream_id='a'*32, last_segment_at='2026-10-04T11:01:00-04:00', segments=[])
        self.video = self.folder/self.data['video_file']; self.video.write_bytes(b'video-payload')
        self.save()
        self.url = f'/gravacoes/{self.rid}/excluir/'
        self.kw = dict(HTTP_HOST='central.example.test', secure=True)
    def save(self):
        (self.folder/'manifest.json').write_text(json.dumps(self.data))
    def get(self, url=None):
        return self.client.get(url or self.url, **self.kw)
    def post(self, **data):
        return self.client.post(self.url, {'confirmation': str(self.rid), **data}, **self.kw)
    def test_inline_actions_and_preview_are_non_destructive(self):
        response = self.get('/gravacoes/?busca=Alfa')
        self.assertContains(response, '>Assistir</a><a')
        self.assertContains(response, '>Baixar MP4</a><a class="danger"')
        self.assertContains(response, 'retorno=busca%3DAlfa')
        response = self.get(); self.assertContains(response, 'Excluir definitivamente')
        self.assertContains(response, 'Equipe Alfa'); self.assertContains(response, '04/10/2026 11:00:00')
        self.assertTrue(self.video.exists()); self.assertFalse(AuditEvent.objects.exists())
    def test_delete_payload_and_preserved_sources_with_audit(self):
        (self.folder/'segments').mkdir(); (self.folder/'segments'/'original.ts').write_bytes(b'original')
        (self.folder/'recording.m3u8').write_text('#EXTM3U')
        self.assertEqual(self.post().status_code, 302)
        self.assertFalse(self.video.exists()); self.assertFalse((self.folder/'segments').exists())
        manifest = json.loads((self.folder/'manifest.json').read_text())
        self.assertEqual(manifest['status'], 'deleted'); self.assertEqual(manifest['deleted_by'], self.owner.pk)
        self.assertTrue(manifest['deleted_at'].endswith('-04:00'))
        self.assertEqual(entries(), [])
        self.assertEqual(AuditEvent.objects.filter(action='Gravação excluída', owner_only=True, actor=self.owner).count(), 1)
        self.assertEqual(self.get(f'/gravacoes/{self.rid}/video/').status_code, 404)
        self.assertEqual(self.post().status_code, 404)
    def test_filters_and_page_survive_delete(self):
        query = urlencode({'servidor':str(self.env.pk), 'busca':'Equipe Alfa', 'inicio':'2026-10-01', 'fim':'2026-10-04', 'pagina':'2'})
        self.assertContains(self.get(self.url+'?'+urlencode({'retorno':query})), 'Cancelar')
        response = self.post(retorno=query)
        self.assertEqual(response['Location'], '/gravacoes/?'+query)
        self.assertEqual(self.client.get(response['Location'], **self.kw).status_code, 200)
    def test_confirmation_required(self):
        self.assertEqual(self.post(confirmation='wrong').status_code, 400)
        self.assertTrue(self.video.exists())
        self.assertEqual(json.loads((self.folder/'manifest.json').read_text())['status'], 'complete')
    def test_csrf_required_even_for_owner(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {'confirmation':str(self.rid)}, **self.kw).status_code, 403)
        preview = client.get(self.url, **self.kw)
        token = client.cookies['sma_csrf'].value
        response = client.post(self.url, {'confirmation':str(self.rid), 'csrfmiddlewaretoken':token}, HTTP_REFERER='https://central.example.test'+self.url, **self.kw)
        self.assertEqual(response.status_code, 302); self.assertFalse(self.video.exists())
    def test_anonymous_and_other_roles_cannot_delete(self):
        self.client.logout(); self.assertEqual(self.get().status_code, 302)
        self.assertEqual(self.post().status_code, 302)
        for role in (User.Role.ADMIN, User.Role.VIEWER):
            user = User.objects.create_user(username='delete_'+role, role=role, environment=self.env, must_change_password=False)
            self.client.force_login(user)
            self.assertEqual(self.get().status_code, 403); self.assertEqual(self.post().status_code, 403)
        self.assertTrue(self.video.exists()); self.assertFalse(AuditEvent.objects.exists())
    def test_owner_host_and_password_gates(self):
        response = self.client.post(self.url, {'confirmation':str(self.rid)}, HTTP_HOST='sma1.example.test', secure=True)
        self.assertEqual(response.status_code, 403)
        self.owner.must_change_password = True; self.owner.save()
        self.assertEqual(self.post().status_code, 302); self.assertTrue(self.video.exists())
    def test_active_or_exporting_recording_cannot_be_deleted(self):
        for status in ('recording', 'pending_export'):
            self.data['status'] = status; self.save()
            self.assertEqual(self.get().status_code, 409); self.assertEqual(self.post().status_code, 409)
            self.assertTrue(self.video.exists())
            self.assertNotContains(self.get('/gravacoes/'), self.url)
    def test_path_symlinks_do_not_escape_archive(self):
        outside = Path(self.tmp.name)/'outside'; outside.mkdir()
        canary = outside/'keep.txt'; canary.write_text('keep')
        (self.folder/'segments').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(canary.read_text(), 'keep')
    def test_symlinked_parent_and_changed_scope_fail_closed(self):
        self.channel.environment = Environment.objects.create(name='SMA 2', slug='sma2'); self.channel.save()
        self.assertEqual(self.post().status_code, 404); self.assertTrue(self.video.exists())
        self.channel.environment = self.env; self.channel.save()
        moved = self.folder.with_name('outside'); self.folder.rename(moved)
        self.folder.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.post().status_code, 404)
        self.assertTrue((moved/self.data['video_file']).exists())
    def test_retry_after_partial_failure(self):
        (self.folder/'segments').mkdir(); (self.folder/'segments'/'clip.ts').write_bytes(b'x')
        with patch('portal.recordings.shutil.rmtree', side_effect=OSError('test failure')) as mock:
            mock.avoids_symlink_attacks = True
            self.assertEqual(self.post().status_code, 409)
        self.assertEqual(json.loads((self.folder/'manifest.json').read_text())['status'], 'deleting')
        self.assertContains(self.get('/gravacoes/'), 'Exclusão pendente')
        self.assertEqual(self.get(f'/gravacoes/{self.rid}/video/').status_code, 404)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(entries(), [])
    def test_concurrent_attempt_does_not_delete_twice(self):
        with (self.folder/'.delete.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.post().status_code, 409)
            self.assertTrue(self.video.exists())
        self.assertEqual(self.post().status_code, 302)
    def test_return_url_and_http_methods_are_constrained(self):
        self.assertEqual(self.client.delete(self.url, **self.kw).status_code, 405)
        response = self.post(retorno='https://attacker.example/steal?next=bad')
        self.assertEqual(response['Location'], '/gravacoes/')

import json
from pathlib import Path
import tempfile
import uuid
from django.test import TestCase, Client, override_settings
from .models import User, Environment, Channel, Unit
from .recordings import entries

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], ALLOWED_HOSTS=['testserver','central.example.test','sma1.example.test'], SMA_OWNER_HOST='central.example.test', SMA_TENANT_HOSTS={'sma1.example.test':'sma1'})
class RecordingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(SMA_RECORDINGS_DIR=self.tmp.name); self.override.enable(); self.addCleanup(self.override.disable)
        self.env = Environment.objects.create(name='SMA 1', slug='sma1')
        self.channel = Channel.objects.create(environment=self.env, name='Canal teste')
        self.owner = User.objects.create_user(username='archive_owner', role=User.Role.OWNER, must_change_password=False)
        self.admin = User.objects.create_user(username='archive_admin', role=User.Role.ADMIN, environment=self.env, must_change_password=False)
        self.viewer = User.objects.create_user(username='archive_viewer', role=User.Role.VIEWER, environment=self.env, must_change_password=False)
        self.rid = uuid.uuid4(); self.folder = Path(self.tmp.name)/'sma1'/'2026-10-04'/self.rid.hex; self.folder.mkdir(parents=True)
        self.data = dict(recording_id=self.rid.hex, channel_id=str(self.channel.pk), environment_id=str(self.env.pk), environment_name='SMA 1', channel_name='Canal teste', unit_name='Equipe A', started_at='2026-10-04T15:30:00+00:00', status='complete', duration_seconds=62, video_file='Canal-teste__04102026_113000-UTC-04.mp4', slot=2)
        (self.folder/self.data['video_file']).write_bytes(b'0123456789')
        self.save(); self.client.force_login(self.owner)
        self.base=f'/gravacoes/{self.rid}/'
    def save(self):
        (self.folder/'manifest.json').write_text(json.dumps(self.data))
    def get(self, path, **kwargs):
        return self.client.get(path, HTTP_HOST='central.example.test', secure=True, **kwargs)
    def body(self,response):
        return b''.join(response.streaming_content)
    def test_list_detail_timezone_filters_and_nav(self):
        r=self.get('/gravacoes/'); self.assertContains(r,'Gravações'); self.assertContains(r,'04/10/2026 11:30:00'); self.assertContains(r,'00:01:02')
        self.assertContains(self.get(self.base),'controls playsinline')
        self.assertContains(self.get('/gravacoes/?busca=Equipe+A'),'Canal teste')
        self.assertNotContains(self.get('/gravacoes/?inicio=2026-10-05'),'Canal teste')
        self.assertNotContains(self.get('/gravacoes/?servidor=missing'),'Canal teste')
        self.assertEqual(self.get('/gravacoes/?inicio=invalid&pagina=invalid').status_code,200)
        self.assertContains(self.get('/ambientes/'), 'href="/gravacoes/"')
    def test_anonymous_cannot_access_any_endpoint(self):
        self.client.logout()
        for path in ['/gravacoes/',self.base,self.base+'video/',self.base+'baixar/']:
            self.assertEqual(self.get(path).status_code,302)
    def test_tenant_roles_cannot_access_list_or_files(self):
        for user in (self.admin,self.viewer):
            self.client.force_login(user)
            for path in ['/gravacoes/',self.base,self.base+'video/',self.base+'baixar/']:
                self.assertEqual(self.get(path).status_code,403)
                self.assertEqual(self.client.get(path,HTTP_HOST='sma1.example.test',secure=True).status_code,403)
        self.assertNotContains(self.client.get('/',HTTP_HOST='sma1.example.test',secure=True), 'href="/gravacoes/"')
    def test_owner_wrong_host_and_password_gate(self):
        self.assertNotEqual(self.client.get('/gravacoes/',HTTP_HOST='sma1.example.test',secure=True).status_code,200)
        self.owner.must_change_password=True; self.owner.save()
        self.assertEqual(self.get(self.base+'video/').status_code,302)
    def test_download_and_full_video(self):
        r=self.get(self.base+'baixar/'); self.assertIn('attachment;',r['Content-Disposition']); self.assertEqual(self.body(r),b'0123456789')
        r=self.get(self.base+'video/'); self.assertIn('inline;',r['Content-Disposition']); self.assertEqual(r['Content-Type'],'video/mp4'); self.assertEqual(self.body(r),b'0123456789')
    def test_range_seek_and_suffix(self):
        for value,body,content_range in [('bytes=2-5',b'2345','bytes 2-5/10'),('bytes=7-',b'789','bytes 7-9/10'),('bytes=-3',b'789','bytes 7-9/10'),('bytes=0-999',b'0123456789','bytes 0-9/10')]:
            r=self.get(self.base+'video/',HTTP_RANGE=value); self.assertEqual(r.status_code,206); self.assertEqual(r['Content-Range'],content_range); self.assertEqual(self.body(r),body)
    def test_invalid_ranges_and_head(self):
        for value in ('bytes=99-','bytes=5-2','bytes=-0','bytes=0-2,5-6','invalid'):
            r=self.get(self.base+'video/',HTTP_RANGE=value); self.assertEqual(r.status_code,416); self.assertEqual(r['Content-Range'],'bytes */10')
        r=self.client.head(self.base+'video/',HTTP_HOST='central.example.test',secure=True)
        self.assertEqual(r.status_code,200); self.assertEqual(r['Content-Length'],'10'); self.assertEqual(r.content,b'')
    def test_incomplete_and_unidentified_recordings_not_downloadable(self):
        self.data['status']='recording'; self.save()
        self.assertContains(self.get('/gravacoes/'),'Gravando'); self.assertEqual(self.get(self.base+'video/').status_code,404)
        self.data.pop('channel_id'); self.save(); self.assertEqual(entries(),[])
    def test_symlink_video_and_manifest_rejected(self):
        video=self.folder/self.data['video_file']; video.unlink(); video.symlink_to('/etc/passwd')
        self.assertEqual(self.get(self.base+'baixar/').status_code,404)
        video.unlink(); video.write_bytes(b'0123456789')
        (self.folder/'manifest.json').unlink(); (self.folder/'manifest.json').symlink_to('/etc/passwd')
        self.assertEqual(self.get(self.base+'video/').status_code,404)
    def test_traversal_filename_rejected(self):
        self.data['video_file']='../../../../etc/passwd.mp4'; self.save()
        self.assertEqual(self.get(self.base+'baixar/').status_code,404)
    def test_post_disallowed_and_no_public_cache(self):
        self.assertEqual(self.client.post('/gravacoes/',HTTP_HOST='central.example.test',secure=True).status_code,405)
        r=self.get(self.base+'video/'); self.assertIn('no-store',r['Cache-Control']); self.body(r)
    def test_multiple_sources_and_pagination(self):
        for i in range(21):
            rid=uuid.uuid4(); folder=self.folder.parent/rid.hex; folder.mkdir()
            data={**self.data,'recording_id':rid.hex,'slot':i+3,'status':'pending_export'}
            (folder/'manifest.json').write_text(json.dumps(data))
        self.assertContains(self.get('/gravacoes/'),'Página 1 de 2')
        self.assertContains(self.get('/gravacoes/?pagina=2'),'Página 2 de 2')

    def test_deleted_channel_and_scope_mismatch_deny_access(self):
        self.data['environment_id'] = str(uuid.uuid4()); self.save()
        self.assertEqual(self.get(self.base+'video/').status_code, 404)
        self.data['environment_id'] = str(self.env.pk)
        self.data['unit_id'] = str(uuid.uuid4()); self.save()
        self.assertEqual(self.get(self.base+'baixar/').status_code, 404)
        self.data['unit_id'] = None; self.save()
        self.channel.delete()
        self.assertEqual(self.get(self.base).status_code, 404)

    def test_unit_scope_and_reassignment(self):
        unit = Unit.objects.create(environment=self.env, name='Equipe A', slug='equipe-a')
        self.channel.unit = unit; self.channel.save()
        self.assertEqual(self.get(self.base).status_code, 404)
        self.data['unit_id'] = str(unit.pk); self.save()
        self.assertEqual(self.get(self.base).status_code, 200)

    def test_malformed_manifest_and_parent_symlink_fail_closed(self):
        self.data['channel_id'] = {}; self.save()
        self.assertEqual(self.get('/gravacoes/').status_code, 200)
        self.assertEqual(self.get(self.base).status_code, 404)
        self.data['channel_id'] = str(self.channel.pk); self.save()
        moved = self.folder.with_name('outside'); self.folder.rename(moved)
        self.folder.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.get(self.base+'video/').status_code, 404)

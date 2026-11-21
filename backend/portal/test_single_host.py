"""Role routing and server isolation when all users sign in at one IP."""
import io
import json
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import Channel, Environment, User


IP = '192.0.2.70'
CENTRAL_ALIAS = 'central.example.test'
TENANT_ALIAS = 'one.example.test'
OTHER_HOST = 'backend'
RTMP = f'rtmp://{IP}:19350/live'


@override_settings(
    ALLOWED_HOSTS=[IP, CENTRAL_ALIAS, TENANT_ALIAS, OTHER_HOST],
    SMA_SINGLE_HOST=True, SMA_OWNER_HOST=IP, SMA_OWNER_ALIASES={CENTRAL_ALIAS},
    SMA_TENANT_HOSTS={TENANT_ALIAS:'one'}, SMA_RTMP_URLS={'one':'rtmp://one.example.test:19350/live'},
    RTMP_PUBLIC_URL=RTMP, SMA_REALTIME_ENABLED=True,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class SingleHostTests(TestCase):
    def setUp(self):
        self.password = secrets.token_urlsafe(24)
        self.owner = User.objects.create_user(username='central', password=self.password,
                                             role=User.Role.OWNER, must_change_password=False)
        self.owner_client = self.login('central')
        self.env = self.create_server('one')
        self.other = self.create_server('two')
        self.admin = self.login('admin_one')
        self.admin_password = secrets.token_urlsafe(24)
        resp = self.admin.post('/conta/senha/', {'old_password': self.password,
            'new_password1':self.admin_password, 'new_password2':self.admin_password}, secure=True)
        self.assertEqual(resp.url, '/painel/')
        self.viewer = User.objects.create_user(username='viewer',password=self.password,
            role=User.Role.VIEWER, environment=self.env, must_change_password=False)

    def login(self, username, host=IP, password=None):
        c=Client(HTTP_HOST=host)
        resp=c.post('/login/', {'username': username,'password':password or self.password}, secure=True)
        self.assertEqual(resp.status_code,302)
        return c

    def create_server(self, slug):
        resp=self.owner_client.post('/ambientes/novo/', {'name':'Servidor '+slug, 'slug':slug,
            'username':'admin_'+slug, 'password1':self.password,'password2':self.password},secure=True)
        self.assertEqual(resp.url,'/ambientes/')
        return Environment.objects.get(slug=slug)

    def test_distinct_landing_pages_and_forced_password_change(self):
        for username,password,landing in [
            ('central',self.password,'/ambientes/'),('admin_one',self.admin_password,'/painel/'),
            ('admin_two',self.password,'/conta/senha/'),('viewer',self.password,'/')]:
            c=Client(HTTP_HOST=IP)
            resp=c.post('/login/', {'username':username,'password':password},secure=True)
            self.assertEqual(resp.url,landing)
            self.assertTrue(resp.cookies['sma_session']['secure'])
        generic=Client(HTTP_HOST=IP).get('/login/',secure=True)
        self.assertContains(generic,'SMA Central')
        self.assertNotContains(generic,'Entre com a conta do proprietário')
        self.assertContains(self.owner_client.get('/ambientes/',secure=True),'Novo servidor')

    def test_new_servers_have_working_urls_without_host_registration(self):
        select=self.owner_client.post(f'/ambientes/{self.other.pk}/abrir/',secure=True)
        self.assertEqual(select.url,'/painel/')
        user=User.objects.get(username='admin_two')
        credentials=self.owner_client.get(f'/usuarios/{user.pk}/credenciais/',secure=True)
        self.assertContains(credentials, f'https://{IP}/login/')
        created=self.owner_client.post('/canais/novo/', {'name':'Fonte nova'},secure=True)
        self.assertContains(created, RTMP)
        self.assertEqual(Channel.objects.get(name='Fonte nova').environment,self.other)
        old=self.owner_client.get(f'/usuarios/{user.pk}/credenciais/', HTTP_HOST=CENTRAL_ALIAS,secure=True)
        self.assertContains(old, f'https://{IP}/login/')
        # The IP offers its ingest URL even for a server which also has a historical address.
        direct=self.admin.post('/canais/novo/',{'name':'Fonte histórica'},secure=True)
        self.assertContains(direct,RTMP)
        ch=Channel.objects.get(name='Fonte histórica')
        legacy=self.admin.get(f'/canais/{ch.pk}/credenciais/',HTTP_HOST=TENANT_ALIAS,secure=True)
        self.assertContains(legacy,'rtmp://one.example.test:19350/live')

    def test_same_ip_rejects_other_server_scopes_and_owner_actions(self):
        foreign=User.objects.get(username='admin_two')
        viewer=self.login('viewer')
        for c in (self.admin,viewer):
            self.assertEqual(c.get('/gravacoes/', secure=True).status_code,403)
            self.assertEqual(c.get('/ambientes/',secure=True).status_code,403)
            self.assertEqual(c.post(f'/ambientes/{self.other.pk}/abrir/',secure=True).status_code,403)
            self.assertEqual(c.get('/streams.json',{'sma':str(self.other.pk)},secure=True).status_code,403)
            session=c.session;session['environment_id']=str(self.other.pk);session.save()
            self.assertNotIn(str(self.other.pk),c.get('/streams.json',secure=True).content.decode())
        resp=self.admin.get(f'/usuarios/{foreign.pk}/credenciais/',secure=True)
        self.assertEqual(resp.status_code,404)
        spoof=self.admin.post('/canais/novo/',{'name':'Fonte local','environment':str(self.other.pk)},secure=True)
        self.assertEqual(spoof.status_code,200)
        self.assertEqual(Channel.objects.get(name='Fonte local').environment_id,self.env.pk)
        self.assertEqual(self.owner_client.get('/gravacoes/',secure=True).status_code,200)

    def test_historical_aliases_and_internal_host_keep_their_boundaries(self):
        owner=self.login('central',CENTRAL_ALIAS)
        self.assertEqual(owner.get('/gravacoes/',secure=True).status_code,200)
        self.assertEqual(owner.get('/streams.json',HTTP_HOST=TENANT_ALIAS,secure=True).status_code,403)
        for username in ('viewer','admin_one'):
            c=Client(HTTP_HOST=CENTRAL_ALIAS)
            result=c.post('/login/',{'username':username,'password':self.admin_password if username=='admin_one' else self.password},secure=True)
            self.assertEqual(result.status_code,200)
            self.assertNotIn('_auth_user_id',c.session)
        self.assertEqual(self.admin.get('/painel/',HTTP_HOST=CENTRAL_ALIAS,secure=True).status_code,403)
        c=Client(HTTP_HOST=TENANT_ALIAS)
        c.post('/login/',{'username':'admin_two','password':self.password},secure=True)
        self.assertNotIn('_auth_user_id',c.session)
        self.assertEqual(self.admin.get('/painel/',HTTP_HOST=OTHER_HOST,secure=True).status_code,404)

    def test_hls_realtime_and_hidden_audiences_on_shared_ip(self):
        viewer=self.login('viewer')
        with tempfile.TemporaryDirectory() as tmp, override_settings(HLS_DIR=Path(tmp)):
            by_env=[]
            for title,env,hidden in [('Própria',self.env,False),('Restrita',self.env,True),('Outra',self.other,False)]:
                ch=Channel(name=title,environment=env,audience_restricted=hidden)
                ch.issue_key();ch.last_seen=timezone.now();ch.save()
                (Path(tmp)/(ch.stream_id+'.m3u8')).write_text('#EXTM3U\n')
                by_env.append(ch)
            items=viewer.get('/streams.json',secure=True).json()
            self.assertEqual([r['id'] for r in items],[by_env[0].stream_id])
            self.assertEqual(items[0]['webrtc_url'],f'/rtc/{by_env[0].stream_id}/whep')
            self.assertEqual(len(self.owner_client.get('/streams.json',secure=True).json()),3)
            for ch,permitted in zip(by_env,[True,False,False]):
                hdr={'HTTP_X_SMA_INTERNAL':settings.INTERNAL_TOKEN}
                h=viewer.get('/internal/hls/',HTTP_X_ORIGINAL_URI=f'/hls/{ch.stream_id}.m3u8',secure=True,**hdr)
                rtc=viewer.get(f'/internal/realtime/view/{ch.stream_id}/',secure=True,**hdr)
                self.assertEqual(h.status_code,204 if permitted else 403)
                self.assertEqual(rtc.status_code,200 if permitted else 403)

    def test_disabled_tenant_rejects_current_sessions_and_fresh_logins(self):
        self.owner_client.post(f'/ambientes/{self.env.pk}/estado/',{'active':'0'},secure=True)
        self.assertEqual(self.admin.get('/painel/',secure=True).status_code,403)
        client=Client(HTTP_HOST=IP)
        result=client.post('/login/',{'username':'viewer','password':self.password},secure=True)
        self.assertEqual(result.status_code,200)
        self.assertNotIn('_auth_user_id',client.session)

    def test_cross_origin_posts_still_require_same_ip_and_csrf(self):
        client=Client(enforce_csrf_checks=True,HTTP_HOST=IP)
        client.get('/login/',secure=True)
        token=client.cookies[settings.CSRF_COOKIE_NAME].value
        payload={'username':'viewer','password':self.password,'csrfmiddlewaretoken':token}
        self.assertEqual(client.post('/login/',payload,HTTP_ORIGIN=f'https://{CENTRAL_ALIAS}',secure=True).status_code,403)
        self.assertEqual(client.post('/login/',payload,HTTP_ORIGIN=f'https://{IP}',secure=True).status_code,302)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class OwnerOnlyBootstrapTests(TestCase):
    def test_new_install_with_owner_only_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bootstrap.json'
            p.write_text(json.dumps({'owner':{'username':'proprietario','password':secrets.token_urlsafe(24)}}))
            with patch.dict('os.environ', SMA_BOOTSTRAP_FILE=str(p)):
                for _ in range(2): call_command('bootstrap_sma',stdout=io.StringIO())
        self.assertFalse(Environment.objects.exists())
        self.assertEqual(User.objects.count(),1)
        self.assertTrue(User.objects.get().must_change_password)
        self.assertTrue(User.objects.get().is_owner)

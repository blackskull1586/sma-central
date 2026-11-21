"""Registrations, host activation and revocation across web, TLS and media."""
import secrets

from django.conf import settings
from django.test import Client, TestCase, override_settings

from .models import AuditEvent, Channel, CustomDomain, Environment, User


IP = '192.0.2.80'
V6 = '2001:db8::80'
STATIC_CENTRAL = 'central.legado.example.net'
STATIC_SMA = 'sma1.legado.example.net'
NAME = 'portal.unidade.example.net'


@override_settings(
    SMA_DOMAIN_MANAGEMENT=True, SMA_PUBLIC_IP=IP, SMA_PUBLIC_IPV6='',
    SMA_STATIC_ALLOWED_HOSTS={IP, STATIC_CENTRAL, STATIC_SMA, 'backend'},
    ALLOWED_HOSTS=['*'], SMA_SINGLE_HOST=True, SMA_OWNER_HOST=IP,
    SMA_OWNER_ALIASES={STATIC_CENTRAL}, SMA_TENANT_HOSTS={STATIC_SMA:'regional'},
    SMA_REALTIME_ENABLED=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class CustomDomainTests(TestCase):
    def setUp(self):
        self.password = secrets.token_urlsafe(24)
        self.owner = User.objects.create_user(username='proprietario',password=self.password,
            role=User.Role.OWNER,must_change_password=False)
        self.sma = Environment.objects.create(name='Regional',slug='regional')
        self.admin = User.objects.create_user(username='gestor',password=self.password,
            role=User.Role.ADMIN,environment=self.sma,must_change_password=False)
        self.central = Client(HTTP_HOST=IP)
        self.central.force_login(self.owner)
        self.back = Client(HTTP_HOST='backend')
        self.token = {'HTTP_X_SMA_INTERNAL':settings.INTERNAL_TOKEN}

    def register(self, name=NAME):
        return self.central.post('/dominios/',{'hostname':name},secure=True)

    def permit(self, query, **extra):
        return self.back.get('/internal/domains/permit/?'+query,secure=False,**extra)

    def test_owner_registers_normalized_unique_name_and_reads_dns_and_status(self):
        self.assertEqual(self.register(NAME.upper()+'.').status_code,302)
        row=CustomDomain.objects.get()
        self.assertEqual((row.hostname,row.status,row.enabled),(NAME,'pending',True))
        self.assertEqual(self.register().status_code,400)
        self.assertEqual(CustomDomain.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(owner_only=True,actor=self.owner).count(),1)
        CustomDomain.objects.update(status='dns_error',detail='A aponta para outro endereço.')
        resp=self.central.get('/dominios/',secure=True)
        for expected in [IP,NAME,'Verifique o DNS','A aponta para outro endereço.','sem registro AAAA']:
            self.assertContains(resp,expected)
        self.assertNotContains(resp,'href="https://'+NAME)
        with override_settings(SMA_PUBLIC_IPV6=V6):
            self.assertContains(self.central.get('/dominios/',secure=True),V6)

    def test_limits_and_conflicts_are_rejected_without_dns_or_db_registration(self):
        for value in ['https://'+NAME, NAME+':443',NAME+'/login/','*.'+NAME,
                      'a..example.net','-a.example.net','a.'+('x'*64)+'.net','127.1','127.0.0.1',
                      '[::1]','metadata.google.internal','a.localhost','a.onion','a.invalid',
                      'a|example.net','firma@'+NAME,'worker','caixa..','sécurité.example.net',
                      STATIC_CENTRAL, STATIC_SMA]:
            with self.subTest(value=value):
                self.assertEqual(self.register(value).status_code,400)
        self.assertFalse(CustomDomain.objects.exists())

    def test_owner_link_source_and_admin_visibility(self):
        page=self.central.get('/ambientes/',secure=True)
        self.assertContains(page,'href="/dominios/"')
        self.assertContains(page,'<!-- desenvolvido por sgt Chagas e sgt Braga -->')
        self.assertContains(Client(HTTP_HOST=IP).get('/login/',secure=True),'desenvolvido por sgt Chagas e sgt Braga')
        self.assertEqual(Client(HTTP_HOST=IP).get('/dominios/',secure=True).status_code,302)
        for role in (self.admin,User.objects.create_user(username='leitor',environment=self.sma,
                                                        role=User.Role.VIEWER,must_change_password=False)):
            c=Client(HTTP_HOST=IP);c.force_login(role)
            self.assertNotContains(c.get('/',secure=True),'href="/dominios/"')
            self.assertEqual(c.post('/dominios/',{'hostname':NAME},secure=True).status_code,403)
        with override_settings(SMA_DOMAIN_MANAGEMENT=False):
            self.assertNotContains(self.central.get('/ambientes/',secure=True),'href="/dominios/"')
            self.assertEqual(self.central.get('/dominios/',secure=True).status_code,404)

    def test_same_origin_token_required_for_owner_changes(self):
        c=Client(enforce_csrf_checks=True,HTTP_HOST=IP);c.force_login(self.owner)
        c.get('/dominios/',secure=True)
        token=c.cookies[settings.CSRF_COOKIE_NAME].value
        for origin,csrf,status in [('https://'+IP,'',403),('https://untrusted.example.net',token,403),
                                   ('https://'+IP,token,302)]:
            self.assertEqual(c.post('/dominios/',{'hostname':NAME,'csrfmiddlewaretoken':csrf},secure=True,
                                    HTTP_ORIGIN=origin).status_code,status)

    def test_internal_permission_requires_exact_query_token_host_and_state(self):
        row=CustomDomain.objects.create(hostname=NAME)
        expected={'pending':(403,403),'dns_error':(403,403),'provisioning':(204,403),
                  'active':(204,204),'tls_error':(204,403),'error':(403,403),'disabled':(403,403)}
        for status,(issuance,access) in expected.items():
            CustomDomain.objects.filter(pk=row.pk).update(status=status)
            self.assertEqual(self.permit('domain='+NAME,**self.token).status_code,issuance,status)
            self.assertEqual(self.permit('access=1&domain='+NAME,**self.token).status_code,access,status)
        CustomDomain.objects.filter(pk=row.pk).update(status='active')
        for q in [f'domain={NAME}&domain={NAME}',f'domain={NAME}&access=0',f'domain={NAME}&access=1&access=1',
                  f'domain={NAME}&extra=x','domain=invalid','']:
            self.assertEqual(self.permit(q,**self.token).status_code,403)
        self.assertEqual(self.permit('domain='+NAME).status_code,403)
        self.assertEqual(self.permit('domain='+NAME,HTTP_HOST=IP,**self.token).status_code,403)
        row.enabled=False;row.save(update_fields=['enabled'])
        self.assertEqual(self.permit('domain='+NAME,**self.token).status_code,403)

    def test_tls_permission_for_public_static_addresses_and_exact_ip_without_sni(self):
        for suffix in ('', '&access=1'):
            for name in (IP,STATIC_CENTRAL,STATIC_SMA):
                self.assertEqual(self.permit('domain='+name+suffix,**self.token).status_code,204)
            for name in ('127.0.0.1','localhost','backend','192.0.2.81',IP+'.',IP+':443','0.0.0.0'):
                self.assertEqual(self.permit('domain='+name+suffix,**self.token).status_code,403)
        with override_settings(SMA_SINGLE_HOST=False):
            self.assertEqual(self.permit('domain='+IP,**self.token).status_code,403)
        with override_settings(SMA_OWNER_HOST='127.0.0.1'):
            self.assertEqual(self.permit('domain=127.0.0.1',**self.token).status_code,403)
        self.assertEqual(self.permit('domain='+IP).status_code,403)
        self.assertEqual(self.permit('domain='+IP+'&domain='+IP,**self.token).status_code,403)

    def test_only_verified_hosts_reach_login_and_keep_roles_isolated(self):
        row=CustomDomain.objects.create(hostname=NAME)
        c=Client(HTTP_HOST=NAME)
        for host in (NAME,'other.example.net'):
            self.assertEqual(c.get('/login/',HTTP_HOST=host,secure=True).status_code,400)
        row.status='active';row.save()
        self.assertContains(c.get('/login/',secure=True),'SMA Central')
        self.assertEqual(c.post('/login/',{'username':self.admin.username,'password':self.password},secure=True).url,'/painel/')
        self.assertEqual(c.get('/gravacoes/',secure=True).status_code,403)
        self.assertEqual(c.get('/dominios/',secure=True).status_code,403)
        self.assertEqual(c.cookies[settings.SESSION_COOKIE_NAME]['domain'],'')
        other=Environment.objects.create(name='Outro',slug='outro')
        ch=Channel(name='Vizinho',environment=other);ch.issue_key();ch.save()
        self.assertEqual(c.get(f'/internal/realtime/view/{ch.stream_id}/',secure=True,**self.token).status_code,403)
        own=Channel(name='Próprio',environment=self.sma);own.issue_key();own.save()
        self.assertEqual(c.get(f'/internal/realtime/view/{own.stream_id}/',secure=True,**self.token).status_code,200)
        owner=Client(HTTP_HOST=NAME);owner.force_login(self.owner)
        self.assertEqual(owner.get('/gravacoes/',secure=True).status_code,200)
        disabled=owner.post(f'/dominios/{row.pk}/estado/',{'action':'disable'},secure=True)
        self.assertEqual(disabled.url,f'https://{IP}/dominios/')
        self.assertEqual(c.get('/login/',secure=True).status_code,400)
        self.assertEqual(c.get(f'/internal/realtime/view/{own.stream_id}/',secure=True,**self.token).status_code,400)
        self.assertEqual(self.permit('domain='+NAME+'&access=1',**self.token).status_code,403)
        self.assertContains(self.central.get('/dominios/',secure=True),'Desativação solicitada')

    def test_recheck_reenable_and_duplicate_actions_preserve_desired_state(self):
        self.register()
        row=CustomDomain.objects.get()
        url=f'/dominios/{row.pk}/estado/'
        self.assertEqual(self.central.get(url,secure=True).status_code,405)
        self.assertEqual(self.central.post(url,{'action':['enable','disable']},secure=True).status_code,400)
        CustomDomain.objects.filter(pk=row.pk).update(status='tls_error',detail='Pendente')
        self.assertEqual(self.central.post(url,{'action':'retry'},secure=True).status_code,302)
        row.refresh_from_db()
        self.assertEqual((row.status,row.detail,row.checked_at),('pending','',None))
        for action in ('disable','disable','enable'):
            self.assertEqual(self.central.post(url,{'action':action},secure=True).status_code,302)
        row.refresh_from_db()
        self.assertTrue(row.enabled)
        self.assertEqual(AuditEvent.objects.filter(action='Domínio desativado').count(),1)

from django.test import TestCase, Client, override_settings
from .models import User, Environment, Channel


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    ALLOWED_HOSTS=['testserver', 'sma1.example.test', 'sma2.example.test', 'central.example.test'],
    SMA_OWNER_HOST='central.example.test',
    SMA_TENANT_HOSTS={'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'},
    SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
)
class OwnerDomainTests(TestCase):
    def setUp(self):
        self.password = 'Owner-Domain-Test-Only-97!'
        self.a = Environment.objects.create(name='SMA 1', slug='sma1')
        self.b = Environment.objects.create(name='SMA 2', slug='sma2')
        self.owner = User.objects.create_user(username='owner_domain_test', password=self.password,
            role=User.Role.OWNER, must_change_password=False)
        self.admin = User.objects.create_user(username='admin_domain_test', password=self.password,
            role=User.Role.ADMIN, environment=self.a, must_change_password=False)
        self.viewer = User.objects.create_user(username='viewer_domain_test', password=self.password,
            role=User.Role.VIEWER, environment=self.a, must_change_password=False)

    def test_owner_login_opens_environment_management_and_has_host_only_cookie(self):
        response = self.client.get('/login/', HTTP_HOST='central.example.test', secure=True)
        self.assertContains(response, 'SMA Central')
        response = self.client.post('/login/', {'username': self.owner.username, 'password': self.password,
            'next': 'https://untrusted.example/'}, HTTP_HOST='central.example.test', secure=True)
        self.assertRedirects(response, '/ambientes/', fetch_redirect_response=False)
        self.assertTrue(response.cookies['sma_session']['secure'])
        self.assertEqual(response.cookies['sma_session']['domain'], '')
        self.assertEqual(self.client.get('/ambientes/', HTTP_HOST='central.example.test', secure=True).status_code, 200)

    def test_unmapped_backend_host_only_accepts_health_and_internal_callbacks(self):
        self.assertEqual(self.client.get('/healthz', HTTP_HOST='testserver').status_code, 200)
        self.assertEqual(self.client.get('/login/', HTTP_HOST='testserver').status_code, 404)
        self.client.force_login(self.admin)
        for path in ['/painel/', '/streams.json', '/internal/hls/']:
            self.assertEqual(self.client.get(path, HTTP_HOST='testserver').status_code, 404)

    def test_owner_cannot_log_in_on_tenant_domains(self):
        for host in ['sma1.example.test', 'sma2.example.test']:
            client = Client()
            response = client.post('/login/', {'username': self.owner.username, 'password': self.password},
                HTTP_HOST=host, secure=True)
            self.assertContains(response, 'central.example.test')
            self.assertNotIn('_auth_user_id', client.session)

    def test_admin_and_viewer_cannot_log_in_or_reuse_session_on_central(self):
        for user in [self.admin, self.viewer]:
            client = Client()
            response = client.post('/login/', {'username': user.username, 'password': self.password},
                HTTP_HOST='central.example.test', secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('_auth_user_id', client.session)
            client.force_login(user)
            for path in ['/ambientes/', '/painel/', '/streams.json', '/internal/hls/']:
                self.assertEqual(client.get(path, HTTP_HOST='central.example.test', secure=True).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/painel/', HTTP_HOST='sma1.example.test', secure=True).status_code, 200)

    def test_previous_owner_session_redirects_html_and_cannot_mutate_or_authorize_hls(self):
        self.client.force_login(self.owner)
        response = self.client.get('/ambientes/', HTTP_HOST='sma1.example.test', secure=True)
        self.assertRedirects(response, 'https://central.example.test/login/', fetch_redirect_response=False)
        self.assertEqual(self.client.post('/canais/novo/', {'name': 'Must not exist'},
            HTTP_HOST='sma1.example.test', secure=True).status_code, 403)
        self.assertFalse(Channel.objects.exists())
        for path in ['/streams.json', '/api/channels/', '/internal/hls/']:
            self.assertEqual(self.client.get(path, HTTP_HOST='sma1.example.test', secure=True).status_code, 403)

    def test_central_can_select_and_manage_each_environment_explicitly(self):
        self.client.force_login(self.owner)
        for env in [self.a, self.b]:
            response = self.client.post(f'/ambientes/{env.pk}/abrir/',
                HTTP_HOST='central.example.test', secure=True)
            self.assertRedirects(response, '/painel/', fetch_redirect_response=False)
            response = self.client.get('/painel/', HTTP_HOST='central.example.test', secure=True)
            self.assertEqual(response.context['current_environment'], env)

    def test_central_requires_its_own_csrf_origin(self):
        client = Client(enforce_csrf_checks=True)
        client.get('/login/', HTTP_HOST='central.example.test', secure=True)
        token = client.cookies['sma_csrf'].value
        data = {'username': self.owner.username, 'password': self.password}
        self.assertEqual(client.post('/login/', data, HTTP_HOST='central.example.test', secure=True,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://sma1.example.test').status_code, 403)
        self.assertEqual(client.post('/login/', data, HTTP_HOST='central.example.test', secure=True,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://central.example.test').status_code, 302)

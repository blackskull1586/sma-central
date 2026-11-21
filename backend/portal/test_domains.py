from django.test import TestCase, Client, override_settings
from .models import User, Environment


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    ALLOWED_HOSTS=['testserver', 'sma1.example.test', 'sma2.example.test'],
    SMA_TENANT_HOSTS={'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'},
    SMA_RTMP_URLS={'sma1': 'rtmp://sma1.example.test:19350/live',
                   'sma2': 'rtmp://sma2.example.test:19350/live'},
    SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
)
class DomainAccessTests(TestCase):
    def setUp(self):
        self.a = Environment.objects.create(name='SMA 1', slug='sma1')
        self.b = Environment.objects.create(name='SMA 2', slug='sma2')
        self.password = 'Test-Only-Domain-Pass-398!'
        self.admin = User.objects.create_user(username='domain_admin', password=self.password,
            environment=self.a, role=User.Role.ADMIN, must_change_password=False)
        self.owner = User.objects.create_user(username='domain_owner', password=self.password,
            role=User.Role.OWNER, must_change_password=False)

    def test_domain_title_and_login_are_bound_to_the_environment(self):
        response = self.client.get('/login/', HTTP_HOST='sma2.example.test', secure=True)
        self.assertContains(response, 'Acesso ao SMA 2')
        self.assertEqual(self.client.post('/login/', {'username': self.admin.username,
            'password': self.password}, HTTP_HOST='sma2.example.test', secure=True).status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        response = self.client.post('/login/', {'username': self.admin.username,
            'password': self.password}, HTTP_HOST='sma1.example.test', secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.cookies['sma_session']['secure'])
        self.assertTrue(response.cookies['sma_session']['httponly'])
        self.assertEqual(response.cookies['sma_session']['domain'], '')

    def test_copied_session_cannot_cross_domains_or_authorize_hls(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/painel/', HTTP_HOST='sma1.example.test', secure=True).status_code, 200)
        for path in ['/painel/', '/streams.json', '/internal/hls/']:
            self.assertEqual(self.client.get(path, HTTP_HOST='sma2.example.test', secure=True).status_code, 403)

    def test_owner_defaults_to_domain_and_retains_explicit_management_selection(self):
        self.client.force_login(self.owner)
        response = self.client.get('/painel/', HTTP_HOST='sma2.example.test', secure=True)
        self.assertEqual(response.context['current_environment'], self.b)
        self.client.post(f'/ambientes/{self.a.pk}/abrir/', HTTP_HOST='sma2.example.test', secure=True)
        response = self.client.get('/painel/', HTTP_HOST='sma2.example.test', secure=True)
        self.assertEqual(response.context['current_environment'], self.a)

    def test_rtmp_address_follows_channel_environment_even_for_the_owner(self):
        self.client.force_login(self.owner)
        self.client.post(f'/ambientes/{self.b.pk}/abrir/', HTTP_HOST='sma1.example.test', secure=True)
        response = self.client.post('/canais/novo/', {'name': 'Domain probe'},
            HTTP_HOST='sma1.example.test', secure=True)
        self.assertContains(response, 'rtmp://sma2.example.test:19350/live')

    def test_https_csrf_accepts_same_origin_and_rejects_other_domain(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get('/login/', HTTP_HOST='sma1.example.test', secure=True)
        self.assertTrue(response.cookies['sma_csrf']['secure'])
        token = client.cookies['sma_csrf'].value
        data = {'username': self.admin.username, 'password': self.password}
        self.assertEqual(client.post('/login/', data, HTTP_HOST='sma1.example.test', secure=True,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://sma2.example.test').status_code, 403)
        self.assertEqual(client.post('/login/', data, HTTP_HOST='sma1.example.test', secure=True,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://sma1.example.test').status_code, 302)

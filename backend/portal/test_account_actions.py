"""Account credential disclosure and deletion obey server and unit permissions."""
import datetime

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
HOSTS = {'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'}
PASSWORD = 'Account-Actions-Existing-Password-346!'
INITIAL_PASSWORD = 'Account-Actions-Provisional-Password-743!'
RESET_PASSWORD = 'Account-Actions-Reset-Password-537!'
PERSONAL_PASSWORD = 'Account-Actions-Personal-Password-835!'


@override_settings(
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=HOSTS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AccountActionAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.unit_a = Unit.objects.create(environment=cls.env, name='Unidade Alfa', slug='alfa')
        cls.unit_b = Unit.objects.create(environment=cls.env, name='Unidade Bravo', slug='bravo')
        cls.other_unit = Unit.objects.create(environment=cls.other_env, name='Unidade Externa', slug='externa')
        cls.owner = cls.make_user('account_actions_owner', User.Role.OWNER, None, None)
        cls.other_owner = cls.make_user('account_actions_other_owner', User.Role.OWNER, None, None)
        cls.admin_a = cls.make_user('account_actions_admin_a', User.Role.ADMIN, cls.env, cls.unit_a)
        cls.admin_b = cls.make_user('account_actions_admin_b', User.Role.ADMIN, cls.env, cls.unit_b)
        cls.admin_null = cls.make_user('account_actions_admin_null', User.Role.ADMIN, cls.env, None)
        cls.viewer_a = cls.make_user('account_actions_viewer_a', User.Role.VIEWER, cls.env, cls.unit_a)
        cls.viewer_b = cls.make_user('account_actions_viewer_b', User.Role.VIEWER, cls.env, cls.unit_b)
        cls.viewer_null = cls.make_user('account_actions_viewer_null', User.Role.VIEWER, cls.env, None)
        cls.viewer_other = cls.make_user('account_actions_viewer_other', User.Role.VIEWER, cls.other_env, cls.other_unit)
        cls.channels = []
        for name, env, unit in [('Alfa', cls.env, cls.unit_a), ('Bravo', cls.env, cls.unit_b),
                                ('Legado', cls.env, None), ('Externo', cls.other_env, cls.other_unit)]:
            channel = Channel(environment=env, unit=unit, name='Canal ' + name)
            channel.issue_key()
            channel.save()
            cls.channels.append(channel)

    @staticmethod
    def make_user(username, role, environment, unit):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, must_change_password=False)

    def setUp(self):
        self.owner_client = self.client_for(self.owner, self.env)

    def client_for(self, user, environment=None, csrf=False):
        client = Client(enforce_csrf_checks=csrf)
        client.force_login(user)
        if user.is_owner and environment is not None:
            session = client.session
            session['environment_id'] = str(environment.pk)
            session.save()
        return client

    def request(self, client, user, method, path, data=None, **headers):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        return getattr(client, method)(path, data=data or {}, HTTP_HOST=host, secure=True, **headers)

    def owner_request(self, method, path, data=None, **headers):
        return self.request(self.owner_client, self.owner, method, path, data, **headers)

    @staticmethod
    def url(name, target):
        return reverse(name, kwargs={'pk': target.pk})

    def delete(self, target, client=None, actor=None, **extra):
        return self.request(client or self.owner_client, actor or self.owner, 'post',
            self.url('account_delete', target), {'confirmation': str(target.pk), **extra})

    def data_snapshot(self):
        return {'channels': list(Channel.objects.order_by('pk').values()),
                'units': list(Unit.objects.order_by('pk').values()),
                'environments': list(Environment.objects.order_by('pk').values())}

    def account_payload(self, username, role=User.Role.VIEWER, unit=None):
        return {'username': username, 'first_name': 'Pessoa de teste', 'role': role,
                'unit': str(unit.pk) if unit else '',
                'password1': INITIAL_PASSWORD, 'password2': INITIAL_PASSWORD}

    def test_owner_can_read_credentials_and_delete_confirmation_for_all_selected_server_scopes(self):
        for target in [self.admin_a, self.admin_b, self.admin_null, self.viewer_a, self.viewer_b, self.viewer_null]:
            with self.subTest(target=target.username):
                before = User.objects.filter(pk=target.pk).values().get()
                for name in ['account_credentials', 'account_delete']:
                    response = self.owner_request('get', self.url(name, target))
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, target.username)
                    self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)

    def test_admin_can_read_and_confirm_delete_only_viewers_in_exact_unit_including_null(self):
        for actor, target in [(self.admin_a, self.viewer_a), (self.admin_null, self.viewer_null)]:
            client = self.client_for(actor)
            for name in ['account_credentials', 'account_delete']:
                response = self.request(client, actor, 'get', self.url(name, target))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, target.username)
        peer = self.make_user('account_actions_admin_peer', User.Role.ADMIN, self.env, self.unit_a)
        client = self.client_for(self.admin_a)
        for method, name in [('get', 'account_credentials'), ('get', 'account_delete'), ('post', 'account_delete')]:
            response = self.request(client, self.admin_a, method, self.url(name, peer), {'confirmation': str(peer.pk)})
            self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=peer.pk).exists())

    def test_cross_unit_and_server_actions_are_404_without_data_or_session_changes(self):
        for actor, targets in [(self.admin_a, [self.viewer_b, self.viewer_null, self.viewer_other]),
                               (self.admin_null, [self.viewer_a, self.viewer_b]),
                               (self.owner, [self.viewer_other])]:
            client = self.owner_client if actor.is_owner else self.client_for(actor)
            for target in targets:
                target_client = self.client_for(target)
                session_key = target_client.session.session_key
                before = User.objects.filter(pk=target.pk).values().get()
                audit_count = AuditEvent.objects.count()
                for method, name in [('get', 'account_credentials'), ('get', 'account_delete'), ('post', 'account_delete')]:
                    with self.subTest(actor=actor.username, target=target.username, name=name, method=method):
                        response = self.request(client, actor, method, self.url(name, target),
                            {'confirmation': str(target.pk)})
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)
                        self.assertTrue(Session.objects.filter(pk=session_key).exists())
                        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_viewer_cannot_read_credentials_or_delete_even_own_account(self):
        client = self.client_for(self.viewer_a)
        for target in [self.viewer_a, self.viewer_b, self.admin_a]:
            for method, name in [('get', 'account_credentials'), ('get', 'account_delete'), ('post', 'account_delete')]:
                response = self.request(client, self.viewer_a, method, self.url(name, target),
                    {'confirmation': str(target.pk)})
                self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.viewer_a.pk).exists())

    def test_self_and_owner_accounts_are_never_manageable(self):
        for actor, targets in [(self.owner, [self.owner, self.other_owner]), (self.admin_a, [self.admin_a])]:
            client = self.owner_client if actor.is_owner else self.client_for(actor)
            for target in targets:
                before = User.objects.filter(pk=target.pk).values().get()
                for method, name in [('get', 'account_credentials'), ('get', 'account_delete'), ('post', 'account_delete')]:
                    response = self.request(client, actor, method, self.url(name, target),
                        {'confirmation': str(target.pk)})
                    self.assertIn(response.status_code, [403, 404])
                    self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)

    def test_delete_get_and_invalid_confirmation_cannot_delete_or_revoke(self):
        target = self.viewer_a
        target_client = self.client_for(target)
        key = target_client.session.session_key
        before = User.objects.filter(pk=target.pk).values().get()
        audit_count = AuditEvent.objects.count()
        get = self.owner_request('get', self.url('account_delete', target), {'confirmation': str(target.pk)})
        self.assertEqual(get.status_code, 200)
        for payload in [{}, {'confirmation': '0'}, {'confirmation': str(self.viewer_b.pk)},
                        {'confirmation': [str(target.pk), str(target.pk)]},
                        {'confirmation': [str(self.viewer_b.pk), str(target.pk)]}]:
            with self.subTest(payload=payload):
                response = self.owner_request('post', self.url('account_delete', target), payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)
                self.assertTrue(Session.objects.filter(pk=key).exists())
                self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_last_active_admin_cannot_be_deleted_per_unit_or_legacy_bucket(self):
        for target in [self.admin_a, self.admin_b, self.admin_null]:
            with self.subTest(target=target.username):
                inactive = self.make_user(target.username + '_inactive', User.Role.ADMIN, self.env, target.unit)
                inactive.is_active = False
                inactive.save(update_fields=['is_active'])
                audit_count = AuditEvent.objects.count()
                response = self.delete(target)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(User.objects.filter(pk=target.pk, is_active=True).exists())
                self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_owner_can_delete_admin_with_replacement_and_inactive_admin_without_one(self):
        self.make_user('account_actions_admin_backup', User.Role.ADMIN, self.env, self.unit_a)
        self.assertEqual(self.delete(self.admin_a).status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.admin_a.pk).exists())
        self.admin_b.is_active = False
        self.admin_b.save(update_fields=['is_active'])
        self.assertEqual(self.delete(self.admin_b).status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.admin_b.pk).exists())

    def test_delete_removes_only_target_sessions_preserves_neighbors_channels_units_and_history(self):
        target = self.viewer_a
        active_clients = [self.client_for(target), self.client_for(target)]
        active_keys = [client.session.session_key for client in active_clients]
        expired_client = self.client_for(target)
        expired_key = expired_client.session.session_key
        Session.objects.filter(pk=expired_key).update(expire_date=timezone.now() - datetime.timedelta(days=1))
        neighbor_client = self.client_for(self.viewer_b)
        neighbor_key = neighbor_client.session.session_key
        owner_key = self.owner_client.session.session_key
        anonymous_session = Client().session
        anonymous_session['unrelated_test'] = 'keep'
        anonymous_session.save()
        anonymous_key = anonymous_session.session_key
        historical = AuditEvent.objects.create(environment=self.env, unit=self.unit_a, actor=target,
            action='Atividade anterior', target='Preservar o histórico')
        data_before = self.data_snapshot()
        other_users = list(User.objects.exclude(pk=target.pk).order_by('pk').values())
        response = self.delete(target)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())
        self.assertFalse(Session.objects.filter(pk__in=[*active_keys, expired_key]).exists())
        self.assertEqual(Session.objects.filter(pk__in=[owner_key, neighbor_key, anonymous_key]).count(), 3)
        self.assertEqual(self.data_snapshot(), data_before)
        self.assertEqual(list(User.objects.order_by('pk').values()), other_users)
        historical.refresh_from_db()
        self.assertIsNone(historical.actor_id)
        self.assertEqual((historical.environment_id, historical.unit_id, historical.target),
                         (self.env.pk, self.unit_a.pk, 'Preservar o histórico'))
        deletion = AuditEvent.objects.get(action='Acesso excluído', actor=self.owner)
        self.assertEqual((deletion.environment_id, deletion.unit_id), (self.env.pk, self.unit_a.pk))
        self.assertIn(target.username, deletion.target)
        self.assertIn(str(target.pk), deletion.target)
        for client in active_clients:
            self.assertEqual(self.request(client, target, 'get', '/streams.json').status_code, 401)
        self.assertEqual(self.request(neighbor_client, self.viewer_b, 'get', '/streams.json').status_code, 200)

    def test_admin_delete_own_viewer_and_null_scope_records_correct_actor_and_unit(self):
        for actor, target in [(self.admin_a, self.viewer_a), (self.admin_null, self.viewer_null)]:
            client = self.client_for(actor)
            data_before = self.data_snapshot()
            response = self.delete(target, client=client, actor=actor)
            self.assertEqual(response.status_code, 302)
            self.assertFalse(User.objects.filter(pk=target.pk).exists())
            event = AuditEvent.objects.get(action='Acesso excluído', actor=actor)
            self.assertEqual((event.environment_id, event.unit_id), (actor.environment_id, actor.unit_id))
            self.assertEqual(self.data_snapshot(), data_before)

    def test_delete_requires_csrf_and_valid_token_allows_confirmed_deletion(self):
        browser = self.client_for(self.owner, self.env, csrf=True)
        path = self.url('account_delete', self.viewer_a)
        response = self.request(browser, self.owner, 'get', path)
        self.assertEqual(response.status_code, 200)
        confirmation = {'confirmation': str(self.viewer_a.pk)}
        self.assertEqual(self.request(browser, self.owner, 'post', path, confirmation).status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.viewer_a.pk).exists())
        token = browser.cookies[settings.CSRF_COOKIE_NAME].value
        response = self.request(browser, self.owner, 'post', path, confirmation,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://' + OWNER_HOST)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.viewer_a.pk).exists())

    def test_credentials_get_uses_trusted_exact_server_url_and_never_resets_legacy_password(self):
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        mapping = {'sma10.example.test': 'sma10', 'stream-acesso.example.test': 'sma1',
                   'sma2.example.test': 'sma2'}
        with override_settings(SMA_TENANT_HOSTS=mapping):
            response = self.owner_request('get', self.url('account_credentials', self.viewer_a),
                {'url': 'https://attacker.example/login/', 'password': 'forged'},
                HTTP_X_FORWARDED_HOST='attacker.example')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://stream-acesso.example.test/login/')
        self.assertContains(response, self.viewer_a.username)
        self.assertNotContains(response, 'https://sma10.example.test/login/')
        self.assertNotContains(response, 'https://attacker.example/login/')
        self.assertNotContains(response, PASSWORD)
        self.assertNotContains(response, self.viewer_a.password)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')

    def test_credentials_without_configured_domain_do_not_invent_login_url(self):
        with override_settings(SMA_TENANT_HOSTS={'sma2.example.test': 'sma2'}):
            response = self.owner_request('get', self.url('account_credentials', self.viewer_a))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.viewer_a.username)
        for invented in ['https://sma1.example.test/login/', 'https://central.example.test/login/',
                         'https://192.0.2.44/login/']:
            self.assertNotContains(response, invented)

    def test_legacy_credentials_post_never_changes_or_discloses_personal_password(self):
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        response = self.owner_request('post', self.url('account_credentials', self.viewer_a),
            {'password': RESET_PASSWORD, 'new_password1': RESET_PASSWORD, 'new_password2': RESET_PASSWORD})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
        response = self.owner_request('get', self.url('account_credentials', self.viewer_a))
        self.assertNotContains(response, PASSWORD)
        self.assertTrue(User.objects.get(pk=self.viewer_a.pk).check_password(PASSWORD))

    def test_create_reset_and_personal_change_disclose_only_current_provisional_password(self):
        username = 'consulta_ciclo_viewer'
        response = self.owner_request('post', '/usuarios/novo/', self.account_payload(username, unit=self.unit_a))
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username=username)
        credentials_url = self.url('account_credentials', created)
        response = self.owner_request('get', credentials_url)
        self.assertContains(response, INITIAL_PASSWORD)
        self.assertContains(response, 'https://sma1.example.test/login/')
        self.assertTrue(created.check_password(INITIAL_PASSWORD))
        self.assertTrue(created.must_change_password)
        self.assertNotEqual(created.provisional_password_ciphertext, INITIAL_PASSWORD)
        self.assertNotContains(response, created.provisional_password_ciphertext)
        reset = self.owner_request('post', f'/usuarios/{created.pk}/senha/',
            {'new_password1': RESET_PASSWORD, 'new_password2': RESET_PASSWORD})
        self.assertEqual(reset.status_code, 302)
        created.refresh_from_db()
        response = self.owner_request('get', credentials_url)
        self.assertContains(response, RESET_PASSWORD)
        self.assertNotContains(response, INITIAL_PASSWORD)
        self.assertTrue(created.check_password(RESET_PASSWORD))
        personal_client = Client()
        self.assertTrue(personal_client.login(username=username, password=RESET_PASSWORD))
        changed = self.request(personal_client, created, 'post', '/conta/senha/',
            {'old_password': RESET_PASSWORD, 'new_password1': PERSONAL_PASSWORD, 'new_password2': PERSONAL_PASSWORD})
        self.assertEqual(changed.status_code, 302)
        created.refresh_from_db()
        self.assertFalse(created.must_change_password)
        self.assertEqual(created.provisional_password_ciphertext, '')
        self.assertIsNone(created.reveal_provisional_password())
        self.assertTrue(created.check_password(PERSONAL_PASSWORD))
        self.assertNotIn('personal_password_ciphertext', {f.name for f in created._meta.get_fields()})
        final = self.owner_request('get', credentials_url)
        self.assertContains(final, username)
        for secret in [INITIAL_PASSWORD, RESET_PASSWORD, PERSONAL_PASSWORD]:
            self.assertNotContains(final, secret)
            self.assertFalse(AuditEvent.objects.filter(target__contains=secret).exists())
            self.assertFalse(AuditEvent.objects.filter(action__contains=secret).exists())
        self.assertEqual(self.request(personal_client, created, 'get', '/streams.json').status_code, 200)

    def test_admin_created_and_reset_viewer_has_recoverable_provisional_only_within_its_unit(self):
        actor = self.admin_a
        client = self.client_for(actor)
        username = 'account_actions_admin_created'
        response = self.request(client, actor, 'post', '/usuarios/novo/',
            self.account_payload(username, unit=self.unit_b))
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username=username)
        self.assertEqual(created.unit_id, actor.unit_id)
        response = self.request(client, actor, 'get', self.url('account_credentials', created))
        self.assertContains(response, INITIAL_PASSWORD)
        self.assertEqual(self.request(client, actor, 'post', f'/usuarios/{created.pk}/senha/',
            {'new_password1': RESET_PASSWORD, 'new_password2': RESET_PASSWORD}).status_code, 302)
        response = self.request(client, actor, 'get', self.url('account_credentials', created))
        self.assertContains(response, RESET_PASSWORD)
        neighbor_client = self.client_for(self.admin_b)
        denied = self.request(neighbor_client, self.admin_b, 'get', self.url('account_credentials', created))
        self.assertEqual(denied.status_code, 404)

    def test_server_creation_remembers_initial_admin_password_without_new_domain_assumption(self):
        response = self.owner_request('post', '/ambientes/novo/',
            {'name': 'Servidor de teste', 'slug': 'created-server', 'username': 'account_actions_server_admin',
             'password1': INITIAL_PASSWORD, 'password2': INITIAL_PASSWORD})
        self.assertEqual(response.status_code, 302)
        env = Environment.objects.get(slug='created-server')
        admin = User.objects.get(username='account_actions_server_admin')
        self.assertEqual((admin.environment_id, admin.unit_id, admin.role), (env.pk, None, User.Role.ADMIN))
        self.assertTrue(admin.must_change_password)
        self.assertEqual(admin.reveal_provisional_password(), INITIAL_PASSWORD)
        self.owner_request('post', f'/ambientes/{env.pk}/abrir/')
        response = self.owner_request('get', self.url('account_credentials', admin))
        self.assertContains(response, INITIAL_PASSWORD)
        self.assertNotContains(response, 'https://created-server.example.test/login/')

    def test_legacy_initial_password_without_ciphertext_remains_unrecoverable_without_reset(self):
        self.viewer_a.must_change_password = True
        self.viewer_a.save(update_fields=['must_change_password'])
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        response = self.owner_request('get', self.url('account_credentials', self.viewer_a))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.viewer_a.username)
        self.assertNotContains(response, PASSWORD)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)

    def test_dashboard_links_exist_only_for_manageable_accounts(self):
        owner_page = self.owner_request('get', '/painel/')
        for target in [self.admin_a, self.admin_b, self.admin_null, self.viewer_a, self.viewer_b, self.viewer_null]:
            for name in ['account_credentials', 'account_delete']:
                self.assertContains(owner_page, self.url(name, target))
        for target in [self.owner, self.other_owner, self.viewer_other]:
            for name in ['account_credentials', 'account_delete']:
                self.assertNotContains(owner_page, self.url(name, target))
        peer = self.make_user('account_actions_ui_peer', User.Role.ADMIN, self.env, self.unit_a)
        client = self.client_for(self.admin_a)
        page = self.request(client, self.admin_a, 'get', '/painel/')
        for name in ['account_credentials', 'account_delete']:
            self.assertContains(page, self.url(name, self.viewer_a))
            for target in [self.admin_a, peer, self.viewer_b, self.viewer_null]:
                self.assertNotContains(page, self.url(name, target))

    def test_owner_management_selection_cannot_be_changed_by_read_query(self):
        before = User.objects.filter(pk=self.viewer_other.pk).values().get()
        for name in ['account_credentials', 'account_delete']:
            response = self.owner_request('get', self.url(name, self.viewer_other), {'sma': str(self.other_env.pk)})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.env.pk))
        self.assertEqual(User.objects.filter(pk=self.viewer_other.pk).values().get(), before)
        self.owner_request('post', f'/ambientes/{self.other_env.pk}/abrir/')
        response = self.owner_request('get', self.url('account_credentials', self.viewer_other))
        self.assertContains(response, 'https://sma2.example.test/login/')

    def test_disabled_server_and_forced_password_change_prevent_account_actions(self):
        self.admin_a.must_change_password = True
        self.admin_a.save(update_fields=['must_change_password'])
        client = self.client_for(self.admin_a)
        for name in ['account_credentials', 'account_delete']:
            response = self.request(client, self.admin_a, 'get', self.url(name, self.viewer_a))
            self.assertRedirects(response, '/conta/senha/', fetch_redirect_response=False)
        self.env.active = False
        self.env.save(update_fields=['active'])
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        for name in ['account_credentials', 'account_delete']:
            response = self.owner_request('get', self.url(name, self.viewer_a))
            self.assertNotEqual(response.status_code, 200)
        self.delete(self.viewer_a)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)

    def test_anonymous_and_wrong_domain_requests_cannot_disclose_or_delete(self):
        anonymous = Client()
        path = self.url('account_credentials', self.viewer_a)
        response = anonymous.get(path, HTTP_HOST=OWNER_HOST, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])
        admin = self.client_for(self.admin_a)
        response = admin.get(path, HTTP_HOST='sma2.example.test', secure=True)
        self.assertEqual(response.status_code, 403)
        response = self.owner_client.post(self.url('account_delete', self.viewer_a),
            {'confirmation': str(self.viewer_a.pk)}, HTTP_HOST='sma1.example.test', secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.viewer_a.pk).exists())

    def test_unknown_account_identifiers_are_404(self):
        missing = max(User.objects.values_list('pk', flat=True)) + 1000
        for name, method in [('account_credentials', 'get'), ('account_delete', 'get'), ('account_delete', 'post')]:
            path = reverse(name, kwargs={'pk': missing})
            self.assertEqual(self.owner_request(method, path, {'confirmation': str(missing)}).status_code, 404)

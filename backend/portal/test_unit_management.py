"""Central unit management does not cross server or delegated-access boundaries."""
import uuid

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.text import slugify

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOSTS = {f'sma{i}.example.test': f'sma{i}' for i in range(1, 6)}
PASSWORD = 'Unit-Management-Test-Password-748!'


@override_settings(
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class UnitManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.environments = []
        cls.units = []
        cls.admins = []
        cls.viewers = []
        cls.channels = []
        for index in range(1, 6):
            env = Environment.objects.create(name=f'SMA {index}', slug=f'sma{index}')
            unit = Unit.objects.create(environment=env, name=f'Unidade {index} Norte', slug=f'unidade-{index}')
            admin = cls.make_user(f'unit_management_admin_{index}', User.Role.ADMIN, env, unit)
            viewer = cls.make_user(f'unit_management_viewer_{index}', User.Role.VIEWER, env, unit)
            channel = Channel(environment=env, unit=unit, name=f'Canal {index}')
            channel.issue_key()
            channel.save()
            cls.environments.append(env)
            cls.units.append(unit)
            cls.admins.append(admin)
            cls.viewers.append(viewer)
            cls.channels.append(channel)
        cls.owner = cls.make_user('unit_management_owner', User.Role.OWNER, None, None)
        cls.legacy_admin = cls.make_user('unit_management_legacy_admin', User.Role.ADMIN, cls.environments[0], None)
        cls.second_unit = Unit.objects.create(environment=cls.environments[0], name='Unidade Reserva', slug='reserva')
        second_channel = Channel(environment=cls.environments[0], unit=cls.units[0], name='Segundo canal')
        second_channel.issue_key()
        second_channel.save()
        legacy_channel = Channel(environment=cls.environments[0], unit=None, name='Canal legado')
        legacy_channel.issue_key()
        legacy_channel.save()

    @staticmethod
    def make_user(username, role, environment, unit):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, must_change_password=False)

    def setUp(self):
        self.owner_client = self.client_for(self.owner)

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
    def unit_url(name, unit=None):
        return reverse(name, kwargs={'pk': unit.pk} if unit is not None else None)

    def snapshot(self):
        return {
            'users': list(User.objects.order_by('pk').values()),
            'channels': list(Channel.objects.order_by('pk').values()),
            'environments': list(Environment.objects.order_by('pk').values()),
        }

    def access_payload(self, username, role=User.Role.VIEWER, unit=None):
        return {'username': username, 'role': role, 'first_name': 'Teste',
                'unit': str(unit.pk) if unit else '', 'password1': PASSWORD, 'password2': PASSWORD}

    def test_owner_global_list_without_selected_server_shows_units_origins_and_distinct_counts(self):
        self.assertNotIn('environment_id', self.owner_client.session)
        response = self.owner_request('get', self.unit_url('units'))
        self.assertEqual(response.status_code, 200)
        items = {item.pk: item for item in response.context['units']}
        self.assertEqual(set(items), {unit.pk for unit in [*self.units, self.second_unit]})
        for index, unit in enumerate(self.units):
            with self.subTest(unit=unit.name):
                self.assertContains(response, unit.name)
                self.assertContains(response, unit.environment.name)
                self.assertEqual(items[unit.pk].user_count, 2)
                self.assertEqual(items[unit.pk].channel_count, 2 if index == 0 else 1)
        self.assertEqual((items[self.second_unit.pk].user_count, items[self.second_unit.pk].channel_count), (0, 0))
        self.assertNotIn('environment_id', self.owner_client.session)

    def test_disabled_server_units_remain_listed_but_create_form_offers_only_active_servers(self):
        env = self.environments[4]
        env.active = False
        env.save(update_fields=['active'])
        listing = self.owner_request('get', self.unit_url('units'))
        self.assertContains(listing, self.units[4].name)
        self.assertContains(listing, env.name)
        page = self.owner_request('get', self.unit_url('unit_create'))
        self.assertEqual(page.status_code, 200)
        choices = set(page.context['form'].fields['environment'].queryset.values_list('pk', flat=True))
        self.assertEqual(choices, {item.pk for item in self.environments[:4]})

    def test_create_unit_without_selected_server_preserves_accounts_channels_and_servers(self):
        before = self.snapshot()
        name = 'Nova Brigada de Teste'
        env = self.environments[2]
        response = self.owner_request('post', self.unit_url('unit_create'),
            {'name': name, 'environment': str(env.pk), 'slug': 'forged-slug', 'active': '0'})
        self.assertEqual(response.status_code, 302)
        unit = Unit.objects.get(environment=env, name=name)
        self.assertEqual(unit.slug, slugify(name)[:40])
        self.assertEqual(self.snapshot(), before)
        event = AuditEvent.objects.first()
        self.assertEqual((event.actor_id, event.environment_id, event.unit_id), (self.owner.pk, env.pk, unit.pk))

    def test_unit_names_are_trimmed_unique_case_insensitively_per_server_and_allowed_elsewhere(self):
        env = self.environments[0]
        original_count = Unit.objects.count()
        duplicate = self.owner_request('post', self.unit_url('unit_create'),
            {'name': '  ' + self.units[0].name.upper() + '  ', 'environment': str(env.pk)})
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(Unit.objects.count(), original_count)
        created = self.owner_request('post', self.unit_url('unit_create'),
            {'name': '  ' + self.units[0].name + '  ', 'environment': str(self.environments[1].pk)})
        self.assertEqual(created.status_code, 302)
        self.assertTrue(Unit.objects.filter(environment=self.environments[1], name=self.units[0].name).exists())

    def test_distinct_names_with_same_slug_base_receive_unique_generated_slugs(self):
        env = self.environments[0]
        for name in ['Equipe A', 'Equipe-A']:
            self.assertEqual(self.owner_request('post', self.unit_url('unit_create'),
                {'name': name, 'environment': str(env.pk)}).status_code, 302)
        a = Unit.objects.get(environment=env, name='Equipe A')
        b = Unit.objects.get(environment=env, name='Equipe-A')
        self.assertEqual(a.slug, 'equipe-a')
        self.assertNotEqual(a.slug, b.slug)
        self.assertTrue(b.slug.startswith('equipe-a-'))
        long_name = 'a' * 80
        response = self.owner_request('post', self.unit_url('unit_create'),
            {'name': long_name, 'environment': str(env.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Unit.objects.get(environment=env, name=long_name).slug, 'a' * 40)

    def test_create_rejects_invalid_inactive_or_missing_server_and_blank_name(self):
        inactive = self.environments[4]
        inactive.active = False
        inactive.save(update_fields=['active'])
        count = Unit.objects.count()
        cases = [{'name': 'Inválida', 'environment': str(inactive.pk)},
                 {'name': 'Inválida', 'environment': str(uuid.uuid4())},
                 {'name': 'Inválida', 'environment': 'invalid'},
                 {'name': 'Inválida'}, {'name': '   ', 'environment': str(self.environments[0].pk)}]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.owner_request('post', self.unit_url('unit_create'), payload).status_code, 400)
                self.assertEqual(Unit.objects.count(), count)

    def test_owner_edit_is_global_but_cannot_reassign_unit_server_or_slug(self):
        self.owner_client = self.client_for(self.owner, self.environments[0])
        target = self.units[1]
        old_slug = target.slug
        before = self.snapshot()
        page = self.owner_request('get', self.unit_url('unit_edit', target))
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('environment', page.context['form'].fields)
        self.assertNotIn('slug', page.context['form'].fields)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))
        response = self.owner_request('post', self.unit_url('unit_edit', target),
            {'name': '  Unidade renomeada  ', 'environment': str(self.environments[0].pk),
             'environment_id': str(self.environments[0].pk), 'slug': 'alterado', 'active': '0'})
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.name, 'Unidade renomeada')
        self.assertEqual((target.environment_id, target.slug), (self.environments[1].pk, old_slug))
        self.assertEqual(self.snapshot(), before)
        event = AuditEvent.objects.first()
        self.assertEqual((event.environment_id, event.unit_id, event.actor_id),
                         (self.environments[1].pk, target.pk, self.owner.pk))

    def test_rename_rejects_same_server_duplicate_but_accepts_name_used_on_other_server(self):
        target = self.second_unit
        original = (target.name, target.slug, target.environment_id)
        rejected = self.owner_request('post', self.unit_url('unit_edit', target),
            {'name': self.units[0].name.upper()})
        self.assertEqual(rejected.status_code, 400)
        target.refresh_from_db()
        self.assertEqual((target.name, target.slug, target.environment_id), original)
        accepted = self.owner_request('post', self.unit_url('unit_edit', target), {'name': self.units[1].name})
        self.assertEqual(accepted.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.name, self.units[1].name)
        self.assertEqual((target.slug, target.environment_id), original[1:])

    def test_get_list_and_edit_never_change_management_selection_or_unit_data(self):
        self.owner_client = self.client_for(self.owner, self.environments[0])
        before = list(Unit.objects.order_by('pk').values())
        audit_count = AuditEvent.objects.count()
        for path in [self.unit_url('units'), self.unit_url('unit_create'), self.unit_url('unit_edit', self.units[2])]:
            response = self.owner_request('get', path,
                {'environment': str(self.environments[2].pk), 'name': 'Mudança indevida', 'action': 'manage'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))
        self.assertEqual(list(Unit.objects.order_by('pk').values()), before)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_unit_accounts_create_action_selects_server_and_prefills_owner_account_unit(self):
        unit = self.units[3]
        response = self.owner_request('post', self.unit_url('unit_accounts', unit), {'action': 'create'})
        self.assertRedirects(response, '/usuarios/novo/?unit=' + str(unit.pk), fetch_redirect_response=False)
        self.assertEqual(self.owner_client.session['environment_id'], str(unit.environment_id))
        page = self.owner_request('get', response['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertEqual(str(page.context['form']['unit'].value()), str(unit.pk))
        created = self.owner_request('post', '/usuarios/novo/',
            self.access_payload('unit_management_new_admin', role=User.Role.ADMIN, unit=unit))
        self.assertEqual(created.status_code, 302)
        user = User.objects.get(username='unit_management_new_admin')
        self.assertEqual((user.environment_id, user.unit_id, user.role),
                         (unit.environment_id, unit.pk, User.Role.ADMIN))
        self.assertTrue(user.must_change_password)

    def test_unit_accounts_manage_action_selects_server_without_filtering_out_other_units(self):
        response = self.owner_request('post', self.unit_url('unit_accounts', self.second_unit), {'action': 'manage'})
        self.assertRedirects(response, '/painel/#usuarios', fetch_redirect_response=False)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))
        page = self.owner_request('get', '/painel/')
        ids = {user.pk for user in page.context['accounts']}
        self.assertTrue({self.admins[0].pk, self.viewers[0].pk, self.legacy_admin.pk}.issubset(ids))
        self.assertNotIn(self.admins[1].pk, ids)
        self.assertContains(page, 'Alterar ambiente')

    def test_accounts_action_requires_post_and_allowlisted_action_before_session_change(self):
        self.owner_client = self.client_for(self.owner, self.environments[0])
        target = self.units[2]
        path = self.unit_url('unit_accounts', target)
        self.assertEqual(self.owner_request('get', path, {'action': 'manage'}).status_code, 405)
        for payload in [{}, {'action': 'delete'}, {'action': 'http://example.invalid'}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.owner_request('post', path, payload).status_code, 400)
                self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))

    def test_disabled_server_blocks_unit_mutation_and_access_actions(self):
        unit = self.units[4]
        env = self.environments[4]
        self.owner_client = self.client_for(self.owner, self.environments[0])
        env.active = False
        env.save(update_fields=['active'])
        before = list(Unit.objects.order_by('pk').values())
        audit_count = AuditEvent.objects.count()
        operations = [(self.unit_url('unit_edit', unit), {'name': 'Não deve alterar'}),
                      (self.unit_url('unit_accounts', unit), {'action': 'create'}),
                      (self.unit_url('unit_accounts', unit), {'action': 'manage'})]
        for path, payload in operations:
            with self.subTest(path=path, payload=payload):
                response = self.owner_request('post', path, payload)
                self.assertIn(response.status_code, [403, 404])
                self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))
                self.assertEqual(list(Unit.objects.order_by('pk').values()), before)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_prefilled_account_unit_must_belong_to_selected_server_and_get_cannot_change_it(self):
        self.owner_client = self.client_for(self.owner, self.environments[0])
        valid = self.owner_request('get', '/usuarios/novo/', {'unit': str(self.second_unit.pk)})
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(str(valid.context['form']['unit'].value()), str(self.second_unit.pk))
        for value in [str(self.units[1].pk), str(uuid.uuid4()), 'invalid']:
            with self.subTest(value=value):
                response = self.owner_request('get', '/usuarios/novo/', {'unit': value})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.owner_client.session['environment_id'], str(self.environments[0].pk))

    def test_admin_ignores_unit_prefill_and_forged_create_inherits_scope_while_elevation_is_rejected(self):
        for actor in [self.admins[0], self.legacy_admin]:
            client = self.client_for(actor)
            page = self.request(client, actor, 'get', '/usuarios/novo/', {'unit': str(self.units[1].pk)})
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('unit', page.context['form'].fields)
            username = actor.username + '_viewer'
            created = self.request(client, actor, 'post', '/usuarios/novo/',
                self.access_payload(username, unit=self.units[1]))
            self.assertEqual(created.status_code, 302)
            user = User.objects.get(username=username)
            self.assertEqual((user.environment_id, user.unit_id, user.role),
                             (actor.environment_id, actor.unit_id, User.Role.VIEWER))
            for role in [User.Role.ADMIN, User.Role.OWNER]:
                elevated = actor.username + '_forged_' + role
                response = self.request(client, actor, 'post', '/usuarios/novo/',
                    self.access_payload(elevated, role=role, unit=self.units[1]))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(User.objects.filter(username=elevated).exists())

    def test_tenant_accounts_cannot_list_create_edit_or_select_unit_management(self):
        before = list(Unit.objects.order_by('pk').values())
        for actor in [self.admins[0], self.legacy_admin, self.viewers[0]]:
            client = self.client_for(actor)
            operations = [('get', self.unit_url('units'), {}),
                          ('get', self.unit_url('unit_create'), {}),
                          ('post', self.unit_url('unit_create'), {'name': 'Forjada', 'environment': str(self.environments[0].pk)}),
                          ('get', self.unit_url('unit_edit', self.units[0]), {}),
                          ('post', self.unit_url('unit_edit', self.units[0]), {'name': 'Forjada'}),
                          ('post', self.unit_url('unit_accounts', self.units[0]), {'action': 'create'}),
                          ('post', self.unit_url('unit_accounts', self.units[1]), {'action': 'manage'})]
            for method, path, payload in operations:
                with self.subTest(actor=actor.username, method=method, path=path):
                    self.assertEqual(self.request(client, actor, method, path, payload).status_code, 403)
            self.assertNotIn('environment_id', client.session)
        self.assertEqual(list(Unit.objects.order_by('pk').values()), before)

    def test_unit_controls_require_csrf_and_valid_token_allows_explicit_actions(self):
        browser = self.client_for(self.owner, self.environments[0], csrf=True)
        form_page = self.request(browser, self.owner, 'get', self.unit_url('unit_create'))
        self.assertEqual(form_page.status_code, 200)
        unit = self.units[1]
        before_units = list(Unit.objects.order_by('pk').values())
        mutations = [(self.unit_url('unit_create'), {'name': 'Criada com CSRF', 'environment': str(unit.environment_id)}),
                     (self.unit_url('unit_edit', unit), {'name': 'Editada com CSRF'}),
                     (self.unit_url('unit_accounts', unit), {'action': 'manage'})]
        for path, payload in mutations:
            self.assertEqual(self.request(browser, self.owner, 'post', path, payload).status_code, 403)
        self.assertEqual(list(Unit.objects.order_by('pk').values()), before_units)
        self.assertEqual(browser.session['environment_id'], str(self.environments[0].pk))
        token = browser.cookies[settings.CSRF_COOKIE_NAME].value
        for path, payload in mutations:
            response = self.request(browser, self.owner, 'post', path, payload,
                HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://' + OWNER_HOST)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(browser.session['environment_id'], str(unit.environment_id))
        unit.refresh_from_db()
        self.assertEqual(unit.name, 'Editada com CSRF')

    def test_owner_navigation_distinguishes_units_from_servers_and_tenants_have_no_units_tab(self):
        page = self.owner_request('get', self.unit_url('units'))
        self.assertContains(page, 'href="' + self.unit_url('units') + '"')
        self.assertContains(page, 'Ambientes')
        self.assertContains(page, 'href="' + reverse('environments') + '"')
        self.assertContains(page, 'Servidores')
        for actor in [self.admins[0], self.viewers[0], self.legacy_admin]:
            client = self.client_for(actor)
            response = self.request(client, actor, 'get', '/')
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'href="' + self.unit_url('units') + '"')

    def test_anonymous_and_wrong_owner_domain_cannot_enter_unit_management(self):
        anonymous = Client()
        response = anonymous.get(self.unit_url('units'), HTTP_HOST=OWNER_HOST, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])
        before = Unit.objects.count()
        response = self.owner_client.post(self.unit_url('unit_create'),
            {'name': 'Domínio incorreto', 'environment': str(self.environments[0].pk)},
            HTTP_HOST='sma1.example.test', secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Unit.objects.count(), before)

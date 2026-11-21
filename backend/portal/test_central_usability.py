"""Central creation controls retain their displayed roles and management scope."""
from html.parser import HTMLParser

from django.test import Client, TestCase, override_settings

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOSTS = {'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'}
PASSWORD = 'Central-Usability-Fixture-942!'


class SelectOptions(HTMLParser):
    """Read the actual rendered select, rather than the form's Python choices."""
    def __init__(self, name, html):
        super().__init__()
        self.name = name
        self.in_select = False
        self.options = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.in_select = attrs.get('name') == self.name
        elif tag == 'option' and self.in_select:
            self.options[attrs.get('value', '')] = 'selected' in attrs

    def handle_endtag(self, tag):
        if tag == 'select':
            self.in_select = False


@override_settings(
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class CentralUsabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.unit_a = Unit.objects.create(environment=cls.env, name='Ambiente Alfa', slug='alfa')
        cls.unit_b = Unit.objects.create(environment=cls.env, name='Ambiente Bravo', slug='bravo')
        cls.other_unit = Unit.objects.create(environment=cls.other_env, name='Ambiente Externo', slug='externo')
        cls.owner = cls.make_user('usability_owner', User.Role.OWNER)
        cls.admin = cls.make_user('usability_admin', User.Role.ADMIN, cls.env, cls.unit_a)
        cls.initial_admin = cls.make_user('usability_initial_admin', User.Role.ADMIN, cls.env)
        cls.viewer = cls.make_user('usability_viewer', User.Role.VIEWER, cls.env, cls.unit_a)
        cls.channels = []
        for name, env, unit in [('Canal exclusivo Alfa', cls.env, cls.unit_a),
                                ('Canal inicial', cls.env, None),
                                ('Canal externo', cls.other_env, cls.other_unit)]:
            channel = Channel(environment=env, unit=unit, name=name)
            channel.issue_key()
            channel.save()
            cls.channels.append(channel)
        cls.channels[0].audience_restricted = True
        cls.channels[0].save(update_fields=['audience_restricted'])
        cls.channels[0].allowed_viewers.add(cls.viewer)

    @staticmethod
    def make_user(username, role, environment=None, unit=None):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, must_change_password=False)

    def setUp(self):
        self.owner_client = self.browser(self.owner, self.env)

    def browser(self, user, environment=None):
        browser = Client()
        browser.force_login(user)
        if user.is_owner and environment is not None:
            session = browser.session
            session['environment_id'] = str(environment.pk)
            session.save()
        return browser

    def request(self, browser, user, method, path, data=None):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        return getattr(browser, method)(path, data={} if data is None else data,
            HTTP_HOST=host, secure=True)

    def owner_request(self, method, path, data=None):
        return self.request(self.owner_client, self.owner, method, path, data)

    def account_payload(self, username='usability_created', unit=None, role=User.Role.VIEWER, **extra):
        return {'username': username, 'first_name': 'Conta de teste', 'role': role,
                'unit': str(unit.pk) if unit else '', 'password1': PASSWORD, 'password2': PASSWORD,
                'management_environment': str(self.env.pk), **extra}

    def channel_payload(self, name='Canal criado pelo Central', unit=None, **extra):
        return {'name': name, 'unit': str(unit.pk) if unit else '',
                'management_environment': str(self.env.pk), **extra}

    @staticmethod
    def snapshot():
        return {model.__name__: list(model.objects.order_by('pk').values())
                for model in [Environment, Unit, User, Channel, AuditEvent]}

    def existing_channel_snapshot(self):
        ids = [channel.pk for channel in self.channels]
        return (list(Channel.objects.filter(pk__in=ids).order_by('pk').values()),
                list(Channel.allowed_viewers.through.objects.filter(channel_id__in=ids).order_by('pk').values()))

    def test_owner_rendered_role_select_offers_both_viewer_and_administrator(self):
        response = self.owner_request('get', '/usuarios/novo/')
        self.assertEqual(response.status_code, 200)
        options = SelectOptions('role', response.content.decode()).options
        self.assertEqual(set(options), {User.Role.VIEWER, User.Role.ADMIN})
        self.assertTrue(options[User.Role.VIEWER])

    def test_unnamed_route_still_renders_csrf_error_instead_of_server_error(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        response = browser.post('/api/channels/', {}, HTTP_HOST=OWNER_HOST, secure=True)
        self.assertContains(response, 'Sessão do formulário expirada', status_code=403)

    def test_tenant_admin_rendered_form_and_post_cannot_create_administrators(self):
        browser = self.browser(self.admin)
        page = self.request(browser, self.admin, 'get', '/usuarios/novo/')
        self.assertEqual(set(SelectOptions('role', page.content.decode()).options), {User.Role.VIEWER})
        self.assertNotIn('unit', page.context['form'].fields)
        self.assertNotIn('management_environment', page.context['form'].fields)
        before = self.snapshot()
        response = self.request(browser, self.admin, 'post', '/usuarios/novo/',
            self.account_payload(role=User.Role.ADMIN, unit=self.unit_b))
        self.assertEqual(response.status_code, 400)
        self.assertIn('role', response.context['form'].errors)
        self.assertEqual(self.snapshot(), before)

    def test_owner_creation_forms_include_current_context_and_only_its_units(self):
        for path in ['/usuarios/novo/', '/canais/novo/']:
            with self.subTest(path=path):
                response = self.owner_request('get', path)
                self.assertEqual(response.status_code, 200)
                form = response.context['form']
                self.assertTrue(form.fields['management_environment'].widget.is_hidden)
                self.assertEqual(str(form['management_environment'].value()), str(self.env.pk))
                self.assertFalse(form.fields['unit'].required)
                self.assertEqual(set(form.fields['unit'].queryset.values_list('pk', flat=True)),
                                 {self.unit_a.pk, self.unit_b.pk})
                self.assertNotContains(response, self.other_unit.name)

    def test_same_context_creates_administrator_and_channel_in_selected_unit_without_changing_existing_channels(self):
        before = self.existing_channel_snapshot()
        response = self.owner_request('post', '/usuarios/novo/', self.account_payload(
            role=User.Role.ADMIN, unit=self.unit_b, environment=str(self.other_env.pk)))
        self.assertEqual(response.status_code, 302)
        account = User.objects.get(username='usability_created')
        self.assertEqual((account.environment_id, account.unit_id, account.role),
                         (self.env.pk, self.unit_b.pk, User.Role.ADMIN))
        self.assertFalse(account.is_staff or account.is_superuser)
        response = self.owner_request('post', '/canais/novo/', self.channel_payload(
            unit=self.unit_b, environment=str(self.other_env.pk)))
        self.assertEqual(response.status_code, 200)
        channel = Channel.objects.get(name='Canal criado pelo Central')
        self.assertEqual((channel.environment_id, channel.unit_id), (self.env.pk, self.unit_b.pk))
        self.assertEqual(response.context['stream_key'], channel.reveal_key())
        self.assertEqual(self.existing_channel_snapshot(), before)

    def test_foreign_units_remain_invalid_for_owner_account_and_channel_creation(self):
        before = self.snapshot()
        for path, payload in [('/usuarios/novo/', self.account_payload(unit=self.other_unit)),
                              ('/canais/novo/', self.channel_payload(unit=self.other_unit))]:
            with self.subTest(path=path):
                response = self.owner_request('post', path, payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn('unit', response.context['form'].errors)
                self.assertEqual(self.snapshot(), before)

    def test_stale_account_form_cannot_create_on_server_selected_in_another_tab(self):
        page = self.owner_request('get', '/usuarios/novo/')
        old_context = str(page.context['form']['management_environment'].value())
        self.assertEqual(self.owner_request('post', f'/ambientes/{self.other_env.pk}/abrir/').status_code, 302)
        before = self.snapshot()
        response = self.owner_request('post', '/usuarios/novo/',
            self.account_payload(management_environment=old_context))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.other_env.pk))

    def test_stale_channel_form_cannot_create_on_server_selected_in_another_tab(self):
        page = self.owner_request('get', '/canais/novo/')
        old_context = str(page.context['form']['management_environment'].value())
        self.assertEqual(self.owner_request('post', f'/ambientes/{self.other_env.pk}/abrir/').status_code, 302)
        before = self.snapshot()
        response = self.owner_request('post', '/canais/novo/',
            self.channel_payload(management_environment=old_context))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.other_env.pk))

    def test_invalid_or_duplicate_context_is_rejected_without_using_it_as_server_selection(self):
        before = self.snapshot()
        for value in [str(self.other_env.pk), 'invalid-context',
                      [str(self.env.pk), str(self.env.pk)], [str(self.other_env.pk), str(self.env.pk)]]:
            for path, payload in [('/usuarios/novo/', self.account_payload(management_environment=value)),
                                  ('/canais/novo/', self.channel_payload(management_environment=value))]:
                with self.subTest(path=path, value=value):
                    response = self.owner_request('post', path, payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertTrue(response.context['form'].errors)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.owner_client.session['environment_id'], str(self.env.pk))

    def test_omitted_context_remains_compatible_and_uses_selected_server(self):
        account = self.account_payload(unit=self.unit_a)
        channel = self.channel_payload(unit=self.unit_a)
        account.pop('management_environment')
        channel.pop('management_environment')
        self.assertEqual(self.owner_request('post', '/usuarios/novo/', account).status_code, 302)
        self.assertEqual(self.owner_request('post', '/canais/novo/', channel).status_code, 200)
        self.assertEqual(User.objects.get(username=account['username']).environment_id, self.env.pk)
        self.assertEqual(Channel.objects.get(name=channel['name']).environment_id, self.env.pk)

    def test_owner_channel_name_uniqueness_uses_chosen_unit_not_owner_null_unit(self):
        before = self.existing_channel_snapshot()
        duplicate_name = self.channels[0].name
        response = self.owner_request('post', '/canais/novo/',
            self.channel_payload(name=duplicate_name, unit=self.unit_b))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Channel.objects.filter(name=duplicate_name, unit=self.unit_b, environment=self.env).exists())
        count = Channel.objects.count()
        response = self.owner_request('post', '/canais/novo/',
            self.channel_payload(name=duplicate_name.upper(), unit=self.unit_b))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(Channel.objects.count(), count)
        response = self.owner_request('post', '/canais/novo/',
            self.channel_payload(name=duplicate_name, unit=self.unit_a))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Channel.objects.count(), count)
        self.assertEqual(self.existing_channel_snapshot(), before)

    def test_owner_can_create_in_initial_area_and_duplicate_check_still_applies_there(self):
        response = self.owner_request('post', '/canais/novo/', self.channel_payload())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(Channel.objects.get(name='Canal criado pelo Central').unit_id)
        before = self.snapshot()
        response = self.owner_request('post', '/canais/novo/', self.channel_payload(name='CANAL INICIAL'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_nonowner_creation_keeps_own_unit_even_when_foreign_scope_is_posted(self):
        for actor in [self.admin, self.initial_admin]:
            with self.subTest(actor=actor.username):
                browser = self.browser(actor)
                page = self.request(browser, actor, 'get', '/canais/novo/')
                self.assertNotIn('unit', page.context['form'].fields)
                self.assertNotIn('management_environment', page.context['form'].fields)
                username = 'usability_from_' + actor.username
                name = 'Canal de ' + actor.username
                response = self.request(browser, actor, 'post', '/usuarios/novo/', self.account_payload(
                    username=username, unit=self.other_unit, environment=str(self.other_env.pk),
                    management_environment=str(self.other_env.pk)))
                self.assertEqual(response.status_code, 302)
                account = User.objects.get(username=username)
                self.assertEqual((account.environment_id, account.unit_id, account.role),
                                 (actor.environment_id, actor.unit_id, User.Role.VIEWER))
                response = self.request(browser, actor, 'post', '/canais/novo/', self.channel_payload(
                    name=name, unit=self.other_unit, environment=str(self.other_env.pk),
                    management_environment=str(self.other_env.pk)))
                self.assertEqual(response.status_code, 200)
                channel = Channel.objects.get(name=name)
                self.assertEqual((channel.environment_id, channel.unit_id), (actor.environment_id, actor.unit_id))

    def test_owner_channel_edit_cannot_reassign_unit_or_change_key_audience_and_existing_fields(self):
        channel = self.channels[0]
        page = self.owner_request('get', f'/canais/{channel.pk}/editar/')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('unit', page.context['form'].fields)
        before = Channel.objects.filter(pk=channel.pk).values().get()
        audience_before = list(channel.allowed_viewers.order_by('pk').values_list('pk', flat=True))
        response = self.owner_request('post', f'/canais/{channel.pk}/editar/', self.channel_payload(
            name='Canal Alfa renomeado', unit=self.other_unit, key_digest='0' * 64, audience_restricted=''))
        self.assertEqual(response.status_code, 302)
        after = Channel.objects.filter(pk=channel.pk).values().get()
        self.assertEqual(after['name'], 'Canal Alfa renomeado')
        self.assertEqual({key: value for key, value in after.items() if key != 'name'},
                         {key: value for key, value in before.items() if key != 'name'})
        self.assertEqual(list(channel.allowed_viewers.order_by('pk').values_list('pk', flat=True)), audience_before)

    def test_empty_creation_posts_are_bound_invalid_forms_without_database_changes(self):
        before = self.snapshot()
        for path in ['/ambientes/novo/', '/usuarios/novo/', '/canais/novo/']:
            with self.subTest(path=path):
                response = self.owner_request('post', path, {})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['form'].is_bound)
                self.assertTrue(response.context['form'].errors)
                self.assertEqual(self.snapshot(), before)

    def test_context_field_cannot_replace_explicit_management_server_selection(self):
        browser = self.browser(self.owner)
        before = self.snapshot()
        for path, payload in [('/usuarios/novo/', self.account_payload()),
                              ('/canais/novo/', self.channel_payload())]:
            response = self.request(browser, self.owner, 'post', path, payload)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response['Location'].endswith('/ambientes/'))
            self.assertNotIn('environment_id', browser.session)
            self.assertEqual(self.snapshot(), before)

    def test_invalid_account_form_keeps_admin_option_selected_in_rendered_html(self):
        response = self.owner_request('post', '/usuarios/novo/', self.account_payload(
            username=self.admin.username, role=User.Role.ADMIN, unit=self.unit_b))
        self.assertEqual(response.status_code, 400)
        options = SelectOptions('role', response.content.decode()).options
        self.assertEqual(set(options), {User.Role.VIEWER, User.Role.ADMIN})
        self.assertTrue(options[User.Role.ADMIN])
        self.assertEqual(str(response.context['form']['unit'].value()), str(self.unit_b.pk))

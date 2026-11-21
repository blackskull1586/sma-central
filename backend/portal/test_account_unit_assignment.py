"""Central account assignment preserves unit boundaries, sessions and channels."""
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
HOSTS = {'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'}
PASSWORD = 'Assignment-Test-Password-746!'


@override_settings(
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=HOSTS,
    SMA_REALTIME_ENABLED=True,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AccountUnitAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.unit_a = Unit.objects.create(environment=cls.env, name='Unidade Alfa Reservada', slug='alfa')
        cls.unit_b = Unit.objects.create(environment=cls.env, name='Unidade Bravo Reservada', slug='bravo')
        cls.other_unit = Unit.objects.create(environment=cls.other_env, name='Unidade Externa Reservada', slug='externa')
        cls.owner = cls.make_user('assignment_owner', User.Role.OWNER, None, None)
        cls.another_owner = cls.make_user('assignment_owner_other', User.Role.OWNER, None, None)
        cls.admin_a = cls.make_user('assignment_admin_a', User.Role.ADMIN, cls.env, cls.unit_a)
        cls.admin_b = cls.make_user('assignment_admin_b', User.Role.ADMIN, cls.env, cls.unit_b)
        cls.admin_null = cls.make_user('assignment_admin_null', User.Role.ADMIN, cls.env, None)
        cls.viewer_a = cls.make_user('assignment_viewer_a', User.Role.VIEWER, cls.env, cls.unit_a)
        cls.viewer_b = cls.make_user('assignment_viewer_b', User.Role.VIEWER, cls.env, cls.unit_b)
        cls.viewer_null = cls.make_user('assignment_viewer_null', User.Role.VIEWER, cls.env, None)
        cls.viewer_other = cls.make_user('assignment_viewer_other', User.Role.VIEWER, cls.other_env, cls.other_unit)
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
    def url(user):
        return reverse('account_unit', kwargs={'pk': user.pk})

    @staticmethod
    def token(unit):
        return str(unit.pk) if unit is not None else 'none'

    def transfer(self, target, destination, original=None, **extra):
        original = self.token(target.unit) if original is None else original
        payload = {'unit': str(destination.pk) if destination is not None else '',
                   'original_unit': original, **extra}
        return self.owner_request('post', self.url(target), payload)

    def channel_state(self):
        return list(Channel.objects.order_by('pk').values())

    def create_payload(self, username, role=User.Role.VIEWER, unit=None, **extra):
        return {'username': username, 'first_name': 'Pessoa de teste', 'role': role,
                'password1': PASSWORD, 'password2': PASSWORD,
                'unit': str(unit.pk) if unit is not None else '', **extra}

    def test_owner_create_form_offers_only_selected_environment_units_and_optional_original_area(self):
        response = self.owner_request('get', '/usuarios/novo/')
        self.assertEqual(response.status_code, 200)
        field = response.context['form'].fields['unit']
        self.assertFalse(field.required)
        self.assertCountEqual(list(field.queryset.values_list('pk', flat=True)), [self.unit_a.pk, self.unit_b.pk])
        self.assertNotContains(response, self.other_unit.name)
        self.owner_request('post', f'/ambientes/{self.other_env.pk}/abrir/')
        field = self.owner_request('get', '/usuarios/novo/').context['form'].fields['unit']
        self.assertEqual(list(field.queryset.values_list('pk', flat=True)), [self.other_unit.pk])

    def test_owner_creates_viewers_and_admins_in_selected_unit_or_original_area(self):
        for role in [User.Role.VIEWER, User.Role.ADMIN]:
            for unit in [self.unit_a, self.unit_b, None]:
                with self.subTest(role=role, unit=unit):
                    username = 'new_assignment_' + role + '_' + (unit.slug if unit else 'null')
                    response = self.owner_request('post', '/usuarios/novo/', self.create_payload(username, role, unit,
                        environment=str(self.other_env.pk), is_staff='1', is_superuser='1'))
                    self.assertEqual(response.status_code, 302)
                    created = User.objects.get(username=username)
                    self.assertEqual((created.environment_id, created.unit_id, created.role),
                                     (self.env.pk, unit.pk if unit else None, role))
                    self.assertFalse(created.is_staff or created.is_superuser)
                    self.assertTrue(created.must_change_password)
                    self.assertTrue(created.check_password(PASSWORD))
                    event = AuditEvent.objects.first()
                    self.assertEqual((event.environment_id, event.unit_id, event.actor_id),
                                     (self.env.pk, created.unit_id, self.owner.pk))

    def test_owner_cannot_create_with_foreign_or_invalid_unit(self):
        before = User.objects.count()
        for value in [str(self.other_unit.pk), 'not-a-unit']:
            with self.subTest(value=value):
                payload = self.create_payload('rejected_assignment')
                payload['unit'] = value
                response = self.owner_request('post', '/usuarios/novo/', payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(User.objects.count(), before)

    def test_admin_form_has_no_unit_control_and_forged_creation_stays_in_actor_scope(self):
        for actor in [self.admin_a, self.admin_null]:
            with self.subTest(actor=actor.username):
                client = self.client_for(actor)
                response = self.request(client, actor, 'get', '/usuarios/novo/')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('unit', response.context['form'].fields)
                username = actor.username + '_child'
                payload = self.create_payload(username, unit=self.other_unit, environment=str(self.other_env.pk))
                response = self.request(client, actor, 'post', '/usuarios/novo/', payload)
                self.assertEqual(response.status_code, 302)
                created = User.objects.get(username=username)
                self.assertEqual((created.environment_id, created.unit_id), (actor.environment_id, actor.unit_id))

    def test_assignment_get_only_reads_and_offers_current_environment_units(self):
        target_client = self.client_for(self.viewer_a)
        session_key = target_client.session.session_key
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        audit_count = AuditEvent.objects.count()
        response = self.owner_request('get', self.url(self.viewer_a))
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertCountEqual(list(form.fields['unit'].queryset.values_list('pk', flat=True)),
                             [self.unit_a.pk, self.unit_b.pk])
        self.assertEqual(str(form['original_unit'].value()), str(self.unit_a.pk))
        self.assertTrue(form.fields['original_unit'].widget.is_hidden)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        self.assertTrue(Session.objects.filter(pk=session_key).exists())

    def test_viewer_moves_between_legacy_and_units_without_changing_credentials_or_channels(self):
        target = self.viewer_null
        before = User.objects.filter(pk=target.pk).values().get()
        channels = self.channel_state()
        for destination in [self.unit_a, self.unit_b, None]:
            with self.subTest(destination=destination):
                response = self.transfer(target, destination,
                    role='owner', environment=str(self.other_env.pk), password='forged',
                    is_staff='1', is_superuser='1', must_change_password='1')
                self.assertEqual(response.status_code, 302)
                target.refresh_from_db()
                self.assertEqual(target.unit_id, destination.pk if destination else None)
                after = User.objects.filter(pk=target.pk).values().get()
                self.assertEqual({k: v for k, v in after.items() if k != 'unit_id'},
                                 {k: v for k, v in before.items() if k != 'unit_id'})
                self.assertEqual(self.channel_state(), channels)
                self.assertTrue(target.check_password(PASSWORD))

    def test_admin_can_move_out_of_unit_and_legacy_only_with_active_replacement(self):
        cases = [(self.admin_a, self.unit_b), (self.admin_null, self.unit_a), (self.admin_b, None)]
        for index, (target, destination) in enumerate(cases):
            with self.subTest(target=target.username, destination=destination):
                self.make_user('assignment_backup_' + str(index), User.Role.ADMIN, self.env, target.unit)
                before = (target.password, target.role, target.environment_id)
                response = self.transfer(target, destination)
                self.assertEqual(response.status_code, 302)
                target.refresh_from_db()
                self.assertEqual(target.unit_id, destination.pk if destination else None)
                self.assertEqual((target.password, target.role, target.environment_id), before)

    def test_last_active_admin_cannot_leave_source_unit_even_with_admins_elsewhere(self):
        for target in [self.admin_a, self.admin_b, self.admin_null]:
            with self.subTest(target=target.username):
                backup = self.make_user(target.username + '_inactive', User.Role.ADMIN, self.env, target.unit)
                backup.is_active = False
                backup.save(update_fields=['is_active'])
                destination = self.unit_b if target.unit_id != self.unit_b.pk else self.unit_a
                original = target.unit_id
                audit_count = AuditEvent.objects.count()
                client = self.client_for(target)
                session_key = client.session.session_key
                response = self.transfer(target, destination)
                self.assertEqual(response.status_code, 400)
                target.refresh_from_db()
                self.assertEqual(target.unit_id, original)
                self.assertEqual(AuditEvent.objects.count(), audit_count)
                self.assertTrue(Session.objects.filter(pk=session_key).exists())

    def test_inactive_admin_can_move_without_removing_an_active_source_administrator(self):
        self.admin_a.is_active = False
        self.admin_a.save(update_fields=['is_active'])
        response = self.transfer(self.admin_a, self.unit_b)
        self.assertEqual(response.status_code, 302)
        self.admin_a.refresh_from_db()
        self.assertFalse(self.admin_a.is_active)
        self.assertEqual(self.admin_a.unit_id, self.unit_b.pk)

    def test_stale_original_unit_rejects_second_form_and_preserves_new_session(self):
        target = self.viewer_a
        stale = self.token(target.unit)
        self.assertEqual(self.transfer(target, self.unit_b).status_code, 302)
        target.refresh_from_db()
        new_client = self.client_for(target)
        new_key = new_client.session.session_key
        audit_count = AuditEvent.objects.count()
        response = self.transfer(target, None, original=stale)
        self.assertEqual(response.status_code, 400)
        target.refresh_from_db()
        self.assertEqual(target.unit_id, self.unit_b.pk)
        self.assertTrue(Session.objects.filter(pk=new_key).exists())
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_missing_or_invalid_original_unit_cannot_bypass_stale_form_guard(self):
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        for payload in [{'unit': str(self.unit_b.pk)},
                        {'unit': str(self.unit_b.pk), 'original_unit': 'not-a-unit'},
                        {'unit': str(self.unit_b.pk), 'original_unit': 'none'}]:
            with self.subTest(payload=payload):
                response = self.owner_request('post', self.url(self.viewer_a), payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)

    def test_foreign_or_invalid_destination_is_rejected_without_revocation(self):
        client = self.client_for(self.viewer_a)
        session_key = client.session.session_key
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        for destination in [str(self.other_unit.pk), 'not-a-unit']:
            response = self.owner_request('post', self.url(self.viewer_a),
                {'unit': destination, 'original_unit': str(self.unit_a.pk)})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
            self.assertTrue(Session.objects.filter(pk=session_key).exists())

    def test_noop_does_not_revoke_sessions_or_emit_transfer_audits_even_for_last_admin(self):
        for target in [self.viewer_a, self.viewer_null, self.admin_a, self.admin_null]:
            with self.subTest(target=target.username):
                client = self.client_for(target)
                session_key = client.session.session_key
                before = User.objects.filter(pk=target.pk).values().get()
                audit_count = AuditEvent.objects.count()
                response = self.transfer(target, target.unit)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(Session.objects.filter(pk=session_key).exists())
                self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)
                self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_only_target_old_sessions_are_deleted_and_original_password_opens_new_scope(self):
        target_clients = [self.client_for(self.viewer_a), self.client_for(self.viewer_a)]
        target_keys = [client.session.session_key for client in target_clients]
        other_client = self.client_for(self.viewer_b)
        other_key = other_client.session.session_key
        owner_key = self.owner_client.session.session_key
        anonymous = Client()
        anonymous_session = anonymous.session
        anonymous_session['assignment_unrelated'] = True
        anonymous_session.save()
        anonymous_key = anonymous_session.session_key
        before_password = User.objects.get(pk=self.viewer_a.pk).password
        self.assertEqual(self.transfer(self.viewer_a, self.unit_b).status_code, 302)
        self.assertFalse(Session.objects.filter(pk__in=target_keys).exists())
        self.assertEqual(Session.objects.filter(pk__in=[other_key, owner_key, anonymous_key]).count(), 3)
        for client in target_clients:
            response = self.request(client, self.viewer_a, 'get', '/streams.json')
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.request(other_client, self.viewer_b, 'get', '/streams.json').status_code, 200)
        self.assertEqual(self.owner_request('get', '/painel/').status_code, 200)
        self.viewer_a.refresh_from_db()
        self.assertEqual(self.viewer_a.password, before_password)
        fresh = Client()
        self.assertTrue(fresh.login(username=self.viewer_a.username, password=PASSWORD))
        new_key = fresh.session.session_key
        # Public playback follows the SMA, while management remains unit-scoped.
        for channel, expected in [(self.channels[0], 200), (self.channels[1], 200)]:
            response = self.request(fresh, self.viewer_a, 'get', f'/internal/realtime/view/{channel.stream_id}/',
                HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
            self.assertEqual(response.status_code, expected)
        self.assertTrue(Session.objects.filter(pk=new_key).exists())

    def test_transfer_audits_are_scoped_to_source_and_destination_without_foreign_unit_names(self):
        before = set(AuditEvent.objects.values_list('pk', flat=True))
        self.assertEqual(self.transfer(self.viewer_a, self.unit_b).status_code, 302)
        events = list(AuditEvent.objects.exclude(pk__in=before))
        self.assertEqual(len(events), 2)
        self.assertCountEqual([event.unit_id for event in events], [self.unit_a.pk, self.unit_b.pk])
        for event in events:
            self.assertEqual((event.actor_id, event.environment_id), (self.owner.pk, self.env.pk))
            self.assertIn(self.viewer_a.username, event.target)
            other = self.unit_b if event.unit_id == self.unit_a.pk else self.unit_a
            self.assertNotIn(other.name, event.action + ' ' + event.target)
        for admin in [self.admin_a, self.admin_b, self.admin_null]:
            client = self.client_for(admin)
            page = self.request(client, admin, 'get', '/painel/')
            visible_ids = {event.pk for event in page.context['events']}
            own_ids = {event.pk for event in events if event.unit_id == admin.unit_id}
            self.assertEqual(visible_ids.intersection({event.pk for event in events}), own_ids)

    def test_legacy_source_and_destination_audits_use_null_scope(self):
        target = self.viewer_null
        for destination in [self.unit_a, None]:
            before = set(AuditEvent.objects.values_list('pk', flat=True))
            source_id = target.unit_id
            self.assertEqual(self.transfer(target, destination).status_code, 302)
            target.refresh_from_db()
            events = list(AuditEvent.objects.exclude(pk__in=before))
            self.assertEqual(len(events), 2)
            self.assertCountEqual([event.unit_id for event in events], [source_id, target.unit_id])

    def test_admins_and_viewers_cannot_get_or_post_assignment_even_for_their_own_users(self):
        for actor in [self.admin_a, self.admin_null, self.viewer_a]:
            client = self.client_for(actor)
            for target in [self.viewer_a, actor]:
                for method in ['get', 'post']:
                    with self.subTest(actor=actor.username, target=target.username, method=method):
                        response = self.request(client, actor, method, self.url(target),
                            {'unit': str(self.unit_b.pk), 'original_unit': self.token(target.unit)})
                        self.assertEqual(response.status_code, 403)
        self.viewer_a.refresh_from_db()
        self.assertEqual(self.viewer_a.unit_id, self.unit_a.pk)

    def test_owner_cannot_assign_self_another_owner_or_account_in_another_environment(self):
        for target in [self.owner, self.another_owner, self.viewer_other]:
            before = User.objects.filter(pk=target.pk).values().get()
            for method in ['get', 'post']:
                with self.subTest(target=target.username, method=method):
                    response = self.owner_request(method, self.url(target),
                        {'unit': str(self.unit_a.pk), 'original_unit': self.token(target.unit)})
                    if target == self.viewer_other:
                        self.assertEqual(response.status_code, 404)
                    else:
                        self.assertIn(response.status_code, [403, 404])
                    self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)

    def test_selected_environment_is_management_scope_even_if_monitor_query_requests_another(self):
        response = self.owner_request('post', self.url(self.viewer_other) + '?sma=' + str(self.other_env.pk),
            {'unit': str(self.other_unit.pk), 'original_unit': str(self.other_unit.pk)})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.env.pk))
        self.owner_request('post', f'/ambientes/{self.other_env.pk}/abrir/')
        response = self.owner_request('get', self.url(self.viewer_other))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['form'].fields['unit'].queryset.values_list('pk', flat=True)),
                         [self.other_unit.pk])
        self.assertEqual(self.owner_request('get', self.url(self.viewer_a)).status_code, 404)

    def test_browser_assignment_requires_csrf_and_get_does_not_apply_query_parameters(self):
        browser = self.client_for(self.owner, self.env, csrf=True)
        payload = {'unit': str(self.unit_b.pk), 'original_unit': str(self.unit_a.pk)}
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        response = self.request(browser, self.owner, 'get', self.url(self.viewer_a), payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
        response = self.request(browser, self.owner, 'post', self.url(self.viewer_a), payload)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)
        token = browser.cookies[settings.CSRF_COOKIE_NAME].value
        response = self.request(browser, self.owner, 'post', self.url(self.viewer_a), payload,
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://' + OWNER_HOST)
        self.assertEqual(response.status_code, 302)
        self.viewer_a.refresh_from_db()
        self.assertEqual(self.viewer_a.unit_id, self.unit_b.pk)

    def test_dashboard_owner_has_assignment_control_and_admin_does_not(self):
        owner_page = self.owner_request('get', '/painel/')
        self.assertContains(owner_page, 'Alterar ambiente')
        self.assertContains(owner_page, self.url(self.viewer_a))
        self.assertContains(owner_page, self.unit_a.name)
        self.assertContains(owner_page, self.unit_b.name)
        admin = self.client_for(self.admin_a)
        admin_page = self.request(admin, self.admin_a, 'get', '/painel/')
        self.assertNotContains(admin_page, 'Alterar ambiente')
        self.assertNotContains(admin_page, self.url(self.viewer_a))

    def test_disabled_environment_blocks_assignment_without_mutation(self):
        self.env.active = False
        self.env.save(update_fields=['active'])
        before = User.objects.filter(pk=self.viewer_a.pk).values().get()
        response = self.transfer(self.viewer_a, self.unit_b)
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), before)

    def test_admin_opened_reset_form_cannot_reset_account_after_owner_transfers_it(self):
        admin_client = self.client_for(self.admin_a)
        reset_url = f'/usuarios/{self.viewer_a.pk}/senha/'
        opened = self.request(admin_client, self.admin_a, 'get', reset_url)
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(self.transfer(self.viewer_a, self.unit_b).status_code, 302)
        after_transfer = User.objects.filter(pk=self.viewer_a.pk).values().get()
        audit_count = AuditEvent.objects.count()
        new_password = 'Rejected-After-Transfer-Secret-265!'
        submitted = self.request(admin_client, self.admin_a, 'post', reset_url,
            {'new_password1': new_password, 'new_password2': new_password})
        self.assertEqual(submitted.status_code, 404)
        self.assertEqual(User.objects.filter(pk=self.viewer_a.pk).values().get(), after_transfer)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        self.viewer_a.refresh_from_db()
        self.assertEqual(self.viewer_a.unit_id, self.unit_b.pk)
        self.assertTrue(self.viewer_a.check_password(PASSWORD))
        self.assertFalse(self.viewer_a.check_password(new_password))

    def test_password_save_preserves_database_unit_and_profile_when_form_instance_is_stale(self):
        # This deliberately makes the in-memory form stale with a same-connection
        # write. It checks restricted-field persistence, not PostgreSQL concurrency
        # or lock scheduling: a real concurrent transfer must acquire the row lock.
        cases = [(self.viewer_a, PasswordChangeForm, '/conta/senha/', False),
                 (self.viewer_null, SetPasswordForm, f'/usuarios/{self.viewer_null.pk}/senha/', True)]
        for target, form_class, path, administrative in cases:
            with self.subTest(administrative=administrative):
                client = self.owner_client if administrative else self.client_for(target)
                actor = self.owner if administrative else target
                original_save = form_class.save
                marker = 'Perfil atualizado durante edição da senha'
                password = 'Updated-Without-Stale-Overwrite-523!'
                channels = self.channel_state()

                def make_form_stale_then_save(form, *args, **kwargs):
                    User.objects.filter(pk=form.user.pk).update(unit=self.unit_b, first_name=marker)
                    return original_save(form, *args, **kwargs)

                with patch.object(form_class, 'save', make_form_stale_then_save):
                    response = self.request(client, actor, 'post', path,
                        {'old_password': PASSWORD, 'new_password1': password, 'new_password2': password})
                self.assertEqual(response.status_code, 302)
                target.refresh_from_db()
                self.assertEqual(target.unit_id, self.unit_b.pk)
                self.assertEqual(target.first_name, marker)
                self.assertEqual(target.environment_id, self.env.pk)
                self.assertEqual(target.role, User.Role.VIEWER)
                self.assertTrue(target.check_password(password))
                self.assertEqual(target.must_change_password, administrative)
                self.assertEqual(self.channel_state(), channels)

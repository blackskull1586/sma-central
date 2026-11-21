"""Deleting an empty unit preserves data and keeps its archived history private."""
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .access import unit_records
from .models import AuditEvent, Channel, ChannelInput, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOSTS = {'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'}
PASSWORD = 'Unit-Deletion-Test-Fixture-821!'


@override_settings(
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class UnitDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.empty = Unit.objects.create(environment=cls.env, name='Ambiente vazio reservado', slug='vazio')
        cls.occupied = Unit.objects.create(environment=cls.env, name='Ambiente em uso', slug='ocupado')
        cls.other_empty = Unit.objects.create(environment=cls.other_env, name='Ambiente vazio externo', slug='externo')
        cls.owner = cls.make_user('unit_delete_owner', User.Role.OWNER)
        cls.admin = cls.make_user('unit_delete_admin', User.Role.ADMIN, cls.env, cls.occupied)
        cls.viewer = cls.make_user('unit_delete_viewer', User.Role.VIEWER, cls.env, cls.occupied)
        cls.legacy_admin = cls.make_user('unit_delete_legacy', User.Role.ADMIN, cls.env)
        cls.channel = cls.make_channel(cls.occupied, 'Canal preservado')
        cls.input = ChannelInput.objects.create(channel=cls.channel, slot=2,
            key_digest=cls.channel.key_digest, publisher_client_id='501')
        cls.channel.allowed_viewers.add(cls.viewer)

    @staticmethod
    def make_user(username, role, environment=None, unit=None, active=True):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, is_active=active, must_change_password=False)

    @staticmethod
    def make_channel(unit, name, active=True):
        channel = Channel(environment=unit.environment, unit=unit, name=name, active=active)
        channel.issue_key()
        channel.save()
        return channel

    def setUp(self):
        self.owner_client = self.browser(self.owner)

    def browser(self, user, environment=None, csrf=False):
        browser = Client(enforce_csrf_checks=csrf)
        browser.force_login(user)
        if user.is_owner and environment is not None:
            session = browser.session
            session['environment_id'] = str(environment.pk)
            session.save()
        return browser

    @staticmethod
    def url(unit):
        return reverse('unit_delete', kwargs={'pk': unit.pk})

    def request(self, browser, user, method, path, data=None, **headers):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        return getattr(browser, method)(path, data={} if data is None else data,
            HTTP_HOST=host, secure=True, **headers)

    def owner_request(self, method, unit=None, data=None, **headers):
        return self.request(self.owner_client, self.owner, method, self.url(unit or self.empty), data, **headers)

    def delete(self, unit=None):
        unit = unit or self.empty
        return self.owner_request('post', unit, {'confirmation': str(unit.pk)})

    @staticmethod
    def snapshot():
        return {model.__name__: list(model.objects.order_by('pk').values())
                for model in [Environment, Unit, User, Channel, ChannelInput, AuditEvent]}

    def test_owner_get_confirms_unit_and_server_without_requiring_selection_or_mutating_data(self):
        before = self.snapshot()
        response = self.owner_request('get')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.empty.name)
        self.assertContains(response, self.env.name)
        self.assertContains(response, 'name="confirmation"')
        self.assertContains(response, str(self.empty.pk))
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('environment_id', self.owner_client.session)

    def test_owner_can_delete_empty_unit_and_preserve_every_unrelated_record(self):
        audience = list(Channel.allowed_viewers.through.objects.order_by('pk').values())
        viewer_browser = self.browser(self.viewer)
        session_key = viewer_browser.session.session_key
        stored_session = Session.objects.get(session_key=session_key).session_data
        before = self.snapshot()
        response = self.delete()
        self.assertRedirects(response, reverse('units'), fetch_redirect_response=False)
        self.assertFalse(Unit.objects.filter(pk=self.empty.pk).exists())
        after = self.snapshot()
        for model in ['Environment', 'User', 'Channel', 'ChannelInput']:
            self.assertEqual(after[model], before[model])
        self.assertEqual(after['Unit'], [row for row in before['Unit'] if row['id'] != self.empty.pk])
        self.assertEqual(list(Channel.allowed_viewers.through.objects.order_by('pk').values()), audience)
        self.assertEqual(Session.objects.get(session_key=session_key).session_data, stored_session)
        event = AuditEvent.objects.get(action='Ambiente excluído')
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.environment_id, self.env.pk)
        self.assertIsNone(event.unit_id)
        self.assertTrue(event.owner_only)
        self.assertIn(self.empty.name, event.target)
        self.assertIn(str(self.empty.pk), event.target)

    def test_global_owner_deletion_uses_targets_server_and_keeps_other_selected_server(self):
        self.owner_client = self.browser(self.owner, self.other_env)
        self.assertEqual(self.owner_request('get').status_code, 200)
        self.assertEqual(self.delete().status_code, 302)
        self.assertEqual(self.owner_client.session['environment_id'], str(self.other_env.pk))
        self.assertTrue(Environment.objects.filter(pk=self.other_env.pk, active=True).exists())
        self.assertTrue(Unit.objects.filter(pk=self.other_empty.pk).exists())
        event = AuditEvent.objects.get(action='Ambiente excluído')
        self.assertEqual(event.environment_id, self.env.pk)

    def test_global_owner_can_delete_unit_in_another_server_without_selecting_it(self):
        self.owner_client = self.browser(self.owner, self.env)
        response = self.delete(self.other_empty)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Unit.objects.filter(pk=self.other_empty.pk).exists())
        self.assertTrue(Unit.objects.filter(pk=self.empty.pk).exists())
        self.assertEqual(self.owner_client.session['environment_id'], str(self.env.pk))
        self.assertEqual(AuditEvent.objects.get(action='Ambiente excluído').environment_id, self.other_env.pk)

    def test_missing_wrong_and_duplicate_confirmation_are_rejected_without_mutation(self):
        before = self.snapshot()
        payloads = [{}, {'confirmation': ''}, {'confirmation': str(self.other_empty.pk)},
                    {'confirmation': 'not-an-id'},
                    {'confirmation': [str(self.empty.pk), str(self.empty.pk)]},
                    {'confirmation': [str(self.other_empty.pk), str(self.empty.pk)]}]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.owner_request('post', data=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_administrators_and_viewers_cannot_view_or_execute_deletion(self):
        clients = [(user, self.browser(user)) for user in [self.admin, self.legacy_admin, self.viewer]]
        before = self.snapshot()
        for user, browser in clients:
            for method in ['get', 'post']:
                with self.subTest(user=user.username, method=method):
                    response = self.request(browser, user, method, self.url(self.empty),
                        {'confirmation': str(self.empty.pk)})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_anonymous_requests_require_login_and_owner_password_change_blocks_deletion(self):
        before = self.snapshot()
        anonymous = Client()
        for method in ['get', 'post']:
            response = getattr(anonymous, method)(self.url(self.empty),
                {'confirmation': str(self.empty.pk)}, HTTP_HOST=OWNER_HOST, secure=True)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response['Location'])
        self.assertEqual(self.snapshot(), before)
        User.objects.filter(pk=self.owner.pk).update(must_change_password=True)
        before = self.snapshot()
        response = self.delete()
        self.assertRedirects(response, '/conta/senha/', fetch_redirect_response=False)
        self.assertEqual(self.snapshot(), before)

    def test_csrf_and_http_method_guards_protect_deletion(self):
        browser = self.browser(self.owner, csrf=True)
        response = self.request(browser, self.owner, 'post', self.url(self.empty),
            {'confirmation': str(self.empty.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Unit.objects.filter(pk=self.empty.pk).exists())
        for method in ['put', 'delete', 'patch']:
            self.assertEqual(self.owner_request(method).status_code, 405)
        self.assertEqual(self.request(browser, self.owner, 'get', self.url(self.empty)).status_code, 200)
        response = self.request(browser, self.owner, 'post', self.url(self.empty), {
            'confirmation': str(self.empty.pk),
            'csrfmiddlewaretoken': browser.cookies[settings.CSRF_COOKIE_NAME].value,
        }, HTTP_ORIGIN=f'https://{OWNER_HOST}', HTTP_REFERER=f'https://{OWNER_HOST}' + self.url(self.empty))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Unit.objects.filter(pk=self.empty.pk).exists())

    def test_linked_users_block_deletion_including_inactive_accounts(self):
        for index, active in enumerate([True, False]):
            with self.subTest(active=active):
                unit = Unit.objects.create(environment=self.env, name=f'Com conta {index}', slug=f'conta-{index}')
                self.make_user(f'unit_delete_blocker_{index}', User.Role.VIEWER, self.env, unit, active)
                before = self.snapshot()
                response = self.delete(unit)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_linked_channels_block_deletion_including_inactive_channels(self):
        for index, active in enumerate([True, False]):
            with self.subTest(active=active):
                unit = Unit.objects.create(environment=self.env, name=f'Com canal {index}', slug=f'canal-{index}')
                self.make_channel(unit, f'Canal bloqueador {index}', active)
                before = self.snapshot()
                response = self.delete(unit)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_unit_with_both_accounts_and_channels_is_not_partially_cleared(self):
        before = self.snapshot()
        self.assertEqual(self.owner_request('get', self.occupied).status_code, 200)
        self.assertEqual(self.delete(self.occupied).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_account_added_after_confirmation_page_blocks_submission(self):
        self.assertEqual(self.owner_request('get').status_code, 200)
        self.make_user('unit_delete_late_user', User.Role.VIEWER, self.env, self.empty)
        before = self.snapshot()
        self.assertEqual(self.delete().status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_channel_added_after_confirmation_page_blocks_submission(self):
        self.assertEqual(self.owner_request('get').status_code, 200)
        self.make_channel(self.empty, 'Canal criado após confirmação')
        before = self.snapshot()
        self.assertEqual(self.delete().status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_disabled_parent_server_and_unknown_unit_cannot_be_deleted(self):
        Environment.objects.filter(pk=self.env.pk).update(active=False)
        before = self.snapshot()
        self.assertEqual(self.delete().status_code, 404)
        missing = uuid.uuid4()
        path = reverse('unit_delete', kwargs={'pk': missing})
        for method in ['get', 'post']:
            self.assertEqual(self.request(self.owner_client, self.owner, method, path,
                {'confirmation': str(missing)}).status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_repeated_deletion_is_not_successful_and_does_not_duplicate_history(self):
        self.assertEqual(self.delete().status_code, 302)
        before = self.snapshot()
        self.assertEqual(self.delete().status_code, 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(AuditEvent.objects.filter(action='Ambiente excluído').count(), 1)

    def test_history_is_preserved_but_archived_events_never_become_visible_to_initial_area_admin(self):
        first = AuditEvent.objects.create(environment=self.env, unit=self.empty, actor=self.owner,
            action='Registro reservado A', target='historia-reservada-vazio-A')
        second = AuditEvent.objects.create(environment=self.env, unit=self.empty, actor=self.legacy_admin,
            action='Registro reservado B', target='historia-reservada-vazio-B')
        initial = AuditEvent.objects.create(environment=self.env, unit=None, actor=self.legacy_admin,
            action='Registro inicial', target='historia-publica-area-inicial')
        previous = list(AuditEvent.objects.filter(pk__in=[first.pk, second.pk]).order_by('pk').values())
        self.assertEqual(self.delete().status_code, 302)
        archived = list(AuditEvent.objects.filter(pk__in=[first.pk, second.pk]).order_by('pk').values())
        self.assertEqual(len(archived), 2)
        for old, new in zip(previous, archived):
            self.assertIsNone(new['unit_id'])
            self.assertTrue(new['owner_only'])
            self.assertEqual({key: value for key, value in new.items() if key not in ['unit_id', 'owner_only']},
                             {key: value for key, value in old.items() if key not in ['unit_id', 'owner_only']})
        initial.refresh_from_db()
        self.assertFalse(initial.owner_only)
        self.owner_client = self.browser(self.owner, self.env)
        owner_page = self.request(self.owner_client, self.owner, 'get', '/painel/')
        for target in [first.target, second.target, initial.target]:
            self.assertContains(owner_page, target)
        self.assertContains(owner_page, self.empty.name)
        local_page = self.request(self.browser(self.legacy_admin), self.legacy_admin, 'get', '/painel/')
        self.assertContains(local_page, initial.target)
        for target in [first.target, second.target, self.empty.name]:
            self.assertNotContains(local_page, target)

    def test_owner_only_filter_applies_to_scoped_events_without_hiding_other_models(self):
        private = AuditEvent.objects.create(environment=self.env, unit=self.occupied, actor=self.owner,
            action='Somente Central', target='historia-reservada-ocupado', owner_only=True)
        public = AuditEvent.objects.create(environment=self.env, unit=self.occupied, actor=self.admin,
            action='Da equipe', target='historia-equipe-ocupado')
        local_request = SimpleNamespace(user=self.admin)
        owner_request = SimpleNamespace(user=self.owner)
        self.assertEqual(set(unit_records(local_request, AuditEvent.objects.all()).values_list('pk', flat=True)),
                         {public.pk})
        self.assertEqual(set(unit_records(owner_request, AuditEvent.objects.all()).values_list('pk', flat=True)),
                         {private.pk, public.pk})
        self.assertEqual(list(unit_records(local_request, Channel.objects.all()).values_list('pk', flat=True)),
                         [self.channel.pk])
        self.assertEqual(set(unit_records(local_request, User.objects.all()).values_list('pk', flat=True)),
                         {self.admin.pk, self.viewer.pk})

    def test_failure_to_record_deletion_rolls_back_unit_and_archived_history(self):
        AuditEvent.objects.create(environment=self.env, unit=self.empty, actor=self.owner,
            action='Antes da exclusão', target='registro-rollback-preservado')
        before = self.snapshot()
        create = AuditEvent.objects.create

        def fail_deletion_record(*args, **kwargs):
            if kwargs.get('action') == 'Ambiente excluído':
                raise RuntimeError('Simulated deletion audit failure')
            return create(*args, **kwargs)

        with patch('portal.views.AuditEvent.objects.create', side_effect=fail_deletion_record):
            with self.assertRaises(RuntimeError):
                self.delete()
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_reference_errors_return_conflict_and_restore_archived_history(self):
        AuditEvent.objects.create(environment=self.env, unit=self.empty, actor=self.owner,
            action='Antes de novo vínculo', target='registro-conflito-preservado')
        before = self.snapshot()
        failures = [ProtectedError('Simulated concurrent protected reference', [self.channel]),
                    IntegrityError('Simulated concurrent database reference')]
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                with patch('portal.views.Unit.delete', side_effect=failure):
                    response = self.delete()
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.snapshot(), before)

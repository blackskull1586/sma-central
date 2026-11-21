"""Viewer selection controls playback independently of a channel's management unit."""
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOSTS = {'sma1.example.test': 'sma1', 'sma2.example.test': 'sma2'}
PASSWORD = 'RioClaro-835!Janela'


@override_settings(
    SMA_REALTIME_ENABLED=True,
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    HLS_FRESH_SECONDS=120,
)
class ChannelAudienceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.unit_a = Unit.objects.create(environment=cls.env, name='Unidade Alfa', slug='alfa')
        cls.unit_b = Unit.objects.create(environment=cls.env, name='Unidade Bravo', slug='bravo')
        cls.other_unit = Unit.objects.create(environment=cls.other_env, name='Unidade Externa', slug='externa')
        cls.owner = cls.make_user('audience_owner', User.Role.OWNER, None, None)
        cls.admin_a = cls.make_user('audience_admin_a', User.Role.ADMIN, cls.env, cls.unit_a)
        cls.admin_b = cls.make_user('audience_admin_b', User.Role.ADMIN, cls.env, cls.unit_b)
        cls.admin_null = cls.make_user('audience_admin_null', User.Role.ADMIN, cls.env, None)
        cls.viewer_a = cls.make_user('audience_viewer_a', User.Role.VIEWER, cls.env, cls.unit_a)
        cls.viewer_b = cls.make_user('audience_viewer_b', User.Role.VIEWER, cls.env, cls.unit_b)
        cls.viewer_null = cls.make_user('audience_viewer_null', User.Role.VIEWER, cls.env, None)
        cls.foreign_viewer = cls.make_user('audience_foreign', User.Role.VIEWER, cls.other_env, cls.other_unit)
        cls.inactive_viewer = cls.make_user('audience_inactive', User.Role.VIEWER, cls.env, cls.unit_b)
        User.objects.filter(pk=cls.inactive_viewer.pk).update(is_active=False)
        cls.inactive_viewer.refresh_from_db()
        # Bypass full_clean deliberately to exercise defensive handling of inconsistent data.
        cls.invalid_viewer = cls.make_user('audience_invalid', User.Role.VIEWER, cls.env, cls.other_unit)
        cls.public_a, cls.key_a = cls.make_channel(cls.env, cls.unit_a, 'Público Alfa')
        cls.public_b, cls.key_b = cls.make_channel(cls.env, cls.unit_b, 'Público Bravo')
        cls.public_null, cls.key_null = cls.make_channel(cls.env, None, 'Público original')
        cls.private_a, cls.key_private = cls.make_channel(cls.env, cls.unit_a, 'Reservado Alfa', restricted=True)
        cls.private_a.allowed_viewers.set([cls.viewer_b, cls.admin_b, cls.viewer_null])
        cls.empty_private, cls.key_empty = cls.make_channel(cls.env, cls.unit_b, 'Reservado sem acessos', restricted=True)
        cls.foreign_channel, cls.key_foreign = cls.make_channel(cls.other_env, cls.other_unit, 'Público outro SMA')

    @staticmethod
    def make_user(username, role, environment, unit):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, must_change_password=False)

    @staticmethod
    def make_channel(environment, unit, name, *, restricted=False):
        channel = Channel(environment=environment, unit=unit, name=name, audience_restricted=restricted)
        key = channel.issue_key()
        channel.save()
        return channel, key

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hls_dir = Path(directory.name)
        override = override_settings(HLS_DIR=self.hls_dir)
        override.enable()
        self.addCleanup(override.disable)
        caches['rtmp_auth'].clear()
        for channel in self.channels:
            Channel.objects.filter(pk=channel.pk).update(last_seen=timezone.now(), publisher_client_id='123')
            (self.hls_dir / f'{channel.stream_id}.m3u8').write_text('#EXTM3U\n')
        self.owner_client = self.client_for(self.owner, self.env)

    @property
    def channels(self):
        return [self.public_a, self.public_b, self.public_null,
                self.private_a, self.empty_private, self.foreign_channel]

    @property
    def tenant_users(self):
        return [self.admin_a, self.admin_b, self.admin_null, self.viewer_a, self.viewer_b, self.viewer_null]

    def client_for(self, user, environment=None, *, csrf=False):
        client = Client(enforce_csrf_checks=csrf)
        client.force_login(user)
        if user.is_owner and environment is not None:
            session = client.session
            session['environment_id'] = str(environment.pk)
            session.save()
        return client

    def request(self, client, user, method, path, data=None, **extra):
        host = extra.pop('HTTP_HOST', OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test')
        return getattr(client, method)(path, data=data or {}, HTTP_HOST=host, secure=True, **extra)

    def path(self, channel=None):
        return reverse('channel_viewers', args=[(channel or self.public_a).pk])

    def owner_request(self, method, channel=None, data=None, **extra):
        return self.request(self.owner_client, self.owner, method, self.path(channel), data, **extra)

    def set_viewers(self, channel, users):
        return self.owner_request('post', channel, {'allowed_viewers': [str(user.pk) for user in users]})

    def catalog(self, client, user, data=None):
        return self.request(client, user, 'get', '/streams.json', data)

    def media(self, client, user, channel, kind, **extra):
        headers = {'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN, **extra}
        if kind == 'rtc':
            path = f'/internal/realtime/view/{channel.stream_id}/'
        else:
            path = '/internal/hls/'
            suffix = '.m3u8' if kind == 'manifest' else '-123.ts'
            headers['HTTP_X_ORIGINAL_URI'] = f'/hls/{channel.stream_id}{suffix}'
        return self.request(client, user, 'get', path, **headers)

    def assert_playback(self, client, user, channel, allowed):
        response = self.catalog(client, user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(channel.stream_id in [row['id'] for row in response.json()], allowed)
        for kind in ['manifest', 'segment', 'rtc']:
            with self.subTest(user=user.username, channel=channel.name, kind=kind, allowed=allowed):
                expected = (200 if kind == 'rtc' else 204) if allowed else 403
                self.assertEqual(self.media(client, user, channel, kind).status_code, expected)

    def snapshot(self, channel):
        return (Channel.objects.filter(pk=channel.pk).values().get(),
                set(channel.allowed_viewers.values_list('pk', flat=True)))

    def test_public_and_selected_private_playback_spans_units_but_not_servers(self):
        public = [self.public_a, self.public_b, self.public_null]
        selected = {self.viewer_b.pk, self.admin_b.pk, self.viewer_null.pk}
        for user in self.tenant_users:
            with self.subTest(user=user.username):
                client = self.client_for(user)
                expected = public + ([self.private_a] if user.pk in selected else [])
                response = self.catalog(client, user, {'unit': str(self.other_unit.pk), 'user': self.viewer_b.pk})
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual([row['id'] for row in response.json()], [c.stream_id for c in expected])
                for channel in self.channels:
                    for kind in ['manifest', 'segment', 'rtc']:
                        allowed = channel.pk in {c.pk for c in expected}
                        code = (200 if kind == 'rtc' else 204) if allowed else 403
                        self.assertEqual(self.media(client, user, channel, kind).status_code, code)

    def test_owner_reads_every_active_channel_even_private_with_empty_selection(self):
        client = self.client_for(self.owner)
        response = self.catalog(client, self.owner)
        self.assertCountEqual([row['id'] for row in response.json()], [c.stream_id for c in self.channels])
        for channel in self.channels:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.media(client, self.owner, channel, kind).status_code,
                                 200 if kind == 'rtc' else 204)
        response = self.catalog(client, self.owner, {'sma': str(self.env.pk)})
        self.assertCountEqual([row['id'] for row in response.json()],
                             [c.stream_id for c in self.channels if c.environment_id == self.env.pk])

    def test_unselected_admin_manages_own_private_channel_without_playback_privilege(self):
        client = self.client_for(self.admin_a)
        self.assert_playback(client, self.admin_a, self.private_a, False)
        dashboard = self.request(client, self.admin_a, 'get', '/painel/')
        self.assertCountEqual([c.pk for c in dashboard.context['channels']], [self.public_a.pk, self.private_a.pk])
        api = self.request(client, self.admin_a, 'get', '/api/channels/')
        self.assertCountEqual([row['id'] for row in api.json()], [str(self.public_a.pk), str(self.private_a.pk)])
        response = self.request(client, self.admin_a, 'get', f'/canais/{self.private_a.pk}/credenciais/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stream_key'], self.key_private)
        # Being selected to watch another unit does not grant any management capability.
        client = self.client_for(self.admin_b)
        self.assert_playback(client, self.admin_b, self.private_a, True)
        for suffix in ['credenciais', 'editar', 'excluir']:
            self.assertEqual(self.request(client, self.admin_b, 'get',
                f'/canais/{self.private_a.pk}/{suffix}/').status_code, 404)

    def test_owner_get_lists_valid_same_server_admins_and_viewers_without_mutation(self):
        before = self.snapshot(self.private_a)
        response = self.owner_request('get', self.private_a)
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertIn('allowed_viewers', form.fields)
        choices = set(form.fields['allowed_viewers'].queryset.values_list('pk', flat=True))
        self.assertEqual(choices, {u.pk for u in self.tenant_users + [self.inactive_viewer]})
        self.assertEqual({str(pk) for pk in form['allowed_viewers'].value()},
                         {str(self.viewer_b.pk), str(self.admin_b.pk), str(self.viewer_null.pk)})
        self.assertEqual(self.snapshot(self.private_a), before)
        self.assertNotContains(response, self.key_private)
        self.assertNotContains(response, self.private_a.key_ciphertext)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_owner_selection_restricts_playback_then_revokes_and_explicit_empty_reopens(self):
        channel = self.public_a
        clients = {u.pk: self.client_for(u) for u in self.tenant_users}
        for users in [[self.viewer_a, self.viewer_b, self.admin_b], [self.viewer_b], []]:
            with self.subTest(selected=[u.username for u in users]):
                before = self.snapshot(channel)[0]
                self.assertEqual(self.set_viewers(channel, users).status_code, 302)
                channel.refresh_from_db()
                self.assertEqual(channel.audience_restricted, bool(users))
                self.assertEqual(set(channel.allowed_viewers.values_list('pk', flat=True)), {u.pk for u in users})
                after = self.snapshot(channel)[0]
                self.assertEqual({k: v for k, v in after.items() if k != 'audience_restricted'},
                                 {k: v for k, v in before.items() if k != 'audience_restricted'})
                for user in self.tenant_users:
                    self.assert_playback(clients[user.pk], user, channel, not users or user in users)
                event = AuditEvent.objects.first()
                self.assertEqual((event.actor_id, event.environment_id, event.unit_id),
                                 (self.owner.pk, self.env.pk, self.unit_a.pk))
                self.assertNotIn(self.key_a, event.action + event.target)

    def test_invalid_selection_is_rejected_atomically_without_widening_access(self):
        before = self.snapshot(self.private_a)
        count = AuditEvent.objects.count()
        bad_values = [self.owner.pk, self.foreign_viewer.pk, self.invalid_viewer.pk,
                      User.objects.order_by('-pk').first().pk + 1000, 'not-an-id']
        for invalid in bad_values:
            with self.subTest(invalid=invalid):
                response = self.owner_request('post', self.private_a,
                    {'allowed_viewers': [str(self.viewer_a.pk), str(invalid)]})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(self.private_a), before)
                self.assertEqual(AuditEvent.objects.count(), count)

    def test_nonowners_and_anonymous_cannot_read_or_change_viewer_selection(self):
        before = self.snapshot(self.public_a)
        for user in [self.admin_a, self.admin_b, self.admin_null, self.viewer_a, self.viewer_b]:
            client = self.client_for(user)
            for method in ['get', 'post']:
                with self.subTest(user=user.username, method=method):
                    response = self.request(client, user, method, self.path(), {'allowed_viewers': [user.pk]})
                    self.assertEqual(response.status_code, 403)
        anonymous = Client()
        for method in ['get', 'post']:
            response = getattr(anonymous, method)(self.path(), HTTP_HOST=OWNER_HOST, secure=True)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response['Location'])
        self.assertEqual(self.snapshot(self.public_a), before)

    def test_owner_must_use_selected_management_server_not_monitoring_filter(self):
        before = self.snapshot(self.foreign_channel)
        for method in ['get', 'post']:
            response = self.owner_request(method, self.foreign_channel,
                {'sma': str(self.other_env.pk), 'environment': str(self.other_env.pk),
                 'allowed_viewers': [self.foreign_viewer.pk]})
            self.assertEqual(response.status_code, 404)
            response = self.request(self.owner_client, self.owner, method,
                self.path(self.foreign_channel) + '?sma=' + str(self.other_env.pk),
                {'allowed_viewers': [self.foreign_viewer.pk]})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.snapshot(self.foreign_channel), before)
        missing = reverse('channel_viewers', args=[uuid.uuid4()])
        self.assertEqual(self.request(self.owner_client, self.owner, 'post', missing).status_code, 404)

    def test_csrf_and_allowed_http_methods_protect_selection_changes(self):
        browser = self.client_for(self.owner, self.env, csrf=True)
        payload = {'allowed_viewers': [str(self.viewer_a.pk)]}
        response = self.request(browser, self.owner, 'post', self.path(), payload)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Channel.objects.get(pk=self.public_a.pk).audience_restricted)
        self.assertEqual(self.request(browser, self.owner, 'get', self.path()).status_code, 200)
        payload['csrfmiddlewaretoken'] = browser.cookies[settings.CSRF_COOKIE_NAME].value
        response = self.request(browser, self.owner, 'post', self.path(), payload,
            HTTP_ORIGIN=f'https://{OWNER_HOST}', HTTP_REFERER=f'https://{OWNER_HOST}' + self.path())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.owner_request('put').status_code, 405)
        self.assertEqual(self.owner_request('delete').status_code, 405)

    def test_dashboard_controls_are_central_only_and_other_forms_cannot_set_audience(self):
        owner_page = self.request(self.owner_client, self.owner, 'get', '/painel/')
        self.assertContains(owner_page, self.path(self.public_a))
        for actor in [self.admin_a, self.admin_null]:
            client = self.client_for(actor)
            page = self.request(client, actor, 'get', '/painel/')
            self.assertNotContains(page, '/visualizadores/')
            create_form = self.request(client, actor, 'get', '/canais/novo/').context['form']
            self.assertNotIn('allowed_viewers', create_form.fields)
            self.assertNotIn('audience_restricted', create_form.fields)
            response = self.request(client, actor, 'post', '/canais/novo/',
                {'name': 'Canal forjado ' + actor.username, 'audience_restricted': '1',
                 'allowed_viewers': [str(self.viewer_b.pk)], 'unit': str(self.unit_b.pk)})
            self.assertEqual(response.status_code, 200)
            channel = Channel.objects.get(name='Canal forjado ' + actor.username)
            self.assertEqual(channel.unit_id, actor.unit_id)
            self.assertFalse(channel.audience_restricted)
            self.assertFalse(channel.allowed_viewers.exists())
        client = self.client_for(self.admin_a)
        before = set(self.private_a.allowed_viewers.values_list('pk', flat=True))
        response = self.request(client, self.admin_a, 'post', f'/canais/{self.private_a.pk}/editar/',
            {'name': 'Reservado renomeado', 'audience_restricted': '', 'allowed_viewers': [self.admin_a.pk]})
        self.assertEqual(response.status_code, 302)
        self.private_a.refresh_from_db()
        self.assertTrue(self.private_a.audience_restricted)
        self.assertEqual(set(self.private_a.allowed_viewers.values_list('pk', flat=True)), before)

    def test_deleting_last_selected_account_keeps_channel_private_until_explicit_clear(self):
        self.assertEqual(self.set_viewers(self.public_a, [self.viewer_a]).status_code, 302)
        before_key = self.public_a.key_digest
        response = self.request(self.owner_client, self.owner, 'post',
            f'/usuarios/{self.viewer_a.pk}/excluir/', {'confirmation': str(self.viewer_a.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.viewer_a.pk).exists())
        self.public_a.refresh_from_db()
        self.assertTrue(self.public_a.audience_restricted)
        self.assertFalse(self.public_a.allowed_viewers.exists())
        self.assertEqual(self.public_a.key_digest, before_key)
        for user in [self.admin_a, self.viewer_b, self.viewer_null]:
            self.assert_playback(self.client_for(user), user, self.public_a, False)
        self.assert_playback(self.owner_client, self.owner, self.public_a, True)
        self.assertEqual(self.set_viewers(self.public_a, []).status_code, 302)
        self.assert_playback(self.client_for(self.viewer_b), self.viewer_b, self.public_a, True)

    def test_inactive_selection_is_preserved_but_cannot_authenticate_until_reactivated(self):
        self.assertEqual(self.set_viewers(self.public_a, [self.inactive_viewer]).status_code, 302)
        selected = self.owner_request('get').context['form']['allowed_viewers'].value()
        self.assertEqual({str(pk) for pk in selected}, {str(self.inactive_viewer.pk)})
        client = self.client_for(self.inactive_viewer)
        self.assertEqual(self.catalog(client, self.inactive_viewer).status_code, 401)
        for kind in ['manifest', 'segment', 'rtc']:
            self.assertEqual(self.media(client, self.inactive_viewer, self.public_a, kind).status_code, 401)
        self.assert_playback(self.client_for(self.admin_a), self.admin_a, self.public_a, False)
        User.objects.filter(pk=self.inactive_viewer.pk).update(is_active=True)
        self.inactive_viewer.refresh_from_db()
        self.assert_playback(self.client_for(self.inactive_viewer), self.inactive_viewer, self.public_a, True)

    def test_cross_server_membership_and_inconsistent_unit_never_override_identity_scope(self):
        # Simulate invalid legacy/direct database rows that the selection form rejects.
        self.private_a.allowed_viewers.add(self.foreign_viewer, self.invalid_viewer)
        foreign = self.client_for(self.foreign_viewer)
        self.assert_playback(foreign, self.foreign_viewer, self.private_a, False)
        invalid = self.client_for(self.invalid_viewer)
        self.assertEqual(self.catalog(invalid, self.invalid_viewer).status_code, 403)
        for channel in [self.public_a, self.private_a]:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.media(invalid, self.invalid_viewer, channel, kind).status_code, 403)

    def test_selected_viewer_keeps_explicit_membership_after_same_server_unit_move(self):
        client = self.client_for(self.viewer_b)
        self.assert_playback(client, self.viewer_b, self.private_a, True)
        for unit in [self.unit_a, None]:
            User.objects.filter(pk=self.viewer_b.pk).update(unit=unit)
            self.assert_playback(client, self.viewer_b, self.private_a, True)
        # A change of SMA, unlike a change of unit, makes the old membership insufficient.
        User.objects.filter(pk=self.viewer_b.pk).update(environment=self.other_env, unit=self.other_unit)
        self.viewer_b.refresh_from_db()
        self.assert_playback(client, self.viewer_b, self.private_a, False)

    def test_disabled_channel_and_server_deny_even_selected_readers_and_owner(self):
        viewer = self.client_for(self.viewer_b)
        Channel.objects.filter(pk=self.private_a.pk).update(active=False)
        for client, user in [(viewer, self.viewer_b), (self.owner_client, self.owner)]:
            self.assert_playback(client, user, self.private_a, False)
        Environment.objects.filter(pk=self.env.pk).update(active=False)
        self.assertEqual(self.catalog(viewer, self.viewer_b).status_code, 403)
        for client, user in [(viewer, self.viewer_b), (self.owner_client, self.owner)]:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.media(client, user, self.public_a, kind).status_code, 403)
        before = self.snapshot(self.public_a)
        response = self.set_viewers(self.public_a, [self.viewer_a])
        self.assertIn(response.status_code, [302, 403, 404])
        self.assertEqual(self.snapshot(self.public_a), before)

    def test_selection_does_not_bypass_password_change_domain_or_internal_header(self):
        client = self.client_for(self.viewer_b)
        for host in [OWNER_HOST, 'sma2.example.test']:
            response = self.request(client, self.viewer_b, 'get', '/streams.json', HTTP_HOST=host)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.media(client, self.viewer_b, self.private_a, 'rtc', HTTP_HOST=host).status_code, 403)
        for kind in ['manifest', 'segment', 'rtc']:
            self.assertEqual(self.media(client, self.viewer_b, self.private_a, kind,
                HTTP_X_SMA_INTERNAL='').status_code, 403)
        User.objects.filter(pk=self.viewer_b.pk).update(must_change_password=True)
        self.assertEqual(self.catalog(client, self.viewer_b).status_code, 403)
        for kind in ['manifest', 'segment', 'rtc']:
            self.assertEqual(self.media(client, self.viewer_b, self.private_a, kind).status_code, 403)

    def test_audience_update_rolls_back_flag_membership_and_audit_together(self):
        before = self.snapshot(self.public_a)
        count = AuditEvent.objects.count()
        with patch('portal.views.audit', side_effect=RuntimeError('Simulated audience audit failure')):
            with self.assertRaises(RuntimeError):
                self.set_viewers(self.public_a, [self.viewer_a])
        self.assertEqual(self.snapshot(self.public_a), before)
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_post_rechecks_owner_privilege_in_database_before_mutating_audience(self):
        before = self.snapshot(self.public_a)
        count = AuditEvent.objects.count()
        original_lock = Channel.objects.select_for_update

        def demote_after_request_authentication(*args, **kwargs):
            # Deterministic stale-request injection, not a PostgreSQL concurrency test.
            # The decorator saw an owner; the transactional authorization must use fresh data.
            User.objects.filter(pk=self.owner.pk).update(
                role=User.Role.VIEWER, environment=self.env, unit=self.unit_a)
            return original_lock(*args, **kwargs)

        with patch('portal.views.Channel.objects.select_for_update',
                   side_effect=demote_after_request_authentication):
            response = self.set_viewers(self.public_a, [self.viewer_a])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(self.public_a), before)
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_privacy_changes_do_not_interrupt_authenticated_rtmp_publication(self):
        with patch('portal.ingest.socket.gethostbyname', return_value='172.19.0.5'):
            for users in [[self.viewer_b], []]:
                self.assertEqual(self.set_viewers(self.public_a, users).status_code, 302)
                response = self.client.post('/internal/rtmp/publish/', {
                    'name': self.key_a, 'app': 'live', 'clientid': '123', 'addr': '192.0.2.51', 'call': 'publish',
                }, HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
                self.assertEqual(response.status_code, 302)
                self.public_a.refresh_from_db()
                self.assertEqual(self.public_a.reveal_key(), self.key_a)
                self.assertEqual(self.public_a.publisher_client_id, '123')

    def test_monitoring_payloads_never_include_recipients_or_publishing_credentials(self):
        for user in [self.owner, self.admin_a, self.viewer_b]:
            response = self.catalog(self.client_for(user), user)
            self.assertEqual(response.status_code, 200)
            for channel in self.channels:
                for secret in [channel.key_digest, channel.key_ciphertext, channel.reveal_key()]:
                    self.assertNotContains(response, secret)
            for field in ['allowed_viewers', 'audience_restricted', 'key_digest', 'key_ciphertext']:
                self.assertNotContains(response, field)

    def test_default_public_and_flag_are_not_inferred_from_stale_membership_rows(self):
        new = Channel(environment=self.env, unit=self.unit_a, name='Público por padrão')
        new.issue_key()
        new.save()
        new.refresh_from_db()
        self.assertFalse(new.audience_restricted)
        self.assertFalse(new.allowed_viewers.exists())
        self.public_a.allowed_viewers.add(self.viewer_a)
        self.assert_playback(self.client_for(self.viewer_b), self.viewer_b, self.public_a, True)
        self.assert_playback(self.client_for(self.viewer_b), self.viewer_b, self.empty_private, False)

    def test_thirty_five_selected_accounts_can_be_saved_without_form_field_overflow(self):
        users = [self.make_user(f'audience_many_{i}', User.Role.VIEWER, self.env,
                               self.unit_a if i % 2 else self.unit_b) for i in range(35)]
        response = self.set_viewers(self.public_a, users)
        self.assertEqual(response.status_code, 302)
        self.public_a.refresh_from_db()
        self.assertTrue(self.public_a.audience_restricted)
        self.assertEqual(set(self.public_a.allowed_viewers.values_list('pk', flat=True)), {u.pk for u in users})

"""Unit boundaries hold for management; public playback spans its own SMA."""
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import AuditEvent, Channel, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOSTS = {f'sma{i}.example.test': f'sma{i}' for i in range(1, 6)}
PASSWORD = 'Unit-Only-Test-Password-836!'


@override_settings(
    SMA_REALTIME_ENABLED=True,
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS=TENANT_HOSTS,
    SMA_RTMP_URLS={slug: f'rtmp://{host}:19350/live' for host, slug in TENANT_HOSTS.items()},
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, *TENANT_HOSTS],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class UnitIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.env = Environment.objects.create(name='SMA 1', slug='sma1')
        cls.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        cls.unit_a = Unit.objects.create(environment=cls.env, name='Equipe Norte', slug='norte')
        cls.unit_b = Unit.objects.create(environment=cls.env, name='Equipe Leste', slug='leste')
        cls.other_unit = Unit.objects.create(environment=cls.other_env, name='Equipe Sul', slug='sul')
        cls.owner = cls.make_user('unit_owner', User.Role.OWNER, None, None)
        cls.admin_a = cls.make_user('unit_admin_a', User.Role.ADMIN, cls.env, cls.unit_a)
        cls.admin_b = cls.make_user('unit_admin_b', User.Role.ADMIN, cls.env, cls.unit_b)
        cls.admin_null = cls.make_user('unit_admin_legacy', User.Role.ADMIN, cls.env, None)
        cls.admin_other = cls.make_user('unit_admin_other', User.Role.ADMIN, cls.other_env, cls.other_unit)
        cls.viewer_a = cls.make_user('unit_viewer_a', User.Role.VIEWER, cls.env, cls.unit_a)
        cls.viewer_b = cls.make_user('unit_viewer_b', User.Role.VIEWER, cls.env, cls.unit_b)
        cls.viewer_null = cls.make_user('unit_viewer_legacy', User.Role.VIEWER, cls.env, None)
        cls.viewer_other = cls.make_user('unit_viewer_other', User.Role.VIEWER, cls.other_env, cls.other_unit)
        cls.channel_a, cls.key_a = cls.make_channel(cls.env, cls.unit_a, 'Canal Alfa')
        cls.channel_b, cls.key_b = cls.make_channel(cls.env, cls.unit_b, 'Canal Bravo')
        cls.channel_null, cls.key_null = cls.make_channel(cls.env, None, 'Canal legado')
        cls.channel_other, cls.key_other = cls.make_channel(cls.other_env, cls.other_unit, 'Canal externo')
        cls.event_a = AuditEvent.objects.create(environment=cls.env, unit=cls.unit_a, actor=cls.admin_a,
            action='Teste unidade', target='Evento reservado Alfa')
        cls.event_b = AuditEvent.objects.create(environment=cls.env, unit=cls.unit_b, actor=cls.admin_b,
            action='Teste unidade', target='Evento reservado Bravo')
        cls.event_null = AuditEvent.objects.create(environment=cls.env, unit=None, actor=cls.admin_null,
            action='Teste unidade', target='Evento legado')
        cls.event_owner_a = AuditEvent.objects.create(environment=cls.env, unit=cls.unit_a, actor=cls.owner,
            action='Ação do proprietário', target='Evento do proprietário na Alfa')

    @staticmethod
    def make_user(username, role, environment, unit):
        return User.objects.create_user(username=username, password=PASSWORD, role=role,
            environment=environment, unit=unit, must_change_password=False)

    @staticmethod
    def make_channel(environment, unit, name):
        channel = Channel(environment=environment, unit=unit, name=name)
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
        for channel in self.all_channels:
            Channel.objects.filter(pk=channel.pk).update(last_seen=timezone.now(), publisher_client_id='11')
            (self.hls_dir / f'{channel.stream_id}.m3u8').write_text('#EXTM3U\n')

    @property
    def all_channels(self):
        return [self.channel_a, self.channel_b, self.channel_null, self.channel_other]

    def session_for(self, user, environment=None):
        client = Client()
        client.force_login(user)
        if user.is_owner and environment is not None:
            session = client.session
            session['environment_id'] = str(environment.pk)
            session.save()
        return client

    def request(self, client, user, method, path, data=None, **headers):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        return getattr(client, method)(path, data=data or {}, HTTP_HOST=host, secure=True, **headers)

    def media_request(self, client, user, channel, kind):
        headers = {'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}
        if kind == 'rtc':
            path = f'/internal/realtime/view/{channel.stream_id}/'
        else:
            path = '/internal/hls/'
            suffix = '.m3u8' if kind == 'manifest' else '-123456789.ts'
            headers['HTTP_X_ORIGINAL_URI'] = f'/hls/{channel.stream_id}{suffix}'
        return self.request(client, user, 'get', path, **headers)

    def test_admin_and_viewer_public_catalogs_span_same_server_including_legacy_null(self):
        for user in [self.admin_a, self.viewer_a, self.admin_b, self.viewer_b,
                     self.admin_null, self.viewer_null]:
            with self.subTest(user=user.username):
                client = self.session_for(user)
                response = self.request(client, user, 'get', '/streams.json',
                    {'unit': str(self.unit_b.pk), 'unit_id': str(self.unit_b.pk)})
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual([row['id'] for row in response.json()],
                                      [self.channel_a.stream_id, self.channel_b.stream_id, self.channel_null.stream_id])
                for secret in [self.key_a, self.key_b, self.key_null, self.key_other]:
                    self.assertNotContains(response, secret)

    def test_public_media_crosses_units_but_never_server_boundary_with_known_stream_id(self):
        for user in [self.viewer_a, self.admin_b, self.viewer_null]:
            client = self.session_for(user)
            for channel in self.all_channels:
                for kind in ['manifest', 'segment', 'rtc']:
                    with self.subTest(user=user.username, channel=channel.name, kind=kind):
                        expected = (200 if kind == 'rtc' else 204) if channel.environment_id == user.environment_id else 403
                        self.assertEqual(self.media_request(client, user, channel, kind).status_code, expected)

    def test_owner_reads_all_units_and_legacy_channels_without_management_selection(self):
        client = self.session_for(self.owner)
        rows = self.request(client, self.owner, 'get', '/streams.json').json()
        self.assertCountEqual([row['id'] for row in rows], [c.stream_id for c in self.all_channels])
        for channel in self.all_channels:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.media_request(client, self.owner, channel, kind).status_code,
                                 200 if kind == 'rtc' else 204)
        rows = self.request(client, self.owner, 'get', '/streams.json', {'sma': str(self.env.pk)}).json()
        self.assertCountEqual([row['id'] for row in rows],
                             [self.channel_a.stream_id, self.channel_b.stream_id, self.channel_null.stream_id])

    def test_dashboard_and_channel_api_hide_other_units_including_audit_events(self):
        cases = [(self.admin_a, self.channel_a, [self.admin_a, self.viewer_a], [self.event_a, self.event_owner_a]),
                 (self.admin_b, self.channel_b, [self.admin_b, self.viewer_b], [self.event_b]),
                 (self.admin_null, self.channel_null, [self.admin_null, self.viewer_null], [self.event_null])]
        for user, channel, accounts, events in cases:
            with self.subTest(user=user.username):
                client = self.session_for(user)
                page = self.request(client, user, 'get', '/painel/')
                self.assertEqual(page.status_code, 200)
                self.assertCountEqual([a.pk for a in page.context['accounts']], [a.pk for a in accounts])
                self.assertEqual([c.pk for c in page.context['channels']], [channel.pk])
                self.assertCountEqual([e.pk for e in page.context['events']], [e.pk for e in events])
                api = self.request(client, user, 'get', '/api/channels/')
                self.assertEqual([row['id'] for row in api.json()], [str(channel.pk)])

    def test_owner_dashboard_still_lists_every_unit_of_selected_environment(self):
        client = self.session_for(self.owner, self.env)
        page = self.request(client, self.owner, 'get', '/painel/')
        self.assertEqual(page.status_code, 200)
        self.assertCountEqual([c.pk for c in page.context['channels']],
                             [self.channel_a.pk, self.channel_b.pk, self.channel_null.pk])
        self.assertCountEqual([a.pk for a in page.context['accounts']],
                             [self.admin_a.pk, self.admin_b.pk, self.admin_null.pk,
                              self.viewer_a.pk, self.viewer_b.pk, self.viewer_null.pk])
        self.assertCountEqual([e.pk for e in page.context['events']],
                             [self.event_a.pk, self.event_b.pk, self.event_null.pk, self.event_owner_a.pk])

    def test_all_foreign_unit_channel_actions_return_404_without_mutation_or_secret(self):
        for actor, foreign in [(self.admin_a, [self.channel_b, self.channel_null, self.channel_other]),
                               (self.admin_null, [self.channel_a, self.channel_b])]:
            client = self.session_for(actor)
            for channel in foreign:
                snapshot = Channel.objects.filter(pk=channel.pk).values().get()
                audit_count = AuditEvent.objects.count()
                actions = [('get', 'editar', {}), ('post', 'editar', {'name': 'Renomeado sem permissão'}),
                           ('get', 'credenciais', {}), ('get', 'excluir', {}),
                           ('post', 'excluir', {'confirmation': str(channel.pk)}),
                           ('post', 'chave', {}), ('post', 'estado', {'active': '0'})]
                for method, action, data in actions:
                    with self.subTest(actor=actor.username, channel=channel.name, action=action, method=method):
                        response = self.request(client, actor, method, f'/canais/{channel.pk}/{action}/', data)
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(Channel.objects.filter(pk=channel.pk).values().get(), snapshot)
                        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_other_unit_accounts_cannot_be_reset_or_deactivated(self):
        for actor, foreign in [(self.admin_a, [self.viewer_b, self.viewer_null, self.viewer_other]),
                               (self.admin_null, [self.viewer_a, self.viewer_b])]:
            client = self.session_for(actor)
            for target in foreign:
                before = User.objects.filter(pk=target.pk).values().get()
                actions = [('get', 'senha', {}), ('post', 'senha',
                    {'new_password1': PASSWORD + 'new', 'new_password2': PASSWORD + 'new'}),
                    ('post', 'estado', {'active': '0'})]
                for method, action, data in actions:
                    with self.subTest(actor=actor.username, target=target.username, action=action, method=method):
                        response = self.request(client, actor, method, f'/usuarios/{target.pk}/{action}/', data)
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(User.objects.filter(pk=target.pk).values().get(), before)

    def test_created_viewers_and_channels_inherit_actor_unit_despite_forged_fields(self):
        for actor in [self.admin_a, self.admin_null]:
            with self.subTest(actor=actor.username):
                client = self.session_for(actor)
                username = actor.username + '_created'
                forged = {'unit': str(self.unit_b.pk), 'unit_id': str(self.unit_b.pk),
                          'environment': str(self.other_env.pk), 'is_superuser': '1', 'is_staff': '1'}
                payload = {**forged, 'username': username, 'first_name': 'Acesso de teste', 'role': 'viewer',
                           'password1': PASSWORD, 'password2': PASSWORD}
                self.assertEqual(self.request(client, actor, 'post', '/usuarios/novo/', payload).status_code, 302)
                user = User.objects.get(username=username)
                self.assertEqual((user.environment_id, user.unit_id), (actor.environment_id, actor.unit_id))
                self.assertFalse(user.is_superuser or user.is_staff)
                self.assertEqual(user.role, User.Role.VIEWER)
                self.assertTrue(user.must_change_password)
                name = 'Canal criado por ' + actor.username
                response = self.request(client, actor, 'post', '/canais/novo/', {**forged, 'name': name})
                self.assertEqual(response.status_code, 200)
                channel = Channel.objects.get(name=name)
                self.assertEqual((channel.environment_id, channel.unit_id), (actor.environment_id, actor.unit_id))
                self.assertEqual(AuditEvent.objects.first().unit_id, actor.unit_id)

    def test_unit_admin_cannot_create_owner_or_admin_role(self):
        client = self.session_for(self.admin_a)
        for role in [User.Role.OWNER, User.Role.ADMIN]:
            response = self.request(client, self.admin_a, 'post', '/usuarios/novo/',
                {'username': 'forged_role_' + role, 'role': role, 'unit': str(self.unit_b.pk),
                 'password1': PASSWORD, 'password2': PASSWORD})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(User.objects.filter(username='forged_role_' + role).exists())

    def test_same_channel_name_is_allowed_between_units_and_legacy_but_not_within_one(self):
        for actor in [self.admin_a, self.admin_b, self.admin_null]:
            client = self.session_for(actor)
            self.assertEqual(self.request(client, actor, 'post', '/canais/novo/', {'name': 'Drone comum'}).status_code, 200)
            duplicate = self.request(client, actor, 'post', '/canais/novo/', {'name': '  DRONE COMUM  '})
            self.assertEqual(duplicate.status_code, 400)
            self.assertEqual(Channel.objects.filter(environment=self.env, unit=actor.unit,
                                                    name__iexact='Drone comum').count(), 1)

    def test_owner_actions_record_target_unit_instead_of_owner_null_unit(self):
        client = self.session_for(self.owner, self.env)
        channel = self.channel_b
        actions = [('get', f'/canais/{channel.pk}/credenciais/', {}),
                   ('post', f'/canais/{channel.pk}/editar/', {'name': 'Bravo renomeado'}),
                   ('post', f'/canais/{channel.pk}/chave/', {}),
                   ('post', f'/canais/{channel.pk}/estado/', {'active': '0'}),
                   ('post', f'/usuarios/{self.viewer_b.pk}/senha/',
                    {'new_password1': PASSWORD + 'new', 'new_password2': PASSWORD + 'new'}),
                   ('post', f'/usuarios/{self.viewer_b.pk}/estado/', {'active': '0'}),
                   ('post', f'/canais/{channel.pk}/excluir/', {'confirmation': str(channel.pk)})]
        for method, path, payload in actions:
            with self.subTest(path=path):
                count = AuditEvent.objects.count()
                response = self.request(client, self.owner, method, path, payload)
                self.assertIn(response.status_code, [200, 302])
                self.assertEqual(AuditEvent.objects.count(), count + 1)
                event = AuditEvent.objects.first()
                self.assertEqual((event.actor_id, event.environment_id, event.unit_id),
                                 (self.owner.pk, self.env.pk, self.unit_b.pk))

    def test_last_admin_guard_is_per_unit_including_null_bucket(self):
        client = self.session_for(self.owner, self.env)
        for target in [self.admin_a, self.admin_b, self.admin_null]:
            with self.subTest(target=target.username):
                response = self.request(client, self.owner, 'post', f'/usuarios/{target.pk}/estado/', {'active': '0'})
                self.assertEqual(response.status_code, 400)
                target.refresh_from_db()
                self.assertTrue(target.is_active)
        self.make_user('unit_admin_a_backup', User.Role.ADMIN, self.env, self.unit_a)
        response = self.request(client, self.owner, 'post', f'/usuarios/{self.admin_a.pk}/estado/', {'active': '0'})
        self.assertEqual(response.status_code, 302)
        self.admin_a.refresh_from_db()
        self.assertFalse(self.admin_a.is_active)

    def test_unit_reassignment_keeps_public_playback_in_same_server(self):
        client = self.session_for(self.viewer_a)
        self.assertEqual(self.media_request(client, self.viewer_a, self.channel_a, 'rtc').status_code, 200)
        User.objects.filter(pk=self.viewer_a.pk).update(unit=self.unit_b)
        self.assertEqual(self.media_request(client, self.viewer_a, self.channel_a, 'rtc').status_code, 200)
        self.assertEqual(self.media_request(client, self.viewer_a, self.channel_a, 'segment').status_code, 204)
        self.assertEqual(self.media_request(client, self.viewer_a, self.channel_b, 'rtc').status_code, 200)
        rows = self.request(client, self.viewer_a, 'get', '/streams.json').json()
        self.assertCountEqual([row['id'] for row in rows],
                             [self.channel_a.stream_id, self.channel_b.stream_id, self.channel_null.stream_id])

    def test_disabled_environment_denies_unit_readers_and_owner_media(self):
        self.env.active = False
        self.env.save(update_fields=['active'])
        for user in [self.admin_a, self.viewer_b, self.admin_null]:
            client = self.session_for(user)
            self.assertEqual(self.request(client, user, 'get', '/streams.json').status_code, 403)
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.media_request(client, user, self.channel_a, kind).status_code, 403)
        client = self.session_for(self.owner)
        rows = self.request(client, self.owner, 'get', '/streams.json').json()
        self.assertEqual([row['id'] for row in rows], [self.channel_other.stream_id])
        for kind in ['manifest', 'segment', 'rtc']:
            self.assertEqual(self.media_request(client, self.owner, self.channel_a, kind).status_code, 403)
        response = Client().post('/internal/rtmp/publish/', self.publish_payload(self.key_a),
            HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
        self.assertEqual(response.status_code, 403)

    @staticmethod
    def publish_payload(key):
        return {'name': key, 'app': 'live', 'call': 'publish', 'clientid': '123', 'addr': '192.0.2.61'}

    def test_same_ingest_endpoint_routes_each_unit_and_legacy_key_to_its_own_channel(self):
        client = Client()
        for channel, key in [(self.channel_a, self.key_a), (self.channel_b, self.key_b),
                             (self.channel_null, self.key_null), (self.channel_other, self.key_other)]:
            with self.subTest(channel=channel.name):
                response = client.post('/internal/rtmp/publish/', self.publish_payload(key),
                    HTTP_HOST='backend', HTTP_X_SMA_INTERNAL=settings.INTERNAL_TOKEN)
                self.assertEqual(response.status_code, 302)
                # Fixtures already have a live primary publisher. A new device
                # must receive another input of this same channel, not replace it.
                stream_id = urlsplit(response['Location']).path.rsplit('/', 1)[-1]
                self.assertTrue(channel.inputs.filter(stream_id=stream_id, publisher_client_id='123').exists())
                saved = Channel.objects.get(pk=channel.pk)
                self.assertEqual(saved.publisher_client_id, '11')
                self.assertEqual((saved.environment_id, saved.unit_id), (channel.environment_id, channel.unit_id))
                self.assertEqual(saved.reveal_key(), key)

    def test_sma3_to_sma5_legacy_null_accounts_channels_and_keys_keep_working(self):
        for index in [3, 4, 5]:
            env = Environment.objects.create(name=f'SMA {index}', slug=f'sma{index}')
            admin = self.make_user(f'unit_legacy_admin_{index}', User.Role.ADMIN, env, None)
            channel, key = self.make_channel(env, None, 'Canal existente')
            client = self.session_for(admin)
            self.assertEqual(self.request(client, admin, 'get', '/painel/').status_code, 200)
            response = self.request(client, admin, 'get', f'/canais/{channel.pk}/credenciais/')
            self.assertContains(response, key)
            self.assertContains(response, f'rtmp://sma{index}.example.test:19350/live')
            self.assertEqual(self.media_request(client, admin, channel, 'rtc').status_code, 200)
            self.assertEqual(self.media_request(client, admin, self.channel_a, 'manifest').status_code, 403)
            channel.refresh_from_db()
            self.assertIsNone(channel.unit_id)
            self.assertEqual(channel.reveal_key(), key)

    def test_models_reject_cross_environment_unit_assignments_and_owner_unit(self):
        invalid_user = User(username='invalid_unit_user', role=User.Role.VIEWER,
                            environment=self.env, unit=self.other_unit)
        invalid_user.set_password(PASSWORD)
        invalid_channel = Channel(environment=self.env, unit=self.other_unit, name='Inválido')
        invalid_channel.issue_key()
        invalid_event = AuditEvent(environment=self.env, unit=self.other_unit, actor=self.owner,
                                   action='Inválido', target='Teste')
        invalid_owner = User(username='invalid_unit_owner', role=User.Role.OWNER, unit=self.unit_a)
        invalid_owner.set_password(PASSWORD)
        for instance in [invalid_user, invalid_channel, invalid_event, invalid_owner]:
            with self.subTest(model=type(instance).__name__):
                with self.assertRaises(ValidationError):
                    instance.full_clean()

    def test_full_clean_preserves_channel_name_uniqueness_for_unit_and_null(self):
        for original in [self.channel_a, self.channel_null]:
            duplicate = Channel(environment=original.environment, unit=original.unit, name=original.name.upper())
            duplicate.issue_key()
            with self.assertRaises(ValidationError):
                duplicate.full_clean()
        other_scope = Channel(environment=self.env, unit=self.unit_b, name=self.channel_a.name)
        other_scope.issue_key()
        other_scope.full_clean()

    def test_unit_slugs_are_unique_per_environment_and_referenced_units_are_protected(self):
        duplicate = Unit(environment=self.env, name='Outro nome', slug=self.unit_a.slug)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        elsewhere = Unit(environment=self.other_env, name='Nome no outro SMA', slug=self.unit_a.slug)
        elsewhere.full_clean()
        with self.assertRaises(ProtectedError):
            self.unit_a.delete()

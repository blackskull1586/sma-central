"""Publishers share one credential without replacing each other's video."""
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.cache import caches
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import Channel, ChannelInput, Environment, Unit, User


OWNER_HOST = 'central.example.test'
TENANT_HOST = 'sma1.example.test'


@override_settings(
    SMA_REALTIME_ENABLED=True,
    SMA_OWNER_HOST=OWNER_HOST,
    SMA_TENANT_HOSTS={TENANT_HOST: 'sma1', 'sma2.example.test': 'sma2'},
    ALLOWED_HOSTS=['testserver', 'backend', OWNER_HOST, TENANT_HOST, 'sma2.example.test'],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    HLS_FRESH_SECONDS=120,
)
class MultipublisherTests(TestCase):
    def setUp(self):
        caches['rtmp_auth'].clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hls_dir = Path(directory.name)
        override = override_settings(HLS_DIR=self.hls_dir)
        override.enable()
        self.addCleanup(override.disable)
        resolver = patch('portal.ingest.socket.gethostbyname', return_value='172.19.0.5')
        resolver.start()
        self.addCleanup(resolver.stop)
        self.env = Environment.objects.create(name='SMA 1', slug='sma1')
        self.other_env = Environment.objects.create(name='SMA 2', slug='sma2')
        self.unit_a = Unit.objects.create(environment=self.env, name='Alfa', slug='alfa')
        self.unit_b = Unit.objects.create(environment=self.env, name='Bravo', slug='bravo')
        self.owner = self.user('multi_owner', User.Role.OWNER)
        self.admin = self.user('multi_admin', User.Role.ADMIN, self.env, self.unit_a)
        self.viewer = self.user('multi_viewer', User.Role.VIEWER, self.env, self.unit_a)
        self.other_unit_viewer = self.user('multi_other_unit', User.Role.VIEWER, self.env, self.unit_b)
        self.foreign_viewer = self.user('multi_foreign', User.Role.VIEWER, self.other_env)
        self.channel = Channel(environment=self.env, unit=self.unit_a, name='Equipe de campo')
        self.key = self.channel.issue_key()
        self.channel.save()
        self.internal = {'HTTP_HOST': 'backend', 'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}

    @staticmethod
    def user(username, role, environment=None, unit=None):
        return User.objects.create_user(username=username, role=role, environment=environment,
            unit=unit, must_change_password=False)

    def browser(self, user):
        browser = Client()
        browser.force_login(user)
        if user.is_owner:
            session = browser.session
            session['environment_id'] = str(self.env.pk)
            session.save()
        return browser

    def callback(self, clientid='101', action='publish', key=None, **extra):
        key = self.key if key is None else key
        if '?token=' in key:
            name, token = key.split('?token=')
            data = {'name': name, 'token': token}
        else:
            data = {'name': key}
        data.update({'app': 'live', 'clientid': clientid, 'addr': '192.0.2.75',
            'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action]})
        data.update(extra)
        return self.client.post(f'/internal/rtmp/{action}/', data, **self.internal)

    def start(self, clientid='101', key=None):
        response = self.callback(clientid, key=key)
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response['Location'])
        return {'id': location.path.rsplit('/', 1)[-1],
                'token': parse_qs(location.query)['token'][0], 'clientid': clientid,
                'location': response['Location']}

    def start_three(self):
        return [self.start(str(clientid)) for clientid in [101, 102, 103]]

    def start_many(self, count):
        return [self.start(str(clientid)) for clientid in range(101, 101 + count)]

    def relay(self, stream, action='publish', **extra):
        data = {'name': stream['id'], 'token': stream['token'], 'app': 'live', 'clientid': '888',
            'call': {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}[action]}
        data.update(extra)
        return self.client.post(f'/internal/relay/{action}/', data, **self.internal)

    def srs(self, stream, **extra):
        data = {'name': stream['id'], 'token': stream['token']}
        data.update(extra)
        return self.client.post('/internal/realtime/publish/', data, **self.internal)

    def make_online(self, streams):
        for stream in streams:
            (self.hls_dir / (stream['id'] + '.m3u8')).write_text('#EXTM3U\n')

    def catalog(self, user, browser=None, **query):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        return (browser or self.browser(user)).get('/streams.json', query, HTTP_HOST=host, secure=True)

    def playback(self, user, stream, kind='rtc', browser=None):
        host = OWNER_HOST if user.is_owner else f'{user.environment.slug}.example.test'
        headers = {'HTTP_HOST': host, 'HTTP_X_SMA_INTERNAL': settings.INTERNAL_TOKEN}
        if kind == 'rtc':
            path = f"/internal/realtime/view/{stream['id']}/"
        else:
            path = '/internal/hls/'
            suffix = '.m3u8' if kind == 'manifest' else '-123456789.ts'
            headers['HTTP_X_ORIGINAL_URI'] = f"/hls/{stream['id']}{suffix}"
        return (browser or self.browser(user)).get(path, secure=True, **headers)

    def channel_action(self, action, data=None):
        return self.browser(self.admin).post(f'/canais/{self.channel.pk}/{action}/', data or {},
            HTTP_HOST=TENANT_HOST, secure=True)

    def assert_relays_live(self, streams):
        for stream in streams:
            with self.subTest(stream=stream['id']):
                self.assertEqual(self.relay(stream).status_code, 302)
                self.assertEqual(self.srs(stream).status_code, 204)

    def test_three_identical_keys_allocate_three_independent_streams_without_rotating_credentials(self):
        original = (self.channel.key_digest, self.channel.key_ciphertext, self.channel.stream_id)
        streams = self.start_three()
        self.assertEqual(len({stream['id'] for stream in streams}), 3)
        self.assertEqual(len({stream['token'] for stream in streams}), 3)
        self.channel.refresh_from_db()
        self.assertEqual(streams[0]['id'], self.channel.stream_id)
        self.assertEqual((self.channel.key_digest, self.channel.key_ciphertext, self.channel.stream_id), original)
        self.assertEqual(self.channel.reveal_key(), self.key)
        self.assertEqual(self.channel.publisher_client_id, '101')
        self.assertCountEqual(self.channel.inputs.values_list('slot', flat=True), [2, 3])
        self.assert_relays_live(streams)

    def test_fourth_publisher_is_accepted_without_replacing_any_of_the_three(self):
        streams = self.start_three()
        before = list(ChannelInput.objects.values())
        before_primary = Channel.objects.filter(pk=self.channel.pk).values().get()
        fourth = self.start('104')
        self.assertNotIn(fourth['id'], [stream['id'] for stream in streams])
        self.assertEqual(list(ChannelInput.objects.filter(pk__in=[row['id'] for row in before]).values()), before)
        self.assertEqual(Channel.objects.filter(pk=self.channel.pk).values().get(), before_primary)
        for stream in [*streams, fourth]:
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 204)
        self.assert_relays_live([*streams, fourth])

    def test_twenty_five_publishers_share_exact_name_and_parent_audience_without_a_key_cap(self):
        streams = self.start_many(25)
        self.make_online(streams)
        expected_ids = {stream['id'] for stream in streams}
        self.assertEqual(len(expected_ids), 25)
        self.assertEqual(len({stream['token'] for stream in streams}), 25)
        self.assertEqual(self.channel.inputs.count(), 24)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.reveal_key(), self.key)
        for user in [self.owner, self.viewer, self.other_unit_viewer]:
            rows = self.catalog(user).json()
            self.assertEqual({row['id'] for row in rows}, expected_ids)
            self.assertEqual({row['channel_id'] for row in rows}, {str(self.channel.pk)})
            self.assertEqual({row['label'] for row in rows}, {self.channel.name})
        response = self.browser(self.owner).post(f'/canais/{self.channel.pk}/visualizadores/',
            {'allowed_viewers': [str(self.viewer.pk)]}, HTTP_HOST=OWNER_HOST, secure=True)
        self.assertEqual(response.status_code, 302)
        allowed = self.browser(self.viewer)
        denied = self.browser(self.other_unit_viewer)
        self.assertEqual({row['id'] for row in self.catalog(self.viewer, allowed).json()}, expected_ids)
        self.assertEqual({row['id'] for row in self.catalog(self.owner).json()}, expected_ids)
        self.assertEqual(self.catalog(self.other_unit_viewer, denied).json(), [])
        self.assertEqual(self.catalog(self.foreign_viewer).json(), [])
        for stream in streams:
            with self.subTest(source=stream['id']):
                for kind in ['manifest', 'segment', 'rtc']:
                    self.assertEqual(self.playback(self.viewer, stream, kind, allowed).status_code,
                                     200 if kind == 'rtc' else 204)
                    self.assertEqual(self.playback(self.other_unit_viewer, stream, kind, denied).status_code, 403)
        self.assert_relays_live(streams)

    def test_later_input_can_stop_reconnect_and_grow_without_displacing_twelve_publishers(self):
        streams = self.start_many(12)
        stopped = streams[9]
        survivors = streams[:9] + streams[10:]
        self.assertEqual(self.callback(stopped['clientid'], 'done').status_code, 204)
        before_primary = Channel.objects.filter(pk=self.channel.pk).values().get()
        before_survivors = list(self.channel.inputs.exclude(stream_id=stopped['id']).values())
        replacement = self.start('500')
        self.assertEqual(replacement['id'], stopped['id'])
        self.assertNotEqual(replacement['token'], stopped['token'])
        self.assertEqual(self.channel.inputs.count(), 11)
        self.assertEqual(Channel.objects.filter(pk=self.channel.pk).values().get(), before_primary)
        self.assertEqual(list(self.channel.inputs.exclude(stream_id=stopped['id']).values()), before_survivors)
        self.assertEqual(self.relay(stopped).status_code, 403)
        self.assertEqual(self.srs(stopped).status_code, 403)
        for action in ['update', 'done']:
            self.assertEqual(self.callback(stopped['clientid'], action).status_code, 403)
        repeated = self.callback(replacement['clientid'])
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(repeated['Location'], replacement['location'])
        self.assertEqual(self.channel.inputs.count(), 11)
        thirteenth = self.start('501')
        self.assertNotIn(thirteenth['id'], {stream['id'] for stream in streams})
        self.assertEqual(self.channel.inputs.count(), 12)
        self.assert_relays_live([*survivors, replacement, thirteenth])

    def test_repeated_publish_of_same_live_client_is_idempotent(self):
        streams = self.start_three()
        for stream in streams:
            response = self.callback(stream['clientid'])
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response['Location'], stream['location'])
        self.assertEqual(self.channel.inputs.count(), 2)
        self.assert_relays_live(streams)

    def test_stopping_middle_source_keeps_others_and_frees_only_its_slot(self):
        streams = self.start_three()
        self.assertEqual(self.callback('102', 'done').status_code, 204)
        self.assertEqual(self.relay(streams[1]).status_code, 403)
        self.assertEqual(self.srs(streams[1]).status_code, 403)
        self.assert_relays_live([streams[0], streams[2]])
        replacement = self.start('104')
        self.assertEqual(replacement['id'], streams[1]['id'])
        self.assertNotEqual(replacement['token'], streams[1]['token'])
        for action in ['update', 'done']:
            self.assertEqual(self.callback('102', action).status_code, 403)
        self.assert_relays_live([streams[0], replacement, streams[2]])

    def test_stopping_primary_does_not_stop_secondary_sources_or_hide_them_from_catalog(self):
        streams = self.start_three()
        self.make_online(streams)
        self.assertEqual(self.callback('101', 'done').status_code, 204)
        self.assertEqual(self.relay(streams[0]).status_code, 403)
        self.assert_relays_live(streams[1:])
        rows = self.catalog(self.viewer).json()
        self.assertCountEqual([row['id'] for row in rows], [stream['id'] for stream in streams[1:]])

    def test_stale_secondary_slot_is_reclaimed_and_old_client_cannot_affect_replacement(self):
        streams = self.start_three()
        ChannelInput.objects.filter(stream_id=streams[1]['id']).update(
            last_seen=timezone.now() - timedelta(seconds=121))
        self.assertEqual(self.callback('102', 'update').status_code, 403)
        self.assertEqual(self.relay(streams[1]).status_code, 403)
        replacement = self.start('104')
        self.assertEqual(replacement['id'], streams[1]['id'])
        self.assertNotEqual(replacement['token'], streams[1]['token'])
        self.assertEqual(self.callback('102', 'done').status_code, 403)
        self.assertEqual(self.callback('102', 'update').status_code, 403)
        self.assert_relays_live([streams[0], replacement, streams[2]])

    def test_expired_secondary_client_reuses_its_slot_when_primary_is_free(self):
        streams = self.start_three()
        self.assertEqual(self.callback('101', 'done').status_code, 204)
        ChannelInput.objects.filter(stream_id=streams[1]['id']).update(
            last_seen=timezone.now() - timedelta(seconds=121))
        self.assertEqual(self.callback('102', 'update').status_code, 403)
        replacement = self.start('102')
        self.assertEqual(replacement['id'], streams[1]['id'])
        self.assertNotEqual(replacement['token'], streams[1]['token'])
        self.assertEqual(Channel.objects.filter(pk=self.channel.pk, publisher_client_id='102').count()
                         + self.channel.inputs.filter(publisher_client_id='102').count(), 1)
        self.assertEqual(self.relay(streams[1]).status_code, 403)
        self.assert_relays_live([replacement, streams[2]])
        self.assertEqual(self.callback('102', 'done').status_code, 204)
        self.assertEqual(self.callback('102', 'update').status_code, 403)
        self.assertEqual(self.callback('102', 'done').status_code, 403)
        self.assert_relays_live([streams[2]])

    def test_stale_primary_is_reclaimed_without_replacing_live_secondary_sources(self):
        streams = self.start_three()
        Channel.objects.filter(pk=self.channel.pk).update(last_seen=timezone.now() - timedelta(seconds=121))
        replacement = self.start('104')
        self.assertEqual(replacement['id'], streams[0]['id'])
        self.assertNotEqual(replacement['token'], streams[0]['token'])
        self.assertEqual(self.callback('101', 'done').status_code, 403)
        self.assertEqual(self.relay(streams[0]).status_code, 403)
        self.assert_relays_live([replacement, *streams[1:]])

    def test_reusing_client_number_after_disconnect_never_reuses_internal_relay_token(self):
        first = self.start('101')
        self.assertEqual(self.callback('101', 'done').status_code, 204)
        second = self.start('101')
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['token'], second['token'])
        self.assertEqual(self.relay(first).status_code, 403)
        self.assertEqual(self.srs(first).status_code, 403)
        self.assert_relays_live([second])

    def test_tokens_cannot_be_swapped_between_inputs_and_public_key_is_not_a_relay_token(self):
        streams = self.start_three()
        for index, stream in enumerate(streams):
            swapped = streams[(index + 1) % len(streams)]['token']
            self.assertEqual(self.relay(stream, token=swapped).status_code, 403)
            self.assertEqual(self.srs(stream, token=swapped).status_code, 403)
            self.assertEqual(self.relay(stream, token=self.key).status_code, 403)
            self.assertEqual(self.srs(stream, token=self.key).status_code, 403)

    def test_rotation_invalidates_all_old_publishers_and_new_key_still_admits_three(self):
        streams = self.start_three()
        response = self.channel_action('chave')
        self.assertEqual(response.status_code, 200)
        new_key = response.context['stream_key']
        self.assertNotEqual(new_key, self.key)
        for stream in streams:
            for action in ['publish', 'update', 'done']:
                self.assertEqual(self.callback(stream['clientid'], action).status_code, 403)
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
        self.key = new_key
        replacement = self.start_three()
        self.assertEqual(len({stream['id'] for stream in replacement}), 3)
        self.assert_relays_live(replacement)

    def test_disabling_then_reenabling_channel_does_not_resurrect_old_publications(self):
        streams = self.start_three()
        self.assertEqual(self.channel_action('estado', {'active': '0'}).status_code, 302)
        for stream in streams:
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
        self.assertEqual(self.channel_action('estado', {'active': '1'}).status_code, 302)
        for stream in streams:
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 403)
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
        self.assert_relays_live(self.start_three())

    def test_disabling_server_denies_all_input_ingest_catalog_and_playback(self):
        streams = self.start_three()
        self.make_online(streams)
        Environment.objects.filter(pk=self.env.pk).update(active=False)
        self.assertEqual(self.catalog(self.owner).json(), [])
        for stream in streams:
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 403)
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.playback(self.owner, stream, kind).status_code, 403)

    def test_deleting_parent_channel_removes_inputs_and_invalidates_all_sources(self):
        streams = self.start_three()
        response = self.channel_action('excluir', {'confirmation': str(self.channel.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ChannelInput.objects.filter(channel_id=self.channel.pk).exists())
        for stream in streams:
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 403)
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
            self.assertEqual(self.playback(self.owner, stream).status_code, 403)

    def test_same_legacy_credential_also_supports_more_than_three_publishers(self):
        self.key = self.channel.issue_key(legacy=True)
        self.channel.save()
        streams = self.start_three()
        self.assertEqual(len({stream['id'] for stream in streams}), 3)
        self.assert_relays_live(streams)
        for stream in streams:
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 204)
        fourth = self.start('104')
        self.assertEqual(len({stream['id'] for stream in [*streams, fourth]}), 4)
        self.assert_relays_live([*streams, fourth])

    def test_catalog_exposes_three_video_ids_linked_to_one_parent_and_no_credentials(self):
        streams = self.start_three()
        self.make_online(streams)
        response = self.catalog(self.owner)
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertCountEqual([row['id'] for row in rows], [stream['id'] for stream in streams])
        self.assertEqual({row['channel_id'] for row in rows}, {str(self.channel.pk)})
        self.assertEqual({row['input_number'] for row in rows}, {1, 2, 3})
        self.assertEqual({row['label'] for row in rows}, {self.channel.name})
        for row in rows:
            self.assertEqual(row['url'], f"/hls/{row['id']}.m3u8")
            self.assertEqual(row['webrtc_url'], f"/rtc/{row['id']}/whep")
            self.assertEqual(row['environment']['id'], str(self.env.pk))
        for secret in [self.key, self.channel.key_digest, self.channel.key_ciphertext,
                       *(stream['token'] for stream in streams)]:
            self.assertNotContains(response, secret)

    def test_only_fresh_individual_manifests_are_listed_in_catalog(self):
        streams = self.start_three()
        self.make_online(streams)
        (self.hls_dir / (streams[1]['id'] + '.m3u8')).unlink()
        rows = self.catalog(self.viewer).json()
        self.assertCountEqual([row['id'] for row in rows], [streams[0]['id'], streams[2]['id']])
        self.make_online([streams[1]])
        ChannelInput.objects.filter(stream_id=streams[2]['id']).update(
            last_seen=timezone.now() - timedelta(seconds=121))
        rows = self.catalog(self.viewer).json()
        self.assertCountEqual([row['id'] for row in rows], [streams[0]['id'], streams[1]['id']])

    def test_default_audience_shares_all_inputs_across_units_but_never_other_servers(self):
        streams = self.start_three()
        self.make_online(streams)
        for user in [self.owner, self.viewer, self.other_unit_viewer]:
            self.assertEqual(len(self.catalog(user).json()), 3)
            for stream in streams:
                for kind in ['manifest', 'segment', 'rtc']:
                    self.assertEqual(self.playback(user, stream, kind).status_code, 200 if kind == 'rtc' else 204)
        self.assertEqual(self.catalog(self.foreign_viewer).json(), [])
        for stream in streams:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.playback(self.foreign_viewer, stream, kind).status_code, 403)

    def test_changing_parent_audience_revokes_all_three_without_interrupting_publishers(self):
        streams = self.start_three()
        self.make_online(streams)
        browser = self.browser(self.viewer)
        self.assertEqual(len(self.catalog(self.viewer, browser).json()), 3)
        response = self.browser(self.owner).post(f'/canais/{self.channel.pk}/visualizadores/',
            {'allowed_viewers': [str(self.other_unit_viewer.pk)]}, HTTP_HOST=OWNER_HOST, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.catalog(self.viewer, browser).json(), [])
        for stream in streams:
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.playback(self.viewer, stream, kind, browser).status_code, 403)
                self.assertEqual(self.playback(self.other_unit_viewer, stream, kind).status_code,
                                 200 if kind == 'rtc' else 204)
                self.assertEqual(self.playback(self.owner, stream, kind).status_code,
                                 200 if kind == 'rtc' else 204)
        self.assert_relays_live(streams)

    def test_shared_viewer_cannot_manage_or_read_credentials_for_parent_channel(self):
        self.start_three()
        browser = self.browser(self.other_unit_viewer)
        for action in ['credenciais', 'editar', 'excluir', 'visualizadores']:
            response = browser.get(f'/canais/{self.channel.pk}/{action}/', HTTP_HOST=TENANT_HOST, secure=True)
            self.assertEqual(response.status_code, 403)

    def test_inputs_with_old_key_snapshot_cannot_publish_relay_or_appear_in_catalog(self):
        streams = self.start_three()
        self.make_online(streams)
        ChannelInput.objects.filter(channel=self.channel).update(key_digest='0' * 64)
        self.assertEqual([row['id'] for row in self.catalog(self.viewer).json()], [streams[0]['id']])
        for stream in streams[1:]:
            self.assertEqual(self.relay(stream).status_code, 403)
            self.assertEqual(self.srs(stream).status_code, 403)
            self.assertEqual(self.callback(stream['clientid'], 'update').status_code, 403)
            for kind in ['manifest', 'segment', 'rtc']:
                self.assertEqual(self.playback(self.viewer, stream, kind).status_code, 403)

    def test_malformed_internal_callbacks_cannot_overwrite_live_sources(self):
        streams = self.start_three()
        before = list(ChannelInput.objects.values())
        for extra in [{'clientid': ['101', '102']}, {'name': [self.key, self.key]},
                      {'app': 'other'}, {'token': 'A' * 43}]:
            self.assertEqual(self.callback(**extra).status_code, 403)
        self.assertEqual(list(ChannelInput.objects.values()), before)
        self.assert_relays_live(streams)

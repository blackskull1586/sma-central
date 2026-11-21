"""Resolve publishing credentials to canonical, non-secret channel identifiers."""
import hashlib
import hmac
import ipaddress
import re
import socket
import uuid
from urllib.parse import urlsplit
from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import Channel, ChannelInput, normalize_short_key, short_key_digest


def internal_allowed(request):
    provided = request.headers.get('X-SMA-Internal', '')
    return bool(provided) and constant_time_compare(provided, settings.INTERNAL_TOKEN)


def source_channel(source):
    return source.channel if isinstance(source, ChannelInput) else source


def current_source_key(source):
    return not isinstance(source, ChannelInput) or constant_time_compare(source.key_digest, source.channel.key_digest)


def relay_token(source):
    channel = source_channel(source)
    message = '\n'.join(['sma-canonical-relay-v2', source.stream_id, channel.key_digest,
                         source.publisher_client_id, source.publisher_generation])
    return hmac.new(settings.INTERNAL_TOKEN.encode(), message.encode(), hashlib.sha256).hexdigest()


def active_publisher(source):
    if source is None:
        return False
    channel = source_channel(source)
    return (channel.active and channel.environment.active and current_source_key(source)
            and bool(source.publisher_client_id) and source.last_seen is not None
            and 0 <= (timezone.now() - source.last_seen).total_seconds() < settings.HLS_FRESH_SECONDS)


def live_reader_update(data):
    # nginx's own relay reader has no publishing args. Public RTMP play is denied.
    return (data.getlist('call') == ['update_play'] and data.getlist('app') == ['']
            and 'token' not in data and len(data.getlist('name')) == 1
            and bool(re.fullmatch(r'[0-9a-f]{32}', data['name']) or normalize_short_key(data['name']))
            and len(data.getlist('clientid')) == 1 and bool(re.fullmatch(r'[0-9]{1,20}', data['clientid'])))


def valid_shape(data, action):
    expected = {'publish': 'publish', 'update': 'update_publish', 'done': 'publish_done'}
    return (action in expected and all(len(data.getlist(k)) == 1 for k in ['name', 'app', 'call', 'clientid'])
            and data['app'] == 'live' and data['call'] == expected[action]
            and bool(re.fullmatch(r'[0-9]{1,20}', data['clientid'])))


def within_publish_limit(data):
    # addr is added by nginx; duplicate client-supplied values are rejected.
    if len(data.getlist('addr')) != 1:
        return False
    try:
        address = str(ipaddress.ip_address(data['addr']))
    except ValueError:
        return False
    key = 'publish:' + hashlib.sha256(address.encode()).hexdigest()
    cache = caches['rtmp_auth']
    if cache.add(key, 1, 60):
        return True
    try:
        return cache.incr(key) <= 60
    except ValueError:
        return cache.add(key, 1, 60)


def canonical_channel(name, token):
    if not re.fullmatch(r'[0-9a-f]{32}', name) or not re.fullmatch(r'[0-9a-f]{64}', token):
        return None
    source = Channel.objects.select_related('environment').filter(stream_id=name).first()
    if source is None:
        source = ChannelInput.objects.select_related('channel__environment').filter(stream_id=name).first()
    return source if active_publisher(source) and constant_time_compare(token, relay_token(source)) else None


def reserve_source(channel, client_id):
    """Caller holds the parent row lock; allocate without a per-channel ceiling."""
    inputs = list(channel.inputs.all())
    matching = next((item for item in [channel, *inputs] if item.publisher_client_id == client_id), None)
    if matching is not None and active_publisher(matching):
        return matching  # Repeated notification from this same connection.
    if matching is not None:
        # Reuse an expired connection's slot instead of leaving a duplicate ID
        # behind that a delayed callback could incorrectly revive.
        source = matching
    elif not active_publisher(channel):
        source = channel
    else:
        source = next((item for item in inputs if not active_publisher(item)), None)
        if source is None:
            source = ChannelInput(channel=channel,
                                  slot=max((item.slot for item in inputs), default=1) + 1,
                                  key_digest=channel.key_digest)
    source.publisher_client_id = client_id
    source.publisher_generation = uuid.uuid4().hex
    if isinstance(source, ChannelInput):
        source.key_digest = channel.key_digest
    return source


@csrf_exempt
@require_POST
def rtmp_callback(request, action):
    """Authenticate only the public publisher; never expose its key to HLS/SRS."""
    if not internal_allowed(request) or action not in ['publish', 'update', 'done']:
        return HttpResponse(status=403)
    data = request.POST
    if action == 'update' and data.getlist('call') == ['update_play']:
        return HttpResponse(status=204 if live_reader_update(data) else 403)
    if not valid_shape(data, action):
        return HttpResponse(status=403)
    if action == 'publish' and not within_publish_limit(data):
        return HttpResponse(status=429)
    code = normalize_short_key(data['name'])
    if code and 'token' not in data:
        query = {'key_format': 'short', 'key_digest': short_key_digest(code)}
        digest = query['key_digest']
    elif (re.fullmatch(r'[0-9a-f]{32}', data['name']) and len(data.getlist('token')) == 1
          and re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token'])):
        query = {'key_format': 'legacy', 'stream_id': data['name']}
        digest = hashlib.sha256(data['token'].encode('ascii')).hexdigest()
    else:
        return HttpResponse(status=403)
    with transaction.atomic():
        channel = Channel.objects.select_for_update(of=('self',)).select_related('environment').filter(**query).first()
        if (channel is None or not channel.active or not channel.environment.active
                or not constant_time_compare(channel.key_digest, digest)):
            return HttpResponse(status=403)
        if action == 'publish':
            source = reserve_source(channel, data['clientid'])
        else:
            source = next((item for item in [channel, *channel.inputs.all()]
                           if item.publisher_client_id == data['clientid'] and current_source_key(item)), None)
            if source is None:
                return HttpResponse(status=403)
            if action == 'update' and not active_publisher(source):
                return HttpResponse(status=403)
        if action == 'done':
            source.publisher_client_id = '';source.last_seen = None;source.publisher_generation = ''
        else:
            source.last_seen = timezone.now()
        if isinstance(source, ChannelInput):
            source.save()
        else:
            source.save(update_fields=['publisher_client_id', 'publisher_generation', 'last_seen'])
    if action == 'publish':
        return HttpResponse(status=302, headers={'Location': settings.SMA_CANONICAL_RTMP + '/' + source.stream_id
                                                + '?token=' + relay_token(source)})
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
def rtmp_relay_callback(request, action):
    """The loopback RTMP stage produces HLS using the canonical channel name."""
    if not internal_allowed(request) or action not in ['publish', 'update', 'done']:
        return HttpResponse(status=403)
    data = request.POST
    if action == 'update' and data.getlist('call') == ['update_play']:
        return HttpResponse(status=204 if live_reader_update(data) else 403)
    if not valid_shape(data, action) or len(data.getlist('token')) != 1:
        return HttpResponse(status=403)
    channel = canonical_channel(data['name'], data['token'])
    if channel is None:
        return HttpResponse(status=403)
    if action == 'publish' and settings.SMA_REALTIME_ENABLED:
        destination = urlsplit(settings.SMA_REALTIME_RELAY)
        try:
            address = socket.gethostbyname(destination.hostname)
        except OSError:
            return HttpResponse(status=204)  # canonical HLS remains available
        relay = f'rtmp://{address}:{destination.port or 1935}{destination.path.rstrip("/")}'
        return HttpResponse(status=302, headers={'Location': relay+'/'+channel.stream_id+'?token='+data['token']})
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
def realtime_publish_authorize(request):
    if not settings.SMA_REALTIME_ENABLED or not internal_allowed(request):
        return HttpResponse(status=403)
    if any(len(request.POST.getlist(k)) != 1 for k in ['name', 'token']):
        return HttpResponse(status=403)
    name, token = request.POST['name'], request.POST['token']
    channel = canonical_channel(name, token)
    # Permit existing long-key relays during the rolling media update.
    if channel is None and re.fullmatch(r'[0-9a-f]{32}', name) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        old = Channel.objects.select_related('environment').filter(stream_id=name, key_format='legacy').first()
        if active_publisher(old) and constant_time_compare(old.key_digest, hashlib.sha256(token.encode()).hexdigest()):
            channel = old
    return HttpResponse(status=204 if channel else 403)

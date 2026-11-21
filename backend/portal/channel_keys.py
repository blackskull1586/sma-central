"""Recoverable publish credentials, separate from the authentication digest."""
import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


def key_cipher():
    # The persisted application secret is random, not a human password. Context
    # separation prevents this key from being reused for Django signing/RTMP.
    secrets = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet([
        Fernet(base64.urlsafe_b64encode(hmac.new(
            secret.encode('utf-8'), b'sma-channel-credentials-v1', hashlib.sha256
        ).digest()))
        for secret in secrets
    ])


def key_context(channel):
    return {
        'channel': str(channel.pk),
        'environment': str(channel.environment_id),
        'stream': channel.stream_id,
        'format': channel.key_format,
        'digest': channel.key_digest,
    }


def encrypt_channel_key(channel, key):
    payload = {**key_context(channel), 'key': key}
    return key_cipher().encrypt(json.dumps(payload).encode('utf-8')).decode('ascii')


def decrypt_channel_key(channel):
    if not channel.key_ciphertext:
        return None
    try:
        payload = json.loads(key_cipher().decrypt(channel.key_ciphertext))
        if not isinstance(payload, dict):
            return None
        if any(payload.get(field) != value for field, value in key_context(channel).items()):
            return None
        key = payload.get('key')
        return key if isinstance(key, str) and key else None
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        # Never rotate a key or include a secret in an exception/log on reads.
        return None

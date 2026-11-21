"""Encrypted storage for provisional credentials issued by an administrator."""
import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.views.decorators.debug import sensitive_variables


def password_cipher():
    # Domain separation: account credentials cannot use the channel-key cipher.
    secrets = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet([
        Fernet(base64.urlsafe_b64encode(hmac.new(
            secret.encode('utf-8'), b'sma-provisional-account-credentials-v1', hashlib.sha256
        ).digest()))
        for secret in secrets
    ])


def eligible_account(user):
    return (user.pk is not None and not user._state.adding
            and user.must_change_password and not user.is_owner
            and user.has_usable_password())


def password_context(user):
    # Unit/environment changes affect authorization, not the issued password.
    return {'user': str(user.pk), 'username': user.username, 'password_hash': user.password}


@sensitive_variables()
def encrypt_provisional_password(user, raw_password):
    if (not eligible_account(user) or not isinstance(raw_password, str) or not raw_password
            or not check_password(raw_password, user.password)):
        # Never include submitted passwords or stored hashes in exceptions.
        raise ValueError('Não é possível guardar credenciais provisórias para esta conta.')
    payload = {**password_context(user), 'password': raw_password}
    return password_cipher().encrypt(json.dumps(payload).encode('utf-8')).decode('ascii')


@sensitive_variables()
def decrypt_provisional_password(user):
    if not eligible_account(user) or not user.provisional_password_ciphertext:
        return None
    try:
        payload = json.loads(password_cipher().decrypt(user.provisional_password_ciphertext))
        if not isinstance(payload, dict):
            return None
        if any(payload.get(field) != value for field, value in password_context(user).items()):
            return None
        raw_password = payload.get('password')
        if not isinstance(raw_password, str) or not raw_password:
            return None
        # The standalone checker has no setter: disclosure must not upgrade a
        # password hash, invalidate sessions, or persist any model changes.
        return raw_password if check_password(raw_password, user.password) else None
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return None

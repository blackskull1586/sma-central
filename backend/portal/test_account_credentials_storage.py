"""Provisional password recovery must not expose personal or unrelated secrets."""
import json
from unittest.mock import patch

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase, override_settings

from .account_credentials import password_cipher, password_context
from .channel_keys import key_cipher
from .models import Environment, Unit, User


@override_settings(
    SECRET_KEY='provisional-password-storage-test-secret',
    SECRET_KEY_FALLBACKS=[],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class ProvisionalPasswordStorageTests(TestCase):
    password = 'Temporary-Account-Password-748!'

    def setUp(self):
        self.environment = Environment.objects.create(name='Storage SMA', slug='storage-sma')
        self.unit = Unit.objects.create(environment=self.environment, name='Storage unit', slug='storage-unit')
        self.user = User.objects.create_user(username='storage_account', password=self.password,
            environment=self.environment, role=User.Role.VIEWER, must_change_password=True)

    def remember_and_save(self, user=None, password=None):
        user = user or self.user
        user.remember_provisional_password(password or self.password)
        user.save(update_fields=['provisional_password_ciphertext'])
        return user.provisional_password_ciphertext

    def test_recovery_uses_encrypted_storage_and_preserves_authentication_hash(self):
        original_hash = self.user.password
        ciphertext = self.remember_and_save()
        self.assertTrue(ciphertext)
        self.assertNotIn(self.password, ciphertext)
        self.assertNotIn(original_hash, ciphertext)
        stored = User.objects.get(pk=self.user.pk)
        before = User.objects.filter(pk=stored.pk).values().get()
        self.assertEqual(stored.reveal_provisional_password(), self.password)
        self.assertEqual(stored.password, original_hash)
        self.assertTrue(check_password(self.password, stored.password))
        self.assertEqual(User.objects.filter(pk=stored.pk).values().get(), before)

    def test_existing_and_personal_accounts_validate_without_stored_password(self):
        self.assertEqual(self.user.provisional_password_ciphertext, '')
        self.user.full_clean()
        self.user.must_change_password = False
        self.user.full_clean()
        self.assertIsNone(self.user.reveal_provisional_password())

    def test_remember_requires_a_saved_nonowner_account_with_a_matching_provisional_password(self):
        unsaved = User(pk=999999, username='unsaved_storage', environment=self.environment,
            role=User.Role.VIEWER, must_change_password=True)
        unsaved.set_password(self.password)
        owner = User.objects.create_user(username='storage_owner', password=self.password,
            role=User.Role.OWNER, must_change_password=True)
        personal = User.objects.create_user(username='storage_personal', password=self.password,
            environment=self.environment, role=User.Role.VIEWER, must_change_password=False)
        for target, value in [(unsaved, self.password), (owner, self.password), (personal, self.password),
                              (self.user, 'Wrong-provisional-password'), (self.user, ''), (self.user, None)]:
            with self.subTest(username=target.username, value_type=type(value).__name__):
                old_hash = target.password
                with self.assertRaises(ValueError) as error:
                    target.remember_provisional_password(value)
                self.assertNotIn(self.password, str(error.exception))
                self.assertNotIn(old_hash, str(error.exception))
                self.assertEqual(target.password, old_hash)
                self.assertFalse(target.provisional_password_ciphertext)

    def test_personal_password_flag_prevents_recovery_even_if_old_ciphertext_remains(self):
        self.remember_and_save()
        self.user.must_change_password = False
        self.user.save(update_fields=['must_change_password'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.provisional_password_ciphertext)
        self.assertIsNone(self.user.reveal_provisional_password())

    def test_owner_cannot_recover_a_ciphertext_created_before_a_role_change(self):
        self.remember_and_save()
        self.user.role = User.Role.OWNER
        self.user.environment = None
        self.assertIsNone(self.user.reveal_provisional_password())

    def test_set_password_clears_ciphertext_in_memory_before_saving(self):
        self.remember_and_save()
        self.user.set_password('Replacement-personal-password-863!')
        self.assertEqual(self.user.provisional_password_ciphertext, '')
        self.assertIsNone(self.user.reveal_provisional_password())
        self.user.must_change_password = False
        self.user.save(update_fields=['password', 'must_change_password', 'provisional_password_ciphertext'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.provisional_password_ciphertext, '')

    def test_stale_ciphertext_cannot_reveal_after_password_hash_changes(self):
        ciphertext = self.remember_and_save()
        # A second encoding of even the same password is a different issuance.
        self.user.password = make_password(self.password)
        self.user.provisional_password_ciphertext = ciphertext
        self.user.save(update_fields=['password'])
        self.user.refresh_from_db()
        self.assertIsNone(self.user.reveal_provisional_password())

    def test_recovery_is_bound_to_id_username_and_current_password_hash(self):
        self.remember_and_save()
        for field, value in [('pk', self.user.pk + 5000), ('username', 'another_storage_user'),
                             ('password', make_password('Another-password-173!'))]:
            with self.subTest(field=field):
                old_value = getattr(self.user, field)
                setattr(self.user, field, value)
                try:
                    self.assertIsNone(self.user.reveal_provisional_password())
                finally:
                    setattr(self.user, field, old_value)
        self.assertEqual(self.user.reveal_provisional_password(), self.password)

    def test_copying_ciphertext_to_another_account_does_not_disclose_password(self):
        ciphertext = self.remember_and_save()
        other = User.objects.create_user(username='storage_other', password=self.password,
            environment=self.environment, role=User.Role.VIEWER, must_change_password=True)
        other.password = self.user.password
        other.provisional_password_ciphertext = ciphertext
        other.save(update_fields=['password', 'provisional_password_ciphertext'])
        self.assertIsNone(other.reveal_provisional_password())

    def test_unit_and_environment_are_not_part_of_the_credential_binding(self):
        self.remember_and_save()
        self.user.unit = self.unit
        self.user.save(update_fields=['unit'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.reveal_provisional_password(), self.password)
        another_environment = Environment.objects.create(name='Other storage SMA', slug='other-storage-sma')
        self.user.environment = another_environment
        self.user.unit = None
        self.user.save(update_fields=['environment', 'unit'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.reveal_provisional_password(), self.password)

    def test_corrupted_missing_or_wrong_namespace_ciphertext_returns_none(self):
        ciphertext = self.remember_and_save()
        for value in ['', 'not-a-token', ciphertext[:-8], '!' + ciphertext[1:],
                      key_cipher().encrypt(b'{"password":"not-an-account-credential"}').decode('ascii')]:
            with self.subTest(length=len(value)):
                self.user.provisional_password_ciphertext = value
                self.assertIsNone(self.user.reveal_provisional_password())
        with self.assertRaises(InvalidToken):
            key_cipher().decrypt(ciphertext)

    def test_authenticated_but_malformed_payloads_are_not_returned(self):
        context = password_context(self.user)
        for payload in [[], {}, {**context, 'password': None}, {**context, 'password': ''},
                        {**context, 'password': 'Does-not-match-current-hash-382!'}]:
            with self.subTest(payload_type=type(payload).__name__):
                self.user.provisional_password_ciphertext = password_cipher().encrypt(
                    json.dumps(payload).encode('utf-8')).decode('ascii')
                self.assertIsNone(self.user.reveal_provisional_password())

    def test_secret_key_rotation_requires_explicit_fallback_and_new_cipher_uses_current_secret(self):
        original_secret = settings.SECRET_KEY
        self.remember_and_save()
        with override_settings(SECRET_KEY='unrelated-new-storage-secret', SECRET_KEY_FALLBACKS=[]):
            self.assertIsNone(self.user.reveal_provisional_password())
        with override_settings(SECRET_KEY='unrelated-new-storage-secret', SECRET_KEY_FALLBACKS=[original_secret]):
            self.assertEqual(self.user.reveal_provisional_password(), self.password)
            self.remember_and_save()
        with override_settings(SECRET_KEY='unrelated-new-storage-secret', SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(self.user.reveal_provisional_password(), self.password)
        self.assertIsNone(self.user.reveal_provisional_password())

    def test_reveal_and_remember_do_not_upgrade_password_hashes_or_call_user_setter(self):
        original_hash = self.user.password
        with override_settings(PASSWORD_HASHERS=[
            'django.contrib.auth.hashers.PBKDF2PasswordHasher',
            'django.contrib.auth.hashers.MD5PasswordHasher',
        ]), patch.object(self.user, 'set_password', side_effect=AssertionError('Setter must not be called')):
            self.user.remember_provisional_password(self.password)
            self.assertEqual(self.user.reveal_provisional_password(), self.password)
        self.assertEqual(self.user.password, original_hash)

    def test_unusable_password_has_no_recoverable_credentials(self):
        self.remember_and_save()
        self.user.set_unusable_password()
        self.assertIsNone(self.user.reveal_provisional_password())
        with self.assertRaises(ValueError):
            self.user.remember_provisional_password(self.password)

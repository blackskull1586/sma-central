import hashlib
import secrets
import uuid
import re
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from .channel_keys import encrypt_channel_key, decrypt_channel_key
from .account_credentials import (
    encrypt_provisional_password, decrypt_provisional_password,
)

def new_stream_id():
    return uuid.uuid4().hex

SHORT_KEY_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'

def normalize_short_key(value):
    if not value.isascii():
        return None
    value = value.upper()
    pattern = '[' + SHORT_KEY_ALPHABET + ']'
    if re.fullmatch(pattern + '{12}', value) or re.fullmatch((pattern + '{4}-') * 2 + pattern + '{4}', value):
        return value.replace('-', '')
    return None

def short_key_digest(value):
    return hashlib.sha256(('sma-short-v1:' + value).encode('ascii')).hexdigest()

class Environment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField('Nome do ambiente', max_length=80)
    slug = models.SlugField('Identificador', max_length=50, unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return self.name

class Unit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField('Nome da unidade', max_length=80)
    slug = models.SlugField('Identificador', max_length=50)
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name='units')

    class Meta:
        ordering = ['name', 'id']
        constraints = [models.UniqueConstraint(fields=['environment', 'slug'], name='unit_slug_per_environment')]

    def __str__(self):
        return self.name


def validate_unit_environment(instance):
    if instance.unit_id is None:
        return
    try:
        unit = instance.unit
    except Unit.DoesNotExist:
        raise ValidationError({'unit': 'A unidade informada não existe.'}) from None
    if unit.environment_id != instance.environment_id:
        raise ValidationError({'unit': 'A unidade deve pertencer ao mesmo ambiente.'})


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Proprietário'
        ADMIN = 'admin', 'Administrador'
        VIEWER = 'viewer', 'Visualizador'

    environment = models.ForeignKey(Environment, null=True, blank=True, on_delete=models.PROTECT, related_name='users')
    unit = models.ForeignKey(Unit, null=True, blank=True, default=None, db_default=None,
                             on_delete=models.PROTECT, related_name='users')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VIEWER)
    must_change_password = models.BooleanField(default=True)
    provisional_password_ciphertext = models.TextField(default='', db_default='', blank=True, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(Q(role='owner', environment__isnull=True) | Q(role__in=['admin', 'viewer'], environment__isnull=False)), name='user_role_environment'),
            models.CheckConstraint(condition=(~Q(role='owner') | Q(unit__isnull=True)), name='owner_has_no_unit'),
            models.UniqueConstraint(Lower('username'), name='username_case_insensitive'),
        ]

    def clean(self):
        super().clean()
        if self.role == self.Role.OWNER and self.unit_id is not None:
            raise ValidationError({'unit': 'O proprietário não pode ser vinculado a uma unidade.'})
        validate_unit_environment(self)

    def set_password(self, raw_password):
        self.provisional_password_ciphertext = ''
        super().set_password(raw_password)

    def set_unusable_password(self):
        self.provisional_password_ciphertext = ''
        super().set_unusable_password()

    def remember_provisional_password(self, raw_password):
        self.provisional_password_ciphertext = ''
        self.provisional_password_ciphertext = encrypt_provisional_password(self, raw_password)

    def reveal_provisional_password(self):
        return decrypt_provisional_password(self)

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    @property
    def can_manage(self):
        return self.role in [self.Role.OWNER, self.Role.ADMIN]

class Channel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name='channels')
    unit = models.ForeignKey(Unit, null=True, blank=True, default=None, db_default=None,
                             on_delete=models.PROTECT, related_name='channels')
    name = models.CharField('Nome do canal', max_length=80)
    stream_id = models.CharField(max_length=32, unique=True, default=new_stream_id, editable=False)
    key_digest = models.CharField(max_length=64, editable=False)
    key_ciphertext = models.TextField(default='', db_default='', editable=False)
    key_format = models.CharField(max_length=8, choices=[('legacy', 'Anterior'), ('short', 'Curta')],
                                  default='legacy', db_default='legacy', editable=False)
    active = models.BooleanField(default=True)
    # Keep this flag separate from the relation: deleting the last selected
    # account must not make a restricted channel public.
    audience_restricted = models.BooleanField(default=False, db_default=False)
    allowed_viewers = models.ManyToManyField(User, blank=True, related_name='viewable_channels')
    publisher_client_id = models.CharField(max_length=64, blank=True, editable=False)
    publisher_generation = models.CharField(max_length=32, default='', db_default='', blank=True, editable=False)
    last_seen = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name', 'id']
        constraints = [models.UniqueConstraint(Lower('name'), 'environment', condition=Q(unit__isnull=True), name='channel_name_per_environment'),
                       models.UniqueConstraint(Lower('name'), 'unit', condition=Q(unit__isnull=False), name='channel_name_per_unit'),
                       models.UniqueConstraint(fields=['key_digest'], condition=Q(key_format='short'), name='unique_short_publish_key')]

    def clean(self):
        super().clean()
        validate_unit_environment(self)

    def issue_key(self, *, legacy=False):
        # Legacy generation is retained for compatibility fixtures; the UI always issues short keys.
        if legacy:
            token = secrets.token_urlsafe(32)
            self.key_digest = hashlib.sha256(token.encode('ascii')).hexdigest()
            self.key_format = 'legacy'
            result = f'{self.stream_id}?token={token}'
        else:
            for _ in range(8):
                result = ''.join(secrets.choice(SHORT_KEY_ALPHABET) for _ in range(12))
                digest = short_key_digest(result)
                if digest != self.key_digest and not Channel.objects.filter(key_format='short', key_digest=digest).exists():
                    self.key_digest = digest
                    self.key_format = 'short'
                    break
            else:
                raise RuntimeError('Não foi possível gerar uma chave única. Tente novamente.')
        self.publisher_client_id = ''
        self.publisher_generation = ''
        self.last_seen = None
        self.key_ciphertext = encrypt_channel_key(self, result)
        return result

    def reveal_key(self):
        return decrypt_channel_key(self)


class ChannelInput(models.Model):
    """Additional sources share the parent's credentials, scope and audience.

    The existing Channel publishing fields remain source 1 for compatibility.
    """
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name='inputs')
    slot = models.PositiveBigIntegerField()
    stream_id = models.CharField(max_length=32, unique=True, default=new_stream_id, editable=False)
    key_digest = models.CharField(max_length=64, editable=False)
    publisher_client_id = models.CharField(max_length=64, blank=True, editable=False)
    publisher_generation = models.CharField(max_length=32, default='', blank=True, editable=False)
    last_seen = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['slot']
        constraints = [
            models.CheckConstraint(condition=Q(slot__gte=2), name='channel_input_slot_at_least_two'),
            models.UniqueConstraint(fields=['channel', 'slot'], name='channel_input_slot_unique'),
        ]

class AuditEvent(models.Model):
    environment = models.ForeignKey(Environment, null=True, on_delete=models.CASCADE, related_name='events')
    unit = models.ForeignKey(Unit, null=True, blank=True, default=None, db_default=None,
                             on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    owner_only = models.BooleanField(default=False, db_default=False, editable=False)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def clean(self):
        super().clean()
        validate_unit_environment(self)


class CustomDomain(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', 'Aguardando verificação'
        DNS_ERROR = 'dns_error', 'Verifique o DNS'
        PROVISIONING = 'provisioning', 'Preparando HTTPS'
        ACTIVE = 'active', 'HTTPS ativo'
        TLS_ERROR = 'tls_error', 'Certificado pendente'
        DISABLED = 'disabled', 'Desativado'
        ERROR = 'error', 'Falha na verificação'

    hostname = models.CharField(max_length=253, unique=True)
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    detail = models.CharField(max_length=255, blank=True, default='')
    checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hostname']

import json
import os
from pathlib import Path

from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from portal.models import Environment, User


class Command(BaseCommand):
    help = 'Provisiona o proprietário e os administradores apenas na primeira inicialização.'

    @transaction.atomic
    def handle(self, *args, **options):
        if connection.vendor == 'postgresql':
            # Serialize parallel first starts before deciding who provisions.
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [0x534D4142])
        if User.objects.filter(role=User.Role.OWNER).exists():
            self.stdout.write('Bootstrap já realizado.')
            return
        path = Path(os.environ.get('SMA_BOOTSTRAP_FILE', '/run/secrets/bootstrap.json'))
        if not path.is_file():
            raise CommandError('O arquivo de provisionamento inicial é necessário.')
        data = json.loads(path.read_text())
        owner = data['owner']
        count = self.ensure_user(owner['username'], owner['password'], User.Role.OWNER, None)
        for item in data.get('environments', []):
            env, _ = Environment.objects.get_or_create(slug=item['slug'], defaults={'name': item['name']})
            count += self.ensure_user(item['username'], item['password'], User.Role.ADMIN, env)
        self.stdout.write(f'Provisionamento concluído; novos acessos: {count}.')

    def ensure_user(self, username, password, role, environment):
        existing = User.objects.filter(username__iexact=username).first()
        if existing:
            if existing.role != role or existing.environment_id != (environment.pk if environment else None):
                raise CommandError('Um usuário inicial existente pertence a outro perfil ou ambiente.')
            return 0
        user = User(username=username.lower(), role=role, environment=environment,
                    is_staff=role == User.Role.OWNER, is_superuser=role == User.Role.OWNER,
                    must_change_password=True)
        validate_password(password, user)
        user.set_password(password)
        user.full_clean()
        user.save()
        if not user.is_owner:
            user.remember_provisional_password(password)
            user.save(update_fields=['provisional_password_ciphertext'])
        return 1

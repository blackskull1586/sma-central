import io
import json
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import Environment, User


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class FirstStartTests(TestCase):
    def test_five_servers_and_owner_are_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bootstrap.json'
            owner = dict(username='owner', password=secrets.token_urlsafe(20))
            tenants = [dict(slug=f'sma{i}', name=f'SMA {i}', username=f'admin{i}',
                            password=secrets.token_urlsafe(20)) for i in range(1, 6)]
            path.write_text(json.dumps(dict(owner=owner, environments=tenants)))
            with patch.dict('os.environ', SMA_BOOTSTRAP_FILE=str(path)):
                call_command('bootstrap_sma', stdout=io.StringIO())
                self.assertEqual(Environment.objects.count(), 5)
                self.assertEqual(User.objects.filter(must_change_password=True).count(), 6)
                for tenant in tenants:
                    admin = User.objects.get(username=tenant['username'])
                    self.assertTrue(admin.check_password(tenant['password']))
                    self.assertEqual(admin.reveal_provisional_password(), tenant['password'])
                    self.assertEqual(admin.environment.slug, tenant['slug'])
                User.objects.get(username=tenants[-1]['username']).delete()
                call_command('bootstrap_sma', stdout=io.StringIO())
                self.assertEqual(User.objects.count(), 5)
                path.unlink()
                call_command('bootstrap_sma', stdout=io.StringIO())
                self.assertEqual(User.objects.count(), 5)

"""Provisionamento idempotente do portal e leitura limitada para o gravador."""
import os
from pathlib import Path

import django
from django.core.management import call_command
from django.db import connection

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sma.settings")
django.setup()
call_command("migrate", interactive=False)
call_command("bootstrap_sma")
with connection.cursor() as cursor:
    for name in ("grant-metadata.sql", "grant-domains.sql"):
        cursor.execute((Path("/sma") / name).read_text(encoding="utf-8"))

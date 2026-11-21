import uuid

import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0003_channel_key_ciphertext')]

    operations = [
        migrations.CreateModel(
            name='Unit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=80, verbose_name='Nome da unidade')),
                ('slug', models.SlugField(verbose_name='Identificador')),
                ('environment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='units', to='portal.environment')),
            ],
            options={
                'ordering': ['name', 'id'],
                'constraints': [models.UniqueConstraint(fields=('environment', 'slug'), name='unit_slug_per_environment')],
            },
        ),
        migrations.AddField(
            model_name='user',
            name='unit',
            field=models.ForeignKey(blank=True, db_default=None, default=None, null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name='users', to='portal.unit'),
        ),
        migrations.AddField(
            model_name='channel',
            name='unit',
            field=models.ForeignKey(blank=True, db_default=None, default=None, null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name='channels', to='portal.unit'),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='unit',
            field=models.ForeignKey(blank=True, db_default=None, default=None, null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name='events', to='portal.unit'),
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.CheckConstraint(condition=(~models.Q(role='owner') | models.Q(unit__isnull=True)),
                name='owner_has_no_unit'),
        ),
        migrations.RemoveConstraint(
            model_name='channel',
            name='channel_name_per_environment',
        ),
        migrations.AddConstraint(
            model_name='channel',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), models.F('environment'),
                condition=models.Q(unit__isnull=True), name='channel_name_per_environment'),
        ),
        migrations.AddConstraint(
            model_name='channel',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), models.F('unit'),
                condition=models.Q(unit__isnull=False), name='channel_name_per_unit'),
        ),
    ]

import django.db.models.deletion
import portal.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0007_channel_audience')]
    operations = [
        migrations.AddField(
            model_name='channel', name='publisher_generation',
            field=models.CharField(blank=True, db_default='', default='', editable=False, max_length=32),
        ),
        migrations.CreateModel(
            name='ChannelInput',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slot', models.PositiveSmallIntegerField()),
                ('stream_id', models.CharField(default=portal.models.new_stream_id, editable=False, max_length=32, unique=True)),
                ('key_digest', models.CharField(editable=False, max_length=64)),
                ('publisher_client_id', models.CharField(blank=True, editable=False, max_length=64)),
                ('publisher_generation', models.CharField(blank=True, default='', editable=False, max_length=32)),
                ('last_seen', models.DateTimeField(blank=True, editable=False, null=True)),
                ('channel', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='inputs', to='portal.channel')),
            ],
            options={
                'ordering': ['slot'],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(slot__in=[2, 3]), name='channel_input_slot_two_or_three'),
                    models.UniqueConstraint(fields=('channel', 'slot'), name='channel_input_slot_unique'),
                ],
            },
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0001_initial')]
    operations = [
        migrations.AddField(
            model_name='channel', name='key_format',
            field=models.CharField(choices=[('legacy', 'Anterior'), ('short', 'Curta')],
                                   default='legacy', db_default='legacy', editable=False, max_length=8)),
        migrations.AddConstraint(
            model_name='channel',
            constraint=models.UniqueConstraint(fields=('key_digest',), condition=models.Q(key_format='short'),
                                               name='unique_short_publish_key')),
    ]

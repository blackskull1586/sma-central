from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0002_short_publish_keys')]

    operations = [
        migrations.AddField(
            model_name='channel',
            name='key_ciphertext',
            field=models.TextField(db_default='', default='', editable=False),
        ),
    ]

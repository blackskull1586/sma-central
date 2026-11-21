from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0006_user_personal_password_ciphertext')]

    operations = [
        migrations.AddField(
            model_name='channel', name='audience_restricted',
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name='channel', name='allowed_viewers',
            field=models.ManyToManyField(blank=True, related_name='viewable_channels', to=settings.AUTH_USER_MODEL),
        ),
    ]

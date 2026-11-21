from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0005_user_provisional_password_ciphertext')]

    operations = [
        migrations.AddField(
            model_name='user',
            name='personal_password_ciphertext',
            field=models.TextField(blank=True, db_default='', default='', editable=False),
        ),
    ]

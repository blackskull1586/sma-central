from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0004_unit_scopes')]

    operations = [
        migrations.AddField(
            model_name='user',
            name='provisional_password_ciphertext',
            field=models.TextField(blank=True, db_default='', default='', editable=False),
        ),
    ]

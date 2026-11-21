from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0009_dynamic_channel_inputs')]
    operations = [
        migrations.AddField(
            model_name='auditevent', name='owner_only',
            field=models.BooleanField(default=False, db_default=False, editable=False),
        ),
    ]

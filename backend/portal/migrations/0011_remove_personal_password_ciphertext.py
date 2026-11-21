from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('portal', '0010_audit_owner_only')]
    operations = [
        migrations.RemoveField(model_name='user', name='personal_password_ciphertext'),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0008_channel_inputs')]
    operations = [
        migrations.RemoveConstraint(
            model_name='channelinput', name='channel_input_slot_two_or_three',
        ),
        migrations.AlterField(
            model_name='channelinput', name='slot',
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AddConstraint(
            model_name='channelinput',
            constraint=models.CheckConstraint(
                condition=models.Q(slot__gte=2), name='channel_input_slot_at_least_two',
            ),
        ),
    ]

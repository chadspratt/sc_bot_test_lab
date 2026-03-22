from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0027_ticket'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ticket',
            name='test_group',
        ),
    ]

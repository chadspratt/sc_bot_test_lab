from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0020_add_default_test_suite_to_custombot'),
    ]

    operations = [
        migrations.AddField(
            model_name='testgroup',
            name='branch',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Git branch the test was run against. Empty = current working directory (default).',
                max_length=200,
            ),
        ),
    ]

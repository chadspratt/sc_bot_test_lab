from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0033_custombot_env_file'),
    ]

    operations = [
        migrations.AddField(
            model_name='custombot',
            name='bot_module',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Python module path for dynamic import (e.g. 'bottato.bottato'). Used by the legacy Docker runner.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='custombot',
            name='bot_class',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Python class name that inherits from BotAI (e.g. 'BotTato'). Used by the legacy Docker runner.",
                max_length=100,
            ),
        ),
    ]

# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0039_match_bot_actual_race'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='friendly_build',
            field=models.CharField(
                blank=True, default='', max_length=100,
                help_text='Build config name used by the test bot (from aiarena/configs/). Empty = default config.',
            ),
        ),
        migrations.AddField(
            model_name='match',
            name='opponent_build_config',
            field=models.CharField(
                blank=True, default='', max_length=100,
                help_text='Build config name used by the opponent bot (from aiarena/configs/). Empty = default config.',
            ),
        ),
        migrations.AddField(
            model_name='testsuite',
            name='custom_bot_builds',
            field=models.JSONField(
                blank=True, default=dict,
                help_text=(
                    'Per-bot build config overrides: {"<bot_id>": "<build_name>"}. '
                    'When a bot ID is present, the specified build config from '
                    'aiarena/configs/ is applied during test suite runs.'
                ),
            ),
        ),
    ]

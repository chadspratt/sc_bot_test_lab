# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0040_add_build_config_fields'),
    ]

    operations = [
        # Rename bot_actual_race -> friendly_race
        migrations.RenameField(
            model_name='match',
            old_name='bot_actual_race',
            new_name='friendly_race',
        ),
        # Drop opponent_build_config (use opponent_build instead)
        migrations.RemoveField(
            model_name='match',
            name='opponent_build_config',
        ),
        # Expand opponent_build to 100 chars and remove choices constraint
        migrations.AlterField(
            model_name='match',
            name='opponent_build',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]

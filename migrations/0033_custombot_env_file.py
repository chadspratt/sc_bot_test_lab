from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0032_add_system_config_paths'),
    ]

    operations = [
        migrations.AddField(
            model_name='custombot',
            name='env_file',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Absolute path to a .env file on the host. Passed as --env-file to docker compose run, providing environment variables (e.g. DB credentials) to the container.',
                max_length=500,
            ),
        ),
    ]

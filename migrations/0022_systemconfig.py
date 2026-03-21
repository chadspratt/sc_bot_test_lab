from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0021_testgroup_branch'),
    ]

    operations = [
        migrations.CreateModel(
            name='SystemConfig',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('max_concurrent_matches', models.IntegerField(
                    default=0,
                    help_text='Maximum number of matches that can run at the same time. 0 = unlimited.',
                )),
            ],
            options={
                'db_table': 'system_config',
            },
        ),
    ]

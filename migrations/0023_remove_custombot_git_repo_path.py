from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0022_systemconfig'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='custombot',
            name='git_repo_path',
        ),
    ]

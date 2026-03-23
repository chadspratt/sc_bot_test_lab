"""Create the sentinel TestGroup with id=-1 used for ad-hoc matches.

Ad-hoc matches (run_single_match, run_custom_match, etc.) store
test_group_id=-1.  The FK constraint requires this row to exist.
"""

from django.db import migrations


def create_adhoc_group(apps, schema_editor):
    TestGroup = apps.get_model('test_lab', 'TestGroup')
    if not TestGroup.objects.filter(id=-1).exists():
        TestGroup.objects.create(id=-1, description='Ad-hoc matches')


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0035_add_archive_paths_to_custombot'),
    ]

    operations = [
        migrations.RunPython(create_adhoc_group, migrations.RunPython.noop),
    ]

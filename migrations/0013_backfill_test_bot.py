"""Backfill test_bot_id=5 (BotTato) on all existing Match rows."""

from django.db import migrations


def backfill_test_bot(apps, schema_editor):
    Match = apps.get_model('test_lab', 'Match')
    Match.objects.filter(test_bot__isnull=True).update(test_bot_id=5)


def reverse_backfill(apps, schema_editor):
    Match = apps.get_model('test_lab', 'Match')
    Match.objects.filter(test_bot_id=5).update(test_bot_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0012_generalize_test_subject'),
    ]

    operations = [
        migrations.RunPython(backfill_test_bot, reverse_backfill),
    ]

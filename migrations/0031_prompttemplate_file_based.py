"""Replace template_content (TextField) with filename (CharField).

Template content is now stored as .md files in test_lab/prompt_templates/.
"""

from django.db import migrations, models


def set_filenames(apps, schema_editor):
    """Set filename for existing templates created by migration 0030."""
    PromptTemplate = apps.get_model('test_lab', 'PromptTemplate')
    mapping = {
        'BotTato': 'bottato.md',
        'Default': 'default.md',
    }
    for name, filename in mapping.items():
        PromptTemplate.objects.filter(name=name).update(filename=filename)


def clear_filenames(apps, schema_editor):
    """Reverse: clear filenames."""
    PromptTemplate = apps.get_model('test_lab', 'PromptTemplate')
    PromptTemplate.objects.all().update(filename='')


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0030_initial_prompt_templates'),
    ]

    operations = [
        # 1. Add filename column (nullable initially so we can populate it)
        migrations.AddField(
            model_name='prompttemplate',
            name='filename',
            field=models.CharField(
                default='',
                help_text="Filename (relative to test_lab/prompt_templates/) e.g. bottato.md",
                max_length=200,
                unique=False,  # temporarily non-unique until data is set
            ),
            preserve_default=False,
        ),
        # 2. Populate filenames for existing rows
        migrations.RunPython(set_filenames, clear_filenames),
        # 3. Now make filename unique
        migrations.AlterField(
            model_name='prompttemplate',
            name='filename',
            field=models.CharField(
                help_text="Filename (relative to test_lab/prompt_templates/) e.g. bottato.md",
                max_length=200,
                unique=True,
            ),
        ),
        # 4. Drop the old template_content column
        migrations.RemoveField(
            model_name='prompttemplate',
            name='template_content',
        ),
    ]

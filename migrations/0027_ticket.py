import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0026_rename_all_to_blizzard_ai'),
    ]

    operations = [
        migrations.CreateModel(
            name='Ticket',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=200)),
                ('description', models.TextField(help_text='Detailed spec: what to change, acceptance criteria, files to focus on')),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('ready', 'Ready'), ('in_progress', 'In Progress'), ('review', 'Review'), ('testing', 'Testing'), ('done', 'Done'), ('rejected', 'Rejected')], default='draft', max_length=20)),
                ('branch', models.CharField(blank=True, default='', help_text='Auto-generated branch name, e.g. ticket/42-improve-kiting', max_length=200)),
                ('context_files', models.TextField(blank=True, default='', help_text='Newline-separated list of files the agent should focus on')),
                ('prompt_file', models.CharField(blank=True, default='', help_text='Path to the generated .prompt.md file', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('test_bot', models.ForeignKey(help_text='Which bot to modify', on_delete=django.db.models.deletion.CASCADE, related_name='tickets', to='test_lab.custombot')),
                ('test_suite', models.ForeignKey(blank=True, help_text='Which test suite to run when work is done', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tickets', to='test_lab.testsuite')),
                ('test_group', models.ForeignKey(blank=True, help_text='Link to the test results after tests have run', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tickets', to='test_lab.testgroup')),
            ],
            options={
                'db_table': 'ticket',
                'ordering': ['-created_at'],
            },
        ),
    ]

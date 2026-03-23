import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0028_remove_ticket_test_group'),
    ]

    operations = [
        migrations.CreateModel(
            name='PromptTemplate',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200, unique=True)),
                ('template_content', models.TextField(
                    help_text='Template content with placeholders: {ticket_id}, {title}, {branch_name}, {bot_name}, {source_path}, {description}, {focus_files}',
                )),
                ('bots', models.ManyToManyField(
                    blank=True,
                    help_text='Bots this template is registered for. If empty, it is a generic/default template available to bots with no registered templates.',
                    related_name='prompt_templates',
                    to='test_lab.custombot',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'prompt_template',
            },
        ),
        migrations.AddField(
            model_name='ticket',
            name='prompt_template',
            field=models.ForeignKey(
                blank=True,
                help_text='Prompt template to use when generating the .prompt.md file',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tickets',
                to='test_lab.prompttemplate',
            ),
        ),
    ]

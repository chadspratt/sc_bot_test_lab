from django.db import migrations

BOTTATO_TEMPLATE = """\
---
description: "Ticket #{ticket_id}: {title}"
agent: "ticket-worker"
tools: [read, edit, search, execute]
---

## Ticket #{ticket_id} — {title}

### Branch Setup
First, change to the bot's repository root:
```
cd {source_path}
```

Then create the worktree (this keeps the main checkout clean and allows
multiple tickets to be worked on in parallel):
```
git worktree add worktrees/{branch_name} -b {branch_name}
```
If the branch already exists:
```
git worktree add worktrees/{branch_name} {branch_name}
```

Then **do all your work inside `{source_path}/worktrees/{branch_name}/`**.
All file paths below are relative to that worktree directory.

### Request
**Bot:** {bot_name}

{description}

### Focus Files
{focus_files}

### Constraints
- All imports from bottato use `from bottato.*` (never `from bot.bottato.*`)
- All imports from python_sc2 use `from sc2.*` (never `from python_sc2.sc2.*`)
- Do not modify files outside `bot/bottato/` unless necessary
- Do NOT run the bot's own test suite or pytest — all testing is handled by
  the ticket test system described below

### After Every Commit
After **each** commit to branch `{branch_name}`, trigger the ticket test suite
by running this command:
```
curl -X POST http://localhost:8000/test_lab/api/trigger-ticket-tests/ \\\\
  -H "Content-Type: application/json" \\\\
  -d '{{"ticket_id": {ticket_id}}}'
```
This kicks off the SC2 test matches for your changes. Always do this — do not
skip it or wait until the end.

### When Finished
1. Make sure you have committed all changes and triggered tests (see above)
2. Report what you changed and why
"""

DEFAULT_TEMPLATE = """\
---
description: "Ticket #{ticket_id}: {title}"
agent: "ticket-worker"
tools: [read, edit, search, execute]
---

## Ticket #{ticket_id} — {title}

### Branch Setup
First, change to the bot's repository root:
```
cd {source_path}
```

Then create the worktree (this keeps the main checkout clean and allows
multiple tickets to be worked on in parallel):
```
git worktree add worktrees/{branch_name} -b {branch_name}
```
If the branch already exists:
```
git worktree add worktrees/{branch_name} {branch_name}
```

Then **do all your work inside `{source_path}/worktrees/{branch_name}/`**.
All file paths below are relative to that worktree directory.

### Request
**Bot:** {bot_name}

{description}

### Focus Files
{focus_files}

### After Every Commit
After **each** commit to branch `{branch_name}`, trigger the ticket test suite
by running this command:
```
curl -X POST http://localhost:8000/test_lab/api/trigger-ticket-tests/ \\\\
  -H "Content-Type: application/json" \\\\
  -d '{{"ticket_id": {ticket_id}}}'
```
This kicks off the SC2 test matches for your changes. Always do this — do not
skip it or wait until the end.

### When Finished
1. Make sure you have committed all changes and triggered tests (see above)
2. Report what you changed and why
"""


def create_initial_templates(apps, schema_editor):
    PromptTemplate = apps.get_model('test_lab', 'PromptTemplate')
    CustomBot = apps.get_model('test_lab', 'CustomBot')

    # Create the BotTato-specific template
    bottato_tmpl = PromptTemplate.objects.create(
        name='BotTato',
        template_content=BOTTATO_TEMPLATE,
    )
    # Register it to the BotTato bot if it exists
    bottato_bot = CustomBot.objects.filter(name='BotTato').first()
    if bottato_bot:
        bottato_tmpl.bots.add(bottato_bot)

    # Create the generic/default template (no bots assigned)
    PromptTemplate.objects.create(
        name='Default',
        template_content=DEFAULT_TEMPLATE,
    )


def remove_initial_templates(apps, schema_editor):
    PromptTemplate = apps.get_model('test_lab', 'PromptTemplate')
    PromptTemplate.objects.filter(name__in=['BotTato', 'Default']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('test_lab', '0029_prompttemplate_ticket_prompt_template'),
    ]

    operations = [
        migrations.RunPython(create_initial_templates, remove_initial_templates),
    ]

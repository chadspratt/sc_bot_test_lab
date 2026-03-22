"""Generate .prompt.md files from Ticket data.

No LLM API call is needed — this is pure string templating.
"""

from __future__ import annotations

import os

PROMPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', '.github', 'prompts')
)

TICKET_PROMPT_TEMPLATE = """\
---
description: "Ticket #{ticket_id}: {title}"
agent: "ticket-worker"
tools: [read, edit, search, execute]
---

## Ticket #{ticket_id} — {title}

### Branch Setup
Use a git worktree so the main checkout stays clean and multiple tickets can
be worked on in parallel. Run these commands from the repository root:

Create the branch and worktree in one step:
```
git worktree add worktrees/{branch_name} -b {branch_name}
```
If the branch already exists:
```
git worktree add worktrees/{branch_name} {branch_name}
```

Then **do all your work inside `worktrees/{branch_name}/`**.
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

### When Finished
1. Commit all changes to branch `{branch_name}` (inside the worktree)
2. Trigger the test suite by running:
   ```
   curl -X POST http://localhost:8000/test_lab/api/trigger-ticket-tests/ \\
     -H "Content-Type: application/json" \\
     -d '{{"ticket_id": {ticket_id}}}'
   ```
3. Report what you changed and why
"""


def generate_prompt_content(ticket) -> str:
    """Render prompt file content from a Ticket instance."""
    if ticket.context_files.strip():
        files = [f.strip() for f in ticket.context_files.strip().splitlines() if f.strip()]
        focus_files = '\n'.join(f'- `{f}`' for f in files)
    else:
        focus_files = '(any relevant files — explore the codebase as needed)'

    return TICKET_PROMPT_TEMPLATE.format(
        ticket_id=ticket.id,
        title=ticket.title,
        branch_name=ticket.branch_name,
        bot_name=ticket.test_bot.name,
        description=ticket.description,
        focus_files=focus_files,
    )


def prompt_filename(ticket) -> str:
    """Return the filename for a ticket's prompt file."""
    return f'ticket-{ticket.id}.prompt.md'


def write_prompt_file(ticket) -> str:
    """Write the .prompt.md file to .github/prompts/ and return the path."""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    filename = prompt_filename(ticket)
    filepath = os.path.join(PROMPTS_DIR, filename)
    content = generate_prompt_content(ticket)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return filepath


def delete_prompt_file(ticket) -> None:
    """Remove the .prompt.md file if it exists."""
    filepath = os.path.join(PROMPTS_DIR, prompt_filename(ticket))
    if os.path.exists(filepath):
        os.remove(filepath)

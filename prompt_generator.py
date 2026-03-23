"""Generate .prompt.md files from Ticket data.

No LLM API call is needed — this is pure string templating.
Template content is read from files in test_lab/prompt_templates/.
"""

from __future__ import annotations

import os

PROMPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', '.github', 'prompts')
)

TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), 'prompt_templates')
)

def read_template_file(filename: str) -> str | None:
    """Read a template file from the prompt_templates directory."""
    filepath = os.path.join(TEMPLATES_DIR, filename)
    if not os.path.isfile(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()


def list_template_files() -> list[str]:
    """List all .md files in the prompt_templates directory."""
    if not os.path.isdir(TEMPLATES_DIR):
        return []
    return sorted(
        f for f in os.listdir(TEMPLATES_DIR)
        if f.endswith('.md') and os.path.isfile(os.path.join(TEMPLATES_DIR, f))
    )


def generate_prompt_content(ticket) -> str:
    """Render prompt file content from a Ticket instance.

    Uses the ticket's associated prompt_template file if set,
    otherwise falls back to the default.md template.
    """
    if ticket.context_files.strip():
        files = [f.strip() for f in ticket.context_files.strip().splitlines() if f.strip()]
        focus_files = '\n'.join(f'- `{f}`' for f in files)
    else:
        focus_files = '(any relevant files — explore the codebase as needed)'

    # Load template from file
    template = None
    if ticket.prompt_template and ticket.prompt_template.filename:
        template = read_template_file(ticket.prompt_template.filename)
    if template is None:
        template = read_template_file('default.md')
    if template is None:
        raise FileNotFoundError(
            f"No template file found (tried '{getattr(ticket.prompt_template, 'filename', '')}' and 'default.md')"
        )

    return template.format(
        ticket_id=ticket.id,
        title=ticket.title,
        branch_name=ticket.branch_name,
        bot_name=ticket.test_bot.name,
        source_path=ticket.test_bot.source_path,
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

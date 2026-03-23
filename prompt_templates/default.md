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

### When Finished
1. Make a commit to branch `{branch_name}`
2. Trigger the ticket test suite
by running this command:
```
curl -X POST http://localhost:8000/test_lab/api/trigger-ticket-tests/ \
  -H "Content-Type: application/json" \
  -d '{{"ticket_id": {ticket_id}}}'
```
This kicks off the SC2 test matches for your changes. Always do this — do not
skip it or wait until the end.
3. Report what you changed and why
4. If the user make a further request, work on them in the same worktree and follow the constraints and when-finished instructions 

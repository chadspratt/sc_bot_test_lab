# test_lab

Django app for automated StarCraft II bot testing. Runs matches via Docker
using the [AI Arena local-play-bootstrap](https://github.com/aiarena/local-play-bootstrap)
infrastructure and tracks results.

## Setup

### Database

test_lab uses its own MySQL database (`sc2bot_test_lab_db_2`). Run migrations with:

```bash
python manage.py migrate test_lab --database sc2bot_test_lab_db_2
```

### Custom Bots

Register bots through the **Custom Bots** page. Place AI Arena bot directories
(containing `ladderbots.json`) in `test_lab/aiarena/bots/`.

#### Test Subject Bots

A bot marked as a **test subject** can act as Player 1 in matches. When
registering, fill in:

| Field | Purpose |
|-------|---------|
| **Source Path** | Absolute host path to the bot source — mounted into Docker at runtime |
| **Git Repo Path** | Path to the bot's git repo (enables past-version matches) |
| **Dockerfile** | Custom Dockerfile name for build steps (e.g. Cython compilation) |

Symlinks/junctions in the source directory are auto-detected and stored so
Docker volume mounts resolve correctly.

### Git Commit Hook

To automatically trigger a test suite on every commit, add a `post-commit`
hook to the bot's git repo:

```bash
#!/bin/sh
# Post-commit hook: trigger test_lab test suite via the Django API
# Replace TEST_BOT_ID with the bot's numeric ID from the Custom Bots page.

BRANCH=$(git rev-parse --abbrev-ref HEAD)
SHORT_SHA=$(git rev-parse --short HEAD)
COMMIT_MSG=$(git log -1 --format=%s)
DESCRIPTION="$BRANCH $SHORT_SHA: $COMMIT_MSG"

curl -s -X POST http://localhost:8000/test_lab/api/trigger-tests/ \
  -H "Content-Type: application/json" \
  -d "{\"description\": \"$DESCRIPTION\", \"difficulty\": \"CheatInsane\", \"test_bot_id\": TEST_BOT_ID}" \
  > /dev/null 2>&1 &

echo "Test suite triggered for commit $SHORT_SHA"
```

Save this as `.git/hooks/post-commit` and make it executable (`chmod +x`).

## API

### `POST /test_lab/api/trigger-tests/`

JSON body:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `difficulty` | string | `"CheatInsane"` | AI difficulty level |
| `description` | string | `""` | Test group description |
| `test_bot_id` | int | *null* | Custom bot ID for Player 1 (omit for legacy BotTato) |
| `custom_bot_id` | int | *null* | When set, runs a single match vs this bot instead of the full 15-match suite |

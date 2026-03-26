# test_lab

Django app for automated StarCraft II bot testing. Runs matches via Docker
using the [AI Arena local-play-bootstrap](https://github.com/aiarena/local-play-bootstrap)
infrastructure and tracks results.

Disclaimer: This is intended to work for all bot types but some types are untested.
It's all very WIP so feel free to send a PR and I won't be too picky about merging it. 

## Setup (existing Django project)

If you're adding test_lab to an existing Django project instead of using
the quickstart, follow these steps.

### Database

test_lab uses its own MySQL database (`sc2bot_test_lab_db_2`). Run migrations with:

```bash
python manage.py migrate test_lab --database sc_bot_test_lab
```


## Quickstart (standalone setup)

Use this if you've cloned test_lab on its own and want to get it running
quickly. The only prerequisites are **Docker** (running), **Python 3.12+**,
and **StarCraft II** installed (for map files).

> **Why not fully Dockerize Django too?** The match runner launches SC2
> Docker containers from the Django process using `docker compose` with
> host-path volume mounts. Running Django itself inside Docker would
> require Docker-in-Docker with complex host/container path mapping that
> is fragile across OSes. Keeping Django on the host avoids this entirely.

### Automated setup (recommended)

Clone, run one command, and the browser opens automatically:

```bash
mkdir sc2_test_lab && cd sc2_test_lab
git clone <test_lab_repo_url> test_lab
python test_lab/quickstart/setup.py
```

The script handles everything: starts MySQL in Docker, creates a virtual
environment, installs dependencies, runs migrations, and launches the
development server. It works on Windows, Linux, and macOS.

> On Debian/Ubuntu you may need `sudo apt install python3-venv` first.

### Manual setup (step by step)

<details>
<summary>Click to expand manual steps</summary>
#### 1. Clone into the right directory structure

Django needs `test_lab` to be an importable Python package, so clone it
**inside** a wrapper directory:

```bash
mkdir sc2_test_lab && cd sc2_test_lab
git clone git@github.com:chadspratt/sc_bot_test_lab.git test_lab
```

Your layout should look like:

```
sc2_test_lab/          # <-- you'll run commands from here
  test_lab/            # <-- the cloned repo
    quickstart/
    aiarena/
    models.py
    ...
```

#### 2. Start MySQL via Docker

```bash
docker compose -f test_lab/quickstart/docker-compose.yml up -d
```

This starts a MySQL 8.0 container on `localhost:3306` with database `sc_bot`
(user `root`, password `testlab`). Adjust credentials in the compose file or
via environment variables — see `quickstart/settings.py` for the full list.

#### 3. Create a Python virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate

pip install -r test_lab/requirements.txt
```

#### 4. Run database migrations

```bash
python test_lab/quickstart/manage.py migrate --database default
python test_lab/quickstart/manage.py migrate test_lab --database sc_bot_test_lab
```

#### 5. Start the development server

```bash
python test_lab/quickstart/manage.py runserver
```

Open <http://localhost:8000/test_lab/> in your browser.

### Stopping / resetting

```bash
# Stop MySQL (data is preserved in a Docker volume)
docker compose -f test_lab/quickstart/docker-compose.yml down

# Stop MySQL and delete all data
docker compose -f test_lab/quickstart/docker-compose.yml down -v
```

</details>

### 6. Basic Configuratio

In the app, go to `Config > System` and enter the path for the directory containing the maps.
Also enter the path to SC2Switcher.exe, which enables launching replays from the app.

### 7. Register bots

Go to `Config > Custom Bots` and follow the instructions for adding a bot.

A bot marked as a **test subject** can act as Player 1 in matches. When
registering, fill in:

| Field | Purpose |
|-------|---------|
| **Source Path** | Absolute host path to the bot source — mounted into Docker at runtime |
| **Git Repo Path** | Path to the bot's git repo (enables past-version matches) |
| **Dockerfile** | Custom Dockerfile name for build steps (e.g. Cython compilation) |

Symlinks/junctions in the source directory are auto-detected and stored so
Docker volume mounts resolve correctly.

On `Run Match > Vs Blizzard AI` you can trigger a test run of the bot vs the Blizzard AI. This may take a while for it to build docker images

### 8. Matches
There are 4 kinds of matches that can be run. the `Run Match` page allows running an individual match
* Blizzard AI - custom bot vs in-game bot
* Custom Bot - custom bot vs custom bot
* Past Version - custom bot vs itself. can be use a past version or the current version for a true mirror match
* Replay - custom bot vs in-game bot, but you start from a game state that is pulled from a replay. Will require some edits for a custom bot to make use of this, since the misleading clock and lack of state can trip it up.

### 9. Test Suites
`Config > Test Suites` allows you to bundle different tests together. There is a default suite for running vs 15 variants of the Blizzard AI. These can be attached to Tickets

### 10. Tickets
`Tickets` Allows you to create a ticket for doing work on a bot. You can specify various details and then it will generate a prompt that can be used by the editor of your choice.
The prompt instructs the agent to work in a git worktree so that multiple tickets can be worked on concurrently.
After finishing the work, the agent is instructed to commit and trigger the ticket tests, which will run against the code in the worktree

`Config > Prompt Templates` Allows for creating custom templates for working on specific bots. There are forms for creating and editing them but they are stored in actual files so it's probably easier to edit them outside the app. In the app you can edit them to register them for specific bots.

---
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
| `test_bot_id` | int | **required** | Custom bot ID for Player 1 (the test subject) |
| `difficulty` | string | `"CheatInsane"` | AI difficulty level |
| `description` | string | `""` | Test group description |
| `custom_bot_id` | int | *null* | When set, runs a single match vs this bot instead of the full test suite |
| `test_suite_id` | int | *null* | Run a specific test suite (falls back to the bot's default suite, then "Blizzard AI") |
| `branch` | string | `""` | Git branch name — creates a worktree so the bot source is mounted from that branch |

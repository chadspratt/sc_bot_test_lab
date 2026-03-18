# test_lab

Django app for automated StarCraft II bot testing. Runs matches via Docker
using the [AI Arena local-play-bootstrap](https://github.com/aiarena/local-play-bootstrap)
infrastructure and tracks results.

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
git clone <test_lab_repo_url> test_lab
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

</details>

### 6. Configure StarCraft II map path

Edit `test_lab/aiarena/docker-compose.yml` and update the SC2 maps volume
mount to point to your local StarCraft II installation:

```yaml
# Change this line to match your Maps directory:
- "C:\\Program Files (x86)\\StarCraft II\\Maps:/root/StarCraftII/maps"
```

### 7. Register your bot

1. Place your bot directory (containing `ladderbots.json`) in
   `test_lab/aiarena/bots/`.
2. Open <http://localhost:8000/test_lab/custom-bots/> and register it — see
   [Custom Bots](#custom-bots) below for field details.
3. Navigate to <http://localhost:8000/test_lab/utilities/> to run a match.

### Stopping / resetting

```bash
# Stop MySQL (data is preserved in a Docker volume)
docker compose -f test_lab/quickstart/docker-compose.yml down

# Stop MySQL and delete all data
docker compose -f test_lab/quickstart/docker-compose.yml down -v
```

---

## Setup (existing Django project)

If you're adding test_lab to an existing Django project instead of using
the quickstart, follow these steps.

### Database

test_lab uses its own MySQL database (`sc2bot_test_lab_db_2`). Run migrations with:

```bash
python manage.py migrate test_lab --database sc_bot_test_lab
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

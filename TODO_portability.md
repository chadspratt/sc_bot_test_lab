# test_lab Portability — To-Do List

Making test_lab usable by other SC2 bot authors. Items grouped by area,
ordered roughly by dependency (earlier items unblock later ones).

---

## 1. SystemConfig model expansion

Add new fields to `SystemConfig` so paths are configurable per-installation
instead of hardcoded. On a fresh install with no config row, redirect every
page to a **first-run setup page** that asks the user to fill in these
values (with a note that they can be changed later on the config page).

- [x] `logs_dir` — Directory for legacy Docker match logs.
      **Current:** hardcoded `r'C:\Users\inter\Documents\StarCraft II\Replays\Multiplayer\docker'` in views.py (lines 435, 871, 896).
      **Default:** empty (must be set on first run).

- [x] `sc2_switcher_path` — Path to SC2Switcher.exe (or equivalent) for opening replays.
      **Current:** hardcoded `r"C:\Program Files (x86)\StarCraft II\Support\SC2Switcher.exe"` in views.py (lines 867, 879).
      **Default:** `C:\Program Files (x86)\StarCraft II\Support\SC2Switcher.exe` (Windows default).

- [x] `sc2_maps_path` — Host path to StarCraft II Maps directory (mounted into Docker containers).
      **Current:** hardcoded in `docker-compose.yml` and `aiarena/docker-compose.yml`.
      **Default:** `C:\Program Files (x86)\StarCraft II\Maps` (Windows default).

- [x] `replays_dir` — Host path for legacy Docker replays directory.
      **Current:** hardcoded in `docker-compose.yml`.
      **Default:** empty (must be set on first run if legacy mode is used).

### First-run setup flow
- [x] Add middleware or view decorator that checks `SystemConfig` for required fields.
      If not configured, redirect to `/test_lab/setup/` (a one-time setup page).
- [x] Setup page: form with all required SystemConfig fields, explanatory text,
      and a "Save & Continue" button. After saving, redirect to results page.
- [x] Make these fields editable on the existing config page as well.

### Plumb SystemConfig values into docker-compose
- [x] `aiarena/docker-compose.yml`: replace hardcoded Maps path with a placeholder
      that `_create_run_dir` or `_write_compose_override` substitutes from SystemConfig.
- [x] `docker-compose.yml` (legacy): same treatment for Maps and Replays paths.
- [x] `views.py`: replace all `LOGS_DIR` and SC2Switcher references with `SystemConfig.load()` lookups.

---

## 2. Remove hardcoded BotTato id=5 fallback

Every place that falls back to `CustomBot.objects.filter(id=5)` should
instead **require** `test_bot` to be provided.

- [x] `create_pending_match()` in views.py (line 419): remove the `id=5` fallback;
      make `test_bot` a required parameter (remove `None` default).
- [x] `start_custom_bot_match()` in views.py (line 486): same — require `test_bot`.
- [x] `start_test_suite()` in views.py (line 577): same — require `test_bot`.
- [x] **"Run Single Match vs Computer" form** in `run_match.html`: add a Test Bot
      dropdown (like the custom-match form already has) so the user always picks one.
- [x] Update the `run_single_match` view to read `test_bot_id` from POST and pass it.

---

## 3. Remove legacy Match.test_bot=NULL support

`Match.test_bot` should always be set. Remove `BotTato` string fallbacks.

- [x] `models.py` — `Match.opponent_version_bot_name`: remove `'BotTato'` fallback
      (test_bot will always exist).
- [x] `models.py` — `Match.test_bot_name`: remove `'BotTato'` fallback.
- [x] `models.py` — `Match.test_bot_directory`: remove `'BotTato'` fallback.
- [x] `models.py` — `Match.__str__`: remove hardcoded `vs BotTato@` case.
- [x] `views.py` — all `test_bot.name if test_bot else 'BotTato'` patterns:
      simplify to `test_bot.name` (lines 814, 1810, 2694, etc.).
- [x] Make `Match.test_bot` non-nullable in the model (remove `null=True, blank=True`).
      Migration needed — backfill existing NULL rows first.
- [x] Update `models.py` help_text that mentions "NULL = BotTato (legacy)".

---

## 4. Remove `run_vs_bot.py` (deprecated)

The legacy single-container Bot-vs-Bot runner is superseded by the aiarena
infrastructure.

- [x] Delete `runner/run_vs_bot.py`.
- [x] Delete `runner/run_docker_bot_vs_bot.sh`.
- [x] Remove any references in views.py that launch `run_docker_bot_vs_bot.sh`
      (the legacy path in `start_custom_bot_match`).

---

## 5. Make `run_vs_computer.py` bot-agnostic

Currently hardcodes `from bottato.bottato import BotTato` and `Race.Terran`.

- [x] Accept bot class import path and race via environment variables
      (e.g. `BOT_MODULE=bottato.bottato`, `BOT_CLASS=BotTato`, `BOT_RACE=Terran`).
- [x] Read these from `CustomBot` fields; pass them as env vars in the Docker
      command built by `run_single_match` / `start_test_suite`.
- [x] Remove the hardcoded `from bottato.bottato import BotTato` import;
      use `importlib` to load the configured class dynamically.
- [x] Use the race from the env var instead of hardcoded `Race.Terran`.

---

## 6. Make `run_from_replay.py` bot-agnostic

Same treatment as run_vs_computer.

- [x] Accept `BOT_MODULE`, `BOT_CLASS`, `BOT_RACE` env vars.
- [x] Dynamic import instead of hardcoded BotTato.
- [x] Pull race from CustomBot via env var.

---

## 7. Generalize `prepare_bottato.py` (play-vs-self)

This script creates the aiarena overlay for self-play. It currently only
works for BotTato. Needs significant rework.

- [x] Rename to something generic (e.g. `prepare_bot_overlay.py`).
- [x] Accept bot name, source path, and ladderbots.json key as parameters
      (CLI args or read from CustomBot).
- [x] Remove hardcoded `'BotTato'` / `'BotTato_p2'` constants.
- [x] The embedded `AIARENA_RUN_PY` with Cython compilation is BotTato-specific;
      make it pluggable or move it to the bot's own overlay setup.
- [x] Update `_ensure_mirror_overlay` in aiarena_runner.py to work with any
      test bot (it currently does, but calls into prepare_bottato patterns).

---

## 8. Selectively include Dockerfile.bottato

`_BASE_FILES` in aiarena_runner.py always copies `Dockerfile.bottato` into
every run directory even when it isn't used.

- [ ] Remove `Dockerfile.bottato` from `_BASE_FILES`.
- [ ] In `_create_run_dir`, only copy the Dockerfile referenced by
      `test_bot.dockerfile` (and opponent's dockerfile if set).
- [ ] This way each bot specifies its own custom Dockerfile (or none)
      and nothing BotTato-specific leaks into generic runs.

---

## 9. Generalize `bot_versions.py`

Remove BotTato-specific defaults; use CustomBot fields instead.

- [ ] Remove `BOT_REPO_DIR` constant (hardcoded to `bot/`).
      `get_recent_bot_commits` and `get_or_create_version_cache` already
      accept `repo_path` — callers should always pass `test_bot.source_path`.
- [ ] Remove `BOT_ARCHIVE_PATHS_REQUIRED` and `BOT_ARCHIVE_PATHS_OPTIONAL`
      constants. Replace with a **CustomBot field** (e.g. `archive_paths` JSON)
      that lists which paths to extract from git history for past-version tests.
      Explanation in help_text: "Paths to extract from git history when testing
      against past versions. E.g. `['src/', 'bot.py', 'config/']`."
- [ ] Remove the `is_bottato` branching in `get_or_create_version_cache`.
      If `archive_paths` is configured, use them; otherwise archive the
      entire tree (the current generic fallback).
- [ ] Merge the concept of required/optional archive paths into a single
      list — the optional paths were only needed because BotTato's repo
      structure changed over time. A single list is simpler.

---

## 10. Plugin system for custom utilities

Replace the hardcoded `recompile_cython` view with a plugin discovery system
for the Custom page.

- [ ] Create `test_lab/plugins/` directory with a `__init__.py` that discovers
      plugin modules.
- [ ] Add `plugins/` to `.gitignore` (except `__init__.py`).
- [ ] Each plugin is a `.py` file in `plugins/` that defines:
      - `name: str` — display name for the button/widget
      - `description: str` — short help text
      - `def execute(request) -> str` — runs the action, returns a status message
- [ ] Plugin discovery: scan `plugins/` at startup (or on each request) for
      `.py` files, import them, collect their metadata.
- [ ] `custom_page` view: render discovered plugins as action buttons/cards.
- [ ] POST handler: dispatch to the matching plugin's `execute()`.
- [ ] Move current `recompile_cython` logic into `plugins/recompile_cython.py`
      as the first example plugin.
- [ ] Remove the hardcoded `recompile_cython` view and URL.

---

## 11. Clean up BotTato references in field descriptions and templates

- [ ] `models.py` — `CustomBot.dockerfile` help_text: change
      "e.g. Dockerfile.bottato" → "e.g. Dockerfile.mybot".
- [ ] `models.py` — `Match.test_bot` help_text: remove "NULL = BotTato (legacy)".
- [ ] `models.py` — `PromptTemplate.filename` help_text: change
      "e.g. bottato.md" → "e.g. mybot.md".
- [ ] `run_match.html` — remove the hardcoded
      `<option value="bottato">BotTato (legacy)</option>` from the test bot dropdown.
- [ ] `run_match.html` — change "BotTato takes over" wording to generic
      "test bot takes over" (lines 196, 242, 255).
- [ ] `run_match.html` — remove `<option value="">BotTato (default)</option>`;
      the dropdown should only list registered test-subject bots.

**Keep as-is** (example/placeholder text that's fine for illustration):
- `tickets.html` — `bot/bottato/micro/marine_micro.py` placeholder
- `config.html` — instructional text mentioning Bottato

---

## 12. Docker-compose.yml (legacy) — additional cleanup

- [ ] Remove the hardcoded `../../../bot` and `../../../python_sc2/sc2` volume
      mounts — these are BotTato monorepo paths. The legacy container should
      mount the test bot's `source_path` dynamically (already done in the
      `start_test_suite` code via `-v` flag, but the base compose file still
      has the hardcoded mounts).
- [ ] Or: if legacy mode is being sunset in favor of aiarena, add a
      deprecation notice and plan removal.

---

## Migration / deployment order

1. **SystemConfig expansion + first-run setup** (items 1)
2. **Model changes** (items 2, 3 — remove id=5 fallback, make test_bot required)
3. **Template / view cleanup** (items 5, 11 — dropdowns, text)
4. **Runner scripts** (items 4, 5, 6 — delete run_vs_bot, genericize others)
5. **Bot versions & overlays** (items 7, 8, 9, 10)
6. **Plugin system** (item 10)

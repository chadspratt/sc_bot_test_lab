"""
AI Arena match runner for test_lab.

Handles running bot-vs-bot matches using the aiarena/local-play-bootstrap
Docker infrastructure. This runs on the HOST (not inside Docker) and is called
from Django views.

The aiarena infrastructure uses four containers:
  - sc2_controller: runs StarCraft II
  - bot_controller1: runs Bot 1 (BotTato)
  - bot_controller2: runs Bot 2 (opponent)
  - proxy_controller: coordinates the match

Bots must be placed in aiarena/bots/<bot_name>/ with a ladderbots.json.
"""

from __future__ import annotations

import glob
import json
import os
import random
import shutil
import stat
import subprocess
import threading
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from .models import CustomBot, Match

# Paths
AIARENA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'aiarena'))
AIARENA_BOTS_DIR = os.path.join(AIARENA_DIR, 'bots')
AIARENA_RUNS_DIR = os.path.join(AIARENA_DIR, 'runs')

# Repo root (for finding bots in other_bots/ and live source mounts)
REPO_ROOT = os.path.normpath(os.path.join(AIARENA_DIR, '..', '..', '..', '..'))

# Live source directories (mounted into containers instead of copied)
BOT_SRC_DIR = os.path.join(REPO_ROOT, 'bot')
SC2_SRC_DIR = os.path.join(REPO_ROOT, 'python_sc2', 'sc2')

# BotTato is always Bot 1
BOTTATO_NAME = 'BotTato'
BOTTATO_RACE = 'T'
BOTTATO_TYPE = 'python'

# Mirror copy for self-play matches (distinct name so the proxy can route)
BOTTATO_MIRROR_NAME = 'BotTato_p2'

# Prefix for past-version opponent names (e.g. BotTato_v_d019795)
BOTTATO_VERSION_PREFIX = 'BotTato_v_'

# Base files in AIARENA_DIR that are copied into each per-match run directory
_BASE_FILES = ('docker-compose.yml', 'Dockerfile.bottato', 'config.toml')

# Maps available for aiarena matches (same as the test_lab map pool)
AIARENA_MAP_LIST = [
    "PersephoneAIE_v4",
    "IncorporealAIE_v4",
    "PylonAIE_v4",
    "TorchesAIE_v4",
    "UltraloveAIE_v2",
    "MagannathaAIE_v2",
]

# Map race names to single-letter codes used in the aiarena matches file
RACE_TO_CODE = {
    'Protoss': 'P',
    'Terran': 'T',
    'Zerg': 'Z',
    'Random': 'R',
}

# Map aiarena result types to our Match.Result values
# BotTato is always Player1.
RESULT_MAP = {
    'Player1Win': 'Victory',
    'Player2Win': 'Defeat',
    'Player1Crash': 'Crash',
    'Player2Crash': 'Victory',  # opponent crashed = we win
    'Player1TimeOut': 'Defeat',
    'Player2TimeOut': 'Victory',  # opponent timed out
    'Tie': 'Tie',
    'InitializationError': 'Crash',
    'Error': 'Crash',
}


def _is_junction(path: str) -> bool:
    """Check if a path is an NTFS junction or reparse point (Windows only).

    Junctions look like directories on the host but are not followed
    by Docker bind mounts on Windows.
    """
    try:
        st = os.lstat(path)
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


def _resolve_bot_host_path(bot_dir_name: str) -> str | None:
    """Resolve the actual host filesystem path for a bot directory.

    Checks multiple known locations in priority order, skipping NTFS
    junctions (which Docker on Windows cannot follow inside bind mounts).

    Returns the absolute path, or None if not found.
    """
    candidates = [
        os.path.join(AIARENA_BOTS_DIR, bot_dir_name),
        os.path.join(REPO_ROOT, 'other_bots', bot_dir_name),
    ]
    # Prefer real directories over junctions
    for path in candidates:
        if os.path.isdir(path) and not _is_junction(path):
            return os.path.normpath(path)
    # Fallback: accept junctions (they work on host, just not in Docker)
    for path in candidates:
        if os.path.isdir(path):
            return os.path.normpath(path)
    return None


def _is_mirror_match(opponent_dir_name: str) -> bool:
    """Detect whether the opponent is another copy of BotTato.

    Returns True if the opponent directory name is BotTato itself, or if
    it resolves to the same host path as BotTato (e.g. a user-created copy
    that is actually a junction/symlink back to the same directory).
    """
    if opponent_dir_name in (BOTTATO_NAME, BOTTATO_MIRROR_NAME):
        return True
    opponent_path = _resolve_bot_host_path(opponent_dir_name)
    if opponent_path:
        bot_src_norm = os.path.normpath(BOT_SRC_DIR)
        if opponent_path == bot_src_norm:
            return True
    return False


def _ensure_mirror_overlay() -> None:
    """Ensure the BotTato_p2 overlay directory has the required files.

    The overlay only contains aiarena-specific files (run.py,
    requirements.txt, ladderbots.json with renamed key).  If any are
    missing, copy them from the BotTato overlay directory.
    """
    src = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    dst = os.path.join(AIARENA_BOTS_DIR, BOTTATO_MIRROR_NAME)

    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{BOTTATO_NAME} overlay directory not found. '
            f'Run prepare_bottato.py first.'
        )

    os.makedirs(dst, exist_ok=True)

    # Copy run.py and requirements.txt from BotTato overlay if missing
    for filename in ('run.py', 'requirements.txt'):
        dst_file = os.path.join(dst, filename)
        if not os.path.isfile(dst_file):
            src_file = os.path.join(src, filename)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, dst_file)

    # ladderbots.json with renamed key
    dst_lb = os.path.join(dst, 'ladderbots.json')
    if not os.path.isfile(dst_lb):
        src_lb = os.path.join(src, 'ladderbots.json')
        if os.path.isfile(src_lb):
            with open(src_lb) as f:
                lb_data = json.load(f)
            if 'Bots' in lb_data and BOTTATO_NAME in lb_data['Bots']:
                lb_data['Bots'][BOTTATO_MIRROR_NAME] = lb_data['Bots'].pop(BOTTATO_NAME)
            with open(dst_lb, 'w') as f:
                json.dump(lb_data, f, indent=4)


def _ensure_version_overlay(short_hash: str) -> str:
    """Ensure an overlay directory exists for a past BotTato version.

    Creates ``aiarena/bots/BotTato_v_<short_hash>/`` with the same
    aiarena-specific files as the mirror overlay (run.py, requirements.txt,
    ladderbots.json) but keyed to the version-specific bot name.

    Returns the bot name used in the matches file / compose override
    (e.g. ``BotTato_v_d019795``).
    """
    bot_name = f'{BOTTATO_VERSION_PREFIX}{short_hash}'
    src = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    dst = os.path.join(AIARENA_BOTS_DIR, bot_name)

    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{BOTTATO_NAME} overlay directory not found. '
            f'Run prepare_bottato.py first.'
        )

    os.makedirs(dst, exist_ok=True)

    # Always refresh overlay files so they stay current with the BotTato
    # overlay (run.py compilation logic, requirements, etc.)
    for filename in ('run.py', 'requirements.txt'):
        src_file = os.path.join(src, filename)
        dst_file = os.path.join(dst, filename)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, dst_file)

    # ladderbots.json with version-specific bot key
    src_lb = os.path.join(src, 'ladderbots.json')
    if os.path.isfile(src_lb):
        with open(src_lb) as f:
            lb_data = json.load(f)
        if 'Bots' in lb_data and BOTTATO_NAME in lb_data['Bots']:
            lb_data['Bots'][bot_name] = lb_data['Bots'].pop(BOTTATO_NAME)
        dst_lb = os.path.join(dst, 'ladderbots.json')
        with open(dst_lb, 'w') as f:
            json.dump(lb_data, f, indent=4)

    return bot_name


def _bottato_volume_mounts(bot_name: str) -> list[str]:
    """Generate Docker Compose volume mount lines for a BotTato instance.

    Mounts the live ``bot/`` source tree as the base, then overlays
    ``python_sc2/sc2/`` and the aiarena-specific files (run.py,
    requirements.txt, ladderbots.json) on top.  This means bot code
    changes take effect immediately without re-running prepare_bottato.
    """
    bot_src = BOT_SRC_DIR.replace('\\', '/')
    sc2_src = SC2_SRC_DIR.replace('\\', '/')
    overlay = os.path.join(AIARENA_BOTS_DIR, bot_name).replace('\\', '/')
    return [
        f'      - "{bot_src}:/bots/{bot_name}"',
        f'      - "{sc2_src}:/bots/{bot_name}/sc2"',
        f'      - "{overlay}/run.py:/bots/{bot_name}/run.py"',
        f'      - "{overlay}/requirements.txt:/bots/{bot_name}/requirements.txt"',
        f'      - "{overlay}/ladderbots.json:/bots/{bot_name}/ladderbots.json"',
    ]


def _past_version_volume_mounts(bot_name: str, cache_path: str) -> list[str]:
    """Generate Docker Compose volume mount lines for a past BotTato version.

    Similar to ``_bottato_volume_mounts`` but uses a cached copy of the bot
    source from a previous commit instead of the live ``bot/`` tree.  The
    current ``python_sc2/sc2/`` is still mounted on top so all versions use
    the same SC2 client library.
    """
    cached_src = cache_path.replace('\\', '/')
    sc2_src = SC2_SRC_DIR.replace('\\', '/')
    overlay = os.path.join(AIARENA_BOTS_DIR, bot_name).replace('\\', '/')
    return [
        f'      - "{cached_src}:/bots/{bot_name}"',
        f'      - "{sc2_src}:/bots/{bot_name}/sc2"',
        f'      - "{overlay}/run.py:/bots/{bot_name}/run.py"',
        f'      - "{overlay}/requirements.txt:/bots/{bot_name}/requirements.txt"',
        f'      - "{overlay}/ladderbots.json:/bots/{bot_name}/ladderbots.json"',
    ]


def _write_compose_override(
    run_dir: str,
    bot2_name: str,
    bot2_host_path: str | None,
    *,
    is_mirror: bool = False,
    is_past_version: bool = False,
    past_version_cache_path: str | None = None,
) -> None:
    """Generate docker-compose.override.yml with per-bot volume mounts.

    Bot 1 (BotTato) always uses live source mounts via
    ``_bottato_volume_mounts``.  Bot 2 is either:
    - A regular opponent (single directory mount)
    - A mirror match (live mounts + custom Dockerfile)
    - A past version (cached source + current sc2 + custom Dockerfile)
    """
    lines = [
        'services:',
        '  bot_controller1:',
        '    volumes:',
    ]
    lines += _bottato_volume_mounts(BOTTATO_NAME)

    lines.append('  bot_controller2:')
    if is_past_version:
        assert past_version_cache_path is not None
        lines += [
            '    build:',
            '      context: .',
            '      dockerfile: Dockerfile.bottato',
            '    volumes:',
        ]
        lines += _past_version_volume_mounts(bot2_name, past_version_cache_path)
    elif is_mirror:
        lines += [
            '    build:',
            '      context: .',
            '      dockerfile: Dockerfile.bottato',
            '    volumes:',
        ]
        lines += _bottato_volume_mounts(BOTTATO_MIRROR_NAME)
    else:
        assert bot2_host_path is not None
        b2 = bot2_host_path.replace('\\', '/')
        lines += [
            '    volumes:',
            f'      - "{b2}:/bots/{bot2_name}"',
        ]
    lines.append('')  # trailing newline

    override_path = os.path.join(run_dir, 'docker-compose.override.yml')
    with open(override_path, 'w') as f:
        f.write('\n'.join(lines))


def get_available_aiarena_bots() -> list[str]:
    """Return directory names under aiarena/bots/ that have a ladderbots.json.

    Excludes the internal mirror copy (BotTato_p2) which is an
    implementation detail of self-play — users should register BotTato
    as the opponent and mirror detection handles the rest.
    """
    if not os.path.isdir(AIARENA_BOTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(AIARENA_BOTS_DIR)
        if (
            d != BOTTATO_MIRROR_NAME
            and os.path.isdir(os.path.join(AIARENA_BOTS_DIR, d))
            and os.path.isfile(os.path.join(AIARENA_BOTS_DIR, d, 'ladderbots.json'))
        )
    )


def validate_bot_directory(bot_dir_name: str) -> str | None:
    """Check that a bot directory exists and has ladderbots.json.

    Returns None if valid, or an error message string.
    """
    bot_path = os.path.join(AIARENA_BOTS_DIR, bot_dir_name)
    if not os.path.isdir(bot_path):
        return f'Bot directory not found: {bot_dir_name}'

    ladderbots_path = os.path.join(bot_path, 'ladderbots.json')
    if not os.path.isfile(ladderbots_path):
        return f'ladderbots.json not found in {bot_dir_name}/'

    return None


def read_ladderbots_json(bot_dir_name: str) -> dict | None:
    """Read and parse ladderbots.json for a bot directory."""
    ladderbots_path = os.path.join(AIARENA_BOTS_DIR, bot_dir_name, 'ladderbots.json')
    try:
        with open(ladderbots_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _create_run_dir(match_id: int) -> str:
    """Create an isolated per-match run directory.

    Each match gets its own directory under ``aiarena/runs/<match_id>/``
    containing copies of the base compose files and empty output
    directories.  This allows matches to run concurrently without
    conflicting on shared files like ``matches``, ``results.json``,
    ``docker-compose.override.yml``, or ``logs/``.

    Returns the absolute path to the run directory.
    """
    run_dir = os.path.join(AIARENA_RUNS_DIR, str(match_id))
    os.makedirs(run_dir, exist_ok=True)

    # Copy base infrastructure files into the run directory
    for filename in _BASE_FILES:
        src = os.path.join(AIARENA_DIR, filename)
        dst = os.path.join(run_dir, filename)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)

    # Create output directories that Docker will bind-mount into
    os.makedirs(os.path.join(run_dir, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(run_dir, 'replays'), exist_ok=True)

    # Write empty results.json
    results_path = os.path.join(run_dir, 'results.json')
    with open(results_path, 'w') as f:
        json.dump({"results": []}, f)

    return run_dir


def _write_matches_file(
    run_dir: str,
    bot1_name: str,
    bot1_race: str,
    bot1_type: str,
    bot2_name: str,
    bot2_race: str,
    bot2_type: str,
    map_name: str,
) -> None:
    """Write the aiarena matches file for a single match.

    Format: Bot1ID,Bot1Name,Bot1Race,Bot1Type,Bot2ID,Bot2Name,Bot2Race,Bot2Type,Map
    Bot ID and Bot Name are the same (the directory name).
    """
    line = (
        f"{bot1_name},{bot1_name},{bot1_race},{bot1_type},"
        f"{bot2_name},{bot2_name},{bot2_race},{bot2_type},"
        f"{map_name}"
    )
    matches_path = os.path.join(run_dir, 'matches')
    with open(matches_path, 'w') as f:
        f.write(line + '\n')


def _parse_results(run_dir: str) -> dict | None:
    """Parse results.json and return the first result entry, or None.

    Expected format (written by aiarena proxy_controller):
    {
        "results": [
            {
                "match": <int>,
                "type": "<AiArenaResult>",
                "game_steps": <int>,
                "bot1_avg_step_time": <float|null>,
                "bot2_avg_step_time": <float|null>
            }
        ]
    }
    """
    results_path = os.path.join(run_dir, 'results.json')
    try:
        with open(results_path) as f:
            data = json.load(f)
        results = data.get('results', [])
        if results:
            return results[0]
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _map_result_to_match(aiarena_result: str) -> str:
    """Convert an aiarena result type string to our Match.Result value."""
    return RESULT_MAP.get(aiarena_result, 'Crash')


def _game_steps_to_seconds(game_steps: int) -> int:
    """Convert SC2 game steps (loops) to in-game seconds.

    SC2 "faster" speed runs at 22.4 game loops per second.
    """
    return int(game_steps / 22.4)


def get_run_dir(match_id: int) -> str:
    """Return the run directory path for a match."""
    return os.path.join(AIARENA_RUNS_DIR, str(match_id))


def get_replay_path(match_id: int) -> str | None:
    """Return the path to the replay file for a match, or None."""
    replay_dir = os.path.join(get_run_dir(match_id), 'replays')
    pattern = os.path.join(replay_dir, '*.SC2Replay')
    replays = glob.glob(pattern)
    return replays[0] if replays else None


def get_match_log_path(match_id: int) -> str | None:
    """Return the path to the docker compose output log, or None."""
    log_path = os.path.join(get_run_dir(match_id), 'compose_output.log')
    if os.path.isfile(log_path):
        return log_path
    return None


def get_bot_log_path(match_id: int, bot_name: str) -> str | None:
    """Return the path to a bot's stderr log from the run directory, or None."""
    logs_dir = os.path.join(get_run_dir(match_id), 'logs')
    for controller in ('bot_controller1', 'bot_controller2'):
        stderr_path = os.path.join(logs_dir, controller, bot_name, 'stderr.log')
        if os.path.isfile(stderr_path):
            return stderr_path
    return None


def start_aiarena_match(
    match: Match,
    custom_bot: CustomBot,
    map_name: str | None = None,
) -> None:
    """Launch an aiarena match in a background thread.

    Each match gets its own run directory under ``aiarena/runs/<match_id>/``
    so multiple matches can run concurrently without conflicting.

    The match record should already exist with status 'Pending'.
    """
    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    # Update the match with the chosen map
    match.map_name = map_name
    match.save()

    opponent_race_code = RACE_TO_CODE.get(custom_bot.race, 'R')
    opponent_type = custom_bot.aiarena_bot_type or 'python'
    opponent_dir_name = custom_bot.bot_directory

    # Detect mirror/self-play match: opponent resolves to the same bot as
    # BotTato.  The aiarena proxy routes by name so both players can't share
    # the same name.  We use the BotTato_p2 mirror copy for bot2 instead.
    is_mirror = _is_mirror_match(opponent_dir_name)
    if is_mirror:
        _ensure_mirror_overlay()
        opponent_dir_name = BOTTATO_MIRROR_NAME

    # Verify BotTato overlay exists
    bottato_overlay = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    if not os.path.isdir(bottato_overlay):
        raise FileNotFoundError(
            f'BotTato overlay directory not found. Run prepare_bottato.py first.'
        )

    # Resolve opponent host path (not needed for mirror — handled by live mounts)
    opponent_path = None
    if not is_mirror:
        opponent_path = _resolve_bot_host_path(opponent_dir_name)
        if not opponent_path:
            raise FileNotFoundError(
                f'Bot directory not found for "{opponent_dir_name}". '
                f'Expected in aiarena/bots/{opponent_dir_name}/ or '
                f'other_bots/{opponent_dir_name}/'
            )

    match_id = match.id

    # Create isolated run directory for this match
    run_dir = _create_run_dir(match_id)

    # Set up the match
    _write_matches_file(
        run_dir,
        bot1_name=BOTTATO_NAME,
        bot1_race=BOTTATO_RACE,
        bot1_type=BOTTATO_TYPE,
        bot2_name=opponent_dir_name,
        bot2_race=opponent_race_code,
        bot2_type=opponent_type,
        map_name=map_name,
    )

    # Generate per-match compose override with live source mounts for BotTato.
    # Mirror matches also use live mounts + custom image for bot_controller2.
    _write_compose_override(
        run_dir,
        bot2_name=opponent_dir_name,
        bot2_host_path=opponent_path,
        is_mirror=is_mirror,
    )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _run_match():
        """Background thread: run docker compose and process results."""
        try:
            with open(log_file_path, 'w') as log_file:
                subprocess.run(
                    [
                        'docker', 'compose',
                        '-f', 'docker-compose.yml',
                        '-f', 'docker-compose.override.yml',
                        '-p', f'aiarena_{match_id}',
                        'up', '--abort-on-container-exit',
                    ],
                    cwd=run_dir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=7200,  # 2 hour timeout
                )

            # Docker compose finished — parse results
            aiarena_result = _parse_results(run_dir)

            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
            except MatchModel.DoesNotExist:
                return

            if aiarena_result:
                result_type = aiarena_result.get('type', 'Error')
                game_steps = aiarena_result.get('game_steps', 0)

                match_obj.result = _map_result_to_match(result_type)
                if game_steps > 0:
                    match_obj.duration_in_game_time = _game_steps_to_seconds(game_steps)
            else:
                match_obj.result = 'Crash'

            match_obj.end_timestamp = timezone.now()
            match_obj.save()

            # Bring down containers; artifacts remain in run_dir
            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=run_dir,
                capture_output=True,
                timeout=120,
            )

        except subprocess.TimeoutExpired:
            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
                match_obj.result = 'Crash'
                match_obj.end_timestamp = timezone.now()
                match_obj.save()
            except MatchModel.DoesNotExist:
                pass

            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=run_dir,
                capture_output=True,
                timeout=60,
            )

        except Exception:
            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
                match_obj.result = 'Crash'
                match_obj.end_timestamp = timezone.now()
                match_obj.save()
            except MatchModel.DoesNotExist:
                pass

    thread = threading.Thread(target=_run_match, daemon=True)
    thread.start()


def start_past_version_match(
    match: Match,
    commit_hash: str,
    short_hash: str,
    map_name: str | None = None,
) -> None:
    """Launch an aiarena match of current BotTato vs a past version.

    The past version's bot code is extracted from git history into a
    cache directory.  The current ``python_sc2/sc2`` is mounted on top
    so all versions share the same SC2 client library.

    The match record should already exist with status ``'Pending'``.
    """
    from . import bot_versions

    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    match.map_name = map_name
    match.save()

    # Extract (or reuse) cached bot source for this commit
    cache_path = bot_versions.get_or_create_version_cache(commit_hash)

    # Create overlay directory with aiarena-specific files
    opponent_bot_name = _ensure_version_overlay(short_hash)

    # Verify BotTato overlay exists
    bottato_overlay = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    if not os.path.isdir(bottato_overlay):
        raise FileNotFoundError(
            f'BotTato overlay directory not found. Run prepare_bottato.py first.'
        )

    match_id = match.id

    # Create isolated run directory for this match
    run_dir = _create_run_dir(match_id)

    # Set up the match file
    _write_matches_file(
        run_dir,
        bot1_name=BOTTATO_NAME,
        bot1_race=BOTTATO_RACE,
        bot1_type=BOTTATO_TYPE,
        bot2_name=opponent_bot_name,
        bot2_race=BOTTATO_RACE,  # past version is also Terran
        bot2_type=BOTTATO_TYPE,
        map_name=map_name,
    )

    # Generate compose override with cached source for bot2
    _write_compose_override(
        run_dir,
        bot2_name=opponent_bot_name,
        bot2_host_path=None,
        is_past_version=True,
        past_version_cache_path=cache_path,
    )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _run_match():
        """Background thread: run docker compose and process results."""
        try:
            with open(log_file_path, 'w') as log_file:
                subprocess.run(
                    [
                        'docker', 'compose',
                        '-f', 'docker-compose.yml',
                        '-f', 'docker-compose.override.yml',
                        '-p', f'aiarena_{match_id}',
                        'up', '--abort-on-container-exit',
                    ],
                    cwd=run_dir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=7200,
                )

            aiarena_result = _parse_results(run_dir)

            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
            except MatchModel.DoesNotExist:
                return

            if aiarena_result:
                result_type = aiarena_result.get('type', 'Error')
                game_steps = aiarena_result.get('game_steps', 0)

                match_obj.result = _map_result_to_match(result_type)
                if game_steps > 0:
                    match_obj.duration_in_game_time = _game_steps_to_seconds(
                        game_steps
                    )
            else:
                match_obj.result = 'Crash'

            match_obj.end_timestamp = timezone.now()
            match_obj.save()

            # Bring down containers; artifacts remain in run_dir
            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=run_dir,
                capture_output=True,
                timeout=120,
            )

        except subprocess.TimeoutExpired:
            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
                match_obj.result = 'Crash'
                match_obj.end_timestamp = timezone.now()
                match_obj.save()
            except MatchModel.DoesNotExist:
                pass

            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=run_dir,
                capture_output=True,
                timeout=60,
            )

        except Exception:
            from .models import Match as MatchModel

            try:
                match_obj = MatchModel.objects.get(id=match_id)
                match_obj.result = 'Crash'
                match_obj.end_timestamp = timezone.now()
                match_obj.save()
            except MatchModel.DoesNotExist:
                pass

    thread = threading.Thread(target=_run_match, daemon=True)
    thread.start()

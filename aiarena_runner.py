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
import logging
import os
import random
import shutil
import stat
import subprocess
import threading
from typing import TYPE_CHECKING

from django.utils import timezone

logger = logging.getLogger('test_lab')

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


def scan_directory_symlinks(source_path: str) -> list[dict[str, str]]:
    """Scan a directory for symlinks/junctions that Docker can't follow.

    Docker on Windows cannot follow NTFS junctions or symlinks inside
    bind mounts.  This function detects them so they can be mounted as
    separate volumes.

    Returns a list of ``{"name": "<entry>", "target": "<real_path>"}``
    for each symlink or junction found in the top level of *source_path*.
    """
    results: list[dict[str, str]] = []
    if not os.path.isdir(source_path):
        return results
    for entry in os.scandir(source_path):
        if entry.is_symlink() or _is_junction(entry.path):
            target = os.path.realpath(entry.path)
            results.append({
                'name': entry.name,
                'target': os.path.normpath(target),
            })
    return results


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


def _is_mirror_match(test_bot: CustomBot, opponent_dir_name: str) -> bool:
    """Detect whether the opponent is the same bot as the test subject.

    Returns True if the opponent directory name matches the test bot,
    or if it resolves to the same host path (e.g. a junction/symlink).
    """
    mirror_name = f'{test_bot.name}_p2'
    if opponent_dir_name in (test_bot.bot_directory, test_bot.name, mirror_name):
        return True
    if test_bot.source_path:
        opponent_path = _resolve_bot_host_path(opponent_dir_name)
        if opponent_path:
            if os.path.normpath(opponent_path) == os.path.normpath(test_bot.source_path):
                return True
    return False


def _ensure_mirror_overlay(test_bot: CustomBot) -> str:
    """Ensure a mirror overlay directory exists for the test bot.

    Creates ``aiarena/bots/<BotName>_p2/`` with overlay files cloned
    from the test bot's overlay, but with the bot key renamed in
    ladderbots.json so the proxy can route the two players.

    Returns the mirror bot name (e.g. ``BotTato_p2``).
    """
    bot_dir = test_bot.bot_directory or test_bot.name
    mirror_name = f'{test_bot.name}_p2'
    src = os.path.join(AIARENA_BOTS_DIR, bot_dir)
    dst = os.path.join(AIARENA_BOTS_DIR, mirror_name)

    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{bot_dir} overlay directory not found in aiarena/bots/.'
        )

    os.makedirs(dst, exist_ok=True)

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
            if 'Bots' in lb_data and bot_dir in lb_data['Bots']:
                lb_data['Bots'][mirror_name] = lb_data['Bots'].pop(bot_dir)
            with open(dst_lb, 'w') as f:
                json.dump(lb_data, f, indent=4)

    return mirror_name


def _ensure_version_overlay(test_bot: CustomBot, short_hash: str) -> str:
    """Ensure an overlay directory for a past version of the test bot.

    Creates ``aiarena/bots/<BotName>_v_<short_hash>/`` with overlay
    files cloned from the test bot's overlay.

    Returns the version-specific bot name.
    """
    bot_dir = test_bot.bot_directory or test_bot.name
    bot_name = f'{test_bot.name}_v_{short_hash}'
    src = os.path.join(AIARENA_BOTS_DIR, bot_dir)
    dst = os.path.join(AIARENA_BOTS_DIR, bot_name)

    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{bot_dir} overlay directory not found in aiarena/bots/.'
        )

    os.makedirs(dst, exist_ok=True)

    for filename in ('run.py', 'requirements.txt'):
        src_file = os.path.join(src, filename)
        dst_file = os.path.join(dst, filename)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, dst_file)

    # ladderbots.json with version-specific key
    src_lb = os.path.join(src, 'ladderbots.json')
    if os.path.isfile(src_lb):
        with open(src_lb) as f:
            lb_data = json.load(f)
        if 'Bots' in lb_data and bot_dir in lb_data['Bots']:
            lb_data['Bots'][bot_name] = lb_data['Bots'].pop(bot_dir)
        dst_lb = os.path.join(dst, 'ladderbots.json')
        with open(dst_lb, 'w') as f:
            json.dump(lb_data, f, indent=4)

    return bot_name


def _test_bot_volume_mounts(test_bot: CustomBot, aiarena_name: str) -> list[str]:
    """Generate Docker Compose volume mount lines for a test subject bot.

    Mounts the live source directory as the base, then mounts symlink
    targets separately (Docker on Windows can't follow junctions), and
    overlays any aiarena-specific files (run.py, requirements.txt,
    ladderbots.json) from the bot's overlay directory.
    """
    source = test_bot.source_path.replace('\\', '/')
    mounts = [f'      - "{source}:/bots/{aiarena_name}"']

    # Mount symlink/junction targets explicitly
    for link in test_bot.symlink_mounts or []:
        name = link['name']
        target = link['target'].replace('\\', '/')
        mounts.append(f'      - "{target}:/bots/{aiarena_name}/{name}"')

    # Overlay files from aiarena/bots/<dir>/
    overlay_dir = os.path.join(AIARENA_BOTS_DIR, aiarena_name)
    for filename in ('run.py', 'requirements.txt', 'ladderbots.json'):
        overlay_file = os.path.join(overlay_dir, filename)
        if os.path.isfile(overlay_file):
            f_unix = overlay_file.replace('\\', '/')
            mounts.append(f'      - "{f_unix}:/bots/{aiarena_name}/{filename}"')

    return mounts


def _past_version_volume_mounts(
    test_bot: CustomBot, aiarena_name: str, cache_path: str,
) -> list[str]:
    """Generate volume mounts for a past version of a test subject bot.

    Uses the cached source from a previous commit as the base, then
    mounts the current symlink targets on top (shared libraries like
    python_sc2/sc2 should be the same across versions).  Overlay files
    are also applied.
    """
    cached_src = cache_path.replace('\\', '/')
    mounts = [f'      - "{cached_src}:/bots/{aiarena_name}"']

    # Symlink mounts from the current host (not from the cache)
    for link in test_bot.symlink_mounts or []:
        name = link['name']
        target = link['target'].replace('\\', '/')
        mounts.append(f'      - "{target}:/bots/{aiarena_name}/{name}"')

    overlay_dir = os.path.join(AIARENA_BOTS_DIR, aiarena_name)
    for filename in ('run.py', 'requirements.txt', 'ladderbots.json'):
        overlay_file = os.path.join(overlay_dir, filename)
        if os.path.isfile(overlay_file):
            f_unix = overlay_file.replace('\\', '/')
            mounts.append(f'      - "{f_unix}:/bots/{aiarena_name}/{filename}"')

    return mounts


def _write_compose_override(
    run_dir: str,
    *,
    test_bot: CustomBot,
    test_bot_aiarena_name: str,
    bot2_name: str,
    bot2_host_path: str | None,
    is_mirror: bool = False,
    mirror_aiarena_name: str | None = None,
    is_past_version: bool = False,
    past_version_cache_path: str | None = None,
) -> None:
    """Generate docker-compose.override.yml with per-bot volume mounts.

    Bot 1 (test subject) uses live source mounts via
    ``_test_bot_volume_mounts``.  Bot 2 is either:
    - A regular opponent (single directory mount)
    - A mirror match (live mounts + optional custom Dockerfile)
    - A past version (cached source + symlink mounts + optional Dockerfile)
    """
    lines = [
        'services:',
        '  bot_controller1:',
        '    volumes:',
    ]
    lines += _test_bot_volume_mounts(test_bot, test_bot_aiarena_name)

    lines.append('  bot_controller2:')
    dockerfile = test_bot.dockerfile
    if is_past_version:
        assert past_version_cache_path is not None
        if dockerfile:
            lines += [
                '    build:',
                '      context: .',
                f'      dockerfile: {dockerfile}',
            ]
        lines += ['    volumes:']
        lines += _past_version_volume_mounts(test_bot, bot2_name, past_version_cache_path)
    elif is_mirror:
        assert mirror_aiarena_name is not None
        if dockerfile:
            lines += [
                '    build:',
                '      context: .',
                f'      dockerfile: {dockerfile}',
            ]
        lines += ['    volumes:']
        lines += _test_bot_volume_mounts(test_bot, mirror_aiarena_name)
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

    Excludes internal mirror/version copies (names ending with ``_p2`` or
    matching ``*_v_*``) which are implementation details of self-play and
    past-version testing.
    """
    if not os.path.isdir(AIARENA_BOTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(AIARENA_BOTS_DIR)
        if (
            not d.endswith('_p2')
            and '_v_' not in d
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
    opponent_bot: CustomBot,
    test_bot: CustomBot | None = None,
    map_name: str | None = None,
) -> None:
    """Launch an aiarena match in a background thread.

    *test_bot* is the bot being tested (Player 1).  If ``None``, falls
    back to the legacy BotTato constants for backward compatibility.

    *opponent_bot* is the opponent (Player 2).

    Each match gets its own run directory under ``aiarena/runs/<match_id>/``
    so multiple matches can run concurrently without conflicting.

    The match record should already exist with status 'Pending'.
    """
    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    match.map_name = map_name
    match.save()

    opponent_race_code = RACE_TO_CODE.get(opponent_bot.race, 'R')
    opponent_type = opponent_bot.aiarena_bot_type or 'python'
    opponent_dir_name = opponent_bot.bot_directory

    # Determine test bot info
    if test_bot and test_bot.is_test_subject and test_bot.source_path:
        test_bot_dir = test_bot.bot_directory or test_bot.name
        test_bot_race = RACE_TO_CODE.get(test_bot.race, 'R')
        test_bot_type = test_bot.aiarena_bot_type or 'python'
    else:
        # Legacy fallback: use BotTato constants
        test_bot_dir = BOTTATO_NAME
        test_bot_race = BOTTATO_RACE
        test_bot_type = BOTTATO_TYPE

    # Detect mirror/self-play match
    is_mirror = (
        _is_mirror_match(test_bot, opponent_dir_name)
        if test_bot and test_bot.is_test_subject
        else opponent_dir_name in (BOTTATO_NAME, BOTTATO_MIRROR_NAME)
    )
    mirror_name: str | None = None
    if is_mirror:
        if test_bot and test_bot.is_test_subject:
            mirror_name = _ensure_mirror_overlay(test_bot)
        else:
            mirror_name = _ensure_mirror_overlay_legacy()
        opponent_dir_name = mirror_name

    # Verify overlay exists
    overlay_dir = os.path.join(AIARENA_BOTS_DIR, test_bot_dir)
    if not os.path.isdir(overlay_dir):
        raise FileNotFoundError(
            f'{test_bot_dir} overlay directory not found in aiarena/bots/.'
        )

    # Resolve opponent host path (not needed for mirror)
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
    run_dir = _create_run_dir(match_id)

    _write_matches_file(
        run_dir,
        bot1_name=test_bot_dir,
        bot1_race=test_bot_race,
        bot1_type=test_bot_type,
        bot2_name=opponent_dir_name,
        bot2_race=opponent_race_code,
        bot2_type=opponent_type,
        map_name=map_name,
    )

    if test_bot and test_bot.is_test_subject:
        _write_compose_override(
            run_dir,
            test_bot=test_bot,
            test_bot_aiarena_name=test_bot_dir,
            bot2_name=opponent_dir_name,
            bot2_host_path=opponent_path,
            is_mirror=is_mirror,
            mirror_aiarena_name=mirror_name,
        )
    else:
        _write_compose_override_legacy(
            run_dir,
            bot2_name=opponent_dir_name,
            bot2_host_path=opponent_path,
            is_mirror=is_mirror,
        )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _run_match():
        _run_docker_match(run_dir, match_id, log_file_path)

    thread = threading.Thread(target=_run_match, daemon=True)
    thread.start()


def start_past_version_match(
    match: Match,
    commit_hash: str,
    short_hash: str,
    test_bot: CustomBot | None = None,
    map_name: str | None = None,
) -> None:
    """Launch a match of the current test bot vs a past version.

    The past version's bot code is extracted from git history into a
    cache directory.  Symlink targets (e.g. shared libraries) are mounted
    from the current host so all versions share the same runtime deps.

    If *test_bot* is ``None``, falls back to BotTato legacy behavior.
    """
    from . import bot_versions

    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    match.map_name = map_name
    match.save()

    # Determine test bot info
    if test_bot and test_bot.is_test_subject and test_bot.source_path:
        test_bot_dir = test_bot.bot_directory or test_bot.name
        test_bot_race = RACE_TO_CODE.get(test_bot.race, 'R')
        test_bot_type = test_bot.aiarena_bot_type or 'python'
        repo_path = test_bot.git_repo_path
    else:
        test_bot_dir = BOTTATO_NAME
        test_bot_race = BOTTATO_RACE
        test_bot_type = BOTTATO_TYPE
        repo_path = ''

    # Extract (or reuse) cached bot source for this commit
    cache_path = bot_versions.get_or_create_version_cache(
        commit_hash, repo_path=repo_path or None,
    )

    # Create overlay directory with aiarena-specific files
    if test_bot and test_bot.is_test_subject:
        opponent_bot_name = _ensure_version_overlay(test_bot, short_hash)
    else:
        opponent_bot_name = _ensure_version_overlay_legacy(short_hash)

    # Verify overlay exists
    overlay_dir = os.path.join(AIARENA_BOTS_DIR, test_bot_dir)
    if not os.path.isdir(overlay_dir):
        raise FileNotFoundError(
            f'{test_bot_dir} overlay directory not found in aiarena/bots/.'
        )

    match_id = match.id
    run_dir = _create_run_dir(match_id)

    _write_matches_file(
        run_dir,
        bot1_name=test_bot_dir,
        bot1_race=test_bot_race,
        bot1_type=test_bot_type,
        bot2_name=opponent_bot_name,
        bot2_race=test_bot_race,  # past version has same race
        bot2_type=test_bot_type,
        map_name=map_name,
    )

    if test_bot and test_bot.is_test_subject:
        _write_compose_override(
            run_dir,
            test_bot=test_bot,
            test_bot_aiarena_name=test_bot_dir,
            bot2_name=opponent_bot_name,
            bot2_host_path=None,
            is_past_version=True,
            past_version_cache_path=cache_path,
        )
    else:
        _write_compose_override_legacy(
            run_dir,
            bot2_name=opponent_bot_name,
            bot2_host_path=None,
            is_past_version=True,
            past_version_cache_path=cache_path,
        )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _run_match():
        _run_docker_match(run_dir, match_id, log_file_path)

    thread = threading.Thread(target=_run_match, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Legacy helpers (BotTato-specific, used when test_bot is None)
# ---------------------------------------------------------------------------

def _ensure_mirror_overlay_legacy() -> str:
    """Legacy: ensure BotTato_p2 overlay for backward compat."""
    src = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    dst = os.path.join(AIARENA_BOTS_DIR, BOTTATO_MIRROR_NAME)
    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{BOTTATO_NAME} overlay directory not found. Run prepare_bottato.py first.'
        )
    os.makedirs(dst, exist_ok=True)
    for filename in ('run.py', 'requirements.txt'):
        dst_file = os.path.join(dst, filename)
        if not os.path.isfile(dst_file):
            src_file = os.path.join(src, filename)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, dst_file)
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
    return BOTTATO_MIRROR_NAME


def _ensure_version_overlay_legacy(short_hash: str) -> str:
    """Legacy: ensure BotTato_v_<hash> overlay for backward compat."""
    bot_name = f'{BOTTATO_VERSION_PREFIX}{short_hash}'
    src = os.path.join(AIARENA_BOTS_DIR, BOTTATO_NAME)
    dst = os.path.join(AIARENA_BOTS_DIR, bot_name)
    if not os.path.isdir(src):
        raise FileNotFoundError(
            f'{BOTTATO_NAME} overlay directory not found. Run prepare_bottato.py first.'
        )
    os.makedirs(dst, exist_ok=True)
    for filename in ('run.py', 'requirements.txt'):
        src_file = os.path.join(src, filename)
        dst_file = os.path.join(dst, filename)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, dst_file)
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


def _write_compose_override_legacy(
    run_dir: str,
    bot2_name: str,
    bot2_host_path: str | None,
    *,
    is_mirror: bool = False,
    is_past_version: bool = False,
    past_version_cache_path: str | None = None,
) -> None:
    """Legacy compose override using hardcoded BotTato volume mounts."""
    bot_src = BOT_SRC_DIR.replace('\\', '/')
    sc2_src = SC2_SRC_DIR.replace('\\', '/')

    def _bottato_mounts(name: str) -> list[str]:
        overlay = os.path.join(AIARENA_BOTS_DIR, name).replace('\\', '/')
        return [
            f'      - "{bot_src}:/bots/{name}"',
            f'      - "{sc2_src}:/bots/{name}/sc2"',
            f'      - "{overlay}/run.py:/bots/{name}/run.py"',
            f'      - "{overlay}/requirements.txt:/bots/{name}/requirements.txt"',
            f'      - "{overlay}/ladderbots.json:/bots/{name}/ladderbots.json"',
        ]

    lines = [
        'services:',
        '  bot_controller1:',
        '    volumes:',
    ]
    lines += _bottato_mounts(BOTTATO_NAME)

    lines.append('  bot_controller2:')
    if is_past_version:
        assert past_version_cache_path is not None
        cached = past_version_cache_path.replace('\\', '/')
        overlay = os.path.join(AIARENA_BOTS_DIR, bot2_name).replace('\\', '/')
        lines += [
            '    build:',
            '      context: .',
            '      dockerfile: Dockerfile.bottato',
            '    volumes:',
            f'      - "{cached}:/bots/{bot2_name}"',
            f'      - "{sc2_src}:/bots/{bot2_name}/sc2"',
            f'      - "{overlay}/run.py:/bots/{bot2_name}/run.py"',
            f'      - "{overlay}/requirements.txt:/bots/{bot2_name}/requirements.txt"',
            f'      - "{overlay}/ladderbots.json:/bots/{bot2_name}/ladderbots.json"',
        ]
    elif is_mirror:
        lines += [
            '    build:',
            '      context: .',
            '      dockerfile: Dockerfile.bottato',
            '    volumes:',
        ]
        lines += _bottato_mounts(BOTTATO_MIRROR_NAME)
    else:
        assert bot2_host_path is not None
        b2 = bot2_host_path.replace('\\', '/')
        lines += [
            '    volumes:',
            f'      - "{b2}:/bots/{bot2_name}"',
        ]
    lines.append('')

    override_path = os.path.join(run_dir, 'docker-compose.override.yml')
    with open(override_path, 'w') as f:
        f.write('\n'.join(lines))


def _run_docker_match(run_dir: str, match_id: int, log_file_path: str) -> None:
    """Run a Docker match in the current thread.  Shared by both start functions.

    Docker compose is launched via ``Popen`` so the child process persists
    even if the Django dev-server auto-reloads (which kills daemon threads).
    A PID file is written so that ``collect_match_result`` can reconcile
    matches whose monitoring thread was lost.
    """
    compose_down_cmd = [
        'docker', 'compose',
        '-f', 'docker-compose.yml',
        '-f', 'docker-compose.override.yml',
        '-p', f'aiarena_{match_id}', 'down',
        '--rmi', 'local',
    ]

    pid_file = os.path.join(run_dir, 'docker.pid')
    proc: subprocess.Popen | None = None
    log_file = None

    logger.info('Match %d: starting docker compose in %s', match_id, run_dir)

    try:
        log_file = open(log_file_path, 'w')
        # Use CREATE_NEW_PROCESS_GROUP so docker compose is not killed when
        # the parent Python process exits (e.g. Django dev-server reload).
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
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
            creationflags=creation_flags,
        )

        # Persist PID so recovery can find the process later.
        with open(pid_file, 'w') as f:
            f.write(str(proc.pid))

        logger.info('Match %d: docker compose started (pid %d)', match_id, proc.pid)

        # Block until the process finishes (or the thread is killed).
        proc.wait(timeout=7200)
        log_file.close()

        logger.info('Match %d: docker compose exited with code %d', match_id, proc.returncode)

        _collect_and_save_result(run_dir, match_id)

    except subprocess.TimeoutExpired:
        logger.warning('Match %d: docker compose timed out after 2h', match_id)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        from .models import Match as MatchModel
        try:
            match_obj = MatchModel.objects.get(id=match_id)
            match_obj.result = 'Crash'
            match_obj.end_timestamp = timezone.now()
            match_obj.save()
        except MatchModel.DoesNotExist:
            pass

    except Exception:
        logger.exception('Match %d: unexpected error in _run_docker_match', match_id)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        from .models import Match as MatchModel
        try:
            match_obj = MatchModel.objects.get(id=match_id)
            match_obj.result = 'Crash'
            match_obj.end_timestamp = timezone.now()
            match_obj.save()
        except MatchModel.DoesNotExist:
            pass

    finally:
        # Always attempt cleanup, but never let it affect match results.
        try:
            subprocess.run(
                compose_down_cmd,
                cwd=run_dir,
                capture_output=True,
                timeout=120,
            )
        except Exception:
            pass
        # Remove PID file after cleanup.
        try:
            os.remove(pid_file)
        except OSError:
            pass


def _collect_and_save_result(run_dir: str, match_id: int) -> None:
    """Parse results.json and update the Match record in the database.

    Extracted from ``_run_docker_match`` so it can also be called by the
    stale-match recovery path (``collect_match_result``).
    """
    aiarena_result = _parse_results(run_dir)
    logger.info('Match %d: parsed results: %s', match_id, aiarena_result)

    from .models import Match as MatchModel
    try:
        match_obj = MatchModel.objects.get(id=match_id)
    except MatchModel.DoesNotExist:
        logger.error('Match %d: Match record not found in DB after game finished', match_id)
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
    logger.info(
        'Match %d: saved result=%s duration=%s',
        match_id, match_obj.result, match_obj.duration_in_game_time,
    )


def _is_process_running(pid: int) -> bool:
    """Check whether a process with the given PID is still alive."""
    if os.name == 'nt':
        # On Windows, use tasklist to check.
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def collect_match_result(match_id: int) -> str | None:
    """Check a pending match and collect its result if docker has finished.

    Returns the new result string, or ``None`` if the match is still
    running or has no run directory.

    This is the recovery path for matches whose monitoring daemon thread
    was killed (e.g. by a Django dev-server reload).
    """
    run_dir = get_run_dir(match_id)
    if not os.path.isdir(run_dir):
        return None

    pid_file = os.path.join(run_dir, 'docker.pid')

    # If a PID file exists, check whether the process is still running.
    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            if _is_process_running(pid):
                return None  # still running
        except (ValueError, OSError):
            pass

    # Process is no longer running (or never started).  Try to collect results.
    results_path = os.path.join(run_dir, 'results.json')
    log_path = os.path.join(run_dir, 'compose_output.log')

    # If the log file is empty and results are empty, docker never ran.
    log_size = 0
    try:
        log_size = os.path.getsize(log_path)
    except OSError:
        pass

    _collect_and_save_result(run_dir, match_id)

    # Clean up: docker compose down
    try:
        subprocess.run(
            [
                'docker', 'compose',
                '-f', 'docker-compose.yml',
                '-f', 'docker-compose.override.yml',
                '-p', f'aiarena_{match_id}', 'down',
                '--rmi', 'local',
            ],
            cwd=run_dir,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        pass

    # Remove PID file.
    try:
        os.remove(pid_file)
    except OSError:
        pass

    from .models import Match as MatchModel
    try:
        return MatchModel.objects.get(id=match_id).result
    except MatchModel.DoesNotExist:
        return None


def check_stale_pending_matches() -> dict[int, str]:
    """Scan for pending aiarena matches whose docker process has finished.

    Returns a dict of ``{match_id: new_result}`` for matches that were
    recovered.  Matches still running are left alone.
    """
    from .models import Match as MatchModel

    recovered: dict[int, str] = {}
    pending = MatchModel.objects.filter(result='Pending')

    for match_obj in pending:
        run_dir = get_run_dir(match_obj.id)
        if not os.path.isdir(run_dir):
            continue  # not an aiarena match, or run dir was cleaned up

        result = collect_match_result(match_obj.id)
        if result is not None:
            recovered[match_obj.id] = result

    return recovered

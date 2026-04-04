"""
AI Arena match runner for test_lab.

Handles running bot-vs-bot matches using the aiarena/local-play-bootstrap
Docker infrastructure. This runs on the HOST (not inside Docker) and is called
from Django views.

The aiarena infrastructure uses four containers:
  - sc2_controller: runs StarCraft II
  - bot_controller1: runs Bot 1 (test subject)
  - bot_controller2: runs Bot 2 (opponent)
  - proxy_controller: coordinates the match

Bots must be placed in aiarena/bots/<bot_name>/ with a ladderbots.json
or at minimum a run.py (Python bots without ladderbots.json get a default config).
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
AIARENA_PATCHES_DIR = os.path.join(AIARENA_DIR, 'patches')
AIARENA_CONFIGS_DIR = os.path.join(AIARENA_DIR, 'configs')

# Repo root (for finding live source mounts)
REPO_ROOT = os.path.normpath(os.path.join(AIARENA_DIR, '..', '..', '..', '..'))

# Base files in AIARENA_DIR that are copied into each per-match run directory
_BASE_FILES = (
    'docker-compose.yml', 'config.toml',
    'Dockerfile.proxy_fwd', 'entrypoint_proxy_fwd.sh',
)

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
# The test bot is always Player1.
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

    Looks in ``aiarena/bots/<bot_dir_name>/``, skipping NTFS junctions
    (which Docker on Windows cannot follow inside bind mounts).

    Returns the absolute path, or None if not found.
    """
    path = os.path.join(AIARENA_BOTS_DIR, bot_dir_name)
    if os.path.isdir(path) and not _is_junction(path):
        return os.path.normpath(path)
    # Fallback: accept junctions (they work on host, just not in Docker)
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

    Returns the mirror bot name (e.g. ``MyBot_p2``).
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
        else:
            lb_data = _default_ladderbots_data(bot_dir)
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
    else:
        lb_data = _default_ladderbots_data(bot_dir)
    if 'Bots' in lb_data and bot_dir in lb_data['Bots']:
        lb_data['Bots'][bot_name] = lb_data['Bots'].pop(bot_dir)
    dst_lb = os.path.join(dst, 'ladderbots.json')
    with open(dst_lb, 'w') as f:
        json.dump(lb_data, f, indent=4)

    return bot_name


def _test_bot_volume_mounts(
    test_bot: CustomBot, aiarena_name: str,
    source_override: str | None = None,
) -> list[str]:
    """Generate Docker Compose volume mount lines for a test subject bot.

    When a live source directory is available (via *source_override* or
    ``test_bot.source_path``), it is mounted as the base, symlink targets
    are mounted separately, and aiarena overlay files are layered on top.

    When no source directory is configured, the bot's ``aiarena/bots/``
    directory is mounted directly as a single volume — no overlay mounts
    are needed since all files already live there.
    """
    source = source_override or test_bot.source_path

    if not source:
        # No live source — mount the aiarena/bots/<name>/ directory directly.
        bot_dir = os.path.join(AIARENA_BOTS_DIR, aiarena_name).replace('\\', '/')
        return [f'      - "{bot_dir}:/bots/{aiarena_name}"']

    source = source.replace('\\', '/')
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


def _opponent_volume_mounts(
    opponent_bot: CustomBot, aiarena_name: str,
) -> list[str]:
    """Generate Docker Compose volume mount lines for an opponent bot.

    Resolves the opponent's full source directory (from ``source_path``
    or ``aiarena/bots/`` as a fallback) and layers aiarena overlay files
    on top — the same strategy used for test bots.

    If ``source_path`` points inside ``aiarena/bots/`` it is treated as
    an overlay directory rather than real source and the function falls
    through to ``aiarena/bots/`` discovery.
    """
    source = opponent_bot.source_path

    # Ignore source_path when it points into the aiarena overlay tree —
    # those directories hold only config files, not the full bot source.
    if source and os.path.normcase(os.path.normpath(source)).startswith(
        os.path.normcase(os.path.normpath(AIARENA_BOTS_DIR))
    ):
        source = ''

    if not source:
        # Fall back to mounting aiarena/bots/<name>/ directly
        bot_dir = os.path.join(AIARENA_BOTS_DIR, aiarena_name).replace('\\', '/')
        return [f'      - "{bot_dir}:/bots/{aiarena_name}"']

    source = source.replace('\\', '/')
    mounts = [f'      - "{source}:/bots/{aiarena_name}"']

    for link in opponent_bot.symlink_mounts or []:
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
    bot2_type: str = 'python',
    bot2_dockerfile: str = '',
    opponent_bot: CustomBot | None = None,
    is_mirror: bool = False,
    mirror_aiarena_name: str | None = None,
    is_past_version: bool = False,
    past_version_cache_path: str | None = None,
    source_override: str | None = None,
    friendly_build: str = '',
    opponent_build: str = '',
) -> None:
    """Generate docker-compose.override.yml with per-bot volume mounts.

    Bot 1 (test subject) uses live source mounts via
    ``_test_bot_volume_mounts``.  Bot 2 is either:
    - A regular opponent (source + overlay mounts via ``_opponent_volume_mounts``)
    - A mirror match (live mounts + optional custom Dockerfile)
    - A past version (cached source + symlink mounts + optional Dockerfile)

    When the test bot or opponent has a custom ``dockerfile`` set, the
    corresponding controller's image is replaced with a build directive
    so pre-installed dependencies are available.

    *source_override* is passed through to ``_test_bot_volume_mounts``
    for branch-based testing.

    *friendly_build* and *opponent_build* specify build config names
    from ``aiarena/configs/``. Config files are overlaid on top of the
    bot directory via additional volume mounts.
    """
    lines = [
        'services:',
        '  bot_controller1:',
    ]
    if test_bot.dockerfile:
        lines += [
            '    build:',
            '      context: .',
            f'      dockerfile: {test_bot.dockerfile}',
        ]
    lines.append('    volumes:')
    lines += _test_bot_volume_mounts(test_bot, test_bot_aiarena_name, source_override=source_override)
    if friendly_build:
        lines += get_build_config_volume_mounts(
            test_bot.bot_directory or test_bot.name,
            friendly_build,
            f'/bots/{test_bot_aiarena_name}',
        )

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
        lines += _test_bot_volume_mounts(test_bot, mirror_aiarena_name, source_override=source_override)
    else:
        # Non-Python bots need the proxy_fwd image (installs libs
        # required for C++/Go bots to establish TCP connections).
        effective_dockerfile = bot2_dockerfile
        if not effective_dockerfile and bot2_type != 'python':
            effective_dockerfile = 'Dockerfile.proxy_fwd'
        if effective_dockerfile:
            lines += [
                '    build:',
                '      context: .',
                f'      dockerfile: {effective_dockerfile}',
            ]
        lines.append('    volumes:')
        if opponent_bot is not None:
            lines += _opponent_volume_mounts(opponent_bot, bot2_name)
        else:
            assert bot2_host_path is not None
            b2 = bot2_host_path.replace('\\', '/')
            lines.append(f'      - "{b2}:/bots/{bot2_name}"')
    # Apply opponent build config overlay (for non-mirror, non-past-version)
    if opponent_build and opponent_bot and not is_mirror and not is_past_version:
        lines += get_build_config_volume_mounts(
            opponent_bot.bot_directory or opponent_bot.name,
            opponent_build,
            f'/bots/{bot2_name}',
        )
    lines.append('')  # trailing newline

    override_path = os.path.join(run_dir, 'docker-compose.override.yml')
    with open(override_path, 'w') as f:
        f.write('\n'.join(lines))


def _has_bot_config(bot_path: str) -> bool:
    """Return True if a bot directory has ladderbots.json or run.py."""
    return (
        os.path.isfile(os.path.join(bot_path, 'ladderbots.json'))
        or os.path.isfile(os.path.join(bot_path, 'run.py'))
    )


def _detect_bot_type(bot_path: str) -> str:
    """Detect the bot type from the contents of a bot directory.

    Priority:
    1. ladderbots.json — read the Type from the first bot entry
    2. run.py present — ``python``
    3. Folder contains a single file — ``cpplinux``
    4. Default — ``wine``
    """
    # Normalize aiarena.ai ladder type aliases to internal arenaclient type names
    _TYPE_ALIASES: dict[str, str] = {
        'binarycpp': 'cpplinux',
    }
    ladderbots_path = os.path.join(bot_path, 'ladderbots.json')
    if os.path.isfile(ladderbots_path):
        try:
            with open(ladderbots_path) as f:
                data = json.load(f)
            bots = data.get('Bots', {})
            if bots:
                bot_info = next(iter(bots.values()))
                raw_type: str = bot_info.get('Type', 'wine').lower()
                return _TYPE_ALIASES.get(raw_type) or raw_type
        except (json.JSONDecodeError, StopIteration):
            pass
        return 'wine'
    if os.path.isfile(os.path.join(bot_path, 'run.py')):
        return 'python'
    files = [f for f in os.listdir(bot_path) if os.path.isfile(os.path.join(bot_path, f))]
    if len(files) == 1:
        return 'cpplinux'
    return 'wine'


def _default_ladderbots_data(bot_name: str) -> dict:
    """Generate a default ladderbots.json dict for a Python bot with run.py."""
    return {
        'Bots': {
            bot_name: {
                'Race': 'Random',
                'Type': 'Python',
                'RootPath': './',
                'FileName': 'run.py',
            }
        }
    }


def get_available_aiarena_bots() -> list[str]:
    """Return directory names under aiarena/bots/.

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
            and not d == 'runtimes'
        )
    )


def get_available_aiarena_bot_details() -> list[dict]:
    """Return detailed info for bot directories under aiarena/bots/.

    Each entry is a dict with keys: ``directory``, ``name``, ``race``,
    ``type``, ``file_name``.  When a ``ladderbots.json`` exists the name,
    race, type, and file_name are extracted from the first bot entry;
    otherwise type is detected by heuristic (run.py → python, single
    file → cpplinux, else wine).

    Excludes internal copies (``_p2`` / ``_v_``).
    """
    if not os.path.isdir(AIARENA_BOTS_DIR):
        return []
    results: list[dict] = []
    for d in sorted(os.listdir(AIARENA_BOTS_DIR)):
        if d.endswith('_p2') or '_v_' in d or d == 'runtimes':
            continue
        bot_path = os.path.join(AIARENA_BOTS_DIR, d)
        if not os.path.isdir(bot_path):
            continue
        info: dict = {'directory': d, 'name': d, 'race': '', 'type': _detect_bot_type(bot_path), 'file_name': ''}
        ladderbots_path = os.path.join(bot_path, 'ladderbots.json')
        if os.path.isfile(ladderbots_path):
            try:
                with open(ladderbots_path) as f:
                    data = json.load(f)
                bots = data.get('Bots', {})
                if bots:
                    bot_name, bot_info = next(iter(bots.items()))
                    info['name'] = bot_name
                    info['race'] = bot_info.get('Race', '')
                    info['file_name'] = bot_info.get('FileName', '')
            except (json.JSONDecodeError, StopIteration):
                pass
        results.append(info)
    return results


def validate_bot_directory(bot_dir_name: str) -> str | None:
    """Check that a bot directory exists under aiarena/bots/.

    Returns None if valid, or an error message string.
    """
    bot_path = os.path.join(AIARENA_BOTS_DIR, bot_dir_name)
    if not os.path.isdir(bot_path):
        return f'Bot directory not found: {bot_dir_name}'

    return None


def apply_bot_patches(bot_dir_name: str) -> list[str]:
    """Copy patch files from aiarena/patches/<bot_dir_name>/ into the bot directory.

    If a matching patch folder exists, all files within it are copied
    (recursively) into aiarena/bots/<bot_dir_name>/, overwriting existing
    files.  If no bot-specific patch folder exists, falls back to the
    ``_default`` patch folder so that generic patches (e.g.
    ``run_vs_blizzard.py``) are applied to newly registered bots.

    Returns a list of relative paths that were copied.
    """
    patch_dir = os.path.join(AIARENA_PATCHES_DIR, bot_dir_name)
    if not os.path.isdir(patch_dir):
        patch_dir = os.path.join(AIARENA_PATCHES_DIR, '_default')
    if not os.path.isdir(patch_dir):
        return []

    bot_dir = os.path.join(AIARENA_BOTS_DIR, bot_dir_name)
    copied: list[str] = []
    for root, _dirs, files in os.walk(patch_dir):
        for filename in files:
            src = os.path.join(root, filename)
            rel = os.path.relpath(src, patch_dir)
            dst = os.path.join(bot_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    if copied:
        logger.info('Applied %d patch file(s) to %s: %s', len(copied), bot_dir_name, copied)
    return copied


def get_available_builds(bot_dir_name: str) -> list[str]:
    """Return available build config names for a bot.

    Scans ``aiarena/configs/<bot_dir_name>/`` for subdirectories,
    each representing a named build configuration.

    Returns a sorted list of build names. Returns empty list if no
    configs directory exists for this bot.
    """
    config_dir = os.path.join(AIARENA_CONFIGS_DIR, bot_dir_name)
    if not os.path.isdir(config_dir):
        return []
    return sorted(
        d for d in os.listdir(config_dir)
        if os.path.isdir(os.path.join(config_dir, d))
    )


def get_builds_by_race(bot_dir_name: str) -> dict[str, list[str]]:
    """Return a mapping of race → build names from ``builds.yml``.

    Reads ``aiarena/configs/<bot_dir_name>/builds.yml`` which maps race names
    (Protoss, Terran, Zerg) to build config names (subdirectories).

    Values can be a single string or a YAML list.  Returns
    ``{'Protoss': ['ProbeRush'], 'Terran': ['WorkerRush'], ...}`` or
    an empty dict if no ``builds.yml`` exists.
    """
    import yaml

    builds_path = os.path.join(AIARENA_CONFIGS_DIR, bot_dir_name, 'builds.yml')
    if not os.path.isfile(builds_path):
        return {}
    try:
        with open(builds_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        logger.warning('Failed to parse builds.yml for %s', bot_dir_name)
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str]] = {}
    for race, builds in data.items():
        race_str = str(race)
        if isinstance(builds, list):
            result[race_str] = [str(b) for b in builds]
        elif builds:
            result[race_str] = [str(builds)]
    return result


def get_build_config_volume_mounts(
    bot_dir_name: str,
    build_name: str,
    container_bot_path: str,
) -> list[str]:
    """Return Docker Compose volume mount lines for a build config overlay.

    Files in ``aiarena/configs/<bot_dir_name>/<build_name>/`` are mounted
    individually on top of the bot directory inside the container.

    *container_bot_path* is the container-side path where the bot is mounted
    (e.g. ``/bots/who`` for aiarena matches or ``/root/bot_dir`` for
    vs-computer matches).

    Returns a list of volume mount strings in docker-compose format.
    """
    if not build_name:
        return []
    config_dir = os.path.join(AIARENA_CONFIGS_DIR, bot_dir_name, build_name)
    if not os.path.isdir(config_dir):
        logger.warning(
            'Build config directory not found: %s/%s', bot_dir_name, build_name,
        )
        return []

    mounts: list[str] = []
    for root, _dirs, files in os.walk(config_dir):
        for filename in files:
            src = os.path.join(root, filename)
            rel = os.path.relpath(src, config_dir)
            host_path = src.replace('\\', '/')
            container_path = f'{container_bot_path}/{rel}'.replace('\\', '/')
            mounts.append(f'      - "{host_path}:{container_path}"')
    if mounts:
        logger.info(
            'Build config %s/%s: %d file(s) will be overlaid',
            bot_dir_name, build_name, len(mounts),
        )
    return mounts


def get_build_config_docker_args(
    bot_dir_name: str,
    build_name: str,
    container_bot_path: str = '/root/bot_dir',
) -> list[str]:
    """Return ``['-v', '<host>:<container>', ...]`` for build config overlays.

    Similar to ``get_build_config_volume_mounts`` but returns flat
    ``-v`` args for use with ``docker compose run``.
    """
    if not build_name:
        return []
    config_dir = os.path.join(AIARENA_CONFIGS_DIR, bot_dir_name, build_name)
    if not os.path.isdir(config_dir):
        logger.warning(
            'Build config directory not found: %s/%s', bot_dir_name, build_name,
        )
        return []

    args: list[str] = []
    for root, _dirs, files in os.walk(config_dir):
        for filename in files:
            src = os.path.join(root, filename)
            rel = os.path.relpath(src, config_dir)
            host_path = src.replace('\\', '/')
            container_path = f'{container_bot_path}/{rel}'.replace('\\', '/')
            args += ['-v', f'{host_path}:{container_path}']
    return args


def read_ladderbots_json(bot_dir_name: str) -> dict | None:
    """Read and parse ladderbots.json for a bot directory.

    If ladderbots.json is missing but run.py exists, returns a default
    Python bot configuration pointing to run.py.
    """
    bot_path = os.path.join(AIARENA_BOTS_DIR, bot_dir_name)
    ladderbots_path = os.path.join(bot_path, 'ladderbots.json')
    try:
        with open(ladderbots_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    # Fallback: generate default config if run.py exists
    if os.path.isfile(os.path.join(bot_path, 'run.py')):
        return _default_ladderbots_data(bot_dir_name)
    return None


def _create_run_dir(match_id: int, dockerfiles: tuple[str, ...] = ()) -> str:
    """Create an isolated per-match run directory.

    Each match gets its own directory under ``aiarena/runs/<match_id>/``
    containing copies of the base compose files and empty output
    directories.  This allows matches to run concurrently without
    conflicting on shared files like ``matches``, ``results.json``,
    ``docker-compose.override.yml``, or ``logs/``.

    *dockerfiles* is a tuple of Dockerfile filenames (relative to
    ``aiarena/``) required by the bots in this match.  Only the
    Dockerfiles actually referenced by the test bot or opponent are
    copied, so nothing bot-specific leaks into generic runs.

    Returns the absolute path to the run directory.
    """
    from .models import SystemConfig

    run_dir = os.path.join(AIARENA_RUNS_DIR, str(match_id))
    os.makedirs(run_dir, exist_ok=True)

    # Copy base infrastructure files into the run directory
    for filename in _BASE_FILES:
        src = os.path.join(AIARENA_DIR, filename)
        dst = os.path.join(run_dir, filename)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)

    # Copy bot-specific Dockerfiles referenced by this match
    for dockerfile in dockerfiles:
        if dockerfile:
            src = os.path.join(AIARENA_DIR, dockerfile)
            dst = os.path.join(run_dir, dockerfile)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)

    # Write .env with configured paths for docker-compose variable substitution
    config = SystemConfig.load()
    env_path = os.path.join(run_dir, '.env')
    with open(env_path, 'w') as f:
        f.write(f'SC2_MAPS_PATH={config.sc2_maps_path}\n')

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


def _parse_bot_race_from_log(log_path: str) -> str:
    """Parse the bot's actual race from its stderr log.

    Looks for a ``BOT_RACE:<race>`` line emitted by the bot during on_start.
    Returns the race string (e.g. 'Terran') or empty string if not found.
    """
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('BOT_RACE:'):
                    return line.strip().split(':', 1)[1]
    except OSError:
        pass
    return ''


def start_aiarena_match(
    match: Match,
    opponent_bot: CustomBot,
    test_bot: CustomBot,
    map_name: str | None = None,
    source_override: str | None = None,
    friendly_build: str = '',
    opponent_build: str = '',
    friendly_race: str = '',
    opponent_race_override: str = '',
) -> None:
    """Launch an aiarena match in a background thread.

    *test_bot* is the bot being tested (Player 1).

    *opponent_bot* is the opponent (Player 2).

    *source_override* overrides the test bot's source directory (e.g.
    a git worktree path for branch-based testing).

    *friendly_race* overrides the test bot's race in the matches file
    (e.g. 'Protoss' to lock a Random-race bot to a specific race).

    *opponent_race_override* overrides the opponent's race similarly.

    Each match gets its own run directory under ``aiarena/runs/<match_id>/``
    so multiple matches can run concurrently without conflicting.

    The match record should already exist with status 'Pending'.
    """
    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    match.map_name = map_name
    match.save()

    opponent_race_code = RACE_TO_CODE.get(
        opponent_race_override or opponent_bot.race, 'R',
    )
    opponent_type = opponent_bot.aiarena_bot_type or 'python'
    opponent_dir_name = opponent_bot.bot_directory

    test_bot_dir = test_bot.bot_directory or test_bot.name
    test_bot_race = RACE_TO_CODE.get(
        friendly_race or test_bot.race, 'R',
    )
    test_bot_type = test_bot.aiarena_bot_type or 'python'

    # Detect mirror/self-play match
    is_mirror = _is_mirror_match(test_bot, opponent_dir_name)
    mirror_name: str | None = None
    if is_mirror:
        mirror_name = _ensure_mirror_overlay(test_bot)
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
                f'Expected in aiarena/bots/{opponent_dir_name}/'
            )

    match_id = match.id
    dockerfiles = (test_bot.dockerfile, opponent_bot.dockerfile if not is_mirror else '')
    run_dir = _create_run_dir(match_id, dockerfiles=dockerfiles)

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

    _write_compose_override(
        run_dir,
        test_bot=test_bot,
        test_bot_aiarena_name=test_bot_dir,
        bot2_name=opponent_dir_name,
        bot2_host_path=opponent_path,
        bot2_type=opponent_type,
        bot2_dockerfile=opponent_bot.dockerfile if not is_mirror else '',
        opponent_bot=opponent_bot if not is_mirror else None,
        is_mirror=is_mirror,
        mirror_aiarena_name=mirror_name,
        source_override=source_override,
        friendly_build=friendly_build,
        opponent_build=opponent_build,
    )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _launch():
        thread = threading.Thread(
            target=_run_docker_match,
            args=(run_dir, match_id, log_file_path),
            daemon=True,
        )
        thread.start()

    from . import match_queue
    match_queue.enqueue(match_id, _launch)


def start_past_version_match(
    match: Match,
    commit_hash: str,
    short_hash: str,
    test_bot: CustomBot,
    map_name: str | None = None,
    source_override: str | None = None,
    friendly_build: str = '',
    friendly_race: str = '',
) -> None:
    """Launch a match of the current test bot vs a past version.

    The past version's bot code is extracted from git history into a
    cache directory.  Symlink targets (e.g. shared libraries) are mounted
    from the current host so all versions share the same runtime deps.

    *source_override* overrides Player 1's source directory (e.g. a git
    worktree for branch-based testing).

    *friendly_race* overrides the test bot's race in the matches file.
    """
    from . import bot_versions

    if map_name is None:
        map_name = random.choice(AIARENA_MAP_LIST)

    match.map_name = map_name
    match.save()

    test_bot_dir = test_bot.bot_directory or test_bot.name
    test_bot_race = RACE_TO_CODE.get(friendly_race or test_bot.race, 'R')
    test_bot_type = test_bot.aiarena_bot_type or 'python'

    # Extract (or reuse) cached bot source for this commit
    cache_path = bot_versions.get_or_create_version_cache(
        commit_hash,
        repo_path=test_bot.source_path or None,
        archive_paths=test_bot.archive_paths or None,
    )

    # Create overlay directory with aiarena-specific files
    opponent_bot_name = _ensure_version_overlay(test_bot, short_hash)

    # Verify overlay exists
    overlay_dir = os.path.join(AIARENA_BOTS_DIR, test_bot_dir)
    if not os.path.isdir(overlay_dir):
        raise FileNotFoundError(
            f'{test_bot_dir} overlay directory not found in aiarena/bots/.'
        )

    match_id = match.id
    run_dir = _create_run_dir(match_id, dockerfiles=(test_bot.dockerfile,))

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

    _write_compose_override(
        run_dir,
        test_bot=test_bot,
        test_bot_aiarena_name=test_bot_dir,
        bot2_name=opponent_bot_name,
        bot2_host_path=None,
        is_past_version=True,
        past_version_cache_path=cache_path,
        source_override=source_override,
        friendly_build=friendly_build,
    )

    log_file_path = os.path.join(run_dir, 'compose_output.log')

    def _launch():
        thread = threading.Thread(
            target=_run_docker_match,
            args=(run_dir, match_id, log_file_path),
            daemon=True,
        )
        thread.start()

    from . import match_queue
    match_queue.enqueue(match_id, _launch)


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
                'up', '--build', '--abort-on-container-exit',
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

        # Decrement the active count and start any queued matches.
        try:
            from . import match_queue
            match_queue.notify_match_finished()
        except Exception:
            logger.exception('Match %d: error notifying queue after completion', match_id)


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

    # Try to extract the test bot's resolved race from its stderr log
    if match_obj.test_bot:
        bot_log = get_bot_log_path(match_id, match_obj.test_bot.bot_directory)
        if bot_log:
            bot_race = _parse_bot_race_from_log(bot_log)
            if bot_race:
                match_obj.friendly_race = bot_race

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

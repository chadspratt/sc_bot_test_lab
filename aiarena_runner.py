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
from datetime import datetime
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from .models import CustomBot, Match

# Paths
AIARENA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'aiarena'))
AIARENA_BOTS_DIR = os.path.join(AIARENA_DIR, 'bots')
AIARENA_REPLAYS_DIR = os.path.join(AIARENA_DIR, 'replays')
AIARENA_LOGS_DIR = os.path.join(AIARENA_DIR, 'logs')
MATCHES_FILE = os.path.join(AIARENA_DIR, 'matches')
RESULTS_FILE = os.path.join(AIARENA_DIR, 'results.json')
COMPOSE_OVERRIDE = os.path.join(AIARENA_DIR, 'docker-compose.override.yml')

# Repo root (for finding bots in other_bots/ and live source mounts)
REPO_ROOT = os.path.normpath(os.path.join(AIARENA_DIR, '..', '..', '..', '..'))

# Live source directories (mounted into containers instead of copied)
BOT_SRC_DIR = os.path.join(REPO_ROOT, 'bot')
SC2_SRC_DIR = os.path.join(REPO_ROOT, 'python_sc2', 'sc2')

# Replay destination (same as existing test_lab log/replay dir)
HOST_REPLAYS_DIR = r'C:\Users\inter\Documents\StarCraft II\Replays\Multiplayer\docker'

# BotTato is always Bot 1
BOTTATO_NAME = 'BotTato'
BOTTATO_RACE = 'T'
BOTTATO_TYPE = 'python'

# Mirror copy for self-play matches (distinct name so the proxy can route)
BOTTATO_MIRROR_NAME = 'BotTato_p2'

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


def _write_compose_override(
    bot2_name: str,
    bot2_host_path: str | None,
    *,
    is_mirror: bool = False,
) -> None:
    """Generate docker-compose.override.yml with per-bot volume mounts.

    Bot 1 (BotTato) always uses live source mounts via
    ``_bottato_volume_mounts``.  Bot 2 is either a regular opponent
    (single directory mount) or a mirror match (also uses live mounts
    with a different overlay name plus the custom Dockerfile).
    """
    lines = [
        'services:',
        '  bot_controller1:',
        '    volumes:',
    ]
    lines += _bottato_volume_mounts(BOTTATO_NAME)

    lines.append('  bot_controller2:')
    if is_mirror:
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

    with open(COMPOSE_OVERRIDE, 'w') as f:
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


def _write_matches_file(
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
    with open(MATCHES_FILE, 'w') as f:
        f.write(line + '\n')


def _clear_results() -> None:
    """Reset results.json to empty."""
    with open(RESULTS_FILE, 'w') as f:
        json.dump({"results": []}, f)


def _clean_aiarena_artifacts() -> None:
    """Move stale replays and bot logs to HOST_REPLAYS_DIR before a new match.

    The proxy controller reuses the same filenames each run (e.g.
    ``1_BotTato_vs_who.SC2Replay``), so leftover files from a prior match
    would be collected as if they belong to the current one.

    Normally ``_collect_artifacts`` already moves files out after each match,
    so nothing should be left over.  This acts as a safety net for crashes
    or other failures that prevented normal collection — artifacts are
    preserved in HOST_REPLAYS_DIR with an ``orphaned_`` prefix rather than
    being deleted.
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(HOST_REPLAYS_DIR, exist_ok=True)

    # Replays
    for f in glob.glob(os.path.join(AIARENA_REPLAYS_DIR, '*.SC2Replay')):
        try:
            dest = os.path.join(
                HOST_REPLAYS_DIR,
                f"orphaned_{timestamp}_{os.path.basename(f)}",
            )
            shutil.move(f, dest)
        except OSError:
            pass

    # Bot stderr logs (bot_controller1/<BotName>/stderr.log, etc.)
    for controller_dir in ('bot_controller1', 'bot_controller2'):
        controller_path = os.path.join(AIARENA_LOGS_DIR, controller_dir)
        if not os.path.isdir(controller_path):
            continue
        for bot_name in os.listdir(controller_path):
            stderr_path = os.path.join(controller_path, bot_name, 'stderr.log')
            if os.path.isfile(stderr_path):
                try:
                    dest = os.path.join(
                        HOST_REPLAYS_DIR,
                        f"orphaned_{timestamp}_{controller_dir}_{bot_name}_stderr.log",
                    )
                    shutil.move(stderr_path, dest)
                except OSError:
                    pass


def _parse_results() -> dict | None:
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
    try:
        with open(RESULTS_FILE) as f:
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


def _collect_artifacts(match_id: int, opponent_name: str) -> None:
    """Copy replays and bot logs from the aiarena dirs to HOST_REPLAYS_DIR.

    Replays are saved by the proxy_controller as e.g.
    ``aiarena/replays/1_BotTato_vs_who.SC2Replay`` (using the aiarena match
    id, always 1 for single-match runs).  We rename them to
    ``{match_id}_aiarena_{opponent}.SC2Replay``.

    Bot stderr logs are saved to ``aiarena/logs/bot_controller{1,2}/{name}/stderr.log``.
    We copy them as ``{match_id}_aiarena_{name}_stderr.log``.
    """
    # --- Replays ---
    replay_pattern = os.path.join(AIARENA_REPLAYS_DIR, '*.SC2Replay')
    for replay_src in glob.glob(replay_pattern):
        dest_name = f"{match_id}_aiarena_{opponent_name}.SC2Replay"
        dest_path = os.path.join(HOST_REPLAYS_DIR, dest_name)
        try:
            shutil.move(replay_src, dest_path)
        except OSError:
            pass

    # --- Bot stderr logs ---
    for controller_dir in ('bot_controller1', 'bot_controller2'):
        controller_path = os.path.join(AIARENA_LOGS_DIR, controller_dir)
        if not os.path.isdir(controller_path):
            continue
        for bot_name in os.listdir(controller_path):
            stderr_path = os.path.join(controller_path, bot_name, 'stderr.log')
            if os.path.isfile(stderr_path) and os.path.getsize(stderr_path) > 0:
                dest_name = f"{match_id}_aiarena_{bot_name}_stderr.log"
                dest_path = os.path.join(HOST_REPLAYS_DIR, dest_name)
                try:
                    shutil.move(stderr_path, dest_path)
                except OSError:
                    pass


def get_bot_log_path(match_id: int, bot_name: str) -> str | None:
    """Return the host path for a copied bot stderr log, or None."""
    path = os.path.join(HOST_REPLAYS_DIR, f"{match_id}_aiarena_{bot_name}_stderr.log")
    if os.path.isfile(path):
        return path
    return None


def start_aiarena_match(
    match: Match,
    custom_bot: CustomBot,
    map_name: str | None = None,
) -> None:
    """Launch an aiarena match in a background thread.

    The match record should already exist with status 'Pending'.
    This function:
    1. Writes the matches file
    2. Clears results.json
    3. Runs docker compose up (blocking in a background thread)
    4. Parses results.json
    5. Updates the Match record with the outcome
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

    # Set up the match
    _write_matches_file(
        bot1_name=BOTTATO_NAME,
        bot1_race=BOTTATO_RACE,
        bot1_type=BOTTATO_TYPE,
        bot2_name=opponent_dir_name,
        bot2_race=opponent_race_code,
        bot2_type=opponent_type,
        map_name=map_name,
    )
    _clear_results()

    # Move any leftover artifacts from previous matches to HOST_REPLAYS_DIR
    # so _collect_artifacts doesn't pick up stale replays or logs.
    _clean_aiarena_artifacts()

    # Generate per-match compose override with live source mounts for BotTato.
    # Mirror matches also use live mounts + custom image for bot_controller2.
    _write_compose_override(
        bot2_name=opponent_dir_name,
        bot2_host_path=opponent_path,
        is_mirror=is_mirror,
    )

    # Ensure output directories exist
    os.makedirs(AIARENA_REPLAYS_DIR, exist_ok=True)
    os.makedirs(AIARENA_LOGS_DIR, exist_ok=True)
    os.makedirs(HOST_REPLAYS_DIR, exist_ok=True)

    match_id = match.id
    log_file_path = os.path.join(HOST_REPLAYS_DIR, f"{match_id}_aiarena_{custom_bot.name}.log")

    def _run_match():
        """Background thread: run docker compose and process results."""
        try:
            with open(log_file_path, 'w') as log_file:
                result = subprocess.run(
                    [
                        'docker', 'compose',
                        '-f', 'docker-compose.yml',
                        '-f', 'docker-compose.override.yml',
                        '-p', f'aiarena_{match_id}',
                        'up', '--abort-on-container-exit',
                    ],
                    cwd=AIARENA_DIR,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=7200,  # 2 hour timeout
                )

            # Docker compose finished — parse results
            aiarena_result = _parse_results()

            # Import here to avoid circular imports at module level
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

            # Copy replays and bot logs to the shared output directory
            _collect_artifacts(match_id, custom_bot.name)

            # Clean up: bring down containers
            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=AIARENA_DIR,
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

            # Force stop containers on timeout
            subprocess.run(
                [
                    'docker', 'compose',
                    '-f', 'docker-compose.yml',
                    '-f', 'docker-compose.override.yml',
                    '-p', f'aiarena_{match_id}', 'down',
                ],
                cwd=AIARENA_DIR,
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

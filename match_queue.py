"""Match queue — enforces the max-concurrent-matches limit.

Matches that cannot start immediately are saved in the DB with result
``'Queued'`` and launched automatically when capacity becomes available.

The database is the source of truth:
- **Running** = ``result='Pending'`` with ``start_timestamp`` in the last 24 h.
- **Queued**  = ``result='Queued'``.

Launcher closures are held in memory for the current process.  If the
server restarts (e.g. Django dev-server reload), launchers are
reconstructed from the Match record and on-disk state when the queue
is next drained.
"""

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('test_lab')

# In-memory registry of queued match launchers.
# Key: match_id  Value: zero-arg callable that starts the match.
_queued_launchers: dict[int, Callable[[], None]] = {}
_queue_lock = threading.Lock()


def get_running_match_count() -> int:
    """Return the number of Pending matches started in the last 24 hours."""
    from .models import Match
    cutoff = timezone.now() - timedelta(hours=24)
    return Match.objects.filter(result='Pending', start_timestamp__gte=cutoff).count()


def get_max_concurrent() -> int:
    """Return the configured maximum concurrent matches (0 = unlimited)."""
    from .models import SystemConfig
    return SystemConfig.load().max_concurrent_matches


def has_capacity() -> bool:
    """Return True if another match can be started right now."""
    limit = get_max_concurrent()
    if limit <= 0:
        return True
    return get_running_match_count() < limit


def enqueue(match_id: int, launcher: Callable[[], None]) -> bool:
    """Try to start a match, or queue it if at capacity.

    *launcher* is a zero-arg callable that actually starts the Docker
    process (called in the current thread if capacity is available, or
    deferred if not).

    The match record is expected to already exist with ``result='Pending'``.
    We flip it to Queued first so the DB count is accurate during the
    capacity check, then flip back to Pending if we launch immediately.

    Returns ``True`` if the match was started immediately, ``False`` if
    it was queued.
    """
    from .models import Match

    with _queue_lock:
        # Temporarily mark Queued so it doesn't count as running
        try:
            match = Match.objects.get(id=match_id)
            match.result = 'Queued'
            match.save()
        except Match.DoesNotExist:
            return False

        if has_capacity():
            match.result = 'Pending'
            match.save()
            launcher()
            return True

        _queued_launchers[match_id] = launcher
        logger.info('Match %d: queued (at capacity, %d running)', match_id, get_running_match_count())
        return False


def notify_match_finished() -> None:
    """Called when a match completes. Drains the queue if capacity opened up."""
    with _queue_lock:
        _drain_unlocked()


def drain_queue() -> int:
    """Start as many queued matches as current capacity allows.

    Returns the number of matches started.
    """
    with _queue_lock:
        return _drain_unlocked()


def _drain_unlocked() -> int:
    """Start queued matches while capacity allows.  Caller must hold _queue_lock.

    First processes matches that still have in-memory launchers, then
    rebuilds launchers for any remaining DB-queued matches (handles
    server restarts / dev-server reloads).
    """
    started = 0

    # 1) Drain matches that have in-memory launchers
    while _queued_launchers and has_capacity():
        match_id, launcher = next(iter(_queued_launchers.items()))
        del _queued_launchers[match_id]
        if _start_queued_match(match_id, launcher):
            started += 1

    # 2) Pick up DB-queued matches that lost their in-memory launcher
    if has_capacity():
        from .models import Match
        orphaned = (
            Match.objects
            .filter(result='Queued')
            .exclude(id__in=list(_queued_launchers.keys()))
            .select_related('opponent_bot', 'test_bot', 'replay_test')
            .order_by('id')
        )
        for match_obj in orphaned:
            if not has_capacity():
                break
            launcher = _rebuild_launcher(match_obj)
            if launcher is None:
                logger.warning('Match %d: cannot rebuild launcher, skipping', match_obj.id)
                continue
            if _start_queued_match(match_obj.id, launcher):
                started += 1

    return started


def _start_queued_match(match_id: int, launcher: Callable[[], None]) -> bool:
    """Flip a Queued match to Pending and launch it.  Returns True on success."""
    from .models import Match
    try:
        match = Match.objects.get(id=match_id)
        if match.result != 'Queued':
            return False
        match.result = 'Pending'
        match.save()
    except Match.DoesNotExist:
        return False

    logger.info('Match %d: starting from queue', match_id)
    try:
        launcher()
        return True
    except Exception:
        logger.exception('Match %d: failed to start from queue', match_id)
        return False


# ---------------------------------------------------------------------------
# Launcher reconstruction — rebuilds a launch closure from DB + disk state
# so queued matches survive server restarts.
# ---------------------------------------------------------------------------

def _rebuild_launcher(match) -> Callable[[], None] | None:
    """Create a launcher closure for a queued Match from its DB fields.

    Returns None if the match type can't be identified or required
    on-disk state is missing.
    """
    from . import aiarena_runner

    match_id = match.id

    # --- Aiarena matches (custom bot or past-version) ---
    # These have a pre-built run directory with all docker-compose config.
    run_dir = aiarena_runner.get_run_dir(match_id)
    if os.path.isdir(run_dir):
        log_file_path = os.path.join(run_dir, 'compose_output.log')
        logger.info('Match %d: rebuilding aiarena launcher from run dir', match_id)

        def _launch_aiarena():
            thread = threading.Thread(
                target=aiarena_runner._run_docker_match,
                args=(run_dir, match_id, log_file_path),
                daemon=True,
            )
            thread.start()

        return _launch_aiarena

    # --- Single-container matches (all use a single docker-compose.yml) ---
    from .views import DOCKER_COMPOSE_PATH, _get_logs_dir
    os.makedirs(_get_logs_dir(), exist_ok=True)

    if match.replay_test_id:
        return _rebuild_replay_test_launcher(match)

    # Computer AI match
    if match.opponent_race and match.opponent_build:
        return _rebuild_blizzard_ai_launcher(match)

    return None


def _get_source_override(match) -> str | None:
    """Resolve a branch worktree source override from the match's test group."""
    from .models import TestGroup
    try:
        tg = TestGroup.objects.get(id=match.test_group_id)
    except TestGroup.DoesNotExist:
        return None
    if not tg.branch:
        return None
    test_bot = match.test_bot
    if not test_bot or not test_bot.source_path:
        return None
    from . import worktrees
    return worktrees.get_or_create_worktree(test_bot.source_path, tg.branch)


def _add_source_override(command: list[str], match) -> None:
    """Append a volume-mount flag for the branch worktree if applicable."""
    override = _get_source_override(match)
    if override:
        command += ['-v', f'{override.replace(chr(92), "/")}:/root/bot']


def _rebuild_blizzard_ai_launcher(match) -> Callable[[], None]:
    """Rebuild launcher for a Blizzard AI match."""
    from .views import DOCKER_COMPOSE_PATH, _get_logs_dir

    match_id = match.id
    race = match.opponent_race.lower()
    build = match.opponent_build.lower()
    difficulty = match.opponent_difficulty or 'CheatInsane'
    log_file_path = os.path.join(_get_logs_dir(), f'{match_id}_{race}_{build}.log')

    command = [
        'docker', 'compose', '-p', f'match_{match_id}',
        'run', '--rm', '--no-deps',
        '-e', f'RACE={race}',
        '-e', f'BUILD={build}',
        '-e', f'MATCH_ID={match_id}',
        '-e', f'DIFFICULTY={difficulty}',
        '-e', f'MAP_NAME={match.map_name}',
    ]
    _add_source_override(command, match)
    from .views import _env_file_args
    command += _env_file_args(match.test_bot)
    command.append('bot')

    logger.info('Match %d: rebuilding Blizzard AI launcher (%s %s)', match_id, race, build)
    return _make_sc_docker_launcher(match_id, command, DOCKER_COMPOSE_PATH, log_file_path)


def _rebuild_replay_test_launcher(match) -> Callable[[], None] | None:
    """Rebuild launcher for a replay-test match."""
    from .views import DOCKER_COMPOSE_PATH, _get_logs_dir

    match_id = match.id
    rt = match.replay_test
    if rt is None:
        return None

    game_loop = match.replay_takeover_game_loop
    if not game_loop:
        return None

    container_replay_path = match.replay_file
    if not container_replay_path:
        return None

    rt_difficulty = rt.opponent_difficulty or 'CheatInsane'
    rt_build = rt.opponent_build or 'Macro'
    rt_race = rt.opponent_race or 'Random'
    rt_bot_player_id = rt.bot_player_id or 1

    log_file_path = os.path.join(_get_logs_dir(), f'{match_id}_replay_test.log')

    command = [
        'docker', 'compose', '-p', f'match_{match_id}',
        'run', '--rm', '--no-deps',
        '-e', f'REPLAY_PATH={container_replay_path}',
        '-e', f'TAKEOVER_GAME_LOOP={game_loop}',
        '-e', f'BOT_PLAYER_ID={rt_bot_player_id}',
        '-e', f'DIFFICULTY={rt_difficulty}',
        '-e', f'BUILD={rt_build.lower()}',
        '-e', f'RACE={rt_race.lower()}',
        '-e', f'MATCH_ID={match_id}',
    ]

    from .views import _parse_game_time
    duration_loops = _parse_game_time(rt.duration)
    if duration_loops and duration_loops > 0:
        duration_seconds = duration_loops / 22.4
        command += ['-e', f'REPLAY_DURATION={duration_seconds:.1f}']

    _add_source_override(command, match)
    from .views import _env_file_args
    command += _env_file_args(match.test_bot)
    command += ['bot', 'bash', '/root/runner/run_docker_continue_replay.sh']

    logger.info('Match %d: rebuilding replay-test launcher (%s)', match_id, rt.name)
    return _make_sc_docker_launcher(match_id, command, DOCKER_COMPOSE_PATH, log_file_path)


def _make_sc_docker_launcher(
    match_id: int, command: list[str], cwd: str, log_file_path: str,
) -> Callable[[], None]:
    """Create a launcher closure for a single-container Docker match."""
    def _launcher():
        def _run():
            try:
                with open(log_file_path, 'w') as log:
                    proc = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=log)
                proc.wait(timeout=7200)
            except Exception:
                logger.exception('Single-container match %d: error', match_id)
            finally:
                notify_match_finished()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    return _launcher

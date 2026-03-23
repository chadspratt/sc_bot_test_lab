import glob
import logging
import os
import random
import subprocess
import threading
from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.db.models import Count, Max, Min, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import aiarena_runner, bot_versions, match_queue, prompt_generator, worktrees
from .models import (
    CustomBot,
    Match,
    MatchEvent,
    PromptTemplate,
    ReplayTest,
    SystemConfig,
    TestGroup,
    TestSuite,
    Ticket,
)

logger = logging.getLogger('test_lab')


def _get_match_list_context(request):
    """Return context dict for the Test Groups tab."""
    # Recover any stale pending matches whose docker process has finished
    # but whose monitoring thread was killed (e.g. by a dev-server reload).
    try:
        recovered = aiarena_runner.check_stale_pending_matches()
        if recovered:
            logger.info('Recovered %d stale aiarena match(es): %s', len(recovered), recovered)
    except Exception:
        logger.exception('Error checking stale pending aiarena matches')

    try:
        legacy_recovered = _recover_stale_legacy_matches()
        if legacy_recovered:
            logger.info('Recovered %d stale legacy match(es): %s', len(legacy_recovered), legacy_recovered)
    except Exception:
        logger.exception('Error checking stale pending legacy matches')

    # Start any queued matches that now have capacity.
    try:
        match_queue.drain_queue()
    except Exception:
        logger.exception('Error draining match queue')

    # Get filters from request
    selected_test_bot = request.GET.get('test_bot', '')
    selected_blizzard = request.GET.get('blizzard', 'All')
    show_custom_bots = request.GET.get('custom_bots', '1') != '0'
    show_past_versions = request.GET.get('past_versions', '1') != '0'
    show_replays = request.GET.get('replays', '1') != '0'
    selected_limit = request.GET.get('limit', '')
    selected_branch = request.GET.get('branch', '')

    matches = Match.objects.select_related('opponent_bot', 'test_group', 'test_bot', 'replay_test').exclude(test_group_id=-1)

    # Apply test bot filter
    if selected_test_bot and selected_test_bot.isdigit():
        matches = matches.filter(test_bot_id=int(selected_test_bot))

    # Apply branch filter — only include matches from test groups on this branch
    if selected_branch:
        branch_group_ids = list(
            TestGroup.objects.filter(branch=selected_branch).values_list('id', flat=True)
        )
        matches = matches.filter(test_group_id__in=branch_group_ids)

    # Build inclusive match-type filter from blizzard/custom/past/replay controls.
    # Each enabled type adds an OR clause; matches not matching any are excluded.
    type_q = Q()
    is_blizzard_q = Q(opponent_bot__isnull=True, opponent_commit_hash='', replay_test__isnull=True)

    if selected_blizzard == 'None':
        pass  # Don't include any blizzard AI matches
    elif selected_blizzard and selected_blizzard != 'All':
        type_q |= is_blizzard_q & Q(opponent_difficulty=selected_blizzard)
    else:
        type_q |= is_blizzard_q

    if show_custom_bots:
        type_q |= Q(opponent_bot__isnull=False)
    if show_past_versions:
        type_q |= ~Q(opponent_commit_hash='')
    if show_replays:
        type_q |= Q(replay_test__isnull=False)

    matches = matches.filter(type_q)

    # Apply test group limit — only include matches from the N most recent
    # test groups that contain at least one match after the above filters.
    if selected_limit and selected_limit.isdigit():
        recent_group_ids = list(
            matches.values_list('test_group_id', flat=True)
            .distinct()
            .order_by('-test_group_id')[:int(selected_limit)]
        )
        matches = matches.filter(test_group_id__in=recent_group_ids)

    # ------------------------------------------------------------------
    # Group matches by test_group_id and create pivot structure
    # ------------------------------------------------------------------
    grouped_matches: dict[int, dict[str, Match]] = defaultdict(dict)
    race_groups: dict[str, list[str]] = defaultdict(list)  # race -> builds
    custom_bot_opponents: dict[int, CustomBot] = {}  # bot_id -> CustomBot
    version_opponents: dict[str, str] = {}  # short_hash -> opponent_key
    version_ordering: dict[str, tuple[int, int]] = {}  # short_hash -> (max_test_group_id, match_id)
    replay_test_opponents: dict[int, str] = {}  # replay_test_id -> name

    # Track win/loss counts for each opponent column
    opponent_stats: dict[str, dict] = defaultdict(lambda: {'victories': 0, 'total_games': 0})

    # Track fastest victories / slowest losses per combination
    fastest_victories: dict[tuple, tuple[int, int]] = {}
    slowest_losses: dict[tuple, tuple[int, int]] = {}

    for match in matches:
        opp_bot = match.opponent_bot
        if match.replay_test_id:
            # Replay test match — key by replay test id
            opponent_key = f"replay_{match.replay_test_id}"
            replay_test_opponents[match.replay_test_id] = match.replay_test.name
        elif match.opponent_commit_hash:
            # Past-version opponent — key by short hash
            short_hash = match.opponent_commit_hash[:7]
            opponent_key = f"version_{short_hash}"
            version_opponents[short_hash] = opponent_key
            # Track ordering: prefer highest test_group_id, then lowest match_id
            # (lower offset = more recent commit = created first in a group)
            group_id = match.test_group_id
            prev = version_ordering.get(short_hash)
            if prev is None or group_id > prev[0] or (group_id == prev[0] and match.id < prev[1]):
                version_ordering[short_hash] = (group_id, match.id)
        elif opp_bot is not None:
            # Custom bot opponent — key by bot id
            opponent_key = f"bot_{opp_bot.id}"
            custom_bot_opponents[opp_bot.id] = opp_bot
        else:
            # Computer opponent — key by race-build
            opponent_key = f"{match.opponent_race}-{match.opponent_build}"
            race_groups[match.opponent_race].append(match.opponent_build)

        grouped_matches[match.test_group.id][opponent_key] = match

        if match.result in ('Victory', 'Defeat'):
            opponent_stats[opponent_key]['total_games'] += 1
            if match.result == 'Victory':
                opponent_stats[opponent_key]['victories'] += 1
                if match.duration_in_game_time and match.duration_in_game_time > 0:
                    key = (opponent_key, match.opponent_difficulty, match.map_name)
                    if key not in fastest_victories or match.duration_in_game_time < fastest_victories[key][0]:
                        fastest_victories[key] = (match.duration_in_game_time, match.id)
            else:
                if match.duration_in_game_time and match.duration_in_game_time > 0:
                    key = (opponent_key, match.opponent_difficulty, match.map_name)
                    if key not in slowest_losses or match.duration_in_game_time > slowest_losses[key][0]:
                        slowest_losses[key] = (match.duration_in_game_time, match.id)

    best_time_match_ids = {mid for _, mid in fastest_victories.values()}
    best_time_match_ids.update(mid for _, mid in slowest_losses.values())

    # ------------------------------------------------------------------
    # Build sorted opponent columns and header structure
    # ------------------------------------------------------------------
    # 1) Computer opponents (race-build)
    race_build_map: dict[str, set[str]] = defaultdict(set)
    for race, builds in race_groups.items():
        race_build_map[race].update(builds)

    sorted_opponents: list[str] = []
    header_structure: list[dict] = []

    for race in sorted(race_build_map.keys()):
        builds = sorted(race_build_map[race])
        for build in builds:
            sorted_opponents.append(f"{race}-{build}")
        header_structure.append({
            'name': race,
            'span': len(builds),
            'builds': list(builds),
        })

    # 2) Custom bot opponents (appended after computer columns)
    custom_bot_keys: list[str] = []
    custom_bot_names: list[str] = []
    for bot_id in sorted(custom_bot_opponents.keys()):
        bot = custom_bot_opponents[bot_id]
        key = f"bot_{bot_id}"
        custom_bot_keys.append(key)
        custom_bot_names.append(bot.name)
        sorted_opponents.append(key)

    if custom_bot_keys:
        header_structure.append({
            'name': 'Custom Bots',
            'span': len(custom_bot_keys),
            'builds': list(custom_bot_names),
        })

    # 3) Past-version opponents (appended after custom bot columns)
    #    Sort by most recent version first: highest test_group_id, then
    #    lowest match_id within that group (lower offset = more recent commit).
    version_keys: list[str] = []
    version_labels: list[str] = []
    for short_hash in sorted(
        version_opponents.keys(),
        key=lambda h: (-version_ordering[h][0], version_ordering[h][1]),
    ):
        key = version_opponents[short_hash]
        version_keys.append(key)
        version_labels.append(short_hash)
        sorted_opponents.append(key)

    if version_keys:
        header_structure.append({
            'name': 'Past Versions',
            'span': len(version_keys),
            'builds': list(version_labels),
        })

    # 4) Replay test opponents (appended after past-version columns)
    replay_test_keys: list[str] = []
    replay_test_labels: list[str] = []
    for rt_id in sorted(replay_test_opponents.keys()):
        key = f"replay_{rt_id}"
        replay_test_keys.append(key)
        replay_test_labels.append(replay_test_opponents[rt_id])
        sorted_opponents.append(key)

    if replay_test_keys:
        header_structure.append({
            'name': 'Replay Tests',
            'span': len(replay_test_keys),
            'builds': list(replay_test_labels),
        })

    # ------------------------------------------------------------------
    # Compute per-race (and custom-bots group) win-rate labels
    # ------------------------------------------------------------------
    for race_group in header_structure:
        race_name = race_group['name']
        race_victories = 0
        race_total_games = 0

        if race_name == 'Custom Bots':
            # Aggregate across all custom bot columns
            for key in custom_bot_keys:
                stats = opponent_stats[key]
                race_total_games += stats['total_games']
                race_victories += stats['victories']
        elif race_name == 'Past Versions':
            # Aggregate across all past-version columns
            for key in version_keys:
                stats = opponent_stats[key]
                race_total_games += stats['total_games']
                race_victories += stats['victories']
        elif race_name == 'Replay Tests':
            for key in replay_test_keys:
                stats = opponent_stats[key]
                race_total_games += stats['total_games']
                race_victories += stats['victories']
        else:
            for opp_key, stats in opponent_stats.items():
                if opp_key.startswith(f"{race_name}-"):
                    race_total_games += stats['total_games']
                    race_victories += stats['victories']

        if race_total_games > 0:
            race_group['win_rate'] = f"{(race_victories / race_total_games) * 100:.0f}%"
        else:
            race_group['win_rate'] = "-"

        # Per-build / per-bot win rates
        if race_name == 'Custom Bots':
            for i, key in enumerate(custom_bot_keys):
                s = opponent_stats[key]
                if s['total_games'] > 0:
                    pct = (s['victories'] / s['total_games']) * 100
                    race_group['builds'][i] = f"{custom_bot_names[i]} {pct:.0f}%"
                else:
                    race_group['builds'][i] = f"{custom_bot_names[i]} -"
        elif race_name == 'Past Versions':
            for i, key in enumerate(version_keys):
                s = opponent_stats[key]
                if s['total_games'] > 0:
                    pct = (s['victories'] / s['total_games']) * 100
                    race_group['builds'][i] = f"{version_labels[i]} {pct:.0f}%"
                else:
                    race_group['builds'][i] = f"{version_labels[i]} -"
        elif race_name == 'Replay Tests':
            for i, key in enumerate(replay_test_keys):
                s = opponent_stats[key]
                if s['total_games'] > 0:
                    pct = (s['victories'] / s['total_games']) * 100
                    race_group['builds'][i] = f"{replay_test_labels[i]} {pct:.0f}%"
                else:
                    race_group['builds'][i] = f"{replay_test_labels[i]} -"
        else:
            raw_builds = sorted(race_build_map.get(race_name, set()))
            for i, build in enumerate(raw_builds):
                s = opponent_stats[f"{race_name}-{build}"]
                if s['total_games'] > 0:
                    pct = (s['victories'] / s['total_games']) * 100
                    race_group['builds'][i] = f"{build} {pct:.0f}%"
                else:
                    race_group['builds'][i] = f"{build} -"

    # ------------------------------------------------------------------
    # Build pivot rows
    # ------------------------------------------------------------------
    sorted_groups = sorted(grouped_matches.keys(), reverse=True)
    test_groups = {tg.id: tg.description for tg in TestGroup.objects.filter(id__in=sorted_groups)}
    max_group_id = max(sorted_groups) if sorted_groups else -1

    pivot_data = []
    for group_id in sorted_groups:
        row = {'test_group_id': group_id, 'results': [], 'difficulty': None, 'test_bot_name': ''}

        # Get difficulty and test bot from first match in this group
        for m in grouped_matches[group_id].values():
            if not row['test_bot_name'] and m.test_bot:
                row['test_bot_name'] = m.test_bot.name
            if m.opponent_difficulty:
                row['difficulty'] = m.opponent_difficulty
            if row['test_bot_name'] and row['difficulty']:
                break

        group_victories = 0
        group_total_games = 0
        group_total_duration = 0
        group_games_with_duration = 0

        for opponent in sorted_opponents:
            match_data = grouped_matches[group_id].get(opponent)
            if not match_data:
                row['results'].append(None)
                continue

            if group_id != max_group_id and match_data.result in ('Pending', 'Queued'):
                match_data.result = 'Aborted'

            match_data.is_best_time = match_data.id in best_time_match_ids
            row['results'].append(match_data)

            result = match_data.result
            duration = match_data.duration_in_game_time

            if result in ('Victory', 'Defeat'):
                group_total_games += 1
                if result == 'Victory':
                    group_victories += 1

            if duration is not None and duration > 0:
                group_total_duration += duration
                group_games_with_duration += 1

        if group_total_games > 0:
            row['group_win_percentage'] = f"{(group_victories / group_total_games) * 100:.1f}%"
        else:
            row['group_win_percentage'] = "-"

        if group_games_with_duration > 0:
            row['avg_duration'] = int(group_total_duration / group_games_with_duration)
        else:
            row['avg_duration'] = None

        pivot_data.append(row)

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------
    test_subject_bots = CustomBot.objects.filter(is_test_subject=True).order_by('name')
    test_suites = TestSuite.objects.all().order_by('name')

    # Collect distinct branches for the filter dropdown
    branches_with_results = list(
        TestGroup.objects.exclude(branch='').values_list('branch', flat=True).distinct().order_by('branch')
    )

    return {
        'pivot_data': pivot_data,
        'opponents': sorted_opponents,
        'header_structure': header_structure,
        'selected_blizzard': selected_blizzard,
        'show_custom_bots': show_custom_bots,
        'show_past_versions': show_past_versions,
        'show_replays': show_replays,
        'selected_limit': selected_limit,
        'selected_test_bot': selected_test_bot,
        'selected_branch': selected_branch,
        'branches_with_results': branches_with_results,
        'test_groups': test_groups,
        'test_subject_bots': test_subject_bots,
        'test_suites': test_suites,
    }

def get_next_test_group_id() -> int:
    """Get the next test group ID by incrementing the highest completed test group ID."""
    result = Match.objects.filter(
        end_timestamp__isnull=False
    ).aggregate(Max('test_group_id'))['test_group_id__max']
    
    # If no completed matches exist, start at 0, otherwise increment by 1
    return 0 if result is None else result + 1

MAP_LIST = [
    "PersephoneAIE_v4",
    "IncorporealAIE_v4",
    "PylonAIE_v4",
    "TorchesAIE_v4",
    "UltraloveAIE_v2",
    "MagannathaAIE_v2",
]


def get_least_used_map(
    opponent_race: str, opponent_build: str, opponent_difficulty: str,
) -> str:
    """Return the map with the fewest completed matches for the given opponent config."""
    result = (
        Match.objects.filter(
            opponent_race=opponent_race,
            opponent_build=opponent_build,
            opponent_difficulty=opponent_difficulty,
            result__in=['Victory', 'Defeat'],
            test_group_id__gte=0,
        )
        .values('map_name')
        .annotate(ct=Count('id'))
        .order_by('ct')
        .first()
    )
    return result['map_name'] if result else random.choice(MAP_LIST)


def create_pending_match(
    test_group_id: int, race: str, build: str, difficulty: str,
    test_bot: CustomBot,
    map_name: str = '',
) -> int:
    """Create a pending match entry and return the match ID.

    *test_bot* is the Player-1 bot being tested.

    *map_name* is the pre-selected map.  When empty, the least-used map
    for this opponent config is chosen automatically.
    """
    if not map_name:
        map_name = get_least_used_map(
            race.capitalize(), build.capitalize(), difficulty or 'CheatInsane',
        )
    match = Match(
        test_group_id=test_group_id,
        start_timestamp=datetime.now(),
        map_name=map_name,
        opponent_race=race.capitalize(),
        opponent_difficulty=difficulty or "CheatInsane",
        opponent_build=build.capitalize(),
        test_bot=test_bot,
        result="Pending"
    )
    match.save()
    assert isinstance(match.id, int)
    return match.id

DOCKER_COMPOSE_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__)))


def _get_logs_dir() -> str:
    """Return the configured legacy logs directory from SystemConfig."""
    return SystemConfig.load().logs_dir


def _get_replays_dir() -> str:
    """Return the configured legacy replays directory (falls back to logs_dir)."""
    config = SystemConfig.load()
    return config.replays_dir or config.logs_dir


def _write_legacy_env() -> None:
    """Write a .env file for the legacy docker-compose with configured paths."""
    config = SystemConfig.load()
    env_path = os.path.join(DOCKER_COMPOSE_PATH, '.env')
    replays_dir = config.replays_dir or config.logs_dir
    with open(env_path, 'w') as f:
        f.write(f'SC2_MAPS_PATH={config.sc2_maps_path}\n')
        f.write(f'REPLAYS_DIR={replays_dir}\n')


def _env_file_args(test_bot: CustomBot | None) -> list[str]:
    """Return ``['-e', 'K=V', ...]`` flags parsed from the bot's env_file."""
    if not (test_bot and test_bot.env_file and os.path.isfile(test_bot.env_file)):
        return []
    args: list[str] = []
    with open(test_bot.env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                args += ['-e', line]
    return args


def _parse_legacy_result(log_file_path: str) -> str | None:
    """Parse the match result from a legacy container log file.

    Looks for a ``MATCH_RESULT:<result>`` line printed by the run script.
    """
    try:
        with open(log_file_path, 'r') as f:
            for line in f:
                if line.startswith('MATCH_RESULT:'):
                    return line.strip().split(':', 1)[1]
    except OSError:
        pass
    return None


def _recover_stale_legacy_matches() -> dict[int, str]:
    """Scan for pending legacy matches whose log file contains a result.

    When the Django server restarts, the daemon threads monitoring legacy
    Docker containers are killed.  The containers finish and write
    ``MATCH_RESULT:<result>`` to their log files, but nobody reads it.
    This function finds those orphaned results and updates the database.

    Returns a dict of ``{match_id: result}`` for recovered matches.
    """
    logs_dir = _get_logs_dir()
    if not os.path.isdir(logs_dir):
        return {}

    recovered: dict[int, str] = {}
    pending = Match.objects.filter(result='Pending')

    for match_obj in pending:
        # Skip matches that have an aiarena run directory (handled separately)
        aiarena_run_dir = aiarena_runner.get_run_dir(match_obj.id)
        if os.path.isdir(aiarena_run_dir):
            continue

        # Look for a log file matching this match ID
        pattern = os.path.join(logs_dir, f"{match_obj.id}_*.log")
        log_files = glob.glob(pattern)
        if not log_files:
            continue

        result = _parse_legacy_result(log_files[0])
        if result:
            match_obj.result = result
            match_obj.end_timestamp = timezone.now()
            match_obj.save()
            recovered[match_obj.id] = result

    return recovered


def _launch_legacy_match(match_id: int, command: list[str], cwd: str, log_file_path: str) -> bool:
    """Launch a legacy single-container Docker match through the queue.

    If at capacity the match is queued and started later.  A monitoring
    thread waits for the process to finish, then parses the result from
    the container log and updates the database.

    Returns ``True`` if started immediately, ``False`` if queued.
    """
    def _launcher():
        def _run():
            try:
                with open(log_file_path, 'w') as log:
                    proc = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=log)
                proc.wait(timeout=7200)
                result = _parse_legacy_result(log_file_path)
                try:
                    match = Match.objects.get(id=match_id)
                    match.result = result or 'Crash'
                    match.end_timestamp = timezone.now()
                    match.save()
                except Match.DoesNotExist:
                    logger.error('Legacy match %d: Match record not found', match_id)
            except Exception:
                logger.exception('Legacy match %d: error', match_id)
            finally:
                match_queue.notify_match_finished()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    return match_queue.enqueue(match_id, _launcher)


def start_custom_bot_match(
    custom_bot: CustomBot,
    test_bot: CustomBot,
    test_group_id: int = -1,
    source_override: str | None = None,
) -> int:
    """Launch a single Docker match against a custom bot.
    Returns the match ID.

    ``test_bot`` is the test-subject bot (player 1).

    ``test_group_id`` defaults to ``-1`` (ad-hoc match).  Pass a real
    TestGroup id to include this match in a test group.

    ``source_override`` overrides the test bot's source directory (e.g.
    a git worktree path for branch-based testing).

    For aiarena-type bots, uses the aiarena local-play-bootstrap infrastructure.
    For python_sc2 / external_python bots, uses the existing single-container approach.
    """
    match = Match(
        test_group_id=test_group_id,
        start_timestamp=datetime.now(),
        map_name=random.choice(MAP_LIST),
        opponent_race=custom_bot.race,
        opponent_difficulty='',
        opponent_build='',
        opponent_bot=custom_bot,
        test_bot=test_bot,
        result="Pending",
    )
    match.save()
    match_id = match.id

    if custom_bot.is_aiarena:
        # Use aiarena infrastructure (supports any language/framework)
        aiarena_runner.start_aiarena_match(
            match, custom_bot, test_bot=test_bot,
            source_override=source_override,
        )
        return match_id

    # Legacy path: python_sc2 / external_python bots
    compose_file = os.path.join(DOCKER_COMPOSE_PATH, 'docker-compose.yml')
    if not os.path.exists(compose_file):
        raise FileNotFoundError(f'docker-compose.yml not found at: {compose_file}')

    _write_legacy_env()
    logs_dir = _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)

    log_file = os.path.join(logs_dir, f"{match_id}_vs_{custom_bot.name}.log")

    command = [
        'docker', 'compose', '-p', f'match_{match_id}',
        'run', '--rm', '--no-deps',
        '-e', f'OPPONENT_FILE={custom_bot.bot_file}',
        '-e', f'OPPONENT_CLASS={custom_bot.bot_class_name}',
        '-e', f'OPPONENT_RACE={custom_bot.race.lower()}',
        '-e', f'MAP_NAME={match.map_name}',
        '-e', f'MATCH_ID={match_id}',
    ]

    if custom_bot.is_external and custom_bot.bot_directory:
        command += ['-e', f'EXTERNAL_BOT_DIR={custom_bot.bot_directory}']

    if source_override:
        override_src = source_override.replace('\\', '/')
        command += ['-v', f'{override_src}:/root/bot']

    command += _env_file_args(test_bot)
    command += [
        'bot',
        'bash', '/root/runner/run_docker_bot_vs_bot.sh',
    ]

    _launch_legacy_match(match_id, command, DOCKER_COMPOSE_PATH, log_file)

    return match_id


def start_test_suite(
    description: str,
    test_bot: CustomBot,
    difficulty: str = 'CheatInsane',
    test_suite: TestSuite | None = None,
    branch: str = '',
) -> tuple[int, int]:
    """
    Create a TestGroup and launch Docker match containers based on the
    test suite configuration.

    When *test_suite* is ``None``, falls back to the "Blizzard AI" suite.
    Suite behaviour is driven entirely by the suite's fields.

    When *branch* is provided, a git worktree is created for that branch
    and the bot source is mounted from the worktree instead of the live
    working directory.  This allows testing multiple branches simultaneously.

    Returns (test_group_id, number of matches started).
    Raises FileNotFoundError if docker-compose.yml is missing.

    *test_bot* is the Player-1 bot.
    Custom bot matches run regardless of difficulty.
    """
    compose_file = os.path.join(DOCKER_COMPOSE_PATH, 'docker-compose.yml')
    if not os.path.exists(compose_file):
        raise FileNotFoundError(f'docker-compose.yml not found at: {compose_file}')

    _write_legacy_env()
    logs_dir = _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)

    # Resolve the test suite — fall back to "Blizzard AI" if none specified
    if test_suite is None:
        test_suite = TestSuite.objects.filter(name='Blizzard AI').first()

    include_blizzard = test_suite.include_blizzard_ai if test_suite else True

    # Resolve custom bots for this suite, always excluding inactive bots
    if test_suite and test_suite.include_all_custom_bots:
        suite_custom_bots = list(CustomBot.objects.filter(is_active=True))
    elif test_suite:
        suite_custom_bots = list(test_suite.custom_bots.filter(is_active=True))
    else:
        suite_custom_bots = None

    # Resolve branch worktree source override
    source_override: str | None = None
    if branch and test_bot and test_bot.source_path:
        source_override = worktrees.get_or_create_worktree(
            test_bot.source_path, branch,
        )

    test_group = TestGroup.objects.create(
        description=description[:255],
        test_suite=test_suite,
        branch=branch,
    )
    test_group_id = test_group.id

    count = 0

    # --- Computer AI matches (15 = 3 races x 5 builds) ---
    if include_blizzard:
        for race in ('protoss', 'terran', 'zerg'):
            for build in ('rush', 'timing', 'macro', 'power', 'air'):
                match_id = create_pending_match(test_group_id, race, build, difficulty, test_bot=test_bot)
                match_obj = Match.objects.get(id=match_id)
                log_file = os.path.join(logs_dir, f"{match_id}_{race}_{build}.log")
                command = [
                    'docker', 'compose', '-p', f'match_{match_id}',
                    'run', '--rm', '--no-deps',
                    '-e', f'RACE={race}',
                    '-e', f'BUILD={build}',
                    '-e', f'MATCH_ID={match_id}',
                    '-e', f'DIFFICULTY={difficulty}',
                    '-e', f'MAP_NAME={match_obj.map_name}',
                ]
                if source_override:
                    override_src = source_override.replace('\\', '/')
                    command += ['-v', f'{override_src}:/root/bot']
                command += _env_file_args(test_bot)
                command.append('bot')
                _launch_legacy_match(match_id, command, DOCKER_COMPOSE_PATH, log_file)
                count += 1

    # --- Custom bot matches ---
    if suite_custom_bots is not None:
        # Use bots selected in the test suite; if the test bot is included,
        # run it as a mirror match instead of skipping.
        bots_to_test = list(suite_custom_bots)
    else:
        # Default: all active custom bots except the test bot
        bots_to_test = list(
            CustomBot.objects.filter(is_active=True).exclude(id=test_bot.id) if test_bot else CustomBot.objects.filter(is_active=True)
        )

    for bot in bots_to_test:
        try:
            start_custom_bot_match(
                bot, test_bot=test_bot, test_group_id=test_group_id,
                source_override=source_override,
            )
            count += 1
        except Exception:
            # Don't let a single custom-bot failure abort the whole suite
            pass

    # --- Past-version matches ---
    version_offsets = test_suite.previous_version_offsets if test_suite else []
    if version_offsets and test_bot and test_bot.source_path:
        max_offset = max(version_offsets)
        repo_path = test_bot.source_path or None
        commits = bot_versions.get_recent_bot_commits(
            count=max_offset, repo_path=repo_path,
        )
        test_race = test_bot.race if test_bot else 'Terran'
        for offset in version_offsets:
            # offsets are 1-based: offset 1 = commits[0] (HEAD~1)
            idx = offset - 1
            if idx < len(commits):
                commit = commits[idx]
                try:
                    match = Match(
                        test_group_id=test_group_id,
                        start_timestamp=datetime.now(),
                        map_name='TBD',
                        opponent_race=test_race,
                        opponent_difficulty='',
                        opponent_build='',
                        result='Pending',
                        opponent_commit_hash=commit.hash,
                        test_bot=test_bot,
                    )
                    match.save()
                    aiarena_runner.start_past_version_match(
                        match, commit.hash, commit.short_hash, test_bot=test_bot,
                        source_override=source_override,
                    )
                    count += 1
                except Exception as e:
                    logging.getLogger('test_lab').exception(
                        'Failed to start past-version match for offset %d (commit %s): %s',
                        offset, commit.short_hash, e,
                    )

    # --- Replay test matches ---
    replay_test_list = list(test_suite.replay_tests.all()) if test_suite else []
    for replay_test in replay_test_list:
        try:
            _launch_replay_test_match(
                replay_test, test_group_id=test_group_id, test_bot=test_bot,
                source_override=source_override,
            )
            count += 1
        except Exception as e:
            logging.getLogger('test_lab').exception(
                'Failed to start replay test match for "%s": %s',
                replay_test.name, e,
            )

    return test_group_id, count


def trigger_tests(request):
    """Trigger the test suite from the web UI.

    The form posts the current filter state (test_bot, difficulty) plus an
    optional description.  After starting the suite, the user is redirected
    back to the match list with those filters preserved.
    """
    if request.method == 'POST':
        difficulty = request.POST.get('difficulty', '') or 'CheatInsane'
        description = request.POST.get('description', '').strip()

        # Resolve test subject bot from the filter value
        test_bot = None
        test_bot_id = request.POST.get('test_bot')
        if test_bot_id:
            test_bot = CustomBot.objects.filter(id=test_bot_id).first()
        if test_bot is None:
            messages.error(request, 'Please select a test bot.')
            referer = request.META.get('HTTP_REFERER', '')
            if referer and '/test_lab/' in referer:
                return redirect(referer)
            return redirect(f"{reverse('results')}?tab=test-groups")

        # Resolve test suite
        test_suite = None
        test_suite_id = request.POST.get('test_suite')
        if test_suite_id and test_suite_id.isdigit():
            test_suite = TestSuite.objects.filter(id=int(test_suite_id)).first()

        try:
            _, count = start_test_suite(
                description=description, difficulty=difficulty, test_bot=test_bot,
                test_suite=test_suite,
            )
            suite_name = test_suite.name if test_suite else 'default'
            messages.success(request, f'Test suite "{suite_name}" started with difficulty {difficulty}! {count} tests running.')
        except Exception as e:
            messages.error(request, f'Failed to start test suite: {str(e)}')

    # Redirect back to the page the user came from (preserves filter state)
    referer = request.META.get('HTTP_REFERER', '')
    if referer and '/test_lab/' in referer:
        return redirect(referer)
    return redirect(f"{reverse('results')}?tab=test-groups")


@csrf_exempt
@require_POST
def api_trigger_tests(request):
    """API endpoint to trigger test suite or custom bot match.

    JSON body:
      - difficulty (str): AI difficulty level (default: CheatInsane)
      - description (str): optional test group description
      - custom_bot_id (int): when set, runs a single match against this
        custom bot instead of the full 15-match test suite
      - branch (str): git branch to test against. When set, a git worktree
        is created and the bot source is mounted from the worktree.
        Multiple branches can be tested simultaneously.
    """
    import json
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    custom_bot_id = body.get('custom_bot_id')
    test_bot_id = body.get('test_bot_id')
    description = body.get('description', '')
    branch = body.get('branch', '')

    # Resolve test subject bot
    test_bot = None
    if test_bot_id is not None:
        try:
            test_bot = CustomBot.objects.get(id=test_bot_id)
        except CustomBot.DoesNotExist:
            return JsonResponse(
                {'status': 'error', 'message': f'Test bot with id {test_bot_id} not found'},
                status=404,
            )
    if test_bot is None:
        return JsonResponse(
            {'status': 'error', 'message': 'test_bot_id is required'},
            status=400,
        )

    # Custom bot match
    if custom_bot_id is not None:
        try:
            custom_bot = CustomBot.objects.get(id=custom_bot_id)
        except CustomBot.DoesNotExist:
            return JsonResponse(
                {'status': 'error', 'message': f'Custom bot with id {custom_bot_id} not found'},
                status=404,
            )
        # Resolve source override for branch testing
        source_override = None
        if branch and test_bot and test_bot.source_path:
            try:
                source_override = worktrees.get_or_create_worktree(
                    test_bot.source_path, branch,
                )
            except ValueError as e:
                return JsonResponse(
                    {'status': 'error', 'message': f'Invalid branch: {e}'},
                    status=400,
                )
        try:
            match_id = start_custom_bot_match(
                custom_bot, test_bot=test_bot,
                source_override=source_override,
            )
            return JsonResponse({
                'status': 'ok',
                'match_id': match_id,
                'custom_bot': custom_bot.name,
                'test_bot': test_bot.name,
                'branch': branch or None,
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    # Standard test suite
    difficulty = body.get('difficulty', 'CheatInsane')
    test_suite = None
    test_suite_id = body.get('test_suite_id')
    if test_suite_id is not None:
        test_suite = TestSuite.objects.filter(id=test_suite_id).first()
        if test_suite is None:
            return JsonResponse(
                {'status': 'error', 'message': f'Test suite with id {test_suite_id} not found'},
                status=404,
            )
    elif test_bot and test_bot.default_test_suite:
        test_suite = test_bot.default_test_suite

    # Validate branch early if provided
    if branch and test_bot and test_bot.source_path:
        try:
            worktrees.get_or_create_worktree(test_bot.source_path, branch)
        except ValueError as e:
            return JsonResponse(
                {'status': 'error', 'message': f'Invalid branch: {e}'},
                status=400,
            )

    try:
        test_group_id, count = start_test_suite(
            description=description, difficulty=difficulty, test_bot=test_bot,
            test_suite=test_suite, branch=branch,
        )
        return JsonResponse({
            'status': 'ok',
            'test_group_id': test_group_id,
            'matches_started': count,
            'difficulty': difficulty,
            'description': description,
            'test_suite': test_suite.name if test_suite else 'default',
            'branch': branch or None,
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def serve_replay(request, match_id):
    """Open replay files with StarCraft 2 locally."""
    config = SystemConfig.load()
    sc2_switcher = config.sc2_switcher_path

    # Check aiarena run directory first
    replay_path = aiarena_runner.get_replay_path(match_id)
    if replay_path:
        subprocess.Popen([sc2_switcher, replay_path])
        return HttpResponse(status=204)

    # Fall back to legacy directory
    replay_dir = _get_logs_dir()
    replay_pattern = os.path.join(replay_dir, f"{match_id}_*.SC2Replay")
    replay_files = glob.glob(replay_pattern)
    
    if not replay_files:
        raise Http404("Replay file not found")
    
    file_path = replay_files[0]
    subprocess.Popen([sc2_switcher, file_path])
    return HttpResponse(status=204)

def serve_log(request, match_id):
    """Serve the main log file for a match.

    For aiarena matches this is the docker compose output log.
    For legacy matches this is the single-container log.
    """
    from django.http import FileResponse

    # Check aiarena run directory first
    log_path = aiarena_runner.get_match_log_path(match_id)
    if log_path:
        return FileResponse(open(log_path, 'rb'), content_type='text/plain')

    # Fall back to legacy directory
    legacy_logs = _get_logs_dir()
    log_pattern = os.path.join(legacy_logs, f"{match_id}*.log")
    log_files = [
        f for f in glob.glob(log_pattern)
        if '_stderr.log' not in f
    ]
    
    if not log_files:
        raise Http404("Log file not found")
    
    file_path = log_files[0]
    return FileResponse(open(file_path, 'rb'), content_type='text/plain')

def serve_aiarena_bot_log(request, match_id, bot_name):
    """Serve a bot's stderr log from an aiarena match.

    Falls back to the docker compose output log when the bot-specific
    stderr log is not available (e.g. match never ran or is still running).
    """
    from django.http import FileResponse
    log_path = aiarena_runner.get_bot_log_path(match_id, bot_name)
    if not log_path:
        # Fall back to compose output log
        log_path = aiarena_runner.get_match_log_path(match_id)
    if not log_path:
        raise Http404(f"No log found for match {match_id}")
    return FileResponse(open(log_path, 'rb'), content_type='text/plain')

def _get_map_breakdown_context(request):
    """Return context dict for the Maps tab."""
    # Define difficulty order to match the filter dropdown
    difficulty_order = [
        'Easy', 'Medium', 'MediumHard', 'Hard', 'Harder', 'VeryHard',
        'CheatVision', 'CheatMoney', 'CheatInsane'
    ]
    
    # Get difficulty filter from request
    selected_difficulty = request.GET.get('difficulty', '')
    selected_limit = request.GET.get('limit', '')
    
    matches = Match.objects.all().exclude(test_group_id=-1)
    
    # Apply difficulty filter if selected
    if selected_difficulty:
        matches = matches.filter(opponent_difficulty=selected_difficulty)
    
    # Apply test group limit if selected — only include matches from the N most recent test groups
    if selected_limit and selected_limit.isdigit():
        recent_group_ids = list(
            Match.objects.exclude(test_group_id=-1)
            .values_list('test_group_id', flat=True)
            .distinct()
            .order_by('-test_group_id')[:int(selected_limit)]
        )
        matches = matches.filter(test_group_id__in=recent_group_ids)
    
    # Group matches by map and create pivot structure
    grouped_matches = defaultdict(lambda: defaultdict(list))  # map -> opponent -> [matches]
    all_opponents = set()
    difficulty_groups = defaultdict(lambda: defaultdict(list))  # difficulty -> race -> builds
    
    # Track win/loss counts for each map/opponent combination
    map_opponent_stats = defaultdict(lambda: {'victories': 0, 'total_games': 0, 'total_duration': 0, 'games_with_duration': 0})
    
    for match in matches:
        if match.map_name == "TBD":
            continue  # Skip matches without a valid map name
        opponent_name = f"{match.opponent_race}-{match.opponent_difficulty}-{match.opponent_build}"
        all_opponents.add(opponent_name)
        
        # Group by map
        grouped_matches[match.map_name][opponent_name].append(match)
        
        # Track stats for this map/opponent combination
        key = (match.map_name, opponent_name)
        if match.result in ['Victory', 'Defeat']:
            map_opponent_stats[key]['total_games'] += 1
            if match.result == 'Victory':
                map_opponent_stats[key]['victories'] += 1
        
        if match.duration_in_game_time is not None and match.duration_in_game_time > 0:
            map_opponent_stats[key]['total_duration'] += match.duration_in_game_time
            map_opponent_stats[key]['games_with_duration'] += 1
        
        # Build hierarchical structure for headers
        difficulty_groups[match.opponent_difficulty][match.opponent_race].append(match.opponent_build)
    
    # Sort and deduplicate builds within each race/difficulty group
    for difficulty in difficulty_groups:
        for race in difficulty_groups[difficulty]:
            difficulty_groups[difficulty][race] = sorted(list(set(difficulty_groups[difficulty][race])))
    
    # Create ordered list of opponents for consistent column ordering
    sorted_opponents = []
    sorted_difficulties = sorted(difficulty_groups.keys(), key=lambda x: difficulty_order.index(x) if x in difficulty_order else 999)
    
    # Build header structure and opponent order
    header_structure = []
    for difficulty in sorted_difficulties:
        races = sorted(difficulty_groups[difficulty].keys())
        difficulty_span = sum(len(difficulty_groups[difficulty][race]) for race in races)
        
        race_headers = []
        for race in races:
            builds = difficulty_groups[difficulty][race]
            for build in builds:
                opponent_name = f"{race}-{difficulty}-{build}"
                sorted_opponents.append(opponent_name)
            
            race_headers.append({
                'name': race,
                'span': len(builds),
                'builds': builds
            })
        
        header_structure.append({
            'difficulty': difficulty,
            'span': difficulty_span,
            'races': race_headers
        })
    
    # Sort maps alphabetically
    sorted_maps = sorted(grouped_matches.keys())
    
    # Create the pivot table data
    pivot_data = []
    for map_name in sorted_maps:
        row = {'map_name': map_name, 'results': [], 'overall_win_rate': None, 'overall_avg_duration': None, 'overall_wins': 0, 'overall_games': 0}
        map_total_victories = 0
        map_total_games = 0
        map_total_duration = 0
        map_games_with_duration = 0
        for opponent in sorted_opponents:
            key = (map_name, opponent)
            stats = map_opponent_stats[key]
            
            # Calculate win percentage for this map/opponent combo
            if stats['total_games'] > 0:
                win_percentage = (stats['victories'] / stats['total_games']) * 100
                win_rate_str = f"{win_percentage:.0f}%"
            else:
                win_rate_str = None
            
            # Calculate average duration for this map/opponent combo
            if stats['games_with_duration'] > 0:
                avg_duration = int(stats['total_duration'] / stats['games_with_duration'])
            else:
                avg_duration = None
            
            cell_data = {
                'win_rate': win_rate_str,
                'avg_duration': avg_duration,
                'wins': stats['victories'],
                'games_played': stats['total_games']
            }
            
            row['results'].append(cell_data)
            map_total_victories += stats['victories']
            map_total_games += stats['total_games']
            map_total_duration += stats['total_duration']
            map_games_with_duration += stats['games_with_duration']
        
        if map_total_games > 0:
            overall_win_percentage = (map_total_victories / map_total_games) * 100
            row['overall_win_rate'] = f"{overall_win_percentage:.0f}%"
            row['overall_wins'] = map_total_victories
            row['overall_games'] = map_total_games
        else:
            row['overall_win_rate'] = None
            row['overall_wins'] = 0
            row['overall_games'] = 0
        
        if map_games_with_duration > 0:
            row['overall_avg_duration'] = int(map_total_duration / map_games_with_duration)
        else:
            row['overall_avg_duration'] = None

        pivot_data.append(row)
    
    # Calculate win rates for header structure (same as match_list)
    opponent_stats = defaultdict(lambda: {'victories': 0, 'total_games': 0})
    for opponent in sorted_opponents:
        for map_name in sorted_maps:
            key = (map_name, opponent)
            stats = map_opponent_stats[key]
            opponent_stats[opponent]['total_games'] += stats['total_games']
            opponent_stats[opponent]['victories'] += stats['victories']
    
    # Calculate win rates by race within each difficulty
    for difficulty_group in header_structure:
        difficulty_name = difficulty_group['difficulty']
        for race_group in difficulty_group['races']:
            race_name = race_group['name']
            race_victories = 0
            race_total_games = 0
            
            for opponent in sorted_opponents:
                if opponent.startswith(f"{race_name}-{difficulty_name}-"):
                    stats = opponent_stats[opponent]
                    race_total_games += stats['total_games']
                    race_victories += stats['victories']
            
            if race_total_games > 0:
                race_win_percentage = (race_victories / race_total_games) * 100
                race_group['win_rate'] = f"{race_win_percentage:.0f}%"
            else:
                race_group['win_rate'] = "-"
            
            # Add win rates to individual builds
            for i, build in enumerate(race_group['builds']):
                opponent_name = f"{race_name}-{difficulty_name}-{build}"
                stats = opponent_stats[opponent_name]
                if stats['total_games'] > 0:
                    build_win_percentage = (stats['victories'] / stats['total_games']) * 100
                    race_group['builds'][i] = f"{build} {build_win_percentage:.0f}%"
                else:
                    race_group['builds'][i] = f"{build} -"
    
    # Calculate win rates by difficulty
    difficulty_win_rates = {}
    for difficulty in sorted_difficulties:
        difficulty_victories = 0
        difficulty_total_games = 0
        for opponent in sorted_opponents:
            if f"-{difficulty}-" in opponent:
                stats = opponent_stats[opponent]
                difficulty_total_games += stats['total_games']
                difficulty_victories += stats['victories']
        
        if difficulty_total_games > 0:
            difficulty_win_percentage = (difficulty_victories / difficulty_total_games) * 100
            difficulty_win_rates[difficulty] = f"{difficulty_win_percentage:.0f}%"
        else:
            difficulty_win_rates[difficulty] = "-"
    
    # Add difficulty win rates to header structure
    for difficulty_group in header_structure:
        difficulty_group['win_rate'] = difficulty_win_rates.get(difficulty_group['difficulty'], "-")
    
    return {
        'pivot_data': pivot_data,
        'opponents': sorted_opponents,
        'header_structure': header_structure,
        'selected_difficulty': selected_difficulty,
        'selected_limit': selected_limit
    }


def _get_building_timing_context():
    """View to display earliest building construction times per test group."""
    from collections import defaultdict

    # Get all building events with their match's test_group_id
    # Using Django ORM: Get minimum game_timestamp for each (test_group_id, building_type) combination
    building_events = (
        MatchEvent.objects
        .filter(type='Building')
        .values('match__test_group_id', 'match_id', 'message', 'match__result')
        .annotate(earliest_time=Min('game_timestamp'))
        .order_by('match__test_group_id', 'message')
    )
    
    # Organize data into a pivot structure
    # {test_group_id: {building_type: {min, max, avg}}}
    grouped_data = defaultdict(dict[str, dict[str, any]])
    all_building_types = set()
    
    for event in building_events:
        test_group_id = event['match__test_group_id']
        building_type = event['message']
        earliest_time = event['earliest_time']
        result = event['match__result'][0]
        
        if building_type not in grouped_data[test_group_id]:
            grouped_data[test_group_id][building_type] = { # type: ignore
                "min": earliest_time,
                "max": earliest_time,
                "avg": earliest_time,
                "count": 1,
                "min_result": result,
                "max_result": result,
            }
            all_building_types.add(building_type)
        else:
            current = grouped_data[test_group_id][building_type]
            if earliest_time < current["min"]:
                current["min"] = earliest_time
                current["min_result"] = result
            if earliest_time > current["max"]:
                current["max"] = earliest_time
                current["max_result"] = result
            # For average, we will need to calculate it later
            current["avg"] += earliest_time  # Temporarily sum for average calculation
            current["count"] = current["count"] + 1 # type: ignore
    for test_group_id in grouped_data:
        for building_type in grouped_data[test_group_id]:
            current = grouped_data[test_group_id][building_type]
            current["avg"] = current["avg"] / current["count"] # type: ignore
            del current["count"]  # Remove count as it's no longer needed

    # Sort building types alphabetically for consistent column order
    building_types_list = list(all_building_types)
    
    # Sort test groups in descending order (newest first)
    sorted_groups = sorted(grouped_data.keys(), reverse=True)

    # Calculate average timing for each building type across all test groups
    avg_timings = []
    for building_type in building_types_list:
        timings: List[float | None] = [grouped_data[gid].get(building_type).get("avg") for gid in sorted_groups if grouped_data[gid].get(building_type) is not None] # type: ignore
        if timings:
            avg_timings.append(sum(timings) / len(timings)) # type: ignore
        else:
            avg_timings.append(None)

    # Sort building types by average timing
    sorted_building_types, avg_timings = zip(*sorted(zip(building_types_list, avg_timings), key=lambda x: x[1]))
    
    # Create a dict for quick lookup of average timings
    avg_timing_dict = dict(zip(sorted_building_types, avg_timings))
    
    # Create pivot table data with performance class
    pivot_data = []
    for group_id in sorted_groups:
        row = {
            'test_group_id': group_id,
            'timings': []
        }
        for building_type in sorted_building_types:
            timing = grouped_data[group_id].get(building_type)
            if timing and avg_timing_dict.get(building_type):
                avg = avg_timing_dict[building_type]
                diff = timing['avg'] - avg
                
                # Determine performance class
                if diff < -10:
                    performance_class = 'much-faster'
                elif diff < -5:
                    performance_class = 'faster'
                elif diff < 0:
                    performance_class = 'slightly-faster'
                elif diff > 10:
                    performance_class = 'much-slower'
                elif diff > 5:
                    performance_class = 'slower'
                elif diff > 0:
                    performance_class = 'slightly-slower'
                else:
                    performance_class = 'average'
                
                timing['performance_class'] = performance_class # type: ignore
            
            row['timings'].append(timing)
        pivot_data.append(row)
    
    
    return {
        'pivot_data': pivot_data,
        'building_types': sorted_building_types,
        'avg_timings': avg_timings,
    }


def results_page(request):
    """Combined Results page with tabs for Test Groups, Maps, Building Timing."""
    active_tab = request.GET.get('tab', 'test-groups')

    context = {
        'active_page': 'results',
        'active_tab': active_tab,
    }

    if active_tab == 'maps':
        context.update(_get_map_breakdown_context(request))
    elif active_tab == 'building-timing':
        context.update(_get_building_timing_context())
    else:
        active_tab = 'test-groups'
        context['active_tab'] = active_tab
        context.update(_get_match_list_context(request))

    # Build filter query string for tab links (preserves filters across tabs)
    filter_params = []
    for key, val in request.GET.items():
        if key != 'tab' and val:
            filter_params.append(f'{key}={val}')
    context['filter_qs'] = '&' + '&'.join(filter_params) if filter_params else ''

    return render(request, 'test_lab/results.html', context)


def run_match_page(request):
    """Run Match page with match type tabs and custom match results table."""
    custom_bots_list = CustomBot.objects.all().order_by('name')
    test_subject_bots = CustomBot.objects.filter(is_test_subject=True).order_by('name')
    version_test_bots = CustomBot.objects.filter(source_path__gt='').order_by('name')

    # Collect recent commits for each test-subject bot that has a git repo
    recent_commits_by_bot: dict[int, list] = {}
    for bot in version_test_bots:
        recent_commits_by_bot[bot.id] = bot_versions.get_recent_bot_commits(
            count=5, repo_path=bot.source_path,
        )

    # Default commits shown in the Past Version dropdown (first version bot)
    first_version_bot = version_test_bots.first()
    recent_commits = recent_commits_by_bot.get(first_version_bot.id, []) if first_version_bot else []

    # JSON-serializable version for the commit-switcher JS
    commits_by_bot_json = {
        str(bot_id): [
            {
                'hash': c.hash,
                'short_hash': c.short_hash,
                'subject': c.subject[:60],
                'date': c.date[:10],
                'is_cached': c.is_cached,
            }
            for c in commits
        ]
        for bot_id, commits in recent_commits_by_bot.items()
    }

    # Custom match list data
    matches = (
        Match.objects
        .filter(
            Q(opponent_bot__isnull=False)
            | Q(replay_file__gt='')
            | Q(opponent_commit_hash__gt='')
        )
        .select_related('opponent_bot', 'test_bot')
        .order_by('-start_timestamp')[:50]
    )

    selected_test_bot = request.GET.get('test_bot', '')
    if selected_test_bot:
        if selected_test_bot == 'bottato':
            matches = matches.filter(test_bot__isnull=True)
        elif selected_test_bot.isdigit():
            matches = matches.filter(test_bot_id=int(selected_test_bot))

    return render(request, 'test_lab/run_match.html', {
        'active_page': 'run_match',
        'custom_bots': custom_bots_list,
        'test_subject_bots': test_subject_bots,
        'version_test_bots': version_test_bots,
        'recent_commits': recent_commits,
        'recent_commits_by_bot': recent_commits_by_bot,
        'commits_by_bot_json': commits_by_bot_json,
        'matches': matches,
        'selected_test_bot': selected_test_bot,
        'replay_tests': ReplayTest.objects.order_by('name'),
    })


def config_page(request):
    """Config page with Custom Bots, Test Suites, and System tabs."""
    import json as _json

    bots = CustomBot.objects.all().order_by('-created_at')
    all_bot_details = aiarena_runner.get_available_aiarena_bot_details()
    used_directories = set(
        CustomBot.objects.values_list('bot_directory', flat=True)
    )
    available_bots = [
        b for b in all_bot_details if b['directory'] not in used_directories
    ]
    custom_bots_list = CustomBot.objects.all().order_by('name')
    test_suites = TestSuite.objects.prefetch_related('custom_bots', 'replay_tests').order_by('name')
    test_suites_json = _json.dumps([
        {
            'id': s.id,
            'name': s.name,
            'is_protected': s.is_protected,
            'include_blizzard_ai': s.include_blizzard_ai,
            'include_all_custom_bots': s.include_all_custom_bots,
            'custom_bot_ids': list(s.custom_bots.values_list('id', flat=True)),
            'replay_test_ids': list(s.replay_tests.values_list('id', flat=True)),
            'previous_versions': s.previous_versions,
        }
        for s in test_suites
    ])
    replay_tests_list = ReplayTest.objects.prefetch_related('test_suites').order_by('-created_at')
    all_replay_tests = ReplayTest.objects.order_by('name')
    system_config = SystemConfig.load()
    prompt_templates = PromptTemplate.objects.prefetch_related('bots').order_by('name')
    test_subject_bots = CustomBot.objects.filter(is_test_subject=True).order_by('name')
    prompt_templates_json = _json.dumps([
        {
            'id': t.id,
            'name': t.name,
            'filename': t.filename,
            'bot_ids': list(t.bots.values_list('id', flat=True)),
        }
        for t in prompt_templates
    ])

    return render(request, 'test_lab/config.html', {
        'active_page': 'config',
        'bots': bots,
        'aiarena_bots': available_bots,
        'aiarena_bots_json': _json.dumps(available_bots),
        'custom_bots': custom_bots_list,
        'test_suites': test_suites,
        'test_suites_json': test_suites_json,
        'replay_tests': replay_tests_list,
        'all_replay_tests': all_replay_tests,
        'system_config': system_config,
        'prompt_templates': prompt_templates,
        'prompt_templates_json': prompt_templates_json,
        'test_subject_bots': test_subject_bots,
    })


@require_POST
def update_system_config(request):
    """Update system-wide settings from the System tab."""
    config_url = f"{reverse('config_page')}#system"

    max_concurrent_raw = request.POST.get('max_concurrent_matches', '0').strip()
    if not max_concurrent_raw.isdigit():
        messages.error(request, 'Max concurrent matches must be a non-negative integer.')
        return redirect(config_url)

    max_concurrent = int(max_concurrent_raw)
    config = SystemConfig.load()
    config.max_concurrent_matches = max_concurrent
    config.logs_dir = request.POST.get('logs_dir', '').strip()
    config.sc2_switcher_path = request.POST.get('sc2_switcher_path', '').strip()
    config.sc2_maps_path = request.POST.get('sc2_maps_path', '').strip()
    config.replays_dir = request.POST.get('replays_dir', '').strip()
    config.save()

    label = 'unlimited' if max_concurrent == 0 else str(max_concurrent)
    messages.success(request, f'System config updated (max concurrent: {label}).')

    # Drain queue in case the new limit is higher
    match_queue.drain_queue()

    return redirect(config_url)


def setup_page(request):
    """First-run setup page for configuring required system paths."""
    config = SystemConfig.load()
    if config.is_configured:
        return redirect('results')
    return render(request, 'test_lab/setup.html', {
        'active_page': 'setup',
        'config': config,
    })


@require_POST
def save_setup(request):
    """Save first-run setup form and redirect to the results page."""
    config = SystemConfig.load()

    config.logs_dir = request.POST.get('logs_dir', '').strip()
    config.sc2_maps_path = request.POST.get('sc2_maps_path', '').strip()
    config.sc2_switcher_path = request.POST.get('sc2_switcher_path', '').strip()
    config.replays_dir = request.POST.get('replays_dir', '').strip()

    max_concurrent_raw = request.POST.get('max_concurrent_matches', '0').strip()
    if max_concurrent_raw.isdigit():
        config.max_concurrent_matches = int(max_concurrent_raw)

    if not config.logs_dir or not config.sc2_maps_path:
        messages.error(request, 'Logs Directory and SC2 Maps Path are required.')
        return render(request, 'test_lab/setup.html', {
            'active_page': 'setup',
            'config': config,
        })

    config.save()
    messages.success(request, 'Setup complete! You can change these settings on the Config → System tab.')
    return redirect('results')


def custom_page(request):
    """Custom page with Recompile Cython."""
    return render(request, 'test_lab/custom.html', {
        'active_page': 'custom',
    })




@require_POST
def create_test_suite(request):
    """Create a new test suite from form data."""
    config_url = f"{reverse('config_page')}#test-suites"
    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, 'Test suite name is required.')
        return redirect(config_url)

    if TestSuite.objects.filter(name=name).exists():
        messages.error(request, f'A test suite named "{name}" already exists.')
        return redirect(config_url)

    include_blizzard_ai = request.POST.get('include_blizzard_ai') == 'on'
    include_all_custom_bots = request.POST.get('include_all_custom_bots') == 'on'
    selected_bot_ids = request.POST.getlist('custom_bot_ids')
    selected_replay_test_ids = request.POST.getlist('replay_test_ids')
    previous_versions = request.POST.get('previous_versions', '').strip()

    suite = TestSuite.objects.create(
        name=name,
        include_blizzard_ai=include_blizzard_ai,
        include_all_custom_bots=include_all_custom_bots,
        previous_versions=previous_versions,
    )
    if selected_bot_ids:
        suite.custom_bots.set(selected_bot_ids)
    if selected_replay_test_ids:
        suite.replay_tests.set(selected_replay_test_ids)

    messages.success(request, f'Test suite "{name}" created.')
    return redirect(config_url)


@require_POST
def delete_test_suite(request, suite_id):
    """Delete a test suite. Protected suites cannot be deleted."""
    config_url = f"{reverse('config_page')}#test-suites"
    try:
        suite = TestSuite.objects.get(id=suite_id)
        if suite.is_protected:
            messages.error(request, f'Cannot delete the protected "{suite.name}" test suite.')
            return redirect(config_url)
        suite_name = suite.name
        suite.delete()
        messages.success(request, f'Test suite "{suite_name}" deleted.')
    except TestSuite.DoesNotExist:
        messages.error(request, 'Test suite not found.')
    return redirect(config_url)


@require_POST
def update_test_suite(request, suite_id):
    """Update an existing test suite. Protected suites cannot be edited."""
    config_url = f"{reverse('config_page')}#test-suites"
    try:
        suite = TestSuite.objects.get(id=suite_id)
    except TestSuite.DoesNotExist:
        messages.error(request, 'Test suite not found.')
        return redirect(config_url)

    if suite.is_protected:
        messages.error(request, f'Cannot edit the protected "{suite.name}" test suite.')
        return redirect(config_url)

    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, 'Test suite name is required.')
        return redirect(config_url)

    # Check uniqueness (excluding current suite)
    if TestSuite.objects.filter(name=name).exclude(id=suite_id).exists():
        messages.error(request, f'A test suite named "{name}" already exists.')
        return redirect(config_url)

    suite.name = name
    suite.include_blizzard_ai = request.POST.get('include_blizzard_ai') == 'on'
    suite.include_all_custom_bots = request.POST.get('include_all_custom_bots') == 'on'
    suite.previous_versions = request.POST.get('previous_versions', '').strip()
    suite.save()

    selected_bot_ids = request.POST.getlist('custom_bot_ids')
    suite.custom_bots.set(selected_bot_ids)

    selected_replay_test_ids = request.POST.getlist('replay_test_ids')
    suite.replay_tests.set(selected_replay_test_ids)

    messages.success(request, f'Test suite "{name}" updated.')
    return redirect(config_url)


@require_POST
def update_custom_bot_active(request, bot_id):
    """Toggle a custom bot's active status. Returns JSON."""
    try:
        bot = CustomBot.objects.get(id=bot_id)
    except CustomBot.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Bot not found'}, status=404)

    is_active = request.POST.get('is_active') == 'on'
    bot.is_active = is_active
    bot.save(update_fields=['is_active'])
    return JsonResponse({'status': 'ok', 'is_active': bot.is_active})


def recompile_cython(request):
    """Trigger recompilation of Cython extensions."""
    if request.method == 'POST':
        cython_dir = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', '..', 'bot', 'cython_extensions'
        ))
        setup_py = os.path.join(cython_dir, 'setup.py')

        if not os.path.exists(setup_py):
            messages.error(request, f'setup.py not found at: {setup_py}')
            return redirect('custom_page')

        try:
            result = subprocess.run(
                ['python', 'setup.py', 'build_ext', '--inplace'],
                cwd=cython_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                messages.success(request, 'Cython extensions recompiled successfully.')
            else:
                messages.error(request, f'Cython compilation failed:\n{result.stderr}')
        except subprocess.TimeoutExpired:
            messages.error(request, 'Cython compilation timed out after 120 seconds.')
        except Exception as e:
            messages.error(request, f'Failed to recompile Cython extensions: {str(e)}')

    return redirect('custom_page')


def run_single_match(request):
    """Run a single match outside of a test group."""
    if request.method == 'POST':
        race = request.POST.get('race', 'random')
        build = request.POST.get('build', 'randombuild')
        difficulty = request.POST.get('difficulty', 'CheatInsane')

        # Resolve test subject bot
        test_bot_id = request.POST.get('test_bot_id')
        test_bot = None
        if test_bot_id:
            test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()
        if test_bot is None:
            messages.error(request, 'Please select a test bot.')
            return redirect('run_match')

        docker_compose_path = DOCKER_COMPOSE_PATH
        _write_legacy_env()
        logs_dir = _get_logs_dir()
        os.makedirs(logs_dir, exist_ok=True)

        try:
            # Create a pending match with test_group_id = -1 (not part of a test group)
            match_id = create_pending_match(-1, race, build, difficulty, test_bot=test_bot)
            match_obj = Match.objects.get(id=match_id)

            log_file = os.path.join(logs_dir, f"{match_id}_{race}_{build}.log")

            command = [
                'docker', 'compose', '-p', f'match_{match_id}',
                'run', '--rm', '--no-deps',
                '-e', f'RACE={race}',
                '-e', f'BUILD={build}',
                '-e', f'DIFFICULTY={difficulty}',
                '-e', f'MATCH_ID={match_id}',
                '-e', f'MAP_NAME={match_obj.map_name}',
            ] + _env_file_args(test_bot) + ['bot']

            started = _launch_legacy_match(match_id, command, docker_compose_path, log_file)

            status = 'started' if started else 'queued'
            messages.success(
                request,
                f'Single match {status}: {race} {build} @ {difficulty} (match #{match_id})'
            )
        except Exception as e:
            messages.error(request, f'Failed to start match: {str(e)}')

    return redirect('run_match')


def position_is_between(request):
    """
    Render an interactive visualization page for testing GeometryMixin.position_is_between.

    This view serves the ``test_lab/position_is_between.html`` template, which provides
    a drag-and-drop style interface to visualize how the geometry helper evaluates
    whether a point lies between two other points.
    """
    return render(request, 'test_lab/position_is_between.html')


# ---------------------------------------------------------------------------
# Custom Bot management
# ---------------------------------------------------------------------------

RUNNER_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'runner'))




@require_POST
def create_custom_bot(request):
    """Register a new AI Arena bot from form data."""
    config_bots_url = f"{reverse('config_page')}#custom-bots"
    name = request.POST.get('name', '').strip()
    race = request.POST.get('race', 'Random')
    description = request.POST.get('description', '').strip()
    bot_directory = request.POST.get('bot_directory', '').strip()
    aiarena_bot_type = request.POST.get('aiarena_bot_type', 'python').strip()
    is_test_subject = request.POST.get('is_test_subject') == 'on'
    source_path = request.POST.get('source_path', '').strip()
    enable_version_history = request.POST.get('enable_version_history') == 'on'
    dockerfile = request.POST.get('dockerfile', '').strip()
    env_file = request.POST.get('env_file', '').strip()

    if not name:
        messages.error(request, 'Bot name is required.')
        return redirect(config_bots_url)

    if not bot_directory:
        messages.error(request, 'Bot directory is required.')
        return redirect(config_bots_url)

    error = aiarena_runner.validate_bot_directory(bot_directory)
    if error:
        messages.error(request, error)
        return redirect(config_bots_url)

    if is_test_subject and source_path and not os.path.isdir(source_path):
        messages.error(request, f'Source path not found: {source_path}')
        return redirect(config_bots_url)

    # Auto-detect symlinks/junctions in the source directory
    symlink_mounts: list[dict[str, str]] = []
    if is_test_subject and source_path:
        symlink_mounts = aiarena_runner.scan_directory_symlinks(source_path)

    try:
        bot = CustomBot.objects.create(
            name=name,
            race=race,
            bot_type='aiarena',
            bot_directory=bot_directory,
            aiarena_bot_type=aiarena_bot_type,
            is_test_subject=is_test_subject,
            source_path=source_path,
            enable_version_history=enable_version_history,
            symlink_mounts=symlink_mounts,
            dockerfile=dockerfile,
            env_file=env_file,
            description=description,
        )
        msg = f'Bot "{name}" registered successfully.'
        if symlink_mounts:
            link_names = ', '.join(m['name'] for m in symlink_mounts)
            msg += f' Detected symlinks: {link_names}'
        messages.success(request, msg)
    except Exception as e:
        messages.error(request, f'Failed to create bot: {e}')

    return redirect(config_bots_url)


@csrf_exempt
@require_POST
def update_custom_bot_test_suite(request, bot_id):
    """Update a bot's default test suite."""
    try:
        bot = CustomBot.objects.get(id=bot_id)
    except CustomBot.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Bot not found'}, status=404)

    test_suite_id = request.POST.get('test_suite_id', '').strip()
    if test_suite_id:
        try:
            suite = TestSuite.objects.get(id=int(test_suite_id))
            bot.default_test_suite = suite
        except (TestSuite.DoesNotExist, ValueError):
            return JsonResponse({'status': 'error', 'message': 'Test suite not found'}, status=404)
    else:
        bot.default_test_suite = None

    bot.save(update_fields=['default_test_suite'])
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_POST
def update_custom_bot_test_subject(request, bot_id):
    """Update a bot's test subject settings."""
    try:
        bot = CustomBot.objects.get(id=bot_id)
    except CustomBot.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Bot not found'}, status=404)

    is_test_subject = request.POST.get('is_test_subject') == 'on'

    update_fields = ['is_test_subject']

    if is_test_subject:
        source_path = request.POST.get('source_path', '').strip()
        enable_version_history = request.POST.get('enable_version_history') == 'on'
        dockerfile = request.POST.get('dockerfile', '').strip()
        env_file = request.POST.get('env_file', '').strip()

        if source_path and not os.path.isdir(source_path):
            return JsonResponse({'status': 'error', 'message': f'Source path not found: {source_path}'}, status=400)

        symlink_mounts = aiarena_runner.scan_directory_symlinks(source_path)

        bot.is_test_subject = True
        bot.source_path = source_path
        bot.enable_version_history = enable_version_history
        bot.dockerfile = dockerfile
        bot.env_file = env_file
        bot.symlink_mounts = symlink_mounts
        update_fields += ['source_path', 'enable_version_history', 'dockerfile', 'env_file', 'symlink_mounts']
    else:
        bot.is_test_subject = False
        bot.source_path = ''
        bot.enable_version_history = False
        bot.dockerfile = ''
        bot.env_file = ''
        bot.symlink_mounts = []
        update_fields += ['source_path', 'enable_version_history', 'dockerfile', 'env_file', 'symlink_mounts']

    bot.save(update_fields=update_fields)
    return JsonResponse({'status': 'ok'})


@require_POST
def delete_custom_bot(request, bot_id):
    """Delete a registered custom bot."""
    config_bots_url = f"{reverse('config_page')}#custom-bots"
    try:
        bot = CustomBot.objects.get(id=bot_id)
        bot_name = bot.name
        bot.delete()
        messages.success(request, f'Custom bot "{bot_name}" deleted.')
    except CustomBot.DoesNotExist:
        messages.error(request, 'Custom bot not found.')

    return redirect(config_bots_url)


@require_POST
def run_custom_match(request):
    """Run a match of a test-subject bot vs a custom opponent bot."""
    bot_id = request.POST.get('custom_bot_id')
    if not bot_id:
        messages.error(request, 'No custom bot selected.')
        return redirect('run_match')

    try:
        custom_bot = CustomBot.objects.get(id=bot_id)
    except CustomBot.DoesNotExist:
        messages.error(request, 'Selected custom bot not found.')
        return redirect('run_match')

    # Resolve test subject bot
    test_bot = None
    test_bot_id = request.POST.get('test_bot_id')
    if test_bot_id:
        test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()
    if test_bot is None:
        messages.error(request, 'Please select a test bot.')
        return redirect('run_match')

    try:
        match_id = start_custom_bot_match(custom_bot, test_bot=test_bot)
        messages.success(
            request,
            f'Custom match started: {test_bot.name} vs {custom_bot.name} (match #{match_id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start custom match: {e}')

    return redirect('run_match')


@require_POST
def run_past_version_match(request):
    """Run a match of the current version of a test-subject bot vs a past version."""
    commit_hash = request.POST.get('commit_hash', '').strip()
    if not commit_hash:
        messages.error(request, 'No commit selected.')
        return redirect('run_match')

    # Validate commit hash format (full 40-char SHA)
    if len(commit_hash) != 40 or not all(c in '0123456789abcdef' for c in commit_hash):
        messages.error(request, 'Invalid commit hash.')
        return redirect('run_match')

    short_hash = commit_hash[:7]

    # Resolve test subject bot — must have a git repo for past-version matches
    test_bot = None
    test_bot_id = request.POST.get('test_bot_id')
    if test_bot_id:
        test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()
    if test_bot is None or not test_bot.source_path:
        messages.error(request, 'Selected bot does not have a git repository configured.')
        return redirect('run_match')

    test_name = test_bot.name
    test_race = test_bot.race

    try:
        # Create the match record
        match = Match(
            test_group_id=-1,
            start_timestamp=datetime.now(),
            map_name="TBD",
            opponent_race=test_race,
            opponent_difficulty='',
            opponent_build='',
            result="Pending",
            opponent_commit_hash=commit_hash,
            test_bot=test_bot,
        )
        match.save()

        aiarena_runner.start_past_version_match(
            match, commit_hash, short_hash, test_bot=test_bot,
        )
        messages.success(
            request,
            f'Past version match started: {test_name} (current) vs {test_name}@{short_hash} '
            f'(match #{match.id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start past version match: {e}')

    return redirect('run_match')




# ---------------------------------------------------------------------------
# Continue from Replay
# ---------------------------------------------------------------------------


def _parse_game_time(time_str: str) -> int | None:
    """Parse a game time string (mm:ss or raw seconds) into game loops.

    Game loops = seconds * 22.4 (SC2 "faster" speed).
    Accepts:
        "5:30"  -> 5 min 30 sec -> 7392 loops
        "330"   -> 330 seconds  -> 7392 loops
        "7392"  -> treated as raw seconds unless it looks like mm:ss
    """
    time_str = time_str.strip()
    if not time_str:
        return None

    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) != 2:
            return None
        try:
            minutes, seconds = int(parts[0]), int(parts[1])
            total_seconds = minutes * 60 + seconds
        except ValueError:
            return None
    else:
        try:
            total_seconds = int(time_str)
        except ValueError:
            return None

    return int(total_seconds * 22.4)


@require_POST
def run_replay_match(request):
    """Launch a match that continues from an uploaded replay at a specified time."""
    replay_file = request.FILES.get('replay_file')
    takeover_time = request.POST.get('takeover_time', '').strip()
    difficulty = request.POST.get('difficulty', 'CheatInsane')
    build = request.POST.get('build', 'Macro')
    race = request.POST.get('race', 'Random')
    bot_player_id = request.POST.get('bot_player_id', '1')

    # Resolve test subject bot
    test_bot = None
    test_bot_id = request.POST.get('test_bot_id')
    if test_bot_id:
        test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()
    if test_bot is None:
        messages.error(request, 'Please select a test bot.')
        return redirect('run_match')

    # Validate inputs
    if not replay_file:
        messages.error(request, 'Please upload a replay file.')
        return redirect('run_match')

    if not replay_file.name.endswith('.SC2Replay'):
        messages.error(request, 'File must be a .SC2Replay file.')
        return redirect('run_match')

    game_loop = _parse_game_time(takeover_time)
    if game_loop is None or game_loop <= 0:
        messages.error(request, 'Invalid takeover time. Use mm:ss (e.g. 5:30) or seconds (e.g. 330).')
        return redirect('run_match')

    try:
        bot_player_id_int = int(bot_player_id)
        if bot_player_id_int not in (1, 2):
            raise ValueError
    except ValueError:
        messages.error(request, 'Bot player ID must be 1 or 2.')
        return redirect('run_match')

    docker_compose_path = DOCKER_COMPOSE_PATH
    _write_legacy_env()
    logs_dir = _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)

    try:
        # Create a pending match
        match = Match(
            test_group_id=-1,
            start_timestamp=datetime.now(),
            map_name="TBD (from replay)",
            opponent_race=race,
            opponent_difficulty=difficulty,
            opponent_build=build,
            test_bot=test_bot,
            result="Pending",
            replay_takeover_game_loop=game_loop,
        )
        match.save()
        match_id = match.id

        # Save the uploaded replay to the shared replays directory
        replay_filename = f"{match_id}_source.SC2Replay"
        replay_dest = os.path.join(logs_dir, replay_filename)
        with open(replay_dest, 'wb') as dest:
            for chunk in replay_file.chunks():
                dest.write(chunk)

        # Update the match with the replay file path (container-side path)
        container_replay_path = f"/root/replays/{replay_filename}"
        match.replay_file = container_replay_path
        match.save()

        log_file = os.path.join(logs_dir, f"{match_id}_continue_replay.log")

        command = [
            'docker', 'compose', '-p', f'match_{match_id}',
            'run', '--rm', '--no-deps',
            '-e', f'REPLAY_PATH={container_replay_path}',
            '-e', f'TAKEOVER_GAME_LOOP={game_loop}',
            '-e', f'BOT_PLAYER_ID={bot_player_id_int}',
            '-e', f'DIFFICULTY={difficulty}',
            '-e', f'BUILD={build.lower()}',
            '-e', f'RACE={race.lower()}',
            '-e', f'MATCH_ID={match_id}',
        ]

        # Pass duration limit if provided (seconds after takeover before forfeit)
        replay_duration = request.POST.get('replay_duration', '').strip()
        if replay_duration:
            duration_seconds = _parse_game_time(replay_duration)
            if duration_seconds and duration_seconds > 0:
                command += ['-e', f'REPLAY_DURATION={duration_seconds / 22.4:.1f}']

        command += _env_file_args(test_bot)
        command += [
            'bot',
            'bash', '/root/runner/run_docker_continue_replay.sh',
        ]

        _launch_legacy_match(match_id, command, docker_compose_path, log_file)

        takeover_seconds = game_loop / 22.4
        messages.success(
            request,
            f'Continue-from-replay match started! Takeover at {takeover_seconds:.0f}s '
            f'(loop {game_loop}), difficulty={difficulty} (match #{match_id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start continue-from-replay match: {e}')

    return redirect('run_match')


def _launch_replay_test_match(
    replay_test: ReplayTest, test_group_id: int = -1, test_bot=None,
    source_override: str | None = None,
) -> int:
    """Launch a single replay test match in Docker. Returns the match ID."""
    import shutil as _shutil

    game_loop = _parse_game_time(replay_test.start_time)
    if game_loop is None or game_loop <= 0:
        raise ValueError(f'Invalid start_time "{replay_test.start_time}"')

    rt_difficulty = replay_test.opponent_difficulty or 'CheatInsane'
    rt_build = replay_test.opponent_build or 'Macro'
    rt_race = replay_test.opponent_race or 'Random'
    rt_bot_player_id = replay_test.bot_player_id or 1

    match = Match(
        test_group_id=test_group_id,
        start_timestamp=datetime.now(),
        map_name='TBD (from replay)',
        opponent_race=rt_race,
        opponent_difficulty=rt_difficulty,
        opponent_build=rt_build,
        result='Pending',
        replay_takeover_game_loop=game_loop,
        test_bot=test_bot,
        replay_test=replay_test,
    )
    match.save()
    match_id = match.id

    _write_legacy_env()
    logs_dir = _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)

    replay_filename = f'{match_id}_source.SC2Replay'
    replay_dest = os.path.join(logs_dir, replay_filename)
    if replay_test.replay_file != replay_dest:
        _shutil.copy2(replay_test.replay_file, replay_dest)

    container_replay_path = f'/root/replays/{replay_filename}'
    match.replay_file = container_replay_path
    match.save()

    log_file = os.path.join(logs_dir, f'{match_id}_replay_test.log')

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

    duration_loops = _parse_game_time(replay_test.duration)
    if duration_loops and duration_loops > 0:
        duration_seconds = duration_loops / 22.4
        command += ['-e', f'REPLAY_DURATION={duration_seconds:.1f}']

    if source_override:
        override_src = source_override.replace('\\', '/')
        command += ['-v', f'{override_src}:/root/bot']

    command += _env_file_args(test_bot)
    command += [
        'bot',
        'bash', '/root/runner/run_docker_continue_replay.sh',
    ]

    _launch_legacy_match(match_id, command, DOCKER_COMPOSE_PATH, log_file)

    return match_id


@require_POST
def run_saved_replay_test(request):
    """Launch a single saved replay test from the Run Match page."""
    replay_test_id = request.POST.get('replay_test_id', '')
    test_bot_id = request.POST.get('test_bot_id', '')

    if not replay_test_id or not replay_test_id.isdigit():
        messages.error(request, 'Please select a replay test.')
        return redirect('run_match')

    try:
        replay_test = ReplayTest.objects.get(id=int(replay_test_id))
    except ReplayTest.DoesNotExist:
        messages.error(request, 'Replay test not found.')
        return redirect('run_match')

    test_bot = None
    if test_bot_id and test_bot_id.isdigit():
        test_bot = CustomBot.objects.filter(id=int(test_bot_id)).first()

    try:
        match_id = _launch_replay_test_match(replay_test, test_bot=test_bot)
        messages.success(
            request,
            f'Replay test "{replay_test.name}" started (match #{match_id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start replay test: {e}')

    return redirect('run_match')


# ---------------------------------------------------------------------------
# Replay Tests
# ---------------------------------------------------------------------------

REPLAY_UPLOAD_DIR = os.path.join(
    os.path.dirname(__file__), 'replay_test_files',
)




@require_POST
def create_replay_test(request):
    """Create one or more replay tests from form data.

    The form submits parallel lists: name[], start_time[], duration[] plus a
    single replay_file and shared opponent/player settings.
    Each tuple creates one ReplayTest row.
    """
    names = request.POST.getlist('name')
    start_times = request.POST.getlist('start_time')
    durations = request.POST.getlist('duration')
    replay_file = request.FILES.get('replay_file')

    # Shared settings for all tests in this batch
    bot_player_id = request.POST.get('bot_player_id', '1')
    opponent_type = request.POST.get('opponent_type', 'BuiltInAI')
    opponent_race = request.POST.get('opponent_race', 'Random')
    opponent_difficulty = request.POST.get('opponent_difficulty', 'CheatInsane')
    opponent_build = request.POST.get('opponent_build', 'Macro')
    opponent_bot_id = request.POST.get('opponent_bot_id', '')

    config_tests_url = f"{reverse('config_page')}#test-suites"

    if not replay_file:
        messages.error(request, 'Please upload a replay file.')
        return redirect(config_tests_url)

    if not replay_file.name.endswith('.SC2Replay'):
        messages.error(request, 'File must be a .SC2Replay file.')
        return redirect(config_tests_url)

    if not names or not any(n.strip() for n in names):
        messages.error(request, 'At least one test name is required.')
        return redirect(config_tests_url)

    try:
        bot_player_id_int = int(bot_player_id)
        if bot_player_id_int not in (1, 2):
            raise ValueError
    except ValueError:
        messages.error(request, 'Bot player ID must be 1 or 2.')
        return redirect(config_tests_url)

    # Resolve custom bot if selected
    opponent_bot = None
    if opponent_type == 'CustomBot' and opponent_bot_id:
        try:
            opponent_bot = CustomBot.objects.get(id=int(opponent_bot_id))
        except (CustomBot.DoesNotExist, ValueError):
            messages.error(request, 'Selected custom bot not found.')
            return redirect(config_tests_url)

    # Save the replay file once
    os.makedirs(REPLAY_UPLOAD_DIR, exist_ok=True)
    safe_name = replay_file.name.replace(' ', '_')
    replay_path = os.path.join(REPLAY_UPLOAD_DIR, safe_name)

    if not os.path.exists(replay_path):
        with open(replay_path, 'wb') as dest:
            for chunk in replay_file.chunks():
                dest.write(chunk)

    created = 0
    for i, name in enumerate(names):
        name = name.strip()
        start_time = start_times[i].strip() if i < len(start_times) else ''
        duration = durations[i].strip() if i < len(durations) else ''

        if not name or not start_time or not duration:
            continue

        if _parse_game_time(start_time) is None:
            messages.error(request, f'Row {i + 1}: Invalid start time "{start_time}". Use mm:ss or seconds.')
            continue

        if _parse_game_time(duration) is None:
            messages.error(request, f'Row {i + 1}: Invalid duration "{duration}". Use mm:ss or seconds.')
            continue

        ReplayTest.objects.create(
            name=name,
            replay_file=replay_path,
            start_time=start_time,
            duration=duration,
            bot_player_id=bot_player_id_int,
            opponent_type=opponent_type,
            opponent_race=opponent_race if opponent_type == 'BuiltInAI' else '',
            opponent_difficulty=opponent_difficulty if opponent_type == 'BuiltInAI' else '',
            opponent_build=opponent_build if opponent_type == 'BuiltInAI' else '',
            opponent_bot=opponent_bot if opponent_type == 'CustomBot' else None,
        )
        created += 1

    if created:
        label = 'test' if created == 1 else 'tests'
        messages.success(request, f'Created {created} replay {label}.')
    return redirect(config_tests_url)


@require_POST
def delete_replay_test(request, test_id):
    """Delete a replay test."""
    config_tests_url = f"{reverse('config_page')}#test-suites"
    try:
        test = ReplayTest.objects.get(id=test_id)
        test_name = test.name
        test.delete()
        messages.success(request, f'Replay test "{test_name}" deleted.')
    except ReplayTest.DoesNotExist:
        messages.error(request, 'Replay test not found.')
    return redirect(config_tests_url)


# ---------------------------------------------------------------------------
# Ticket views
# ---------------------------------------------------------------------------

def tickets_page(request):
    """List all tickets."""
    import json as _json
    tickets = Ticket.objects.select_related('test_bot', 'test_suite', 'prompt_template').all()
    test_bots = CustomBot.objects.filter(is_test_subject=True)
    test_suites = TestSuite.objects.all()
    prompt_templates = PromptTemplate.objects.prefetch_related('bots').order_by('name')
    # Build JSON map: bot_id -> [template ids], and '' -> generic template ids
    templates_json = _json.dumps([
        {
            'id': t.id,
            'name': t.name,
            'bot_ids': list(t.bots.values_list('id', flat=True)),
        }
        for t in prompt_templates
    ])
    return render(request, 'test_lab/tickets.html', {
        'active_page': 'tickets',
        'tickets': tickets,
        'test_bots': test_bots,
        'test_suites': test_suites,
        'prompt_templates': prompt_templates,
        'templates_json': templates_json,
    })


def ticket_detail_page(request, ticket_id):
    """View a single ticket with its details and diff."""
    try:
        ticket = Ticket.objects.select_related(
            'test_bot', 'test_suite',
        ).get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    # Fetch all test groups that ran on this ticket's branch
    test_groups = []
    if ticket.branch:
        test_groups = list(
            TestGroup.objects.filter(branch=ticket.branch).order_by('-created_at')
        )

    # Try to get the git diff for the branch
    diff_text = ''
    if ticket.branch and ticket.test_bot and ticket.test_bot.source_path:
        try:
            result = subprocess.run(
                ['git', 'diff', f'main...{ticket.branch}'],
                cwd=ticket.test_bot.source_path,
                capture_output=True, text=True, timeout=10,
            )
            diff_text = result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    test_suites = TestSuite.objects.all().order_by('name')
    prompt_templates = PromptTemplate.objects.prefetch_related('bots').order_by('name')
    import json as _json
    templates_json = _json.dumps([
        {
            'id': t.id,
            'name': t.name,
            'bot_ids': list(t.bots.values_list('id', flat=True)),
        }
        for t in prompt_templates
    ])

    return render(request, 'test_lab/ticket_detail.html', {
        'active_page': 'tickets',
        'ticket': ticket,
        'test_groups': test_groups,
        'test_suites': test_suites,
        'prompt_templates': prompt_templates,
        'templates_json': templates_json,
        'diff_text': diff_text,
        'statuses': Ticket.Status,
    })


@require_POST
def create_ticket(request):
    """Create a new ticket."""
    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    test_bot_id = request.POST.get('test_bot_id')
    test_suite_id = request.POST.get('test_suite_id') or None
    context_files = request.POST.get('context_files', '').strip()

    if not title:
        messages.error(request, 'Title is required.')
        return redirect('tickets')
    if not test_bot_id:
        messages.error(request, 'Test bot is required.')
        return redirect('tickets')

    try:
        test_bot = CustomBot.objects.get(id=test_bot_id)
    except CustomBot.DoesNotExist:
        messages.error(request, 'Invalid test bot.')
        return redirect('tickets')

    test_suite = None
    if test_suite_id:
        test_suite = TestSuite.objects.filter(id=test_suite_id).first()
    if test_suite is None and test_bot.default_test_suite:
        test_suite = test_bot.default_test_suite

    prompt_template = None
    prompt_template_id = request.POST.get('prompt_template_id')
    if prompt_template_id:
        prompt_template = PromptTemplate.objects.filter(id=prompt_template_id).first()

    ticket = Ticket.objects.create(
        title=title,
        description=description,
        test_bot=test_bot,
        test_suite=test_suite,
        prompt_template=prompt_template,
        context_files=context_files,
    )
    # Auto-generate the branch name now that we have the ID
    ticket.branch = ticket.branch_name
    ticket.save(update_fields=['branch'])

    messages.success(request, f'Ticket #{ticket.id} created.')
    return redirect('ticket_detail', ticket_id=ticket.id)


@require_POST
def update_ticket(request, ticket_id):
    """Update an existing ticket."""
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    ticket.title = request.POST.get('title', ticket.title).strip()
    ticket.description = request.POST.get('description', ticket.description).strip()
    ticket.context_files = request.POST.get('context_files', ticket.context_files).strip()

    test_suite_id = request.POST.get('test_suite_id')
    if test_suite_id:
        ticket.test_suite = TestSuite.objects.filter(id=test_suite_id).first()

    prompt_template_id = request.POST.get('prompt_template_id')
    if prompt_template_id:
        ticket.prompt_template = PromptTemplate.objects.filter(id=prompt_template_id).first()
    elif prompt_template_id == '':
        ticket.prompt_template = None

    ticket.save()
    messages.success(request, f'Ticket #{ticket.id} updated.')
    return redirect('ticket_detail', ticket_id=ticket.id)


@require_POST
def run_ticket_tests(request, ticket_id):
    """Run the test suite for a ticket's branch (web form action)."""
    try:
        ticket = Ticket.objects.select_related('test_bot', 'test_suite').get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    test_bot = ticket.test_bot
    test_suite = ticket.test_suite or (test_bot.default_test_suite if test_bot else None)
    branch = ticket.branch

    if not test_bot:
        messages.error(request, 'Ticket has no test bot set.')
        return redirect('ticket_detail', ticket_id=ticket.id)

    if not branch:
        messages.error(request, 'Ticket has no branch set.')
        return redirect('ticket_detail', ticket_id=ticket.id)

    if test_bot.source_path:
        try:
            worktrees.get_or_create_worktree(test_bot.source_path, branch)
        except ValueError as e:
            messages.error(request, f'Invalid branch: {e}')
            return redirect('ticket_detail', ticket_id=ticket.id)

    try:
        test_group_id, count = start_test_suite(
            description=f'Ticket #{ticket.id}: {ticket.title}',
            test_bot=test_bot,
            test_suite=test_suite,
            branch=branch,
        )
    except Exception as e:
        messages.error(request, f'Failed to start tests: {e}')
        return redirect('ticket_detail', ticket_id=ticket.id)

    ticket.status = 'testing'
    ticket.save(update_fields=['status'])
    messages.success(
        request,
        f'Test suite started — {count} matches in group {test_group_id}.',
    )
    return redirect('ticket_detail', ticket_id=ticket.id)


@require_POST
def update_ticket_status(request, ticket_id):
    """Change a ticket's status."""
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    new_status = request.POST.get('status', '')
    if new_status not in dict(Ticket.Status.choices):
        messages.error(request, f'Invalid status: {new_status}')
        return redirect('ticket_detail', ticket_id=ticket.id)

    ticket.status = new_status
    ticket.save(update_fields=['status'])
    messages.success(request, f'Ticket #{ticket.id} status → {new_status}.')
    return redirect('ticket_detail', ticket_id=ticket.id)


@require_POST
def generate_ticket_prompt(request, ticket_id):
    """Generate the .prompt.md file and mark ticket as ready."""
    try:
        ticket = Ticket.objects.select_related(
            'test_bot', 'test_suite', 'prompt_template',
        ).get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    filepath = prompt_generator.write_prompt_file(ticket)
    ticket.prompt_file = filepath
    if ticket.status == 'draft':
        ticket.status = 'ready'
    ticket.save(update_fields=['prompt_file', 'status'])

    messages.success(
        request,
        f'Prompt file generated: .github/prompts/{prompt_generator.prompt_filename(ticket)}  '
        f'— invoke it in VS Code chat with /ticket-{ticket.id}',
    )
    return redirect('ticket_detail', ticket_id=ticket.id)


# ── Prompt Template CRUD ──────────────────────────────────────────────

@require_POST
def create_prompt_template(request):
    """Create a new prompt template (DB record + file on disk)."""
    from .prompt_generator import TEMPLATES_DIR
    config_url = f"{reverse('config_page')}#prompt-templates"
    name = request.POST.get('name', '').strip()
    filename = request.POST.get('filename', '').strip()
    template_content = request.POST.get('template_content', '').strip()

    if not name:
        messages.error(request, 'Template name is required.')
        return redirect(config_url)

    if not filename:
        messages.error(request, 'Filename is required.')
        return redirect(config_url)

    if not filename.endswith('.md'):
        filename += '.md'

    if PromptTemplate.objects.filter(name=name).exists():
        messages.error(request, f'A template named "{name}" already exists.')
        return redirect(config_url)

    if PromptTemplate.objects.filter(filename=filename).exists():
        messages.error(request, f'A template with filename "{filename}" is already registered.')
        return redirect(config_url)

    # Write the file to disk
    if template_content:
        import os
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        filepath = os.path.join(TEMPLATES_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(template_content)

    template = PromptTemplate.objects.create(name=name, filename=filename)
    bot_ids = request.POST.getlist('bot_ids')
    if bot_ids:
        template.bots.set(bot_ids)

    messages.success(request, f'Prompt template "{name}" created.')
    return redirect(config_url)


@require_POST
def update_prompt_template(request, template_id):
    """Update an existing prompt template (DB record + file on disk)."""
    from .prompt_generator import TEMPLATES_DIR
    config_url = f"{reverse('config_page')}#prompt-templates"
    try:
        template = PromptTemplate.objects.get(id=template_id)
    except PromptTemplate.DoesNotExist:
        messages.error(request, 'Template not found.')
        return redirect(config_url)

    name = request.POST.get('name', '').strip()
    template_content = request.POST.get('template_content', '').strip()

    if name and name != template.name:
        if PromptTemplate.objects.filter(name=name).exclude(id=template_id).exists():
            messages.error(request, f'A template named "{name}" already exists.')
            return redirect(config_url)
        template.name = name

    template.save()

    # Write updated content to file on disk
    if template_content and template.filename:
        import os
        filepath = os.path.join(TEMPLATES_DIR, template.filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(template_content)

    bot_ids = request.POST.getlist('bot_ids')
    template.bots.set(bot_ids)

    messages.success(request, f'Prompt template "{template.name}" updated.')
    return redirect(config_url)


@require_POST
def delete_prompt_template(request, template_id):
    """Delete a prompt template."""
    config_url = f"{reverse('config_page')}#prompt-templates"
    try:
        template = PromptTemplate.objects.get(id=template_id)
    except PromptTemplate.DoesNotExist:
        messages.error(request, 'Template not found.')
        return redirect(config_url)

    template_name = template.name
    template.delete()
    messages.success(request, f'Prompt template "{template_name}" deleted.')
    return redirect(config_url)


def get_template_file_content(request):
    """API: return the content of a prompt template file."""
    from .prompt_generator import read_template_file
    filename = request.GET.get('filename', '')
    if not filename:
        return JsonResponse({'error': 'Missing filename'}, status=400)
    # Prevent path traversal
    basename = os.path.basename(filename)
    content = read_template_file(basename)
    if content is None:
        return JsonResponse({'error': 'File not found', 'content': ''}, status=404)
    return JsonResponse({'content': content})


@require_POST
def delete_ticket(request, ticket_id):
    """Delete a ticket and its prompt file."""
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise Http404('Ticket not found')

    prompt_generator.delete_prompt_file(ticket)
    ticket_title = ticket.title
    ticket.delete()
    messages.success(request, f'Ticket "{ticket_title}" deleted.')
    return redirect('tickets')


@csrf_exempt
@require_POST
def api_trigger_ticket_tests(request):
    """API endpoint to trigger tests for a ticket.

    JSON body:
      - ticket_id (int): required — the ticket whose test suite to run

    Looks up the test bot, test suite, and branch from the ticket.
    Creates a TestGroup linked to the ticket and starts the suite.
    """
    import json
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse(
            {'status': 'error', 'message': 'Invalid JSON'}, status=400,
        )

    ticket_id = body.get('ticket_id')
    if ticket_id is None:
        return JsonResponse(
            {'status': 'error', 'message': 'ticket_id is required'}, status=400,
        )

    try:
        ticket = Ticket.objects.select_related(
            'test_bot', 'test_suite',
        ).get(id=ticket_id)
    except Ticket.DoesNotExist:
        return JsonResponse(
            {'status': 'error', 'message': f'Ticket {ticket_id} not found'},
            status=404,
        )

    test_bot = ticket.test_bot
    test_suite = ticket.test_suite or (test_bot.default_test_suite if test_bot else None)
    branch = ticket.branch

    if not test_bot:
        return JsonResponse(
            {'status': 'error', 'message': 'Ticket has no test bot set'}, status=400,
        )

    if not branch:
        return JsonResponse(
            {'status': 'error', 'message': 'Ticket has no branch set'}, status=400,
        )

    # Validate the branch and create worktree if needed
    if test_bot.source_path:
        try:
            worktrees.get_or_create_worktree(test_bot.source_path, branch)
        except ValueError as e:
            return JsonResponse(
                {'status': 'error', 'message': f'Invalid branch: {e}'},
                status=400,
            )

    try:
        test_group_id, count = start_test_suite(
            description=f'Ticket #{ticket.id}: {ticket.title}',
            test_bot=test_bot,
            test_suite=test_suite,
            branch=branch,
        )
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    ticket.status = 'testing'
    ticket.save(update_fields=['status'])

    return JsonResponse({
        'status': 'ok',
        'ticket_id': ticket.id,
        'test_group_id': test_group_id,
        'matches_started': count,
        'test_bot': test_bot.name,
        'test_suite': test_suite.name if test_suite else 'default',
        'branch': branch,
    })

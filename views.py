import glob
import os
import subprocess
from collections import defaultdict
from datetime import datetime

from django.contrib import messages
from django.db.models import Max, Min, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import aiarena_runner, bot_versions
from .models import CustomBot, Match, MatchEvent, TestGroup


def match_list(request):
    """View to display match data grouped by test_group_id in a pivot table."""
    # Get filters from request
    selected_difficulty = request.GET.get('difficulty', '')
    selected_limit = request.GET.get('limit', '')
    selected_test_bot = request.GET.get('test_bot', '')

    matches = Match.objects.select_related('opponent_bot').exclude(test_group_id=-1)

    # Apply test bot filter
    if selected_test_bot and selected_test_bot.isdigit():
        matches = matches.filter(test_bot_id=int(selected_test_bot))

    # Difficulty filter applies only to computer opponents; custom-bot matches
    # are always included (they run regardless of difficulty).
    if selected_difficulty:
        matches = matches.filter(
            Q(opponent_difficulty=selected_difficulty) | Q(opponent_bot__isnull=False)
        )

    # Apply test group limit if selected — only include matches from the N most recent test groups
    if selected_limit and selected_limit.isdigit():
        recent_group_ids = list(
            Match.objects.exclude(test_group_id=-1)
            .values_list('test_group_id', flat=True)
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

    # Track win/loss counts for each opponent column
    opponent_stats: dict[str, dict] = defaultdict(lambda: {'victories': 0, 'total_games': 0})

    # Track fastest victories / slowest losses per combination
    fastest_victories: dict[tuple, tuple[int, int]] = {}
    slowest_losses: dict[tuple, tuple[int, int]] = {}

    for match in matches:
        opp_bot = match.opponent_bot
        if opp_bot is not None:
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
        row = {'test_group_id': group_id, 'results': [], 'difficulty': None}

        # Get difficulty from first computer match in this group
        for m in grouped_matches[group_id].values():
            if m.opponent_difficulty:
                row['difficulty'] = m.opponent_difficulty
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

            if group_id != max_group_id and match_data.result == 'Pending':
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
    test_subject_bots = CustomBot.objects.all().order_by('name')
    return render(request, 'test_lab/match_list.html', {
        'pivot_data': pivot_data,
        'opponents': sorted_opponents,
        'header_structure': header_structure,
        'selected_difficulty': selected_difficulty,
        'selected_limit': selected_limit,
        'selected_test_bot': selected_test_bot,
        'test_groups': test_groups,
        'test_subject_bots': test_subject_bots,
    })

def get_next_test_group_id() -> int:
    """Get the next test group ID by incrementing the highest completed test group ID."""
    result = Match.objects.filter(
        end_timestamp__isnull=False
    ).aggregate(Max('test_group_id'))['test_group_id__max']
    
    # If no completed matches exist, start at 0, otherwise increment by 1
    return 0 if result is None else result + 1

def create_pending_match(
    test_group_id: int, race: str, build: str, difficulty: str,
    test_bot: CustomBot | None = None,
) -> int:
    """Create a pending match entry and return the match ID.

    *test_bot* is the Player-1 bot being tested.  When ``None`` the match
    is attributed to BotTato (id 5) by default.
    """
    if test_bot is None:
        test_bot = CustomBot.objects.filter(id=5).first()
    match = Match(
        test_group_id=test_group_id,
        start_timestamp=datetime.now(),
        map_name="TBD",  # Map will be determined by run_bottato_vs_computer.py
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
LOGS_DIR = r'C:\Users\inter\Documents\StarCraft II\Replays\Multiplayer\docker'


def start_custom_bot_match(
    custom_bot: CustomBot,
    test_bot: CustomBot | None = None,
    test_group_id: int = -1,
) -> int:
    """Launch a single Docker match against a custom bot.
    Returns the match ID.

    ``test_bot`` is the test-subject bot (player 1).  When *None* defaults
    to BotTato (id 5).

    ``test_group_id`` defaults to ``-1`` (ad-hoc match).  Pass a real
    TestGroup id to include this match in a test group.

    For aiarena-type bots, uses the aiarena local-play-bootstrap infrastructure.
    For python_sc2 / external_python bots, uses the existing single-container approach.
    """
    if test_bot is None:
        test_bot = CustomBot.objects.filter(id=5).first()
    match = Match(
        test_group_id=test_group_id,
        start_timestamp=datetime.now(),
        map_name="TBD",
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
        aiarena_runner.start_aiarena_match(match, custom_bot, test_bot=test_bot)
        return match_id

    # Legacy path: python_sc2 / external_python bots
    compose_file = os.path.join(DOCKER_COMPOSE_PATH, 'docker-compose.yml')
    if not os.path.exists(compose_file):
        raise FileNotFoundError(f'docker-compose.yml not found at: {compose_file}')

    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = os.path.join(LOGS_DIR, f"{match_id}_vs_{custom_bot.name}.log")

    command = [
        'docker', 'compose', 'run', '--rm',
        '-e', f'OPPONENT_FILE={custom_bot.bot_file}',
        '-e', f'OPPONENT_CLASS={custom_bot.bot_class_name}',
        '-e', f'OPPONENT_RACE={custom_bot.race.lower()}',
        '-e', f'MATCH_ID={match_id}',
    ]

    if custom_bot.is_external and custom_bot.bot_directory:
        command += ['-e', f'EXTERNAL_BOT_DIR={custom_bot.bot_directory}']

    command += [
        'bot',
        'bash', '/root/runner/run_docker_bot_vs_bot.sh',
    ]

    with open(log_file, 'w') as log:
        subprocess.Popen(command, cwd=DOCKER_COMPOSE_PATH, stdout=log, stderr=log)

    return match_id


def start_test_suite(
    description: str,
    difficulty: str = 'CheatInsane',
    test_bot: CustomBot | None = None,
) -> tuple[int, int]:
    """
    Create a TestGroup and launch Docker match containers for every
    race/build combination against the built-in AI as well as every
    registered custom bot.

    Returns (test_group_id, number of matches started).
    Raises FileNotFoundError if docker-compose.yml is missing.

    *test_bot* is the Player-1 bot; ``None`` defaults to BotTato (id 5).
    Custom bot matches run regardless of difficulty.
    """
    compose_file = os.path.join(DOCKER_COMPOSE_PATH, 'docker-compose.yml')
    if not os.path.exists(compose_file):
        raise FileNotFoundError(f'docker-compose.yml not found at: {compose_file}')

    os.makedirs(LOGS_DIR, exist_ok=True)

    if test_bot is None:
        test_bot = CustomBot.objects.filter(id=5).first()

    test_group = TestGroup.objects.create(
        description=description[:255]  # Truncate to fit CharField max_length
    )
    test_group_id = test_group.id

    count = 0

    # --- Computer AI matches (15 = 3 races x 5 builds) ---
    for race in ('protoss', 'terran', 'zerg'):
        for build in ('rush', 'timing', 'macro', 'power', 'air'):
            match_id = create_pending_match(test_group_id, race, build, difficulty, test_bot=test_bot)
            log_file = os.path.join(LOGS_DIR, f"{match_id}_{race}_{build}.log")
            command = [
                'docker', 'compose', 'run', '--rm',
                '-e', f'RACE={race}',
                '-e', f'BUILD={build}',
                '-e', f'MATCH_ID={match_id}',
                '-e', f'DIFFICULTY={difficulty}',
                'bot',
            ]
            with open(log_file, 'w') as log:
                subprocess.Popen(command, cwd=DOCKER_COMPOSE_PATH, stdout=log, stderr=log)
            count += 1

    # --- Custom bot matches (one per registered bot, excluding the test bot) ---
    custom_bots = CustomBot.objects.exclude(id=test_bot.id) if test_bot else CustomBot.objects.all()
    for bot in custom_bots:
        try:
            start_custom_bot_match(bot, test_bot=test_bot, test_group_id=test_group_id)
            count += 1
        except Exception:
            # Don't let a single custom-bot failure abort the whole suite
            pass

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

        try:
            _, count = start_test_suite(
                description=description, difficulty=difficulty, test_bot=test_bot,
            )
            messages.success(request, f'Test suite started with difficulty {difficulty}! {count} tests running.')
        except Exception as e:
            messages.error(request, f'Failed to start test suite: {str(e)}')

    # Redirect back preserving filter state
    params = []
    for key in ('test_bot', 'difficulty', 'limit'):
        val = request.POST.get(key, '')
        if val:
            params.append(f'{key}={val}')
    qs = '?' + '&'.join(params) if params else ''
    return redirect(f"{reverse('match_list')}{qs}")


@csrf_exempt
@require_POST
def api_trigger_tests(request):
    """API endpoint to trigger test suite or custom bot match.

    JSON body:
      - difficulty (str): AI difficulty level (default: CheatInsane)
      - description (str): optional test group description
      - custom_bot_id (int): when set, runs a single match against this
        custom bot instead of the full 15-match test suite
    """
    import json
    try:
        body = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        body = {}

    custom_bot_id = body.get('custom_bot_id')
    test_bot_id = body.get('test_bot_id')
    description = body.get('description', '')

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

    # Custom bot match
    if custom_bot_id is not None:
        try:
            custom_bot = CustomBot.objects.get(id=custom_bot_id)
        except CustomBot.DoesNotExist:
            return JsonResponse(
                {'status': 'error', 'message': f'Custom bot with id {custom_bot_id} not found'},
                status=404,
            )
        try:
            match_id = start_custom_bot_match(custom_bot, test_bot=test_bot)
            return JsonResponse({
                'status': 'ok',
                'match_id': match_id,
                'custom_bot': custom_bot.name,
                'test_bot': test_bot.name if test_bot else 'BotTato',
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    # Standard test suite
    difficulty = body.get('difficulty', 'CheatInsane')
    try:
        test_group_id, count = start_test_suite(
            description=description, difficulty=difficulty, test_bot=test_bot,
        )
        return JsonResponse({
            'status': 'ok',
            'test_group_id': test_group_id,
            'matches_started': count,
            'difficulty': difficulty,
            'description': description,
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def serve_replay(request, match_id):
    """Open replay files with StarCraft 2 locally."""
    # Check aiarena run directory first
    replay_path = aiarena_runner.get_replay_path(match_id)
    if replay_path:
        subprocess.Popen([r"C:\Program Files (x86)\StarCraft II\Support\SC2Switcher.exe", replay_path])
        return HttpResponse(status=204)

    # Fall back to legacy directory
    replay_dir = r'C:\Users\inter\Documents\StarCraft II\Replays\Multiplayer\docker'
    replay_pattern = os.path.join(replay_dir, f"{match_id}_*.SC2Replay")
    replay_files = glob.glob(replay_pattern)
    
    if not replay_files:
        raise Http404("Replay file not found")
    
    file_path = replay_files[0]
    subprocess.Popen([r"C:\Program Files (x86)\StarCraft II\Support\SC2Switcher.exe", file_path])
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
    replay_dir = r'C:\Users\inter\Documents\StarCraft II\Replays\Multiplayer\docker'
    log_pattern = os.path.join(replay_dir, f"{match_id}*.log")
    log_files = [
        f for f in glob.glob(log_pattern)
        if '_stderr.log' not in f
    ]
    
    if not log_files:
        raise Http404("Log file not found")
    
    file_path = log_files[0]
    return FileResponse(open(file_path, 'rb'), content_type='text/plain')

def serve_aiarena_bot_log(request, match_id, bot_name):
    """Serve a bot's stderr log from an aiarena match."""
    from django.http import FileResponse
    log_path = aiarena_runner.get_bot_log_path(match_id, bot_name)
    if not log_path:
        raise Http404(f"Bot log not found for {bot_name} in match {match_id}")
    return FileResponse(open(log_path, 'rb'), content_type='text/plain')

def map_breakdown(request):
    """View to display match data grouped by map in a pivot table."""
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
    
    return render(request, 'test_lab/map_breakdown.html', {
        'pivot_data': pivot_data,
        'opponents': sorted_opponents,
        'header_structure': header_structure,
        'selected_difficulty': selected_difficulty,
        'selected_limit': selected_limit
    })


def building_timing(request):
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
    
    
    return render(request, 'test_lab/building_timing.html', {
        'pivot_data': pivot_data,
        'building_types': sorted_building_types,
        'avg_timings': avg_timings,
    })


def utilities(request):
    """Page for triggering various utility actions."""
    custom_bots_list = CustomBot.objects.all().order_by('name')
    test_subject_bots = CustomBot.objects.all().order_by('name')

    # Collect recent commits for each test-subject bot that has a git repo,
    # plus the default BotTato repo.
    recent_commits_by_bot: dict[int | None, list] = {
        None: bot_versions.get_recent_bot_commits(count=5),  # legacy BotTato
    }
    for bot in test_subject_bots:
        if bot.git_repo_path:
            recent_commits_by_bot[bot.id] = bot_versions.get_recent_bot_commits(
                count=5, repo_path=bot.git_repo_path
            )

    # Flatten for the default (backwards-compat) template variable
    recent_commits = recent_commits_by_bot.get(None, [])

    return render(request, 'test_lab/utilities.html', {
        'custom_bots': custom_bots_list,
        'test_subject_bots': test_subject_bots,
        'recent_commits': recent_commits,
        'recent_commits_by_bot': recent_commits_by_bot,
    })


def recompile_cython(request):
    """Trigger recompilation of Cython extensions."""
    if request.method == 'POST':
        cython_dir = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', '..', 'bot', 'cython_extensions'
        ))
        setup_py = os.path.join(cython_dir, 'setup.py')

        if not os.path.exists(setup_py):
            messages.error(request, f'setup.py not found at: {setup_py}')
            return redirect('utilities')

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

    return redirect('utilities')


def run_single_match(request):
    """Run a single match outside of a test group."""
    if request.method == 'POST':
        race = request.POST.get('race', 'random')
        build = request.POST.get('build', 'randombuild')
        difficulty = request.POST.get('difficulty', 'CheatInsane')

        docker_compose_path = DOCKER_COMPOSE_PATH
        logs_dir = LOGS_DIR
        os.makedirs(logs_dir, exist_ok=True)

        try:
            # Create a pending match with test_group_id = -1 (not part of a test group)
            match_id = create_pending_match(-1, race, build, difficulty)

            log_file = os.path.join(logs_dir, f"{match_id}_{race}_{build}.log")

            command = [
                'docker', 'compose', 'run', '--rm',
                '-e', f'RACE={race}',
                '-e', f'BUILD={build}',
                '-e', f'DIFFICULTY={difficulty}',
                '-e', f'MATCH_ID={match_id}',
                'bot',
            ]

            with open(log_file, 'w') as log:
                subprocess.Popen(command, cwd=docker_compose_path, stdout=log, stderr=log)

            messages.success(
                request,
                f'Single match started: {race} {build} @ {difficulty} (match #{match_id})'
            )
        except Exception as e:
            messages.error(request, f'Failed to start match: {str(e)}')

    return redirect('utilities')


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


def custom_bots(request):
    """List all registered custom bots and available AI Arena bot directories."""
    bots = CustomBot.objects.all().order_by('-created_at')
    aiarena_bots = aiarena_runner.get_available_aiarena_bots()
    return render(request, 'test_lab/custom_bots.html', {
        'bots': bots,
        'aiarena_bots': aiarena_bots,
    })


@require_POST
def create_custom_bot(request):
    """Register a new AI Arena bot from form data."""
    name = request.POST.get('name', '').strip()
    race = request.POST.get('race', 'Random')
    description = request.POST.get('description', '').strip()
    bot_directory = request.POST.get('bot_directory', '').strip()
    aiarena_bot_type = request.POST.get('aiarena_bot_type', 'python').strip()
    is_test_subject = request.POST.get('is_test_subject') == 'on'
    source_path = request.POST.get('source_path', '').strip()
    git_repo_path = request.POST.get('git_repo_path', '').strip()
    dockerfile = request.POST.get('dockerfile', '').strip()

    if not name:
        messages.error(request, 'Bot name is required.')
        return redirect('custom_bots')

    if not bot_directory:
        messages.error(request, 'Bot directory is required.')
        return redirect('custom_bots')

    error = aiarena_runner.validate_bot_directory(bot_directory)
    if error:
        messages.error(request, error)
        return redirect('custom_bots')

    if is_test_subject and not source_path:
        messages.error(request, 'Source path is required for test subject bots.')
        return redirect('custom_bots')

    if is_test_subject and source_path and not os.path.isdir(source_path):
        messages.error(request, f'Source path not found: {source_path}')
        return redirect('custom_bots')

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
            git_repo_path=git_repo_path,
            symlink_mounts=symlink_mounts,
            dockerfile=dockerfile,
            description=description,
        )
        msg = f'Bot "{name}" registered successfully.'
        if symlink_mounts:
            link_names = ', '.join(m['name'] for m in symlink_mounts)
            msg += f' Detected symlinks: {link_names}'
        messages.success(request, msg)
    except Exception as e:
        messages.error(request, f'Failed to create bot: {e}')

    return redirect('custom_bots')


@require_POST
def delete_custom_bot(request, bot_id):
    """Delete a registered custom bot."""
    try:
        bot = CustomBot.objects.get(id=bot_id)
        bot_name = bot.name
        bot.delete()
        messages.success(request, f'Custom bot "{bot_name}" deleted.')
    except CustomBot.DoesNotExist:
        messages.error(request, 'Custom bot not found.')

    return redirect('custom_bots')


@require_POST
def run_custom_match(request):
    """Run a match of a test-subject bot vs a custom opponent bot."""
    bot_id = request.POST.get('custom_bot_id')
    if not bot_id:
        messages.error(request, 'No custom bot selected.')
        return redirect('utilities')

    try:
        custom_bot = CustomBot.objects.get(id=bot_id)
    except CustomBot.DoesNotExist:
        messages.error(request, 'Selected custom bot not found.')
        return redirect('utilities')

    # Resolve test subject bot
    test_bot = None
    test_bot_id = request.POST.get('test_bot_id')
    if test_bot_id:
        test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()

    try:
        match_id = start_custom_bot_match(custom_bot, test_bot=test_bot)
        test_name = test_bot.name if test_bot else 'BotTato'
        messages.success(
            request,
            f'Custom match started: {test_name} vs {custom_bot.name} (match #{match_id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start custom match: {e}')

    return redirect('utilities')


@require_POST
def run_past_version_match(request):
    """Run a match of the current version of a test-subject bot vs a past version."""
    commit_hash = request.POST.get('commit_hash', '').strip()
    if not commit_hash:
        messages.error(request, 'No commit selected.')
        return redirect('utilities')

    # Validate commit hash format (full 40-char SHA)
    if len(commit_hash) != 40 or not all(c in '0123456789abcdef' for c in commit_hash):
        messages.error(request, 'Invalid commit hash.')
        return redirect('utilities')

    short_hash = commit_hash[:7]

    # Resolve test subject bot (defaults to BotTato id=5)
    test_bot = None
    test_bot_id = request.POST.get('test_bot_id')
    if test_bot_id:
        test_bot = CustomBot.objects.filter(id=test_bot_id, is_test_subject=True).first()
    if test_bot is None:
        test_bot = CustomBot.objects.filter(id=5).first()

    test_name = test_bot.name if test_bot else 'BotTato'
    test_race = test_bot.race if test_bot else 'Terran'

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

    return redirect('utilities')


def custom_match_list(request):
    """List matches against custom bots, past versions, or continued from replay."""
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

    # Optional filter by test subject bot
    selected_test_bot = request.GET.get('test_bot', '')
    if selected_test_bot:
        if selected_test_bot == 'bottato':
            matches = matches.filter(test_bot__isnull=True)
        elif selected_test_bot.isdigit():
            matches = matches.filter(test_bot_id=int(selected_test_bot))

    test_subject_bots = CustomBot.objects.all().order_by('name')

    return render(request, 'test_lab/custom_match_list.html', {
        'matches': matches,
        'test_subject_bots': test_subject_bots,
        'selected_test_bot': selected_test_bot,
    })


# ---------------------------------------------------------------------------
# Continue from Replay
# ---------------------------------------------------------------------------

REPLAY_UPLOAD_DIR = os.path.join(LOGS_DIR)  # Same dir as logs/replays


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
    bot_player_id = request.POST.get('bot_player_id', '1')

    # Validate inputs
    if not replay_file:
        messages.error(request, 'Please upload a replay file.')
        return redirect('utilities')

    if not replay_file.name.endswith('.SC2Replay'):
        messages.error(request, 'File must be a .SC2Replay file.')
        return redirect('utilities')

    game_loop = _parse_game_time(takeover_time)
    if game_loop is None or game_loop <= 0:
        messages.error(request, 'Invalid takeover time. Use mm:ss (e.g. 5:30) or seconds (e.g. 330).')
        return redirect('utilities')

    try:
        bot_player_id_int = int(bot_player_id)
        if bot_player_id_int not in (1, 2):
            raise ValueError
    except ValueError:
        messages.error(request, 'Bot player ID must be 1 or 2.')
        return redirect('utilities')

    docker_compose_path = DOCKER_COMPOSE_PATH
    logs_dir = LOGS_DIR
    os.makedirs(logs_dir, exist_ok=True)

    try:
        # Create a pending match
        match = Match(
            test_group_id=-1,
            start_timestamp=datetime.now(),
            map_name="TBD (from replay)",
            opponent_race='',
            opponent_difficulty=difficulty,
            opponent_build='',
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
            'docker', 'compose', 'run', '--rm',
            '-e', f'REPLAY_PATH={container_replay_path}',
            '-e', f'TAKEOVER_GAME_LOOP={game_loop}',
            '-e', f'BOT_PLAYER_ID={bot_player_id_int}',
            '-e', f'DIFFICULTY={difficulty}',
            '-e', f'MATCH_ID={match_id}',
            'bot',
            'bash', '/root/runner/run_docker_continue_replay.sh',
        ]

        with open(log_file, 'w') as log:
            subprocess.Popen(command, cwd=docker_compose_path, stdout=log, stderr=log)

        takeover_seconds = game_loop / 22.4
        messages.success(
            request,
            f'Continue-from-replay match started! Takeover at {takeover_seconds:.0f}s '
            f'(loop {game_loop}), difficulty={difficulty} (match #{match_id})'
        )
    except Exception as e:
        messages.error(request, f'Failed to start continue-from-replay match: {e}')

    return redirect('utilities')

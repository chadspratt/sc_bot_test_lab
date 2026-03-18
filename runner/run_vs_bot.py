"""
Run BotTato against a custom opponent bot (Bot vs Bot).

Environment variables:
  OPPONENT_FILE  - Python filename in other_bots/ (e.g. worker_rush.py)
  OPPONENT_CLASS - Class name inheriting from BotAI (e.g. WorkerRushBot)
  OPPONENT_RACE  - Race name: protoss, terran, zerg, or random
  MATCH_ID       - (optional) existing match row to update
  EXTERNAL_BOT_DIR - (optional) directory name under /root/external_bots/ for
                     third-party bots with their own framework
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys
import types
from loguru import logger

from config import MAP_LIST, RACE_DICT
from db_helpers import update_match_map, update_match_result
from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.main import run_game
from sc2.player import Bot

from bottato.bottato import BotTato


def load_opponent_class(file_name: str, class_name: str) -> type[BotAI]:
    """Dynamically import the opponent bot class from other_bots/."""
    bot_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'bot', 'other_bots')
    # Inside Docker, other_bots is at /root/bot/other_bots
    if os.path.isdir('/root/bot/other_bots'):
        bot_dir = '/root/bot/other_bots'
    file_path = os.path.join(bot_dir, file_name)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Opponent bot file not found: {file_path}")

    spec = importlib.util.spec_from_file_location("opponent_bot", file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    bot_cls = getattr(module, class_name, None)
    if bot_cls is None:
        raise AttributeError(
            f"Class '{class_name}' not found in {file_path}. "
            f"Available: {[n for n in dir(module) if not n.startswith('_')]}"
        )

    if not (isinstance(bot_cls, type) and issubclass(bot_cls, BotAI)):
        raise TypeError(f"'{class_name}' does not inherit from BotAI")

    return bot_cls


def load_external_opponent(ext_dir: str, rel_file_path: str, class_name: str) -> type[BotAI]:
    """Import an opponent bot class from an external bot directory.

    External bots live under /root/external_bots/<ext_dir>/ inside Docker.
    They may bundle their own frameworks (e.g. ares-sc2) which need to be
    added to sys.path before importing.

    Parameters
    ----------
    ext_dir : str
        Directory name under /root/external_bots/ (e.g. 'who').
    rel_file_path : str
        File path relative to the external bot directory (e.g. 'bot/main.py').
    class_name : str
        Class name to import from that module (e.g. 'MyBot').
    """
    base = f'/root/external_bots/{ext_dir}'
    if not os.path.isdir(base):
        raise FileNotFoundError(f"External bot directory not found: {base}")

    file_path = os.path.join(base, rel_file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"External bot file not found: {file_path}")

    # Add the external bot root and common framework paths to sys.path so
    # the external bot's internal imports resolve correctly.
    paths_to_add = [
        base,
        os.path.join(base, 'ares-sc2', 'src', 'ares'),
        os.path.join(base, 'ares-sc2', 'src'),
        os.path.join(base, 'ares-sc2'),
    ]
    for p in paths_to_add:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    # Pre-register the external bot's top-level package (e.g. 'bot/') in
    # sys.modules so it shadows any same-named .py file elsewhere on sys.path.
    # Without this, Python finds BotTato's bot.py (a module) before the
    # external bot's bot/ directory (a namespace package) and fails with
    # "'bot' is not a package".
    rel_parts = rel_file_path.replace('\\', '/').split('/')
    if len(rel_parts) > 1:
        pkg_name = rel_parts[0]
        pkg_dir = os.path.join(base, pkg_name)
        if os.path.isdir(pkg_dir) and pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [pkg_dir]
            pkg.__package__ = pkg_name
            sys.modules[pkg_name] = pkg

    # Change working directory so the external bot can find its config files
    # (e.g. ares-sc2 config.yml). We leave it here — BotTato uses __file__
    # relative paths, not cwd.
    os.chdir(base)

    try:
        spec = importlib.util.spec_from_file_location("external_opponent_bot", file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec from {file_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        raise

    bot_cls = getattr(module, class_name, None)
    if bot_cls is None:
        raise AttributeError(
            f"Class '{class_name}' not found in '{rel_file_path}'. "
            f"Available: {[n for n in dir(module) if not n.startswith('_')]}"
        )

    return bot_cls


def main():
    opponent_file = os.environ.get("OPPONENT_FILE")
    opponent_class = os.environ.get("OPPONENT_CLASS")
    opponent_race_str = os.environ.get("OPPONENT_RACE", "random")
    existing_match_id = os.environ.get("MATCH_ID")
    external_bot_dir = os.environ.get("EXTERNAL_BOT_DIR", "")

    if not opponent_class:
        logger.error("OPPONENT_CLASS environment variable is required")
        sys.exit(1)

    if not external_bot_dir and not opponent_file:
        logger.error("OPPONENT_FILE is required for non-external bots")
        sys.exit(1)

    opponent_race = RACE_DICT.get(opponent_race_str.lower(), Race.Random)

    # Match ID handling (set up early so crashes can be recorded)
    match_id: int | None = int(existing_match_id) if existing_match_id else None

    try:
        # Load opponent bot class
        if external_bot_dir:
            module_path = opponent_file or ''
            OpponentClass = load_external_opponent(external_bot_dir, module_path, opponent_class)
            logger.info(f"Loaded external opponent: {opponent_class} from {external_bot_dir}/{module_path}")
        else:
            assert opponent_file is not None  # guarded by sys.exit above
            OpponentClass = load_opponent_class(opponent_file, opponent_class)
            logger.info(f"Loaded opponent: {opponent_class} from {opponent_file} ({opponent_race})")

        # Choose map
        chosen_map_name = random.choice(MAP_LIST)
        sc2_map = maps.get(chosen_map_name)

        if match_id:
            update_match_map(match_id, chosen_map_name)
        else:
            logger.warning("No MATCH_ID provided; results will only be logged, not saved to DB.")

        replay_path = f"/root/replays/{match_id}_vs_{opponent_class}_{chosen_map_name}.SC2Replay"

        # Set match ID as environment variable for the bot
        if match_id:
            os.environ["TEST_MATCH_ID"] = str(match_id)

        # Bot vs Bot: run_game with two Bot() players
        result: Result | list[Result | None] = run_game(
            sc2_map,
            [
                Bot(Race.Terran, BotTato(), "BotTato"),
                Bot(opponent_race, OpponentClass(), opponent_class),
            ],
            realtime=False,
            save_replay_as=replay_path,
            game_time_limit=3600,
        )

        # Bot vs Bot returns a list of two Results: [bottato_result, opponent_result]
        if isinstance(result, list):
            bottato_result = result[0]
        else:
            bottato_result = result

        result_name = bottato_result.name if bottato_result else "Unknown"

        logger.info(
            f"\n================================\n"
            f"Result vs {opponent_class}: {bottato_result}\n"
            f"================================"
        )

        if match_id:
            update_match_result(match_id, result_name)

    except Exception as e:
        logger.error(
            f"\n================================\n"
            f"Result vs {opponent_class}: Crash — {e}\n"
            f"================================"
        )
        if match_id:
            update_match_result(match_id, "Crash")
        raise e


if __name__ == "__main__":
    main()

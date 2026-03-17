"""
Run BotTato against a custom opponent bot (Bot vs Bot).

Environment variables:
  OPPONENT_FILE  - Python filename in other_bots/ (e.g. worker_rush.py)
  OPPONENT_CLASS - Class name inheriting from BotAI (e.g. WorkerRushBot)
  OPPONENT_RACE  - Race name: protoss, terran, zerg, or random
  MATCH_ID       - (optional) existing match row to update
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys
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


def main():
    opponent_file = os.environ.get("OPPONENT_FILE")
    opponent_class = os.environ.get("OPPONENT_CLASS")
    opponent_race_str = os.environ.get("OPPONENT_RACE", "random")
    existing_match_id = os.environ.get("MATCH_ID")

    if not opponent_file or not opponent_class:
        logger.error("OPPONENT_FILE and OPPONENT_CLASS environment variables are required")
        sys.exit(1)

    opponent_race = RACE_DICT.get(opponent_race_str.lower(), Race.Random)

    # Match ID handling (set up early so crashes can be recorded)
    match_id: int | None = int(existing_match_id) if existing_match_id else None

    try:
        # Load opponent bot class
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

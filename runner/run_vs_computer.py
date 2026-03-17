"""
Run BotTato against a built-in Computer opponent (Bot vs Computer).

Environment variables:
  RACE       - Opponent race: protoss, terran, zerg, or random
  BUILD      - Opponent build: rush, timing, macro, power, air
  DIFFICULTY - Opponent difficulty (default: CheatInsane)
  MATCH_ID   - (optional) existing match row to update
"""

from __future__ import annotations

import os
from datetime import datetime
from loguru import logger

from config import BUILD_DICT, DIFFICULTY_DICT, MAP_LIST, RACE_DICT
from db_helpers import (
    create_pending_match,
    get_least_used_map,
    get_next_test_group_id,
    update_match_map,
    update_match_result,
)
from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

from bottato.bottato import BotTato

# pyre-ignore[11]
bot_class: type[BotAI] = BotTato


def main():
    race = os.environ.get("RACE")
    build = os.environ.get("BUILD")
    difficulty_env = os.environ.get("DIFFICULTY")
    existing_match_id = os.environ.get("MATCH_ID")

    opponent_race = RACE_DICT.get(race, RACE_DICT[None])
    opponent_build = BUILD_DICT.get(build, BUILD_DICT[None])
    difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])

    least_used_map = get_least_used_map(
        opponent_race.name, opponent_build.name, difficulty.name, MAP_LIST
    )
    map = maps.get(least_used_map)

    opponent = Computer(opponent_race, difficulty, ai_build=opponent_build)
    start_time = datetime.now().isoformat()

    if existing_match_id:
        # Use existing match ID and update it with map name
        match_id = int(existing_match_id)
        update_match_map(match_id, least_used_map)
    else:
        # Fallback: create new match if no ID provided (for backward compatibility)
        test_group_id = get_next_test_group_id()
        match_id = create_pending_match(
            test_group_id, start_time, least_used_map,
            opponent_race.name, difficulty.name, opponent_build.name,
        )
        assert match_id is not None, "Failed to create match entry in the database."

    replay_path = f"/root/replays/{match_id}_{least_used_map}_{race}-{build}.SC2Replay"

    # Set match ID as environment variable for the bot
    os.environ["TEST_MATCH_ID"] = str(match_id)

    try:
        result: Result | list[Result | None] = run_game(
            map,
            [Bot(Race.Terran, bot_class(), "BotTato"), opponent],
            realtime=False,
            save_replay_as=replay_path,
            game_time_limit=3600,
        )

        bottato_result = result[0] if isinstance(result, list) else result
        logger.info(
            f"\n================================\n"
            f"Result vs {opponent}: {bottato_result}\n"
            f"================================"
        )

        # Update the existing match entry with the result
        if bottato_result:
            update_match_result(match_id, bottato_result.name)

    except Exception as e:
        logger.info(
            f"\n================================\n"
            f"Result vs {opponent}: Crash\n"
            f"================================"
        )
        update_match_result(match_id, "Crash")
        raise e

    assert bottato_result == Result.Victory, (
        f"BotTato should win against {opponent}, but got {bottato_result}"
    )


if __name__ == "__main__":
    main()

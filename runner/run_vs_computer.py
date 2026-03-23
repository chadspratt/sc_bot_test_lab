"""
Run BotTato against a built-in Computer opponent (Bot vs Computer).

Runs inside a Docker container.  The match and map are created by the
Django API before launch; this script only needs to play the game and
report the result via stdout.

Environment variables:
  RACE       - Opponent race: protoss, terran, zerg, or random
  BUILD      - Opponent build: rush, timing, macro, power, air
  DIFFICULTY - Opponent difficulty (default: CheatInsane)
  MAP_NAME   - Map to play on (required)
  MATCH_ID   - Match row ID (required, used for replay naming)
"""

from __future__ import annotations

import os
from loguru import logger

from config import BUILD_DICT, DIFFICULTY_DICT, RACE_DICT
from sc2 import maps
from sc2.data import Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

from bottato.bottato import BotTato


def main() -> str:
    """Run a single match and return the result string.

    The result is also printed to stdout as ``MATCH_RESULT:<result>`` so
    the host-side monitoring thread can parse it from the container log.
    """
    race = os.environ.get("RACE")
    build = os.environ.get("BUILD")
    difficulty_env = os.environ.get("DIFFICULTY")
    map_name = os.environ["MAP_NAME"]
    match_id = os.environ["MATCH_ID"]

    opponent_race = RACE_DICT.get(race, RACE_DICT[None])
    opponent_build = BUILD_DICT.get(build, BUILD_DICT[None])
    difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])

    map_data = maps.get(map_name)
    opponent = Computer(opponent_race, difficulty, ai_build=opponent_build)

    replay_path = f"/root/replays/{match_id}_{map_name}_{race}-{build}.SC2Replay"

    os.environ["TEST_MATCH_ID"] = match_id

    try:
        result: Result | list[Result | None] = run_game(
            map_data,
            [Bot(Race.Terran, BotTato(), "BotTato"), opponent],
            realtime=False,
            save_replay_as=replay_path,
            game_time_limit=3600,
        )

        bottato_result = result[0] if isinstance(result, list) else result
        result_str = bottato_result.name if bottato_result else "Crash"

    except Exception:
        logger.exception("Match crashed")
        result_str = "Crash"

    logger.info(
        f"\n================================\n"
        f"Result: {result_str}\n"
        f"================================"
    )
    print(f"MATCH_RESULT:{result_str}", flush=True)
    return result_str


if __name__ == "__main__":
    main()

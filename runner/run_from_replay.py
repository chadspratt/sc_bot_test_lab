"""
Run BotTato by continuing from a replay at a specified game loop.

Environment variables:
  REPLAY_PATH              - Path to .SC2Replay file inside the container
  TAKEOVER_GAME_LOOP       - Game loop at which the bot takes over
  BOT_PLAYER_ID            - Which player in the replay BotTato replaces (1 or 2, default: 1)
  DIFFICULTY               - Computer opponent difficulty (default: CheatInsane)
  BUILD                    - Computer opponent build (default: Macro)
  RACE                     - Computer opponent race (default: Random)
  MATCH_ID                 - (optional) existing match row to update in DB
  REPLAY_DURATION          - (optional) seconds after takeover before the bot forfeits
"""

from __future__ import annotations

import os
import sys
from loguru import logger

from config import BUILD_DICT, DIFFICULTY_DICT, RACE_DICT
from db_helpers import update_match_map, update_match_result
from replay_continuation import run_game_from_replay
from sc2.data import Difficulty, Race, Result
from sc2.player import Bot, Computer

from bottato.bottato import BotTato


def main():
    replay_path = os.environ.get("REPLAY_PATH")
    takeover_loop_str = os.environ.get("TAKEOVER_GAME_LOOP")
    bot_player_id = int(os.environ.get("BOT_PLAYER_ID", "1"))
    difficulty_env = os.environ.get("DIFFICULTY")
    build_env = os.environ.get("BUILD")
    race_env = os.environ.get("RACE")
    existing_match_id = os.environ.get("MATCH_ID")

    if not replay_path:
        logger.error("REPLAY_PATH environment variable is required")
        sys.exit(1)

    if not takeover_loop_str:
        logger.error("TAKEOVER_GAME_LOOP environment variable is required")
        sys.exit(1)

    takeover_game_loop = int(takeover_loop_str)
    difficulty: Difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])
    ai_build = BUILD_DICT.get(build_env, BUILD_DICT[None])
    race = RACE_DICT.get(race_env, RACE_DICT[None])

    match_id: int | None = int(existing_match_id) if existing_match_id else None

    logger.info(f"Continue from replay: {replay_path}")
    logger.info(f"Takeover at game loop: {takeover_game_loop} (~{takeover_game_loop / 22.4:.0f}s)")
    logger.info(f"Bot player ID: {bot_player_id}, Difficulty: {difficulty}, Build: {ai_build}, Race: {race}")

    # Set match ID as environment variable for the bot
    if match_id:
        os.environ["TEST_MATCH_ID"] = str(match_id)

    # Set takeover time so BotTato can offset self.time
    takeover_time_seconds = takeover_game_loop / 22.4
    os.environ["REPLAY_TAKEOVER_TIME"] = str(takeover_time_seconds)
    logger.info(f"Set REPLAY_TAKEOVER_TIME={takeover_time_seconds:.1f}s")

    replay_duration = os.environ.get("REPLAY_DURATION")
    if replay_duration:
        logger.info(f"REPLAY_DURATION={replay_duration}s (bot will forfeit after this)")

    output_replay_path = None
    if match_id:
        output_replay_path = f"/root/replays/{match_id}_continued.SC2Replay"

    try:
        # The opponent race will be determined from the replay automatically
        # by run_game_from_replay (it overrides Computer race to match replay)
        result: Result = run_game_from_replay(
            replay_path=replay_path,
            target_game_loop=takeover_game_loop,
            players=[
                Bot(Race.Terran, BotTato(), "BotTato"),
                Computer(race, difficulty, ai_build=ai_build),
            ],
            bot_player_id=bot_player_id,
            realtime=False,
            save_replay_as=output_replay_path,
            game_time_limit=3600,
        )

        logger.info(
            f"\n================================\n"
            f"Result (continued from replay): {result}\n"
            f"================================"
        )

        if match_id:
            update_match_result(match_id, result.name)

    except Exception as e:
        logger.error(
            f"\n================================\n"
            f"Crash during continue-from-replay: {e}\n"
            f"================================"
        )
        if match_id:
            update_match_result(match_id, "Crash")
        raise e


if __name__ == "__main__":
    main()

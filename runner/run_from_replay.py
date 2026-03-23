"""Run BotTato by continuing from a replay at a specified game loop.

Runs inside a Docker container.  The match is created by the Django API
before launch; this script only needs to play the game and report the
result via stdout.

Environment variables:
  REPLAY_PATH              - Path to .SC2Replay file inside the container
  TAKEOVER_GAME_LOOP       - Game loop at which the bot takes over
  BOT_PLAYER_ID            - Which player in the replay BotTato replaces (1 or 2, default: 1)
  DIFFICULTY               - Computer opponent difficulty (default: CheatInsane)
  BUILD                    - Computer opponent build (default: Macro)
  RACE                     - Computer opponent race (default: Random)
  MATCH_ID                 - Match row ID (required, used for replay naming)
  REPLAY_DURATION          - (optional) seconds after takeover before the bot forfeits
"""

from __future__ import annotations

import os
import sys
from loguru import logger

from config import BUILD_DICT, DIFFICULTY_DICT, RACE_DICT
from replay_continuation import run_game_from_replay
from sc2.data import Difficulty, Race, Result
from sc2.player import Bot, Computer

from bottato.bottato import BotTato


def main() -> str:
    """Run a continue-from-replay match and return the result string.

    The result is also printed to stdout as ``MATCH_RESULT:<result>`` so
    the host-side monitoring thread can parse it from the container log.
    """
    replay_path = os.environ.get("REPLAY_PATH")
    takeover_loop_str = os.environ.get("TAKEOVER_GAME_LOOP")
    bot_player_id = int(os.environ.get("BOT_PLAYER_ID", "1"))
    difficulty_env = os.environ.get("DIFFICULTY")
    build_env = os.environ.get("BUILD")
    race_env = os.environ.get("RACE")
    match_id = os.environ["MATCH_ID"]

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

    logger.info(f"Continue from replay: {replay_path}")
    logger.info(f"Takeover at game loop: {takeover_game_loop} (~{takeover_game_loop / 22.4:.0f}s)")
    logger.info(f"Bot player ID: {bot_player_id}, Difficulty: {difficulty}, Build: {ai_build}, Race: {race}")

    os.environ["TEST_MATCH_ID"] = match_id

    # Set takeover time so BotTato can offset self.time
    takeover_time_seconds = takeover_game_loop / 22.4
    os.environ["REPLAY_TAKEOVER_TIME"] = str(takeover_time_seconds)
    logger.info(f"Set REPLAY_TAKEOVER_TIME={takeover_time_seconds:.1f}s")

    replay_duration = os.environ.get("REPLAY_DURATION")
    if replay_duration:
        logger.info(f"REPLAY_DURATION={replay_duration}s (bot will forfeit after this)")

    output_replay_path = f"/root/replays/{match_id}_continued.SC2Replay"

    try:
        result, map_name = run_game_from_replay(
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

        result_str = result.name if result else "Crash"

        logger.info(
            f"\n================================\n"
            f"Result (continued from replay): {result_str}\n"
            f"Map: {map_name}\n"
            f"================================"
        )

    except Exception:
        logger.exception("Crash during continue-from-replay")
        result_str = "Crash"

    print(f"MATCH_RESULT:{result_str}", flush=True)
    return result_str


if __name__ == "__main__":
    main()

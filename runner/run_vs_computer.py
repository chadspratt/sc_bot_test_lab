"""Run a bot against a built-in Computer opponent (Bot vs Blizzard AI).

Runs inside a Docker container.  The match and map are created by the
Django API before launch; this script only needs to play the game and
report the result via stdout.

Environment variables:
  RACE       - Opponent race: protoss, terran, zerg, or random
  BUILD      - Opponent build: rush, timing, macro, power, air
  DIFFICULTY - Opponent difficulty (default: CheatInsane)
  MAP_NAME   - Map to play on (required)
  MATCH_ID   - Match row ID (required, used for replay naming)
  BOT_DIR    - Absolute path to the bot directory inside the container
               (default: /root/bot_dir). Must contain ladderbots.json
               or BOT_MODULE/BOT_CLASS env vars.
  BOT_MODULE - Python module path to import the bot class from (e.g. 'bottato.bottato')
  BOT_CLASS  - Bot class name within the module (e.g. 'BotTato')
  BOT_RACE   - Bot race: Protoss, Terran, Zerg, or Random
  BOT_NAME   - Display name for the bot (used in replay metadata)
"""

from __future__ import annotations

import importlib
import json
import logging
import os

logger = logging.getLogger(__name__)

from config import BUILD_DICT, DIFFICULTY_DICT, RACE_DICT
from sc2 import maps
from sc2.data import Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer


def _read_ladderbots(bot_dir: str) -> dict:
    """Read ladderbots.json from the bot directory and return the first bot entry."""
    lb_path = os.path.join(bot_dir, "ladderbots.json")
    if not os.path.isfile(lb_path):
        return {}
    with open(lb_path) as f:
        data = json.load(f)
    bots = data.get("Bots", {})
    if not bots:
        return {}
    _name, info = next(iter(bots.items()))
    info["_bot_name"] = _name
    return info


def main() -> str:
    """Run a single match and return the result string.

    The result is also printed to stdout as ``MATCH_RESULT:<result>`` so
    the host-side monitoring thread can parse it from the container log.
    """
    bot_dir = os.environ.get("BOT_DIR", "/root/bot_dir")
    os.chdir(bot_dir)

    race = os.environ.get("RACE")
    build = os.environ.get("BUILD")
    difficulty_env = os.environ.get("DIFFICULTY")
    map_name = os.environ["MAP_NAME"]
    match_id = os.environ["MATCH_ID"]

    # Read ladderbots.json for defaults
    lb_info = _read_ladderbots(bot_dir)

    bot_module_path = os.environ.get("BOT_MODULE") or ""
    bot_class_name = os.environ.get("BOT_CLASS") or ""
    bot_race_name = os.environ.get("BOT_RACE") or lb_info.get("Race", "Random")
    bot_name = os.environ.get("BOT_NAME") or lb_info.get("_bot_name", bot_class_name)

    if not bot_module_path or not bot_class_name:
        raise RuntimeError(
            "BOT_MODULE and BOT_CLASS environment variables are required for "
            "Python bots. Set them on the CustomBot model in the admin panel."
        )

    # Dynamically import the bot class
    bot_module = importlib.import_module(bot_module_path)
    bot_cls = getattr(bot_module, bot_class_name)
    bot_race = RACE_DICT.get(bot_race_name.lower(), Race[bot_race_name])

    opponent_race = RACE_DICT.get(race, RACE_DICT[None])
    opponent_build = BUILD_DICT.get(build, BUILD_DICT[None])
    difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])

    map_data = maps.get(map_name)
    opponent = Computer(opponent_race, difficulty, ai_build=opponent_build)

    replay_path = f"/root/replays/{match_id}_{map_name}_{race}-{build}.SC2Replay"

    os.environ["TEST_MATCH_ID"] = match_id

    bot_instance = bot_cls()
    duration: int | None = None

    try:
        result: Result | list[Result | None] = run_game(
            map_data,
            [Bot(bot_race, bot_instance, bot_name), opponent],
            realtime=False,
            save_replay_as=replay_path,
            game_time_limit=3600,
        )

        bottato_result = result[0] if isinstance(result, list) else result
        result_str = bottato_result.name if bottato_result else "Crash"

        # Extract game duration from the bot's state (BotAI.time = game_loop / 22.4)
        if hasattr(bot_instance, 'time'):
            try:
                duration = int(bot_instance.time)
            except Exception:
                pass

    except Exception:
        logger.exception("Match crashed")
        result_str = "Crash"

    logger.info(
        f"\n================================\n"
        f"Result: {result_str}\n"
        f"================================"
    )
    print(f"MATCH_RESULT:{result_str}", flush=True)
    if duration is not None:
        print(f"MATCH_DURATION:{duration}", flush=True)
    # Report the resolved race (useful when bot was set to Random)
    if hasattr(bot_instance, 'race') and bot_instance.race is not None:
        try:
            print(f"BOT_RACE:{bot_instance.race.name}", flush=True)
        except Exception:
            pass
    return result_str


if __name__ == "__main__":
    main()

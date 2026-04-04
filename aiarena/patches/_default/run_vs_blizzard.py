"""Run a bot against a built-in Blizzard AI inside Docker.

*** GENERIC TEMPLATE — edit the placeholders marked with TODO below ***

Designed to be exec'd from run_docker.sh when a bot ships its own
run_vs_blizzard.py.  Imports the bot class directly, avoiding
module-name collisions between the runner's config.py and
bot modules.

Reads the same environment variables as run_vs_computer.py:
  MAP_NAME, MATCH_ID        - required
  RACE, BUILD, DIFFICULTY   - opponent settings
  BOT_RACE                  - bot race (default from ladderbots.json Race)
"""

from __future__ import annotations

import logging
import os

from sc2 import maps
from sc2.data import AIBuild, Difficulty, Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

# TODO: add any sys.path inserts needed for the bot's dependencies here
# import sys
# sys.path.insert(1, "some_dependency_dir")


# TODO: replace with the bot's actual import
# from my_bot_package.my_bot_module import MyBotClass
raise ImportError(
    "Generic run_vs_blizzard.py template — edit the import and "
    "TODOs before use. See patches/_default/run_vs_blizzard.py"
)
# from my_bot_package.my_bot_module import MyBotClass

logger = logging.getLogger(__name__)

# TODO: set the bot's display name and default race
BOT_NAME = "CHANGE_ME"          # display name, e.g. "Chance"
BOT_DEFAULT_RACE = "Random"     # default race if BOT_RACE env var not set

RACE_DICT: dict[str | None, Race] = {
    None: Race.Random,
    "random": Race.Random,
    "protoss": Race.Protoss,
    "terran": Race.Terran,
    "zerg": Race.Zerg,
}

BUILD_DICT: dict[str | None, AIBuild] = {
    None: AIBuild.RandomBuild,
    "rush": AIBuild.Rush,
    "timing": AIBuild.Timing,
    "macro": AIBuild.Macro,
    "power": AIBuild.Power,
    "air": AIBuild.Air,
    "randombuild": AIBuild.RandomBuild,
}

DIFFICULTY_DICT: dict[str | None, Difficulty] = {
    None: Difficulty.CheatInsane,
    "Easy": Difficulty.Easy,
    "Medium": Difficulty.Medium,
    "MediumHard": Difficulty.MediumHard,
    "Hard": Difficulty.Hard,
    "Harder": Difficulty.Harder,
    "VeryHard": Difficulty.VeryHard,
    "CheatVision": Difficulty.CheatVision,
    "CheatMoney": Difficulty.CheatMoney,
    "CheatInsane": Difficulty.CheatInsane,
}


def main() -> str:
    map_name = os.environ["MAP_NAME"]
    match_id = os.environ["MATCH_ID"]
    race = os.environ.get("RACE")
    build = os.environ.get("BUILD")
    difficulty_env = os.environ.get("DIFFICULTY")
    bot_race_name = os.environ.get("BOT_RACE", BOT_DEFAULT_RACE)

    opponent_race = RACE_DICT.get(race, RACE_DICT[None])
    opponent_build = BUILD_DICT.get(build, BUILD_DICT[None])
    difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])
    bot_race = RACE_DICT.get(bot_race_name.lower(), Race.Random)

    map_data = maps.get(map_name)
    opponent = Computer(opponent_race, difficulty, ai_build=opponent_build)

    replay_path = f"/root/replays/{match_id}_{map_name}_{race}-{build}.SC2Replay"
    os.environ["TEST_MATCH_ID"] = match_id

    # TODO: replace with the bot's constructor
    bot_instance = MyBotClass()  # noqa: F821
    duration: int | None = None
    result_str = "Crash"

    try:
        result: Result | list[Result | None] = run_game(
            map_data,
            [Bot(bot_race, bot_instance, BOT_NAME), opponent],
            realtime=False,
            save_replay_as=replay_path,
            game_time_limit=3600,
        )

        bottato_result = result[0] if isinstance(result, list) else result
        result_str = bottato_result.name if bottato_result else "Crash"

        if hasattr(bot_instance, "time"):
            try:
                duration = int(bot_instance.time)
            except Exception:
                pass

    except Exception:
        logger.exception("Match crashed")

    print(
        f"\n================================\n"
        f"Result: {result_str}\n"
        f"================================"
    )
    print(f"MATCH_RESULT:{result_str}", flush=True)
    if duration is not None:
        print(f"MATCH_DURATION:{duration}", flush=True)
    if hasattr(bot_instance, "race") and bot_instance.race is not None:
        try:
            print(f"BOT_RACE:{bot_instance.race.name}", flush=True)
        except Exception:
            pass
    return result_str


if __name__ == "__main__":
    main()

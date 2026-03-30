"""Launch an external (non-Python) bot against a built-in Computer opponent.

Works for any bot type that speaks the SC2 client protocol (Node.js, C++,
.NET, Java, etc.).  The bot connects to a locally launched SC2 instance
via WebSocket rather than being imported as a Python module.

Flow:
  1. Launch SC2 headless via python_sc2's SC2Process
  2. Create a game (Participant + Computer) via the SC2 API
  3. Spawn the bot process with --GamePort / --LadderServer / --StartPort
  4. Wait for the bot process to exit
  5. Save the replay and report the result

Environment variables (same as run_vs_computer.py):
  RACE, BUILD, DIFFICULTY, MAP_NAME, MATCH_ID, BOT_DIR
  BOT_ENTRY   - Entry point filename (e.g. 'norman.js'), read from
                ladderbots.json FileName if not set
  BOT_RACE    - Bot race for createGame participant (default from ladderbots.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import aiohttp

logger = logging.getLogger(__name__)

from config import BUILD_DICT, DIFFICULTY_DICT, RACE_DICT
from sc2 import maps
from sc2.data import Race, Result
from sc2.player import Computer
from sc2.sc2process import KillSwitch, SC2Process

import s2clientprotocol.sc2api_pb2 as sc_pb

# Max seconds to wait for the bot process after game is expected to end.
_BOT_PROCESS_TIMEOUT = 600

# SC2 proto race values
_RACE_NAME_TO_PROTO = {
    "terran": 1, "zerg": 2, "protoss": 3, "random": 4,
}

# ES module wrapper: connects via CLI args, joins WITHOUT multi-player port
# config.  Ladder bots (e.g. norman) always send sharedPort/serverPorts/
# clientPorts in joinGame, which hangs SC2 in single-player (vs Blizzard AI) mode.
_GAME_VS_COMPUTER_JS = """\
import Game from "./game.js";

export default class VsComputerGame extends Game {
  async connect() {
    const args = process.argv;
    let host = "127.0.0.1", port = 5000;
    for (let i = 0; i < args.length - 1; i++) {
      if (args[i] === "--LadderServer") host = args[i + 1];
      if (args[i] === "--GamePort") port = parseInt(args[i + 1]);
    }
    await this.client.connect({ host, port });
    await this.client.joinGame({ race: __RACE__, options: { raw: true } });
  }
}
"""


def _patch_nodejs_for_vs_computer(
    bot_dir: str, lb_info: dict,
) -> list[tuple[str, str | None]]:
    """Patch a Node.js bot to joinGame without multi-player port config.

    Writes a ``game-vs-computer.js`` wrapper and points the bot's env file
    at it.  Returns a list of ``(path, original_content_or_None)`` for
    rollback — *None* means the file was newly created and should be deleted.
    """
    patches: list[tuple[str, str | None]] = []

    race_str = lb_info.get("Race", "Random").lower()
    race_val = _RACE_NAME_TO_PROTO.get(race_str, 4)

    game_dir = os.path.join(bot_dir, "body", "starcraft")
    if not os.path.isdir(game_dir):
        logger.warning("No body/starcraft/ directory; skipping nodejs patch")
        return patches

    # 1. Write the wrapper module
    wrapper_path = os.path.join(game_dir, "game-vs-computer.js")
    with open(wrapper_path, "w") as f:
        f.write(_GAME_VS_COMPUTER_JS.replace("__RACE__", str(race_val)))
    patches.append((wrapper_path, None))
    logger.info(f"Wrote {wrapper_path} (race={race_val})")

    # 2. Patch env file(s) to reference game-vs-computer instead of game-ladder
    for env_name in ("norman.env",):
        env_path = os.path.join(bot_dir, env_name)
        if not os.path.isfile(env_path):
            continue
        with open(env_path) as f:
            original = f.read()
        if "game-ladder" not in original:
            continue
        try:
            env_data = json.loads(original)
            for body_entry in env_data.get("body", []):
                code = body_entry.get("code", "")
                if "game-ladder" in code:
                    body_entry["code"] = code.replace(
                        "game-ladder", "game-vs-computer",
                    )
            with open(env_path, "w") as f:
                json.dump(env_data, f)
            patches.append((env_path, original))
            logger.info(f"Patched {env_name} → game-vs-computer")
        except (json.JSONDecodeError, KeyError):
            logger.warning(f"Could not parse {env_name}; skipping patch")

    return patches


def _rollback_patches(patches: list[tuple[str, str | None]]) -> None:
    """Restore original files after a vs-computer match."""
    for fpath, original_content in patches:
        try:
            if original_content is None:
                os.remove(fpath)
            else:
                with open(fpath, "w") as f:
                    f.write(original_content)
        except OSError:
            pass


def _read_ladderbots(bot_dir: str) -> dict:
    """Read ladderbots.json (case-insensitive) and return the first bot entry."""
    for name in ("ladderbots.json", "LadderBots.json"):
        lb_path = os.path.join(bot_dir, name)
        if os.path.isfile(lb_path):
            with open(lb_path) as f:
                data = json.load(f)
            bots = data.get("Bots", {})
            if bots:
                _name, info = next(iter(bots.items()))
                info["_bot_name"] = _name
                return info
    return {}


def _build_bot_command(
    bot_dir: str, lb_info: dict, port: int, bot_type: str,
) -> list[str]:
    """Build the command to launch the external bot process."""
    entry = os.environ.get("BOT_ENTRY") or lb_info.get("FileName", "")
    if not entry:
        raise RuntimeError(
            "No bot entry point found. Set BOT_ENTRY or FileName in ladderbots.json."
        )

    entry_path = os.path.join(bot_dir, entry)

    if bot_type == "nodejs":
        return [
            "node", entry_path,
            "--GamePort", str(port),
            "--LadderServer", "127.0.0.1",
            "--StartPort", str(port),
        ]
    elif bot_type in ("cpplinux", "cppwin32"):
        os.chmod(entry_path, 0o755)
        return [
            entry_path,
            "--GamePort", str(port),
            "--LadderServer", "127.0.0.1",
            "--StartPort", str(port),
        ]
    elif bot_type == "dotnetcore":
        return [
            "dotnet", entry_path,
            "--GamePort", str(port),
            "--LadderServer", "127.0.0.1",
            "--StartPort", str(port),
        ]
    elif bot_type == "java":
        return [
            "java", "-jar", entry_path,
            "--GamePort", str(port),
            "--LadderServer", "127.0.0.1",
            "--StartPort", str(port),
        ]
    else:
        raise RuntimeError(f"Unsupported bot type: {bot_type}")


async def _run_match(bot_type: str) -> tuple[str, int | None]:
    """Launch SC2, create the game, run the bot, and return the result and duration.

    SC2 only allows one WebSocket client at a time, so we must disconnect
    Python's WebSocket after creating the game, before spawning the bot.
    """
    bot_dir = os.environ.get("BOT_DIR", "/root/bot_dir")
    os.chdir(bot_dir)

    race_env = os.environ.get("RACE")
    build_env = os.environ.get("BUILD")
    difficulty_env = os.environ.get("DIFFICULTY")
    map_name = os.environ["MAP_NAME"]
    match_id = os.environ["MATCH_ID"]

    lb_info = _read_ladderbots(bot_dir)

    opponent_race = RACE_DICT.get(race_env, RACE_DICT[None])
    opponent_build = BUILD_DICT.get(build_env, BUILD_DICT[None])
    difficulty = DIFFICULTY_DICT.get(difficulty_env, DIFFICULTY_DICT[None])

    map_data = maps.get(map_name)
    replay_path = f"/root/replays/{match_id}_{map_name}_{race_env}-{build_env}.SC2Replay"

    # Manually manage SC2Process so we can disconnect between createGame
    # and bot spawn (SC2 only allows one WebSocket client at a time).
    sc2_proc = SC2Process()
    patches: list[tuple[str, str | None]] = []
    try:
        controller = await sc2_proc.__aenter__()
        sc2_port = sc2_proc._port

        # Create the game: one Participant (the external bot) + one Computer
        req = sc_pb.RequestCreateGame(
            local_map=sc_pb.LocalMap(map_path=str(map_data.relative_path)),
            realtime=False,
        )

        # Player 1: the external bot (Participant)
        p1 = req.player_setup.add()
        p1.type = sc_pb.Participant

        # Player 2: Computer opponent
        p2 = req.player_setup.add()
        p2.type = sc_pb.Computer
        p2.race = opponent_race.value
        p2.difficulty = difficulty.value
        p2.ai_build = opponent_build.value

        logger.info(f"Creating game on {map_name}")
        result = await controller._execute(create_game=req)
        if result.create_game.HasField("error"):
            err = f"Could not create game: {result.create_game.error}"
            if result.create_game.HasField("error_details"):
                err += f": {result.create_game.error_details}"
            raise RuntimeError(err)

        # Disconnect Python's WebSocket so the bot can connect to SC2
        logger.info(f"Game created on port {sc2_port}. Releasing WebSocket for bot.")
        await sc2_proc._close_connection()

        # For Node.js bots: patch joinGame to omit multi-player port config
        if bot_type == "nodejs":
            patches = _patch_nodejs_for_vs_computer(bot_dir, lb_info)

        cmd = _build_bot_command(bot_dir, lb_info, sc2_port, bot_type)
        logger.info(f"Bot command: {' '.join(cmd)}")

        bot_proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=bot_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream bot output with a global timeout so a hanging bot
        # doesn't block the container forever.
        bot_output_lines: list[str] = []

        async def _stream_and_wait() -> int:
            assert bot_proc.stdout is not None
            async for raw_line in bot_proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                sys.stdout.write(line)
                sys.stdout.flush()
                bot_output_lines.append(line)
            return await bot_proc.wait()

        try:
            exit_code = await asyncio.wait_for(
                _stream_and_wait(), timeout=_BOT_PROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Bot process did not exit in time — killing")
            bot_proc.kill()
            await bot_proc.wait()
            exit_code = -1
        logger.info(f"Bot process exited with code {exit_code}")

        # Reconnect to SC2 to query the game result and save the replay.
        # The external bot has disconnected, so we can reclaim the WebSocket.
        game_result: str | None = None
        game_duration: int | None = None
        try:
            sc2_proc._session = aiohttp.ClientSession()
            sc2_proc._ws = await sc2_proc._session.ws_connect(
                sc2_proc.ws_url, timeout=10,
            )
            controller._ws = sc2_proc._ws

            # Query observation — player_result is populated once the game ends
            obs_resp = await controller._execute(
                observation=sc_pb.RequestObservation(),
            )
            if obs_resp.observation.player_result:
                for pr in obs_resp.observation.player_result:
                    if pr.player_id == 1:  # the external bot is player 1
                        game_result = Result(pr.result).name
                        logger.info(f"SC2 reported result for player 1: {game_result}")
                        break

            # Extract game duration from the observation
            game_loop = obs_resp.observation.observation.game_loop
            if game_loop:
                game_duration = int(game_loop / 22.4)
                logger.info(f"Game duration: {game_duration}s ({game_loop} loops)")

            # Save replay
            save_resp = await controller._execute(
                save_replay=sc_pb.RequestSaveReplay(),
            )
            if save_resp.save_replay.data:
                os.makedirs(os.path.dirname(replay_path), exist_ok=True)
                with open(replay_path, "wb") as f:
                    f.write(save_resp.save_replay.data)
                logger.info(f"Replay saved to {replay_path}")
        except Exception:
            logger.warning("Could not reconnect to SC2 (may have already quit)",
                           exc_info=True)

    finally:
        _rollback_patches(patches)
        await sc2_proc._close_connection()
        KillSwitch.kill_all()

    # Prefer the authoritative SC2 result; fall back to log parsing
    result = game_result if game_result else _parse_external_result(exit_code, bot_output_lines)
    return result, game_duration


def _parse_external_result(exit_code: int, output_lines: list[str]) -> str:
    """Fallback result detection when SC2 observation is unavailable.

    Only used if we couldn't reconnect to SC2 after the game to query the
    authoritative player_result via RequestObservation.
    """
    if exit_code != 0:
        return "Crash"

    # Scan output for clues (last 50 lines to avoid long games)
    tail = output_lines[-50:] if len(output_lines) > 50 else output_lines
    for line in reversed(tail):
        low = line.lower().strip()
        # Some bots print explicit result
        if "victory" in low and "defeat" not in low:
            return "Victory"
        if "defeat" in low and "victory" not in low:
            return "Defeat"

    # Bot exited cleanly — could be victory or defeat
    return "Unknown"


def main(bot_type: str) -> None:
    result_str = "Crash"
    duration: int | None = None
    try:
        result_str, duration = asyncio.run(_run_match(bot_type))
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
    # For external bots, report the configured race (can't detect resolved Random)
    bot_race_name = os.environ.get('BOT_RACE', '')
    if bot_race_name:
        print(f"BOT_RACE:{bot_race_name}", flush=True)


if __name__ == "__main__":
    bot_type = sys.argv[1] if len(sys.argv) > 1 else "nodejs"
    main(bot_type)

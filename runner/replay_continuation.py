"""
Continue a game from a replay at a specified game loop.

Approach (based on the VSCode StarCraft extension's proven technique):
1. Start SC2, load the replay, step to the target game loop
2. Capture the full observation (all unit positions, types, owners)
3. Leave the replay
4. Create a new game on the same map with the same player setup
5. Join the game; use debug commands to reconstruct the captured state
6. Bot takes over and plays normally

Limitations (inherent to the reconstruction approach):
- The bot will have no memory of observations before the takeover point
- Hallucinated units from the replay will be spawned as real units
- Upgrades are not fully reconstructed (only units/buildings/resources)
- Slight positional differences from the replay may appear
- The Computer opponent's internal state (build order progress, etc.) resets

See: https://stephanzlatarev.github.io/vscode-starcraft/start-game/continue-replay.html
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from loguru import logger
from pathlib import Path
from typing import Any

from sc2.bot_ai import BotAI
from sc2.client import Client
from sc2.controller import Controller
from sc2.data import AIBuild, Race, Result
from sc2.game_state import GameState
from sc2.main import get_replay_version
from sc2.player import AbstractPlayer, Bot, Computer
from sc2.portconfig import Portconfig
from sc2.protocol import ConnectionAlreadyClosedError, ProtocolError
from sc2.sc2process import SC2Process

from s2clientprotocol import common_pb2 as common_pb
from s2clientprotocol import debug_pb2 as debug_pb
from s2clientprotocol import sc2api_pb2 as sc_pb

# ---------------------------------------------------------------------------
# Data structures for captured replay state
# ---------------------------------------------------------------------------

@dataclass
class CapturedUnit:
    """A unit captured from the replay observation."""
    unit_type: int
    owner: int  # 1 or 2 for players, 16 for neutral
    pos_x: float
    pos_y: float
    pos_z: float
    health: float
    health_max: float
    shield: float
    shield_max: float
    energy: float
    energy_max: float
    build_progress: float
    mineral_contents: int
    vespene_contents: int
    is_flying: bool
    is_burrowed: bool
    is_building: bool  # Determined heuristically


@dataclass
class CapturedReplayState:
    """Full game state captured from a replay at a specific game loop."""
    game_loop: int
    units: list[CapturedUnit]
    map_name: str
    local_map_path: str
    player_races: dict[int, int]  # player_id -> race enum value
    start_locations: list[tuple[float, float]]
    player_minerals: int  # observed player's minerals
    player_vespene: int   # observed player's vespene


# Heuristic: SC2 unit types that are buildings.
# This is NOT exhaustive but covers the main ones. A unit with build_progress < 1.0
# (and not 0) is also likely a building.
# For a more complete solution, we'd query the game data for each unit type.
_TERRAN_BUILDINGS = {
    18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 35, 36, 37, 38, 39,
    40, 41, 42, 43, 44, 45, 46, 47, 130, 132, 134, 5, 6, 7, 8, 9, 10, 11,
    # Barracks=21, Factory=27, Starport=28, CC=18, OrbitalCommand=132,
    # PlanetaryFortress=130, SupplyDepot=19, Refinery=20, EngineeringBay=22,
    # Armory=29, FusionCore=30, GhostAcademy=26, Bunker=24, SensorTower=25,
    # MissileTurret=23, TechLab variants, Reactor variants
}
_PROTOSS_BUILDINGS = {
    59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 133, 894,
    # Nexus=59, Pylon=60, Assimilator=61, Gateway=62, Forge=63,
    # CyberneticsCore=72, PhotonCannon=66, ShieldBattery=894,
    # Stargate=67, RoboticsFacility=71, RoboticsBay=70,
    # WarpGate=133, TwilightCouncil=68, TemplarArchive=69,
    # DarkShrine=65, FleetBeacon=64
}
_ZERG_BUILDINGS = {
    86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101,
    102, 137, 504, 138, 139, 140, 141, 142,
    # Hatchery=86, Lair=100, Hive=101, SpawningPool=89, EvolutionChamber=90,
    # Extractor=88, SpineCrawler=98, SporeCrawler=99,
    # BanelingNest=96, RoachWarren=97, HydraliskDen=91,
    # InfestationPit=94, Spire=92, GreaterSpire=102,
    # NydusNetwork=95, NydusCanal=142, LurkerDen=504,
    # UltraliskCavern=93
}
_ALL_BUILDING_TYPE_IDS = _TERRAN_BUILDINGS | _PROTOSS_BUILDINGS | _ZERG_BUILDINGS

# Townhall unit type IDs (all races, including upgraded forms)
_TOWNHALL_TYPE_IDS = {
    18, 130, 132,  # CommandCenter, PlanetaryFortress, OrbitalCommand
    59,            # Nexus
    86, 100, 101,  # Hatchery, Lair, Hive
}


def _is_building_type(unit_type: int) -> bool:
    """Heuristic check if a unit type ID is a building."""
    return unit_type in _ALL_BUILDING_TYPE_IDS


def _is_player_unit(unit: CapturedUnit) -> bool:
    """Check if a unit belongs to a player (not neutral)."""
    return unit.owner in (1, 2)


def _is_resource(unit: CapturedUnit) -> bool:
    """Check if a unit is a mineral field or vespene geyser."""
    return unit.mineral_contents > 0 or unit.vespene_contents > 0


# ---------------------------------------------------------------------------
# Phase 1: Capture state from replay
# ---------------------------------------------------------------------------

async def _capture_replay_state(
    server: Controller,
    replay_path: str,
    target_game_loop: int,
    bot_player_id: int = 1,
) -> CapturedReplayState:
    """
    Load a replay, step to the target game loop, and capture the full game state.

    :param server: SC2 Controller (from SC2Process context)
    :param replay_path: Path to the .SC2Replay file
    :param target_game_loop: The game loop to capture state at
    :param bot_player_id: Which player the bot will take over (1 or 2)
    :return: CapturedReplayState with all units and metadata
    """
    logger.info(f"Loading replay: {replay_path}")
    logger.info(f"Target game loop: {target_game_loop}")

    # Start replay as observer (player_id=0) with fog disabled to see all units
    ifopts = sc_pb.InterfaceOptions(
        raw=True, score=True, show_cloaked=True,
        raw_affects_selection=True, raw_crop_to_playable_area=False,
    )
    req = sc_pb.RequestStartReplay(
        replay_path=replay_path,
        observed_player_id=bot_player_id,  # Observe as the bot player to get their resources
        realtime=False,
        options=ifopts,
        disable_fog=True,
    )
    result = await server._execute(start_replay=req)
    if result.start_replay.HasField("error"):
        raise RuntimeError(
            f"Failed to start replay: {result.start_replay.error} - "
            f"{result.start_replay.error_details}"
        )
    logger.info("Replay loaded, stepping to target game loop...")

    # Create a temporary client for interacting with the replay
    client = Client(server._ws)

    # Step to the target game loop in chunks (for progress logging)
    STEP_CHUNK = 2000
    current_loop = 0
    while current_loop < target_game_loop:
        step_size = min(STEP_CHUNK, target_game_loop - current_loop)
        await client._execute(step=sc_pb.RequestStep(count=step_size))
        current_loop += step_size
        if current_loop % 10000 == 0:
            logger.info(f"  Replay progress: {current_loop}/{target_game_loop} loops")

    # Capture the observation at the target game loop
    obs_result = await client._execute(observation=sc_pb.RequestObservation())
    observation = obs_result.observation.observation
    game_info_result = await client._execute(game_info=sc_pb.RequestGameInfo())
    game_info = game_info_result.game_info

    actual_loop = observation.game_loop
    logger.info(f"Captured state at game loop {actual_loop}")

    # Extract units
    captured_units: list[CapturedUnit] = []
    for unit in observation.raw_data.units:
        is_building = _is_building_type(unit.unit_type)
        # Also treat any unit with partial build progress as a building
        if 0 < unit.build_progress < 1.0:
            is_building = True

        captured_units.append(CapturedUnit(
            unit_type=unit.unit_type,
            owner=unit.owner,
            pos_x=unit.pos.x,
            pos_y=unit.pos.y,
            pos_z=unit.pos.z,
            health=unit.health,
            health_max=unit.health_max,
            shield=unit.shield,
            shield_max=unit.shield_max,
            energy=unit.energy,
            energy_max=unit.energy_max,
            build_progress=unit.build_progress,
            mineral_contents=unit.mineral_contents,
            vespene_contents=unit.vespene_contents,
            is_flying=unit.is_flying,
            is_burrowed=unit.is_burrowed,
            is_building=is_building,
        ))

    # Extract player info
    player_races: dict[int, int] = {}
    for pi in game_info.player_info:
        player_races[pi.player_id] = pi.race_actual if pi.race_actual else pi.race_requested

    # Extract start locations
    start_locations = [
        (loc.x, loc.y) for loc in game_info.start_raw.start_locations
    ]

    # Player resources (from the observed player)
    player_minerals = observation.player_common.minerals
    player_vespene = observation.player_common.vespene

    logger.info(
        f"Captured {len(captured_units)} units, "
        f"map={game_info.map_name}, "
        f"minerals={player_minerals}, vespene={player_vespene}"
    )

    # Leave the replay
    await client._execute(leave_game=sc_pb.RequestLeaveGame())
    logger.info("Left replay, ready to create new game")

    return CapturedReplayState(
        game_loop=actual_loop,
        units=captured_units,
        map_name=game_info.map_name,
        local_map_path=game_info.local_map_path,
        player_races=player_races,
        start_locations=start_locations,
        player_minerals=player_minerals,
        player_vespene=player_vespene,
    )


# ---------------------------------------------------------------------------
# Phase 2: Reconstruct game state using debug commands
# ---------------------------------------------------------------------------

async def _reconstruct_game_state(
    client: Client,
    state: CapturedReplayState,
    bot_player_id: int = 1,
) -> None:
    """
    Use debug commands to reconstruct the captured game state in the new game.

    Called after the bot has initialized and run a few frames so that
    on_start / _prepare_first_step see a normal starting state. The bot uses
    REPLAY_TAKEOVER_TIME env var to offset self.time so game-time-dependent
    logic works correctly.

    Steps:
    1. Reveal the map so we can see all units
    2. Spawn buildings from the captured state (prevents defeat when old units die)
    3. Kill starting player units and mismatched resources
    4. Spawn non-building units from the captured state
    5. Set resources to match the replay
    6. Restore fog of war
    """
    logger.info(f"Reconstructing game state (from replay loop {state.game_loop})...")

    # 1. Reveal map so all units are visible for killing
    await client._execute(
        debug=sc_pb.RequestDebug(debug=[debug_pb.DebugCommand(game_state=1)])
    )
    await client._execute(step=sc_pb.RequestStep(count=1))

    # Record tags of starting units to kill after spawning replay buildings
    obs = await client._execute(observation=sc_pb.RequestObservation())
    desired_units = state.units
    old_player_tags: list[int] = []
    old_townhall_positions: list[tuple[float, float]] = []
    resource_tags_to_kill: list[int] = []
    for unit in obs.observation.observation.raw_data.units:
        if unit.owner in (1, 2):
            old_player_tags.append(unit.tag)
            if unit.unit_type in _TOWNHALL_TYPE_IDS:
                old_townhall_positions.append((unit.pos.x, unit.pos.y))
        elif not _is_resource_in_desired_state(desired_units, unit):
            resource_tags_to_kill.append(unit.tag)

    logger.info(
        f"  Starting units to replace: {len(old_player_tags)}, "
        f"resources to remove: {len(resource_tags_to_kill)}"
    )

    # 2. Spawn buildings first (must exist before killing old units to prevent defeat)
    #    Townhalls that overlap a starting townhall position are deferred until
    #    after the kill step — spawning them on top of the existing one causes
    #    Terran CCs to fly and Zerg Hatcheries to land off-position.
    buildings_to_spawn = [
        u for u in desired_units
        if _is_player_unit(u) and u.is_building and u.build_progress > 0
    ]

    def _overlaps_old_townhall(unit: CapturedUnit) -> bool:
        if unit.unit_type not in _TOWNHALL_TYPE_IDS:
            return False
        return any(
            abs(unit.pos_x - ox) < 1.0 and abs(unit.pos_y - oy) < 1.0
            for ox, oy in old_townhall_positions
        )

    deferred_townhalls = [u for u in buildings_to_spawn if _overlaps_old_townhall(u)]
    immediate_buildings = [u for u in buildings_to_spawn if not _overlaps_old_townhall(u)]

    # Batch all non-deferred building spawn debug commands, then step once
    if immediate_buildings:
        spawn_cmds = [
            debug_pb.DebugCommand(
                create_unit=debug_pb.DebugCreateUnit(
                    unit_type=unit.unit_type,
                    owner=unit.owner,
                    pos=common_pb.Point2D(x=unit.pos_x, y=unit.pos_y),
                    quantity=1,
                )
            )
            for unit in immediate_buildings
        ]
        await client._execute(
            debug=sc_pb.RequestDebug(debug=spawn_cmds)
        )
        await client._execute(step=sc_pb.RequestStep(count=1))
        logger.info(f"  Spawned {len(immediate_buildings)} buildings")

    # 3. Kill starting player units and mismatched resources
    tags_to_kill = old_player_tags + resource_tags_to_kill
    if tags_to_kill:
        BATCH_SIZE = 64
        for i in range(0, len(tags_to_kill), BATCH_SIZE):
            batch = tags_to_kill[i:i + BATCH_SIZE]
            await client._execute(
                debug=sc_pb.RequestDebug(
                    debug=[debug_pb.DebugCommand(
                        kill_unit=debug_pb.DebugKillUnit(tag=batch)
                    )]
                )
            )
        await client._execute(step=sc_pb.RequestStep(count=1))
        logger.info(f"  Killed {len(tags_to_kill)} old units")

    # 3b. Now spawn the deferred townhalls (old ones are gone, position is clear)
    if deferred_townhalls:
        spawn_cmds = [
            debug_pb.DebugCommand(
                create_unit=debug_pb.DebugCreateUnit(
                    unit_type=unit.unit_type,
                    owner=unit.owner,
                    pos=common_pb.Point2D(x=unit.pos_x, y=unit.pos_y),
                    quantity=1,
                )
            )
            for unit in deferred_townhalls
        ]
        await client._execute(
            debug=sc_pb.RequestDebug(debug=spawn_cmds)
        )
        await client._execute(step=sc_pb.RequestStep(count=1))
        logger.info(f"  Spawned {len(deferred_townhalls)} deferred townhalls")

    # 4. Spawn non-building player units
    units_to_spawn = [
        u for u in desired_units
        if _is_player_unit(u) and not u.is_building
    ]
    if units_to_spawn:
        spawn_cmds = [
            debug_pb.DebugCommand(
                create_unit=debug_pb.DebugCreateUnit(
                    unit_type=unit.unit_type,
                    owner=unit.owner,
                    pos=common_pb.Point2D(x=unit.pos_x, y=unit.pos_y),
                    quantity=1,
                )
            )
            for unit in units_to_spawn
        ]
        await client._execute(
            debug=sc_pb.RequestDebug(debug=spawn_cmds)
        )
        await client._execute(step=sc_pb.RequestStep(count=1))
        logger.info(f"  Spawned {len(units_to_spawn)} units")

    # 5. Log resource state (debug API cannot precisely set minerals/vespene)
    obs = await client._execute(observation=sc_pb.RequestObservation())
    current_minerals = obs.observation.observation.player_common.minerals
    current_vespene = obs.observation.observation.player_common.vespene
    logger.info(
        f"  Resources: current={current_minerals}m/{current_vespene}g, "
        f"replay target={state.player_minerals}m/{state.player_vespene}g"
    )

    # 6. Restore fog of war (toggle show_map off)
    await client._execute(
        debug=sc_pb.RequestDebug(debug=[debug_pb.DebugCommand(game_state=1)])
    )
    await client._execute(step=sc_pb.RequestStep(count=1))

    # Final observation to confirm state
    obs = await client._execute(observation=sc_pb.RequestObservation())
    final_loop = obs.observation.observation.game_loop
    final_unit_count = len(list(obs.observation.observation.raw_data.units))
    logger.info(
        f"State reconstruction complete at loop {final_loop}, "
        f"{final_unit_count} units on map"
    )


def _is_resource_in_desired_state(desired_units: list[CapturedUnit], current_unit) -> bool:
    """Check if a resource (mineral/gas) in the current game matches one in the desired state."""
    if current_unit.mineral_contents > 0:
        return any(
            u.mineral_contents > 0
            and abs(u.pos_x - current_unit.pos.x) < 0.5
            and abs(u.pos_y - current_unit.pos.y) < 0.5
            for u in desired_units
        )
    elif current_unit.vespene_contents > 0:
        # Check if there's an extractor on top of the geyser in the desired state
        has_extractor = any(
            u.vespene_contents > 0
            and _is_player_unit(u) and u.is_building
            and abs(u.pos_x - current_unit.pos.x) < 0.5
            and abs(u.pos_y - current_unit.pos.y) < 0.5
            for u in desired_units
        )
        if has_extractor:
            return False  # Kill the raw geyser; the extractor will be spawned

        return any(
            u.vespene_contents > 0
            and abs(u.pos_x - current_unit.pos.x) < 0.5
            and abs(u.pos_y - current_unit.pos.y) < 0.5
            for u in desired_units
        )
    return True  # Not a resource; keep it


# ---------------------------------------------------------------------------
# Self-contained game loop with replay-state reconstruction hook
# ---------------------------------------------------------------------------

async def _play_game_with_reconstruction(
    client: Client,
    player_id: int,
    ai: BotAI,
    realtime: bool,
    game_time_limit: int | None,
    replay_state: CapturedReplayState,
    bot_player_id: int,
    reconstruct_after: int = 2,
) -> Result:
    """
    Run the bot game loop with replay state reconstruction injected after
    *reconstruct_after* iterations.  This is a self-contained version of the
    standard python-sc2 ``_play_game_ai`` loop so that the upstream library
    does not need any modification.
    """
    gs: GameState | None = None

    # -- Bot initialisation (mirrors _play_game_ai.initialize_first_step) --
    ai._initialize_variables()

    game_data = await client.get_game_data()
    game_info = await client.get_game_info()
    ping_response = await client.ping()

    ai._prepare_start(
        client, player_id, game_info, game_data,
        realtime=realtime, base_build=ping_response.ping.base_build,
    )
    state = await client.observation()
    if client._game_result:
        await ai.on_end(client._game_result[player_id])
        return client._game_result[player_id]
    gs = GameState(state.observation)
    proto_game_info = await client._execute(game_info=sc_pb.RequestGameInfo())
    try:
        ai._prepare_step(gs, proto_game_info)
        await ai.on_before_start()
        ai._prepare_first_step()
        await ai.on_start()
    except Exception as e:
        logger.exception(f"Caught unknown exception in AI on_start: {e}")
        logger.error("Resigning due to previous error")
        await ai.on_end(Result.Defeat)
        return Result.Defeat

    # -- Main game loop --
    reconstructed = False
    for iteration in range(10**10):
        # Inject replay state reconstruction once after a few warm-up frames
        if not reconstructed and iteration == reconstruct_after:
            await _reconstruct_game_state(client, replay_state, bot_player_id)
            reconstructed = True

        state = await client.observation()

        if client._game_result:
            await ai.on_end(client._game_result[player_id])
            return client._game_result[player_id]
        gs = GameState(state.observation)

        if game_time_limit and gs.game_loop / 22.4 > game_time_limit:
            await ai.on_end(Result.Tie)
            return Result.Tie

        proto_game_info = await client._execute(game_info=sc_pb.RequestGameInfo())
        ai._prepare_step(gs, proto_game_info)

        logger.debug(f"Running AI step, it={iteration} {gs.game_loop / 22.4:.2f}s")
        await ai.issue_events()
        try:
            await ai.on_step(iteration)
        except (AttributeError,) as e:
            logger.exception(f"Caught exception: {e}")
            raise
        except Exception as e:
            logger.exception(f"Caught unknown exception: {e}")
            raise
        if not client.in_game:
            return client._game_result.get(player_id, Result.Defeat)
        await ai._after_step()

        if not client.in_game and client._game_result:
            await ai.on_end(client._game_result[player_id])
            return client._game_result[player_id]
        await client.step()
    return Result.Undecided


# ---------------------------------------------------------------------------
# Phase 3: Create game and run
# ---------------------------------------------------------------------------

async def _host_game_from_replay(
    replay_path: str,
    target_game_loop: int,
    players: list[AbstractPlayer],
    bot_player_id: int = 1,
    realtime: bool = False,
    save_replay_as: str | None = None,
    game_time_limit: int | None = None,
) -> tuple[Result, str]:
    """
    Full orchestration: load replay, capture state, create new game, and play.

    :param replay_path: Absolute path to the .SC2Replay file
    :param target_game_loop: Game loop at which bots take over
    :param players: [Bot(...), Computer(...)] — the bot player and opponent
    :param bot_player_id: Which player position in the replay (1 or 2) the bot occupies
    :param realtime: Whether to run the game in realtime
    :param save_replay_as: Path to save the replay of the continued game
    :param game_time_limit: Maximum game time in seconds
    :return: (Result for the bot player, map name from the replay)
    """
    base_build, data_version = get_replay_version(replay_path)

    async with SC2Process(
        fullscreen=False, base_build=base_build, data_hash=data_version
    ) as server:
        # Phase 1: Capture replay state
        state = await _capture_replay_state(
            server, replay_path, target_game_loop, bot_player_id
        )

        # Determine player setup for the new game
        # Override opponent race to match the replay
        opponent_player_id = 2 if bot_player_id == 1 else 1
        replay_opponent_race = state.player_races.get(opponent_player_id)

        # Map the protobuf race value to sc2.data.Race
        race_map = {1: Race.Terran, 2: Race.Zerg, 3: Race.Protoss, 4: Race.Random}
        if replay_opponent_race and isinstance(players[1], Computer):
            mapped_race = race_map.get(replay_opponent_race)
            if mapped_race:
                opponent_computer = players[1]
                players[1] = Computer(
                    mapped_race,
                    opponent_computer.difficulty,
                    ai_build=opponent_computer.ai_build or AIBuild.RandomBuild,
                )
                logger.info(f"Set opponent race to {mapped_race} (from replay)")

        # Phase 2: Create new game on the same map
        logger.info(f"Creating new game on map: {state.map_name}")
        req = sc_pb.RequestCreateGame(
            local_map=sc_pb.LocalMap(map_path=state.local_map_path),
            realtime=realtime,
            disable_fog=False,
        )
        for player in players:
            p = req.player_setup.add()  # type: ignore[attr-defined]
            p.type = player.type.value
            if isinstance(player, Computer):
                p.race = player.race.value
                p.difficulty = player.difficulty.value
                if player.ai_build is not None:
                    p.ai_build = player.ai_build.value

        create_result = await server._execute(create_game=req)
        if create_result.create_game.HasField("error"):
            raise RuntimeError(
                f"Could not create game: {create_result.create_game.error} - "
                f"{create_result.create_game.error_details}"
            )

        # Create client and join game
        client = Client(server._ws, save_replay_as)

        ifopts = sc_pb.InterfaceOptions(
            raw=True, score=True, show_cloaked=True,
            raw_affects_selection=True, raw_crop_to_playable_area=False,
        )
        bot_race = players[0].race if isinstance(players[0], Bot) else Race.Terran
        join_req = sc_pb.RequestJoinGame(
            race=bot_race.value,
            options=ifopts,
        )
        join_result = await client._execute(join_game=join_req)
        if join_result.join_game.HasField("error"):
            raise RuntimeError(
                f"Could not join game: {join_result.join_game.error} - "
                f"{join_result.join_game.error_details}"
            )

        player_id = join_result.join_game.player_id
        client._player_id = player_id
        logger.info(f"Joined game as player {player_id}")

        # Phase 3: Let bot initialize normally, then reconstruct game state
        # after a few frames so on_start / _prepare_first_step see a clean
        # starting game state instead of debug-spawned replay units.

        # Phase 4: Play the game using our self-contained loop that injects
        # the replay state reconstruction after warm-up iterations.
        assert isinstance(players[0], Bot), "First player must be a Bot"
        ai = players[0].ai
        result = await _play_game_with_reconstruction(
            client, player_id, ai, realtime, game_time_limit,
            replay_state=state, bot_player_id=bot_player_id,
            reconstruct_after=2,
        )

        logger.info(f"Game result: {result}")

        # Save replay and clean up
        try:
            if client.save_replay_path is not None:
                await client.save_replay(client.save_replay_path)
            await client.leave()
        except ConnectionAlreadyClosedError:
            logger.error("Connection was closed before the game ended")
        await client.quit()

        return result, state.map_name


def run_game_from_replay(
    replay_path: str | Path,
    target_game_loop: int,
    players: list[AbstractPlayer],
    bot_player_id: int = 1,
    realtime: bool = False,
    save_replay_as: str | None = None,
    game_time_limit: int | None = None,
) -> tuple[Result, str]:
    """
    Continue a game from a replay at a specified game loop.

    This loads the given replay, captures the full game state (all units, buildings,
    and resources) at the target game loop, then creates a new game on the same map
    and uses debug commands to reconstruct that state. The bot then takes over and
    plays normally from that point.

    :param replay_path: Absolute path to the .SC2Replay file (must exist)
    :param target_game_loop: Game loop at which the bot takes over.
        Convert from game time: game_loop = seconds * 22.4
    :param players: List of [Bot(...), Computer(...)] — bot must be first
    :param bot_player_id: Which player in the replay the bot represents (1 or 2)
    :param realtime: Whether to run in realtime mode
    :param save_replay_as: Path to save the replay of the continued game
    :param game_time_limit: Maximum game time in seconds (from the start, not from takeover)
    :return: (Result for the bot player, map name from the replay)

    Example::

        from sc2.data import Difficulty, Race
        from sc2.player import Bot, Computer
        from replay_continuation import run_game_from_replay

        result, map_name = run_game_from_replay(
            replay_path="/path/to/replay.SC2Replay",
            target_game_loop=5000,  # ~3:43 in game time
            players=[
                Bot(Race.Terran, MyBot(), "MyBot"),
                Computer(Race.Protoss, Difficulty.CheatInsane),
            ],
            bot_player_id=1,
            save_replay_as="/path/to/output.SC2Replay",
        )
    """
    replay_path = str(replay_path)
    assert Path(replay_path).is_file(), (
        f"Replay does not exist at the given path: {replay_path}"
    )

    result, map_name = asyncio.run(
        _host_game_from_replay(
            replay_path=replay_path,
            target_game_loop=target_game_loop,
            players=players,
            bot_player_id=bot_player_id,
            realtime=realtime,
            save_replay_as=save_replay_as,
            game_time_limit=game_time_limit,
        )
    )
    assert isinstance(result, Result), f"Unexpected result type: {type(result)}"
    return result, map_name

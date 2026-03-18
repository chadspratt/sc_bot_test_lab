"""
Package BotTato for the aiarena local-play-bootstrap infrastructure.

This script creates lightweight overlay directories under
test_lab/aiarena/bots/ containing only the aiarena-specific files that
differ from the live bot source tree:

    - run.py   — custom entry point that compiles Cython from .c files
    - requirements.txt — Python deps the bot controller installs
    - ladderbots.json  — copied from bot/ (with key renamed for the mirror)

The actual bot code is bind-mounted from ``bot/`` and ``python_sc2/sc2/``
at container start time via docker-compose.override.yml, so you never need
to re-run this script after changing bot code.

Usage (from repo root):
    python web/DjangoLocalApps/test_lab/prepare_bottato.py

Re-run only when:
    - The Cython compilation logic in AIARENA_RUN_PY changes
    - requirements.txt dependencies change
    - ladderbots.json metadata changes
"""

from __future__ import annotations

import json
import os
import sys

# Resolve paths relative to the repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..', '..'))
BOT_SRC = os.path.join(REPO_ROOT, 'bot')
AIARENA_BOTS_DIR = os.path.join(SCRIPT_DIR, 'aiarena', 'bots')
DEST = os.path.join(AIARENA_BOTS_DIR, 'BotTato')
MIRROR_NAME = 'BotTato_p2'
MIRROR_DEST = os.path.join(AIARENA_BOTS_DIR, MIRROR_NAME)

# Python dependencies the aiarena bot controller installs before running.
REQUIREMENTS = """\
loguru
numpy
scipy
cython
setuptools
pymysql
sharepy>=2.0.0
"""

# Custom run.py for the aiarena package.
# Compiles Cython extensions from pre-generated .c files on first run
# (inside the Linux container), then starts the bot normally.
AIARENA_RUN_PY = '''\
#!/usr/bin/env python3
"""Aiarena entry point for BotTato.

Compiles Cython .so extensions from pre-generated .c files on first run,
then starts the bot. The custom bot_controller1 Docker image
(Dockerfile.bottato) has gcc, numpy, and other build dependencies.

We compile from .c files (not .pyx) to avoid Cython cimport resolution
issues when building standalone modules outside the full package context.
"""
import os
import subprocess
import sys


# Modules and whether they need numpy include dirs
MODULES = [
    ("bootstrap", False),
    ("ability_mapping", False),
    ("geometry", False),
    ("general_utils", True),
    ("turn_rate", False),
    ("unit_data", False),
    ("ability_order_tracker", False),
    ("numpy_helper", True),
    ("combat_utils", True),
    ("units_utils", True),
    ("dijkstra", True),
    ("map_analysis", True),
    ("placement_solver", True),
]


def compile_cython():
    """Compile .c -> .so for all Cython extension modules."""
    cython_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cython_extensions")
    python_ver = f"{sys.version_info.major}{sys.version_info.minor}"
    so_count = sum(
        1 for f in os.listdir(cython_dir)
        if f.endswith(".so") and f"cpython-{python_ver}" in f
    )
    if so_count >= len(MODULES):
        print(f"Cython extensions already built ({so_count} .so files found)")
        return

    print(f"Compiling {len(MODULES)} C extensions for Python {python_ver}...")

    # Get Python build flags
    import sysconfig
    import numpy as np

    py_include = sysconfig.get_path("include")
    np_include = np.get_include()
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")  # e.g. .cpython-312-x86_64-linux-gnu.so

    for mod_name, needs_numpy in MODULES:
        c_file = os.path.join(cython_dir, f"{mod_name}.c")
        if not os.path.exists(c_file):
            print(f"  Skipping {mod_name} (.c file not found)")
            continue

        out_file = os.path.join(cython_dir, f"{mod_name}{ext_suffix}")
        if os.path.exists(out_file):
            continue

        cmd = [
            "gcc", "-shared", "-fPIC", "-O2",
            "-I", py_include,
        ]
        if needs_numpy:
            cmd += ["-I", np_include]
        cmd += [c_file, "-o", out_file]

        print(f"  {mod_name}.c -> {mod_name}{ext_suffix}")
        subprocess.check_call(cmd)

    print("Compilation complete.")


if __name__ == "__main__":
    compile_cython()

    # Now import and run the bot (imports must be after compilation)
    from __init__ import run_ladder_game  # type: ignore

    from bot import BotTato  # type: ignore
    from sc2.data import Race
    from sc2.player import Bot

    bot = Bot(Race.Terran, BotTato())

    if "--LadderServer" in sys.argv:
        print("Starting ladder game...")
        result, opponentid = run_ladder_game(bot)
        print(result, " against opponent ", opponentid)
    else:
        from sc2 import maps
        from sc2.data import Difficulty
        from sc2.main import run_game
        from sc2.player import Computer

        print("Starting local game...")
        run_game(
            maps.get("Abyssal Reef LE"),
            [bot, Computer(Race.Protoss, Difficulty.VeryHard)],
            realtime=True,
        )
'''


def _write_overlay(dest: str, bot_key: str) -> None:
    """Create (or refresh) an overlay directory with aiarena-specific files.

    *bot_key* is the ladderbots.json ``Bots`` key — normally ``BotTato``,
    but ``BotTato_p2`` for the self-play mirror.
    """
    os.makedirs(dest, exist_ok=True)

    # run.py — custom entry point
    run_py_path = os.path.join(dest, 'run.py')
    print(f"  Writing {run_py_path}")
    with open(run_py_path, 'w', newline='\n') as f:
        f.write(AIARENA_RUN_PY)

    # requirements.txt
    req_path = os.path.join(dest, 'requirements.txt')
    print(f"  Writing {req_path}")
    with open(req_path, 'w') as f:
        f.write(REQUIREMENTS)

    # ladderbots.json — read from bot/ source tree, patch key if needed
    src_lb = os.path.join(BOT_SRC, 'ladderbots.json')
    if not os.path.isfile(src_lb):
        print(f"  WARNING: {src_lb} not found — skipping ladderbots.json")
        return

    with open(src_lb) as f:
        lb_data = json.load(f)

    # Rename the key if this is a mirror copy
    if bot_key != 'BotTato' and 'Bots' in lb_data and 'BotTato' in lb_data['Bots']:
        lb_data['Bots'][bot_key] = lb_data['Bots'].pop('BotTato')

    lb_path = os.path.join(dest, 'ladderbots.json')
    print(f"  Writing {lb_path} (key={bot_key})")
    with open(lb_path, 'w') as f:
        json.dump(lb_data, f, indent=4)


def main() -> None:
    if not os.path.isdir(BOT_SRC):
        print(f"ERROR: Bot source not found at {BOT_SRC}")
        sys.exit(1)

    print("Creating BotTato overlay (aiarena-specific files only)...")
    _write_overlay(DEST, 'BotTato')

    print(f"\nCreating {MIRROR_NAME} overlay for self-play...")
    _write_overlay(MIRROR_DEST, MIRROR_NAME)

    print(f"\nDone.  Bot code is mounted live from bot/ and python_sc2/sc2/")
    print("via docker-compose.override.yml — no need to re-run this script")
    print("after changing bot code.")


if __name__ == '__main__':
    main()

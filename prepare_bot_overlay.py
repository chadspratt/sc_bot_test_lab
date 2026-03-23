"""
Prepare aiarena overlay directories for any registered bot.

This script creates lightweight overlay directories under
test_lab/aiarena/bots/ containing only the aiarena-specific files that
differ from the live bot source tree:

    - run.py          — custom entry point (copied from bot source, or
                        a default that just runs ``__init__.run_ladder_game``)
    - requirements.txt — Python deps the bot controller installs
    - ladderbots.json  — copied from the bot source (with key renamed
                         for the mirror)

The actual bot code is bind-mounted from the bot's ``source_path``
at container start time via docker-compose.override.yml, so you never
need to re-run this script after changing bot code.

Usage (from repo root):
    # Prepare a specific bot by name (reads from CustomBot DB record):
    python web/DjangoLocalApps/test_lab/prepare_bot_overlay.py --bot-name BotTato

    # Prepare using explicit paths (no DB required):
    python web/DjangoLocalApps/test_lab/prepare_bot_overlay.py \\
        --bot-name MyBot --source-path /path/to/bot \\
        --run-py /path/to/bot/run.py \\
        --requirements /path/to/bot/requirements.txt

Re-run only when:
    - The run.py entry point or compilation logic changes
    - requirements.txt dependencies change
    - ladderbots.json metadata changes
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

# Resolve paths relative to the repo root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AIARENA_BOTS_DIR = os.path.join(SCRIPT_DIR, 'aiarena', 'bots')

# Minimal default run.py for bots that don't provide their own.
# Just runs the aiarena ladder game entry point.
_DEFAULT_RUN_PY = '''\
#!/usr/bin/env python3
"""Default aiarena entry point.

Starts the bot via __init__.run_ladder_game. If your bot needs
custom setup (e.g. Cython compilation), provide your own run.py
in the bot source directory.
"""
import sys

from __init__ import run_ladder_game  # type: ignore

if __name__ == "__main__":
    if "--LadderServer" in sys.argv:
        result, opponentid = run_ladder_game()
        print(result, " against opponent ", opponentid)
'''


def _find_original_bot_key(lb_data: dict) -> str | None:
    """Return the first bot key in a ladderbots.json ``Bots`` dict."""
    bots = lb_data.get('Bots', {})
    if bots:
        return next(iter(bots))
    return None


def write_overlay(
    dest: str,
    bot_key: str,
    *,
    source_path: str | None = None,
    run_py_path: str | None = None,
    requirements_path: str | None = None,
) -> None:
    """Create (or refresh) an overlay directory with aiarena-specific files.

    *bot_key* is the ladderbots.json ``Bots`` key — the bot's primary
    name, or ``<BotName>_p2`` for the self-play mirror.

    *source_path* is the bot's live source directory on the host. Used
    to find ``ladderbots.json`` and (optionally) ``run.py`` /
    ``requirements.txt`` if explicit paths aren't given.

    *run_py_path* overrides the run.py to copy into the overlay. If
    ``None``, looks for ``run.py`` in *source_path*. If that also
    doesn't exist, writes a minimal default.

    *requirements_path* overrides the requirements.txt source. If
    ``None``, looks for ``requirements.txt`` in *source_path*.
    """
    os.makedirs(dest, exist_ok=True)

    # --- run.py ---
    dest_run_py = os.path.join(dest, 'run.py')
    if run_py_path and os.path.isfile(run_py_path):
        print(f"  Copying {run_py_path} -> {dest_run_py}")
        shutil.copy2(run_py_path, dest_run_py)
    elif source_path and os.path.isfile(os.path.join(source_path, 'run.py')):
        src_run = os.path.join(source_path, 'run.py')
        print(f"  Copying {src_run} -> {dest_run_py}")
        shutil.copy2(src_run, dest_run_py)
    else:
        print(f"  Writing default {dest_run_py}")
        with open(dest_run_py, 'w', newline='\n') as f:
            f.write(_DEFAULT_RUN_PY)

    # --- requirements.txt ---
    dest_req = os.path.join(dest, 'requirements.txt')
    if requirements_path and os.path.isfile(requirements_path):
        print(f"  Copying {requirements_path} -> {dest_req}")
        shutil.copy2(requirements_path, dest_req)
    elif source_path and os.path.isfile(os.path.join(source_path, 'requirements.txt')):
        src_req = os.path.join(source_path, 'requirements.txt')
        print(f"  Copying {src_req} -> {dest_req}")
        shutil.copy2(src_req, dest_req)
    else:
        print(f"  No requirements.txt found — skipping")

    # --- ladderbots.json ---
    src_lb = None
    if source_path:
        candidate = os.path.join(source_path, 'ladderbots.json')
        if os.path.isfile(candidate):
            src_lb = candidate

    if not src_lb:
        print(f"  No ladderbots.json found — skipping")
        return

    with open(src_lb) as f:
        lb_data = json.load(f)

    original_key = _find_original_bot_key(lb_data)

    # Rename the key if this overlay uses a different name (e.g. mirror)
    if original_key and original_key != bot_key and 'Bots' in lb_data:
        lb_data['Bots'][bot_key] = lb_data['Bots'].pop(original_key)

    lb_dest = os.path.join(dest, 'ladderbots.json')
    print(f"  Writing {lb_dest} (key={bot_key})")
    with open(lb_dest, 'w') as f:
        json.dump(lb_data, f, indent=4)


def prepare_bot(
    bot_name: str,
    source_path: str,
    *,
    run_py_path: str | None = None,
    requirements_path: str | None = None,
    create_mirror: bool = True,
) -> None:
    """Prepare overlay directories for a bot and optionally its mirror.

    This is the main entry point for programmatic use.
    """
    dest = os.path.join(AIARENA_BOTS_DIR, bot_name)
    print(f"Creating {bot_name} overlay (aiarena-specific files only)...")
    write_overlay(
        dest, bot_name,
        source_path=source_path,
        run_py_path=run_py_path,
        requirements_path=requirements_path,
    )

    if create_mirror:
        mirror_name = f'{bot_name}_p2'
        mirror_dest = os.path.join(AIARENA_BOTS_DIR, mirror_name)
        print(f"\nCreating {mirror_name} overlay for self-play...")
        write_overlay(
            mirror_dest, mirror_name,
            source_path=source_path,
            run_py_path=run_py_path,
            requirements_path=requirements_path,
        )

    print(f"\nDone.  Bot code is mounted live from {source_path}")
    print("via docker-compose.override.yml — no need to re-run this script")
    print("after changing bot code.")


def _load_from_db(bot_name: str) -> dict:
    """Load bot config from the Django CustomBot model.

    Returns a dict with keys: source_path, bot_directory.
    Sets up Django if not already configured.
    """
    # Ensure Django settings are configured
    sys.path.insert(0, os.path.join(SCRIPT_DIR, '..'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'DjangoLocalApps.settings')

    import django
    django.setup()

    from test_lab.models import CustomBot
    bot = CustomBot.objects.using('sc2bot_test_lab_db_2').get(name=bot_name)
    return {
        'source_path': bot.source_path,
        'bot_directory': bot.bot_directory or bot.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Prepare aiarena overlay directories for a bot.',
    )
    parser.add_argument(
        '--bot-name', required=True,
        help='Bot name (used as overlay directory name and ladderbots.json key)',
    )
    parser.add_argument(
        '--source-path',
        help='Absolute path to the bot source directory. If omitted, read from CustomBot DB.',
    )
    parser.add_argument(
        '--run-py',
        help='Path to a custom run.py entry point. If omitted, copies from source or uses default.',
    )
    parser.add_argument(
        '--requirements',
        help='Path to requirements.txt. If omitted, copies from source.',
    )
    parser.add_argument(
        '--no-mirror', action='store_true',
        help='Skip creating the mirror overlay for self-play.',
    )
    args = parser.parse_args()

    source_path = args.source_path
    if not source_path:
        print(f"Loading {args.bot_name} config from database...")
        bot_config = _load_from_db(args.bot_name)
        source_path = bot_config['source_path']

    if not source_path or not os.path.isdir(source_path):
        print(f"ERROR: Bot source not found at {source_path!r}")
        sys.exit(1)

    prepare_bot(
        args.bot_name,
        source_path,
        run_py_path=args.run_py,
        requirements_path=args.requirements,
        create_mirror=not args.no_mirror,
    )


if __name__ == '__main__':
    main()

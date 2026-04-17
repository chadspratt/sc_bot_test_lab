#!/bin/bash
# Generic bot runner for vs-computer matches inside Docker.
# Reads the bot type from ladderbots.json and dispatches accordingly.
#
# Environment:
#   BOT_DIR  – path to the mounted bot directory (default: /root/bot_dir)

set -e

BOT_DIR="${BOT_DIR:-/root/bot_dir}"
cd "$BOT_DIR"

# ---------------------------------------------------------------------------
# Detect bot type from ladderbots.json (case-insensitive filename)
# ---------------------------------------------------------------------------
# BOT_TYPE may already be set via env var from the database.
# Only fall back to ladderbots.json detection if not set.
if [ -z "$BOT_TYPE" ]; then
    BOT_TYPE="python"
    LADDERBOTS_FILE=""
    for candidate in ladderbots.json LadderBots.json; do
        if [ -f "$candidate" ]; then
            LADDERBOTS_FILE="$candidate"
            break
        fi
    done

    if [ -n "$LADDERBOTS_FILE" ]; then
        BOT_TYPE=$(python3 -c "
import json, sys
with open('$LADDERBOTS_FILE') as f:
    data = json.load(f)
bots = data.get('Bots', {})
if bots:
    print(next(iter(bots.values())).get('Type', 'Python').lower())
else:
    print('python')
" 2>/dev/null || echo "python")
    fi
fi

echo "Bot type: $BOT_TYPE"
echo "Bot dir:  $BOT_DIR"

# ---------------------------------------------------------------------------
# Dispatch by bot type
# ---------------------------------------------------------------------------
case "$BOT_TYPE" in
    python)
        # Install core sc2 library dependencies needed by the runner
        echo "Installing sc2 runner dependencies..."
        uv pip install --system -r /root/runner/sc2_deps.txt

        # Install bot-specific requirements if present
        if [ -f "requirements.txt" ]; then
            echo "Installing bot requirements..."
            uv pip install --system -r requirements.txt
        fi

        # Build Cython extensions if the bot ships a setup.py (e.g. BotTato)
        # Build in a container-local temp directory to avoid races — the bot
        # source dir is a bind-mount shared by all concurrent containers.
        CYTHON_BUILD=""
        if [ -d "cython_extensions" ] && [ -f "cython_extensions/setup.py" ]; then
            PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
            echo "Building Cython extensions for Python ${PYTHON_VERSION}..."
            uv pip install --system cython numpy setuptools
            CYTHON_BUILD="/tmp/cython_build"
            mkdir -p "$CYTHON_BUILD"
            cp -a cython_extensions "$CYTHON_BUILD/"
            python3 "$CYTHON_BUILD/cython_extensions/setup.py" build_ext --inplace
        fi

        # Auto-discover common framework paths
        EXTRA_PATHS=""
        if [ -d "$BOT_DIR/ares-sc2/src" ]; then
            EXTRA_PATHS="$BOT_DIR/ares-sc2/src/ares:$BOT_DIR/ares-sc2/src:$BOT_DIR/ares-sc2"
        fi

        export PYTHONPATH="${CYTHON_BUILD:+$CYTHON_BUILD:}${BOT_DIR}${EXTRA_PATHS:+:$EXTRA_PATHS}:/root/runner${PYTHONPATH:+:$PYTHONPATH}"
        echo "PYTHONPATH=$PYTHONPATH"

        # If the bot ships a bot_loader.py or has BOT_MODULE/BOT_CLASS set,
        # use the standard runner (which handles both paths internally).
        # bot_loader.py avoids module-name collisions between the runner's
        # config.py and the bot's own modules (e.g. sharpy bots).
        # Otherwise fall back to the external runner which launches the
        # bot's own run.py via --LadderServer args.
        if [ -f "$BOT_DIR/bot_loader.py" ] || ([ -n "$BOT_MODULE" ] && [ -n "$BOT_CLASS" ]); then
            exec python3 /root/runner/run_vs_computer.py "$@"
        else
            exec python3 /root/runner/run_vs_computer_external.py python
        fi
        ;;

    dotnetcore|cpplinux|cppwin32|java|nodejs)
        # External (non-Python) bots: launch SC2 + create game via Python,
        # then spawn the bot process which connects via WebSocket.
        echo "Installing sc2 runner dependencies..."
        uv pip install --system -r /root/runner/sc2_deps.txt

        # Install bot-specific npm packages if present
        if [ "$BOT_TYPE" = "nodejs" ] && [ -f "package.json" ]; then
            echo "Installing npm dependencies..."
            npm install --omit=dev 2>&1
        fi

        export PYTHONPATH="/root/runner${PYTHONPATH:+:$PYTHONPATH}"
        echo "PYTHONPATH=$PYTHONPATH"
        exec python3 /root/runner/run_vs_computer_external.py "$BOT_TYPE"
        ;;

    *)
        echo "ERROR: Unknown bot type '$BOT_TYPE'. Cannot run vs-computer match."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;
esac

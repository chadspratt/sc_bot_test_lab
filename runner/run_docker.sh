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
# Detect bot type from ladderbots.json
# ---------------------------------------------------------------------------
BOT_TYPE="python"
if [ -f "ladderbots.json" ]; then
    BOT_TYPE=$(python3 -c "
import json, sys
with open('ladderbots.json') as f:
    data = json.load(f)
bots = data.get('Bots', {})
if bots:
    print(next(iter(bots.values())).get('Type', 'Python').lower())
else:
    print('python')
" 2>/dev/null || echo "python")
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
        if [ -d "cython_extensions" ] && [ -f "cython_extensions/setup.py" ]; then
            PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
            SO_FILES=$(ls cython_extensions/*.cpython-${PYTHON_VERSION}*.so 2>/dev/null | wc -l)
            if [ "$SO_FILES" -lt 13 ]; then
                echo "Building Cython extensions for Python ${PYTHON_VERSION}..."
                uv pip install --system cython numpy setuptools
                (cd cython_extensions && python3 setup.py build_ext --inplace)
            else
                echo "Cython extensions already built for Python ${PYTHON_VERSION}"
            fi
        fi

        # Auto-discover common framework paths
        EXTRA_PATHS=""
        if [ -d "$BOT_DIR/ares-sc2/src" ]; then
            EXTRA_PATHS="$BOT_DIR/ares-sc2/src/ares:$BOT_DIR/ares-sc2/src:$BOT_DIR/ares-sc2"
        fi

        export PYTHONPATH="${BOT_DIR}${EXTRA_PATHS:+:$EXTRA_PATHS}:/root/runner${PYTHONPATH:+:$PYTHONPATH}"
        echo "PYTHONPATH=$PYTHONPATH"
        exec python3 /root/runner/run_vs_computer.py "$@"
        ;;

    dotnetcore)
        echo "ERROR: dotnetcore bots are not yet supported for vs-computer matches."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;

    cpplinux|cppwin32)
        echo "ERROR: C++ bots are not yet supported for vs-computer matches."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;

    java)
        echo "ERROR: Java bots are not yet supported for vs-computer matches."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;

    nodejs)
        echo "ERROR: Node.js bots are not yet supported for vs-computer matches."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;

    *)
        echo "ERROR: Unknown bot type '$BOT_TYPE'. Cannot run vs-computer match."
        echo "MATCH_RESULT:Crash"
        exit 1
        ;;
esac

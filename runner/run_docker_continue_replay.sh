#!/bin/bash
# Generic replay-continuation runner inside Docker.
# Sets up the bot environment (same as run_docker.sh) then runs the
# continue-from-replay script instead of the vs-computer script.
#
# Environment:
#   BOT_DIR  – path to the mounted bot directory (default: /root/bot_dir)

set -e

BOT_DIR="${BOT_DIR:-/root/bot_dir}"
cd "$BOT_DIR"

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
exec python3 /root/runner/run_from_replay.py "$@"

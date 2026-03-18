#!/bin/bash
# Build Cython extensions for Linux if needed, then run bot vs bot match.
# This script runs inside the Docker container.

cd /root/bot/cython_extensions

# Check if we need to build (look for any .so files matching current Python version)
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
SO_FILES=$(ls *.cpython-${PYTHON_VERSION}*.so 2>/dev/null | wc -l)

if [ "$SO_FILES" -lt 13 ]; then
    echo "Building Cython extensions for Python ${PYTHON_VERSION}..."
    uv pip install cython numpy setuptools
    uv run python setup.py build_ext --inplace
else
    echo "Cython extensions already built for Python ${PYTHON_VERSION}"
fi

# If this is an external bot match, install its dependencies
if [ -n "$EXTERNAL_BOT_DIR" ]; then
    EXT_BOT_PATH="/root/external_bots/$EXTERNAL_BOT_DIR"
    if [ -d "$EXT_BOT_PATH" ]; then
        echo "Installing dependencies for external bot: $EXTERNAL_BOT_DIR"
        if [ -f "$EXT_BOT_PATH/requirements.txt" ]; then
            uv pip install -r "$EXT_BOT_PATH/requirements.txt"
        fi
        # Install ares-sc2 if bundled
        if [ -d "$EXT_BOT_PATH/ares-sc2" ]; then
            uv pip install "$EXT_BOT_PATH/ares-sc2"
        fi
    else
        echo "WARNING: External bot directory not found: $EXT_BOT_PATH"
    fi
fi

cd /root/bot
export PYTHONPATH="/root/bot:/root/runner:${PYTHONPATH}"
exec uv run /root/runner/run_vs_bot.py "$@"

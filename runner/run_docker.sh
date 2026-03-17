#!/bin/bash
# Build Cython extensions for Linux if needed, then run bot vs computer match.
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

cd /root/bot
export PYTHONPATH="/root/bot:/root/runner:${PYTHONPATH}"
exec uv run /root/runner/run_vs_computer.py "$@"

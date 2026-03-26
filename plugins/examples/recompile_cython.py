"""Plugin: Recompile Cython Extensions.

Runs ``python setup.py build_ext --inplace`` in the bot's cython_extensions
directory to rebuild all .pyx modules.
"""

import os
import subprocess

name = 'Recompile Cython Extensions'
description = 'Runs python setup.py build_ext --inplace in the cython_extensions directory to rebuild all .pyx modules.'


def execute(request) -> str:
    cython_dir = os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', '..', '..', '..', 'bot', 'cython_extensions'
    ))
    setup_py = os.path.join(cython_dir, 'setup.py')

    if not os.path.exists(setup_py):
        raise FileNotFoundError(f'setup.py not found at: {setup_py}')

    result = subprocess.run(
        ['python', 'setup.py', 'build_ext', '--inplace'],
        cwd=cython_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f'Cython compilation failed:\n{result.stderr}')

    return 'Cython extensions recompiled successfully.'

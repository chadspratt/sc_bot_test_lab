#!/usr/bin/env python3
"""
test_lab quickstart — automated setup script.

Performs the full setup in one shot:
  1. Verifies prerequisites (Python 3.12+, Docker running)
  2. Starts a MySQL 8.0 container via Docker Compose
  3. Creates a Python virtual environment and installs dependencies
  4. Runs Django database migrations
  5. Starts the development server and opens the browser

Prerequisites:
  - Python 3.12+
  - Docker (with the daemon running)

Run from the directory that *contains* test_lab/:

    python test_lab/quickstart/setup.py

Or from inside test_lab/:

    python quickstart/setup.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import venv
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Terminal colours (best-effort; harmless no-ops on terminals that ignore ANSI)
# ---------------------------------------------------------------------------
if sys.platform == 'win32':
    os.system('')  # enable ANSI escape processing on Windows 10+

GREEN = '\033[92m'
YELLOW = '\033[93m'
RED = '\033[91m'
BOLD = '\033[1m'
RESET = '\033[0m'

SERVER_URL = 'http://localhost:8000/test_lab/'


def info(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")
    sys.exit(1)


def heading(step: int, msg: str) -> None:
    print(f"\n{BOLD}[{step}/5]{RESET} {msg}")


def run_cmd(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, printing the command line for visibility."""
    display = ' '.join(str(c) for c in cmd)
    print(f"       $ {display}")
    kwargs: dict = dict(cwd=cwd)
    if capture:
        kwargs['capture_output'] = True
        kwargs['text'] = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        fail(f"Command failed (exit {result.returncode}).")
    return result


def venv_python(venv_dir: Path) -> Path:
    """Return the path to the Python interpreter inside a venv."""
    if sys.platform == 'win32':
        return venv_dir / 'Scripts' / 'python.exe'
    return venv_dir / 'bin' / 'python'


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def check_prerequisites() -> None:
    heading(1, "Checking prerequisites")

    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 12):
        fail(f"Python 3.12+ required (found {major}.{minor}).")
    info(f"Python {major}.{minor}")

    if not shutil.which('docker'):
        fail(
            "Docker not found on PATH.\n"
            "         Install Docker Desktop: https://docs.docker.com/get-docker/"
        )

    result = subprocess.run(
        ['docker', 'info'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "Docker daemon is not running.\n"
            "         Start Docker Desktop and try again."
        )
    info("Docker is running")


def start_mysql(compose_file: Path, workspace: Path) -> None:
    heading(2, "Starting MySQL via Docker Compose")

    run_cmd(
        ['docker', 'compose', '-f', str(compose_file), 'up', '-d'],
        cwd=workspace,
    )

    print("       Waiting for MySQL to accept connections …")
    for attempt in range(30):
        result = subprocess.run(
            [
                'docker', 'compose', '-f', str(compose_file),
                'exec', '-T', 'db',
                'mysqladmin', 'ping', '-h', 'localhost', '--silent',
            ],
            capture_output=True,
            cwd=workspace,
        )
        if result.returncode == 0:
            info("MySQL is ready")
            return
        time.sleep(2)

    fail(
        "MySQL did not become ready within 60 seconds.\n"
        "         Check `docker compose -f test_lab/quickstart/docker-compose.yml logs` for details."
    )


def create_venv_and_install(
    venv_dir: Path,
    requirements: Path,
    workspace: Path,
) -> Path:
    heading(3, "Python virtual environment & dependencies")

    python = venv_python(venv_dir)

    if not venv_dir.exists():
        print(f"       Creating venv at {venv_dir.relative_to(workspace)} …")
        try:
            venv.create(str(venv_dir), with_pip=True)
        except Exception as exc:
            fail(
                f"Failed to create virtual environment: {exc}\n"
                "         On Debian/Ubuntu you may need: sudo apt install python3-venv"
            )
    else:
        info(f"venv already exists at {venv_dir.relative_to(workspace)}")

    if not python.exists():
        fail(f"venv Python not found at {python}")

    run_cmd(
        [str(python), '-m', 'pip', 'install', '--upgrade', 'pip'],
        cwd=workspace,
    )

    run_cmd(
        [str(python), '-m', 'pip', 'install', '-r', str(requirements)],
        cwd=workspace,
    )
    info("Dependencies installed")
    return python


def run_migrations(python: Path, manage: Path, workspace: Path) -> None:
    heading(4, "Running database migrations")

    run_cmd(
        [str(python), str(manage), 'migrate', '--database', 'default'],
        cwd=workspace,
    )
    run_cmd(
        [
            str(python), str(manage),
            'migrate', 'test_lab', '--database', 'sc_bot_test_lab',
        ],
        cwd=workspace,
    )
    info("Migrations complete")


def start_server(python: Path, manage: Path, workspace: Path) -> None:
    heading(5, "Starting development server")

    def _open_browser() -> None:
        time.sleep(2)
        webbrowser.open(SERVER_URL)

    threading.Thread(target=_open_browser, daemon=True).start()

    info(f"Opening {SERVER_URL}")
    print(f"\n       Press {BOLD}Ctrl+C{RESET} to stop the server.\n")

    try:
        run_cmd(
            [str(python), str(manage), 'runserver'],
            cwd=workspace,
            check=False,
        )
    except KeyboardInterrupt:
        print(f"\n{GREEN}Server stopped.{RESET}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{BOLD}test_lab quickstart{RESET}\n{'─' * 40}")

    # Resolve key paths from the script's own location.
    script_dir = Path(__file__).resolve().parent        # quickstart/
    test_lab_dir = script_dir.parent                    # test_lab/
    workspace = test_lab_dir.parent                     # parent of test_lab/

    compose_file = script_dir / 'docker-compose.yml'
    manage_py = script_dir / 'manage.py'
    requirements = test_lab_dir / 'requirements.txt'
    venv_dir = workspace / 'venv'

    # Sanity-check that the directory layout looks right.
    for path, label in [
        (compose_file, 'quickstart/docker-compose.yml'),
        (manage_py, 'quickstart/manage.py'),
        (requirements, 'requirements.txt'),
        (test_lab_dir / 'models.py', 'models.py'),
    ]:
        if not path.exists():
            fail(f"Expected file not found: {path}\n         Is test_lab/ in the right place?")

    check_prerequisites()
    start_mysql(compose_file, workspace)
    python = create_venv_and_install(venv_dir, requirements, workspace)
    run_migrations(python, manage_py, workspace)
    start_server(python, manage_py, workspace)


if __name__ == '__main__':
    main()

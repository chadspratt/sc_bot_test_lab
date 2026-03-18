"""
Manage cached copies of past BotTato versions from git history.

Extracts bot source code from previous commits in the ``bot/`` git repo
into a local cache directory.  Each version is identified by its full
commit hash and stored under ``aiarena/bot_versions/<hash>/``.

Cached versions are used for "current vs past" regression testing.  All
versions use the **current** ``python_sc2/sc2`` (mounted at runtime via
Docker Compose), so only the bot's own code varies between versions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..', '..'))
BOT_REPO_DIR = os.path.join(REPO_ROOT, 'bot')
VERSION_CACHE_DIR = os.path.join(SCRIPT_DIR, 'aiarena', 'bot_versions')

# Files/directories to extract from the bot repo at each commit.
# These are the paths passed to ``git archive``.  Ordered by priority;
# paths that don't exist at a given commit are silently skipped.
BOT_ARCHIVE_PATHS_REQUIRED = [
    'bottato/',
    'cython_extensions/',
    'bot.py',
    '__init__.py',
]

BOT_ARCHIVE_PATHS_OPTIONAL = [
    's2clientprotocol/',
    'data/',
    'ladderbots.json',
    'pyproject.toml',
]


@dataclass
class BotCommit:
    """A single commit from the bot repo's history."""
    hash: str           # full 40-char SHA
    short_hash: str     # first 7 chars
    subject: str        # commit message first line
    date: str           # author date (ISO-ish format)
    is_cached: bool     # whether a cached copy already exists


def get_recent_bot_commits(count: int = 5) -> list[BotCommit]:
    """Return the most recent *count* commits from the bot repo.

    Skips the current HEAD commit (index 0) since there's no point
    running the current version against itself — that's what mirror
    matches are for.  Returns commits starting from HEAD~1.

    Each entry includes a flag indicating whether the version is
    already cached locally.
    """
    try:
        result = subprocess.run(
            [
                'git', 'log',
                '--format=%H|%h|%s|%ai',
                f'-{count + 1}',   # +1 because we skip HEAD
            ],
            cwd=BOT_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits = []
    lines = result.stdout.strip().splitlines()

    # Skip the first line (HEAD — current version)
    for line in lines[1:]:
        parts = line.split('|', 3)
        if len(parts) != 4:
            continue
        full_hash, short_hash, subject, date = parts
        commits.append(BotCommit(
            hash=full_hash,
            short_hash=short_hash,
            subject=subject,
            date=date.strip(),
            is_cached=is_version_cached(full_hash),
        ))

    return commits


def is_version_cached(commit_hash: str) -> bool:
    """Check whether a given commit has been extracted into the cache."""
    cache_path = os.path.join(VERSION_CACHE_DIR, commit_hash)
    # A valid cache has at least bot.py and __init__.py
    return (
        os.path.isdir(cache_path)
        and os.path.isfile(os.path.join(cache_path, 'bot.py'))
    )


def get_version_cache_path(commit_hash: str) -> str:
    """Return the cache directory path for a commit (may not exist yet)."""
    return os.path.join(VERSION_CACHE_DIR, commit_hash)


def get_or_create_version_cache(commit_hash: str) -> str:
    """Extract bot source from a past commit into the cache.

    Uses ``git archive`` to create a zip of the relevant files at the
    specified commit, then extracts them into the cache directory.

    Returns the absolute path to the cache directory.

    Raises ``ValueError`` if the commit hash is invalid or extraction fails.
    """
    cache_path = get_version_cache_path(commit_hash)

    if is_version_cached(commit_hash):
        return cache_path

    # Validate the commit exists in the bot repo
    try:
        result = subprocess.run(
            ['git', 'cat-file', '-t', commit_hash],
            cwd=BOT_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or result.stdout.strip() != 'commit':
            raise ValueError(f'Invalid commit hash: {commit_hash}')
    except subprocess.TimeoutExpired:
        raise ValueError(f'Timeout validating commit: {commit_hash}')

    # Clean up any partial extraction
    if os.path.exists(cache_path):
        shutil.rmtree(cache_path)

    os.makedirs(cache_path, exist_ok=True)

    # Use git archive to create a zip, then extract it.
    # First determine which optional paths exist at this commit so we
    # don't pass non-existent paths to git archive (which would fail).
    zip_path = cache_path + '.zip'
    try:
        archive_paths = list(BOT_ARCHIVE_PATHS_REQUIRED)

        # Probe optional paths via git ls-tree
        for opt_path in BOT_ARCHIVE_PATHS_OPTIONAL:
            probe = subprocess.run(
                ['git', 'ls-tree', '--name-only', commit_hash, opt_path],
                cwd=BOT_REPO_DIR,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                archive_paths.append(opt_path)

        archive_cmd = [
            'git', 'archive',
            '--format=zip',
            '-o', zip_path,
            commit_hash,
            '--',
        ] + archive_paths

        result = subprocess.run(
            archive_cmd,
            cwd=BOT_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            raise ValueError(
                f'git archive failed for {commit_hash}: {result.stderr}'
            )

        # Extract the zip
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(cache_path)

        if not os.path.isfile(os.path.join(cache_path, 'bot.py')):
            raise ValueError(
                f'Extraction succeeded but bot.py not found at {commit_hash}'
            )

    finally:
        # Clean up zip file
        if os.path.exists(zip_path):
            os.remove(zip_path)

    return cache_path


def clean_version_cache(keep_hashes: list[str] | None = None) -> int:
    """Remove cached versions that are not in *keep_hashes*.

    If *keep_hashes* is None, removes ALL cached versions.
    Returns the number of entries removed.
    """
    if not os.path.isdir(VERSION_CACHE_DIR):
        return 0

    keep = set(keep_hashes) if keep_hashes else set()
    removed = 0

    for entry in os.listdir(VERSION_CACHE_DIR):
        entry_path = os.path.join(VERSION_CACHE_DIR, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry not in keep:
            shutil.rmtree(entry_path, ignore_errors=True)
            removed += 1

    return removed

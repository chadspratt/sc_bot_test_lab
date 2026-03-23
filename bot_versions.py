"""
Manage cached copies of past bot versions from git history.

Extracts bot source code from previous commits into a local cache
directory.  Each version is identified by its full commit hash and
stored under ``aiarena/bot_versions/<hash>/``.

Cached versions are used for "current vs past" regression testing.
Symlink targets (e.g. shared libraries) are mounted at runtime via
Docker Compose, so only the bot's own code varies between versions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_CACHE_DIR = os.path.join(SCRIPT_DIR, 'aiarena', 'bot_versions')


@dataclass
class BotCommit:
    """A single commit from the bot repo's history."""
    hash: str           # full 40-char SHA
    short_hash: str     # first 7 chars
    subject: str        # commit message first line
    date: str           # author date (ISO-ish format)
    is_cached: bool     # whether a cached copy already exists


def get_recent_bot_commits(
    count: int = 5, repo_path: str | None = None,
) -> list[BotCommit]:
    """Return the most recent *count* commits from a bot repo.

    *repo_path* must be provided — it should be the bot's ``source_path``.

    Skips the current HEAD commit (index 0) since there's no point
    running the current version against itself — that's what mirror
    matches are for.  Returns commits starting from HEAD~1.
    """
    if not repo_path:
        return []
    cwd = repo_path
    try:
        result = subprocess.run(
            [
                'git', 'log',
                '--format=%H|%h|%s|%ai',
                f'-{count + 1}',
            ],
            cwd=cwd,
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
    return os.path.isdir(cache_path) and bool(os.listdir(cache_path))


def get_version_cache_path(commit_hash: str) -> str:
    """Return the cache directory path for a commit (may not exist yet)."""
    return os.path.join(VERSION_CACHE_DIR, commit_hash)


def get_or_create_version_cache(
    commit_hash: str,
    repo_path: str | None = None,
    archive_paths: list[str] | None = None,
) -> str:
    """Extract bot source from a past commit into the cache.

    *repo_path* must be provided — it should be the bot's ``source_path``.

    *archive_paths* is an optional list of paths to extract from the
    commit (passed to ``git archive``).  If provided, only those paths
    are archived; paths that don't exist at the commit are silently
    skipped.  If empty or ``None``, the entire tree is archived.

    Returns the absolute path to the cache directory.
    Raises ``ValueError`` if the commit hash is invalid or extraction fails.
    """
    if not repo_path:
        raise ValueError('repo_path is required')
    cwd = repo_path
    cache_path = get_version_cache_path(commit_hash)

    if is_version_cached(commit_hash):
        return cache_path

    # Validate the commit exists
    try:
        result = subprocess.run(
            ['git', 'cat-file', '-t', commit_hash],
            cwd=cwd,
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
    zip_path = cache_path + '.zip'
    try:
        if archive_paths:
            # Filter to paths that exist at this commit
            valid_paths = []
            for p in archive_paths:
                probe = subprocess.run(
                    ['git', 'ls-tree', '--name-only', commit_hash, p],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    valid_paths.append(p)

            if valid_paths:
                archive_cmd = [
                    'git', 'archive',
                    '--format=zip',
                    '-o', zip_path,
                    commit_hash,
                    '--',
                ] + valid_paths
            else:
                # None of the configured paths exist — fall back to full tree
                archive_cmd = [
                    'git', 'archive',
                    '--format=zip',
                    '-o', zip_path,
                    commit_hash,
                ]
        else:
            # No archive_paths configured — archive entire tree
            archive_cmd = [
                'git', 'archive',
                '--format=zip',
                '-o', zip_path,
                commit_hash,
            ]

        result = subprocess.run(
            archive_cmd,
            cwd=cwd,
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

        # Sanity check: the cache directory should have at least one file
        if not os.listdir(cache_path):
            raise ValueError(
                f'Extraction succeeded but cache directory is empty for {commit_hash}'
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

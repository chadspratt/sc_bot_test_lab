"""
Git worktree management for branch-based testing.

Creates and manages git worktrees so that tests can run against different
branches simultaneously.  Each branch gets its own worktree directory
under ``aiarena/worktrees/<sanitized_branch>/``.

Worktrees are reused across test runs for the same branch.  Callers are
responsible for cleanup via ``remove_worktree()`` when a branch is no
longer needed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger('test_lab')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKTREE_BASE_DIR = os.path.join(SCRIPT_DIR, 'aiarena', 'worktrees')


def _sanitize_branch_name(branch: str) -> str:
    """Convert a branch name to a safe directory name.

    Replaces slashes and other unsafe characters with underscores.
    """
    return re.sub(r'[^a-zA-Z0-9._-]', '_', branch)


def _validate_branch_name(branch: str) -> bool:
    """Basic validation that the branch name looks reasonable.

    Rejects empty strings, strings with shell metacharacters, etc.
    """
    if not branch or len(branch) > 200:
        return False
    # Reject obvious shell injection attempts
    if any(c in branch for c in ';|&$`\\\'\"<>(){}'):
        return False
    return True


def get_worktree_path(repo_path: str, branch: str) -> str:
    """Return the worktree directory path for a branch (may not exist yet)."""
    safe_name = _sanitize_branch_name(branch)
    return os.path.join(WORKTREE_BASE_DIR, safe_name)


def get_or_create_worktree(repo_path: str, branch: str) -> str:
    """Create a git worktree for *branch* if one doesn't already exist.

    *repo_path* is the path to the main git repository (e.g. ``bot/``).

    Returns the absolute path to the worktree directory.
    Raises ``ValueError`` if the branch doesn't exist or the repo is invalid.
    """
    if not _validate_branch_name(branch):
        raise ValueError(f'Invalid branch name: {branch!r}')

    if not os.path.isdir(repo_path):
        raise ValueError(f'Repository path does not exist: {repo_path}')

    # Verify the branch exists in the repo
    result = subprocess.run(
        ['git', 'rev-parse', '--verify', branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise ValueError(
            f'Branch {branch!r} does not exist in {repo_path}: '
            f'{result.stderr.strip()}'
        )

    worktree_path = get_worktree_path(repo_path, branch)

    # If worktree already exists and is valid, just update it
    if os.path.isdir(worktree_path) and os.path.isdir(
        os.path.join(worktree_path, '.git')
    ) or os.path.isfile(os.path.join(worktree_path, '.git')):
        # Pull latest changes for the branch
        try:
            subprocess.run(
                ['git', 'checkout', branch],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info('Reusing existing worktree for branch %s at %s', branch, worktree_path)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('Failed to update worktree for %s: %s', branch, e)
        return worktree_path

    # Clean up any stale directory that isn't a valid worktree
    if os.path.exists(worktree_path):
        # Prune stale worktree references first
        subprocess.run(
            ['git', 'worktree', 'prune'],
            cwd=repo_path,
            capture_output=True,
            timeout=10,
        )
        # If directory still exists but isn't a worktree, remove it
        if os.path.exists(worktree_path):
            import shutil
            shutil.rmtree(worktree_path)

    os.makedirs(WORKTREE_BASE_DIR, exist_ok=True)

    # Create the worktree
    result = subprocess.run(
        ['git', 'worktree', 'add', worktree_path, branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError(
            f'Failed to create worktree for branch {branch!r}: '
            f'{result.stderr.strip()}'
        )

    logger.info('Created worktree for branch %s at %s', branch, worktree_path)
    return worktree_path


def remove_worktree(repo_path: str, branch: str) -> bool:
    """Remove a git worktree for *branch*.

    Returns True if the worktree was removed, False if it didn't exist.
    """
    worktree_path = get_worktree_path(repo_path, branch)

    if not os.path.exists(worktree_path):
        return False

    result = subprocess.run(
        ['git', 'worktree', 'remove', worktree_path, '--force'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            'git worktree remove failed for %s: %s',
            worktree_path, result.stderr.strip(),
        )
        # Fall back to manual cleanup
        import shutil
        shutil.rmtree(worktree_path, ignore_errors=True)
        subprocess.run(
            ['git', 'worktree', 'prune'],
            cwd=repo_path,
            capture_output=True,
            timeout=10,
        )

    logger.info('Removed worktree for branch %s', branch)
    return True


def list_worktrees(repo_path: str) -> list[dict[str, str]]:
    """List all git worktrees for a repository.

    Returns a list of dicts with 'path', 'branch', and 'head' keys.
    """
    result = subprocess.run(
        ['git', 'worktree', 'list', '--porcelain'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []

    worktrees = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith('worktree '):
            if current:
                worktrees.append(current)
            current = {'path': line.split(' ', 1)[1]}
        elif line.startswith('HEAD '):
            current['head'] = line.split(' ', 1)[1]
        elif line.startswith('branch '):
            current['branch'] = line.split(' ', 1)[1]
    if current:
        worktrees.append(current)

    return worktrees

"""
Git worktree management for branch-based testing.

When a branch already has a worktree checked out (e.g. created by the
ticket system), that existing worktree is reused rather than creating a
duplicate.  If no worktree exists yet, a new one is created under
``aiarena/worktrees/<sanitized_branch>/``.

Callers are responsible for cleanup via ``remove_worktree()`` when a
branch is no longer needed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger('test_lab')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
WORKTREE_BASE_DIR = os.path.join(_REPO_ROOT, 'bot', 'worktrees')


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


def _find_existing_worktree(repo_path: str, branch: str) -> str | None:
    """Return the path of an existing worktree for *branch*, or ``None``.

    Checks all worktrees registered with the repository at *repo_path*
    (e.g. those created by the ticket system) so we can reuse them
    instead of creating a conflicting second checkout.
    """
    for wt in list_worktrees(repo_path):
        wt_branch = wt.get('branch', '')
        if wt_branch == f'refs/heads/{branch}' or wt_branch == branch:
            return wt['path']
    return None


def get_or_create_worktree(repo_path: str, branch: str) -> str:
    """Return a worktree for *branch*, reusing an existing one if possible.

    If the branch is already checked out in a worktree (e.g. one created
    by the ticket system under the repo's own ``worktrees/`` directory),
    that path is returned directly — no new worktree is created.

    Otherwise a new worktree is created under ``aiarena/worktrees/``.

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

    # Reuse an existing worktree for this branch (e.g. one created by the
    # ticket system) instead of trying to create a second checkout.
    existing = _find_existing_worktree(repo_path, branch)
    if existing:
        logger.info(
            'Using existing worktree for branch %s at %s', branch, existing,
        )
        return existing

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

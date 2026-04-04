"""Platform detection and repository root finding.

Platform helpers (is_linux, is_macos, is_windows) are thin wrappers around
sys.platform. They exist so command code reads clearly: `if is_macos():`
instead of `if sys.platform == "darwin":`.

find_repo_root() walks up from a starting directory looking for .git as
either a directory (normal repo) or a file (worktree/submodule). Falls back
to `git rev-parse --show-toplevel` if the walk fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def is_linux() -> bool:
    """True if running on Linux."""
    return sys.platform == "linux"


def is_macos() -> bool:
    """True if running on macOS."""
    return sys.platform == "darwin"


def is_windows() -> bool:
    """True if running on Windows."""
    return sys.platform == "win32"


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from start looking for a .git directory or file.

    Returns the repo root Path, or None if not found. Handles:
    - Normal repos (.git is a directory)
    - Worktrees (.git is a file containing 'gitdir: ...')
    - Submodules (same as worktrees)

    Falls back to `git rev-parse --show-toplevel` if the walk doesn't
    find .git, which handles edge cases like bare repos.
    """
    current = (start or Path.cwd()).resolve()

    # Walk up the directory tree looking for .git (file or directory).
    while current != current.parent:
        git_path = current / ".git"
        if git_path.exists():
            return current
        current = current.parent

    # Check the filesystem root too.
    if (current / ".git").exists():
        return current

    # Fallback: ask git directly. This handles edge cases the walk misses.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=start or Path.cwd(),
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

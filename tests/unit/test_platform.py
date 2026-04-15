"""Tests for platform detection and repo root finding.

Platform detection wraps sys.platform checks into readable helpers.
Repo root finding walks up from cwd looking for .git (as directory or file),
with a fallback to git rev-parse. This needs to handle worktrees where .git
is a file pointing at the real gitdir.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


class TestPlatformDetection:
    """Platform helpers return booleans based on sys.platform."""

    def test_is_linux(self):
        from qbrd_tools.platform import is_linux

        with patch.object(sys, "platform", "linux"):
            assert is_linux() is True
        with patch.object(sys, "platform", "darwin"):
            assert is_linux() is False

    def test_is_macos(self):
        from qbrd_tools.platform import is_macos

        with patch.object(sys, "platform", "darwin"):
            assert is_macos() is True
        with patch.object(sys, "platform", "linux"):
            assert is_macos() is False

    def test_is_windows(self):
        from qbrd_tools.platform import is_windows

        with patch.object(sys, "platform", "win32"):
            assert is_windows() is True
        with patch.object(sys, "platform", "linux"):
            assert is_windows() is False


class TestFindRepoRoot:
    """find_repo_root() walks up from a starting directory looking for .git."""

    def test_finds_git_directory(self, tmp_path: Path):
        """Standard case: .git is a directory at the repo root."""
        from qbrd_tools.platform import find_repo_root

        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        assert find_repo_root(subdir) == tmp_path

    def test_finds_git_file_for_worktrees(self, tmp_path: Path):
        """Worktree case: .git is a file containing 'gitdir: /path/to/...'."""
        from qbrd_tools.platform import find_repo_root

        (tmp_path / ".git").write_text("gitdir: /some/other/path")
        assert find_repo_root(tmp_path) == tmp_path

    def test_returns_none_when_no_git(self, tmp_path: Path):
        """When there's no .git anywhere in the hierarchy, return None."""
        from qbrd_tools.platform import find_repo_root

        subdir = tmp_path / "not" / "a" / "repo"
        subdir.mkdir(parents=True)
        assert find_repo_root(subdir) is None

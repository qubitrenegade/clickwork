"""Tests for prerequisite checking.

Commands declare what tools they need: require("docker"), require("gh").
Optionally require("gh", authenticated=True) to also verify auth status.
The framework checks before the command runs and fails fast with a clear
message if something is missing or not authenticated.
"""
import shutil
import subprocess
from unittest.mock import patch

import pytest


class TestRequire:
    """require() checks that a binary exists on PATH."""

    def test_passes_for_existing_binary(self):
        """'echo' exists on every system -- this should always pass."""
        from qbrd_tools.prereqs import require

        # Should not raise.
        require("echo")

    def test_raises_for_missing_binary(self):
        from qbrd_tools.prereqs import require

        with pytest.raises(SystemExit) as exc_info:
            require("definitely-not-a-real-binary-xyz123")
        # Exit code 1 = user error (missing prereq, fixable).
        assert exc_info.value.code == 1

    def test_error_message_names_the_binary(self, capsys):
        from qbrd_tools.prereqs import require

        with pytest.raises(SystemExit):
            require("missing-tool-abc")
        captured = capsys.readouterr()
        assert "missing-tool-abc" in captured.err


class TestRequireAuthenticated:
    """require(binary, authenticated=True) checks auth status."""

    def test_auth_check_passes_when_command_succeeds(self):
        """When the auth check command exits 0, require() should pass."""
        from qbrd_tools.prereqs import require, AUTH_CHECKS

        with patch("shutil.which", return_value="/usr/bin/fake"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            # Register a fake auth check for testing.
            AUTH_CHECKS["fake-tool"] = ["fake-tool", "auth", "status"]
            try:
                require("fake-tool", authenticated=True)
            finally:
                del AUTH_CHECKS["fake-tool"]

    def test_auth_check_fails_when_command_errors(self, capsys):
        """When the auth check command fails, require() should exit."""
        from qbrd_tools.prereqs import require, AUTH_CHECKS

        with patch("shutil.which", return_value="/usr/bin/fake"), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["fake"])):
            AUTH_CHECKS["fake-tool"] = ["fake-tool", "auth", "status"]
            try:
                with pytest.raises(SystemExit) as exc_info:
                    require("fake-tool", authenticated=True)
                assert exc_info.value.code == 1
                captured = capsys.readouterr()
                assert "not authenticated" in captured.err
            finally:
                del AUTH_CHECKS["fake-tool"]

    def test_unknown_binary_skips_auth_check_with_warning(self, capsys):
        """Binaries without a known auth check should warn but not fail."""
        from qbrd_tools.prereqs import require

        with patch("shutil.which", return_value="/usr/bin/unknown"):
            require("unknown-tool", authenticated=True)
            captured = capsys.readouterr()
            assert "no auth check" in captured.err.lower()

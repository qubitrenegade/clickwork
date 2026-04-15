"""Tests for prerequisite checking.

Commands declare what tools they need: require("docker"), require("gh").
Optionally require("gh", authenticated=True) to also verify auth status.
The framework checks before the command runs and fails fast with a clear
message if something is missing or not authenticated.
"""
import subprocess
from unittest.mock import patch

import pytest


class TestRequire:
    """require() checks that a binary exists on PATH."""

    def test_passes_for_existing_binary(self):
        """A present binary should pass regardless of the host platform."""
        from clickwork.prereqs import require

        with patch("shutil.which", return_value="/usr/bin/fake"):
            require("fake-tool")

    def test_raises_for_missing_binary(self):
        from clickwork.prereqs import require
        from clickwork._types import PrerequisiteError

        with pytest.raises(PrerequisiteError):
            require("definitely-not-a-real-binary-xyz123")

    def test_error_message_names_the_binary(self):
        from clickwork.prereqs import require
        from clickwork._types import PrerequisiteError

        with pytest.raises(PrerequisiteError, match="missing-tool-abc"):
            require("missing-tool-abc")


class TestRequireAuthenticated:
    """require(binary, authenticated=True) checks auth status."""

    def test_auth_check_passes_when_command_succeeds(self):
        """When the auth check command exits 0, require() should pass."""
        from clickwork.prereqs import require, AUTH_CHECKS

        with patch("shutil.which", return_value="/usr/bin/fake"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            # Register a fake auth check for testing.
            AUTH_CHECKS["fake-tool"] = ["fake-tool", "auth", "status"]
            try:
                require("fake-tool", authenticated=True)
            finally:
                del AUTH_CHECKS["fake-tool"]

    def test_auth_check_fails_when_command_errors(self):
        """When the auth check command fails, require() should raise."""
        from clickwork.prereqs import require, AUTH_CHECKS
        from clickwork._types import PrerequisiteError

        with patch("shutil.which", return_value="/usr/bin/fake"), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["fake"])):
            AUTH_CHECKS["fake-tool"] = ["fake-tool", "auth", "status"]
            try:
                with pytest.raises(PrerequisiteError, match="not authenticated"):
                    require("fake-tool", authenticated=True)
            finally:
                del AUTH_CHECKS["fake-tool"]

    def test_unknown_binary_skips_auth_check_with_warning(self, caplog):
        """Binaries without a known auth check should warn but not fail."""
        from clickwork.prereqs import require

        with patch("shutil.which", return_value="/usr/bin/unknown"):
            with caplog.at_level("WARNING", logger="clickwork"):
                require("unknown-tool", authenticated=True)
            assert "no auth check" in caplog.text.lower()

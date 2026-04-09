"""Tests for subprocess helpers: run(), capture(), and run_with_confirm().

These are the workhorses of the framework. Commands call run() to execute
mutating operations (deploy, build) and capture() to collect data (list
instances, get version). The key design decisions tested here:

1. run() streams output in real-time (not buffered)
2. run() raises CliProcessError on non-zero exit
3. run() returns CompletedProcess so callers can inspect exit codes
4. run() respects --dry-run (prints command, doesn't execute)
5. capture() always executes, even in dry-run (reads are safe)
6. Both accept argv lists, never invoke a shell
7. run() supports passing env vars for secret safety
8. run_with_confirm() prompts before executing destructive commands
9. On Ctrl-C, run() forwards SIGINT to the child and waits before re-raising
"""
import sys
import subprocess
import signal
from unittest.mock import patch, MagicMock

import pytest


class TestRun:
    """run() executes a command, streams output, raises on failure."""

    def test_runs_command_and_returns_completed_process(self):
        from qbrd_tools.process import run

        result = run([sys.executable, "-c", "print('hello')"])
        assert result.returncode == 0

    def test_raises_cli_process_error_on_failure(self):
        from qbrd_tools.process import run
        from qbrd_tools._types import CliProcessError

        with pytest.raises(CliProcessError) as exc_info:
            run([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert exc_info.value.returncode != 0

    def test_dry_run_does_not_execute(self):
        """In dry-run mode, run() should print what it would do but not run it."""
        from qbrd_tools.process import run

        # This would fail if actually executed.
        result = run([sys.executable, "-c", "import sys; sys.exit(1)"], dry_run=True)
        assert result is None

    def test_passes_extra_env_vars(self):
        """Secrets should be passable as env vars, not argv."""
        from qbrd_tools.process import run

        proc = MagicMock()
        proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            result = run(
                [sys.executable, "-c", "import os; print(os.environ['TEST_SECRET'])"],
                env={"TEST_SECRET": "s3cret"},
            )

        assert result.returncode == 0
        assert mock_popen.call_args.kwargs["env"]["TEST_SECRET"] == "s3cret"

    def test_accepts_only_lists_not_strings(self):
        """Prevent shell injection by rejecting string commands."""
        from qbrd_tools.process import run

        with pytest.raises(TypeError):
            run("echo hello")  # type: ignore

    def test_forwards_sigint_to_child_and_waits(self):
        """Ctrl-C should be forwarded to the child before bubbling up."""
        from qbrd_tools.process import run

        proc = MagicMock()
        proc.send_signal.side_effect = ProcessLookupError()
        proc.wait.side_effect = [KeyboardInterrupt(), OSError()]
        proc.communicate.return_value = ("", "")
        proc.returncode = 130

        with patch("subprocess.Popen", return_value=proc):
            with pytest.raises(KeyboardInterrupt):
                run([sys.executable, "-c", "import time; time.sleep(10)"])

        proc.send_signal.assert_called_once_with(signal.SIGINT)
        assert proc.wait.call_count == 2


class TestCapture:
    """capture() runs a command and returns its stdout."""

    def test_captures_stdout(self):
        from qbrd_tools.process import capture

        output = capture([sys.executable, "-c", "print('hello world')"])
        assert output.strip() == "hello world"

    def test_raises_on_failure(self):
        from qbrd_tools.process import capture
        from qbrd_tools._types import CliProcessError

        with pytest.raises(CliProcessError):
            capture([sys.executable, "-c", "import sys; sys.exit(1)"])

    def test_always_executes_in_dry_run(self):
        """capture() is read-only, so it runs even in dry-run mode."""
        from qbrd_tools.process import capture

        output = capture([sys.executable, "-c", "print('data')"], dry_run=True)
        assert output == "data"

    def test_returns_stripped_output(self):
        """capture() strips trailing whitespace/newlines from stdout."""
        from qbrd_tools.process import capture

        output = capture([sys.executable, "-c", "print('  hello  ')"])
        assert output == "hello"

    def test_accepts_only_lists_not_strings(self):
        """Prevent shell injection by rejecting string commands."""
        from qbrd_tools.process import capture

        with pytest.raises(TypeError):
            capture("echo hello")  # type: ignore


class TestRunWithConfirm:
    """run_with_confirm() prompts before executing destructive commands."""

    def test_executes_when_confirmed(self):
        from qbrd_tools.process import run_with_confirm
        from unittest.mock import patch

        with patch("qbrd_tools.process._confirm_fn", return_value=True):
            result = run_with_confirm(
                [sys.executable, "-c", "print('hello')"],
                "Delete everything?",
            )
            assert result is not None
            assert result.returncode == 0

    def test_skips_when_denied(self):
        from qbrd_tools.process import run_with_confirm
        from unittest.mock import patch

        with patch("qbrd_tools.process._confirm_fn", return_value=False):
            result = run_with_confirm(
                [sys.executable, "-c", "print('hello')"],
                "Delete everything?",
            )
            assert result is None

    def test_yes_flag_bypasses_prompt(self):
        from qbrd_tools.process import run_with_confirm

        result = run_with_confirm([sys.executable, "-c", "print('hello')"], "Delete?", yes=True)
        assert result is not None
        assert result.returncode == 0

    def test_dry_run_skips_execution(self):
        from qbrd_tools.process import run_with_confirm

        result = run_with_confirm(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            "Delete?",
            yes=True,
            dry_run=True,
        )
        assert result is None

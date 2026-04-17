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
        from clickwork.process import run

        result = run([sys.executable, "-c", "print('hello')"])
        assert result.returncode == 0

    def test_raises_cli_process_error_on_failure(self):
        from clickwork.process import run
        from clickwork._types import CliProcessError

        with pytest.raises(CliProcessError) as exc_info:
            run([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert exc_info.value.returncode != 0

    def test_dry_run_does_not_execute(self):
        """In dry-run mode, run() should print what it would do but not run it."""
        from clickwork.process import run

        # This would fail if actually executed.
        result = run([sys.executable, "-c", "import sys; sys.exit(1)"], dry_run=True)
        assert result is None

    def test_passes_extra_env_vars(self):
        """Secrets should be passable as env vars, not argv."""
        from clickwork.process import run

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
        from clickwork.process import run

        with pytest.raises(TypeError):
            run("echo hello")  # type: ignore

    def test_forwards_sigint_to_child_and_waits(self):
        """Ctrl-C should be forwarded to the child before bubbling up."""
        from clickwork.process import run

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

    def test_missing_binary_raises_cli_process_error(self):
        """A nonexistent binary should raise CliProcessError (exit 1), not FileNotFoundError (exit 2).

        WHY: a missing binary is a user/environment error, not a framework bug.
        Without this catch, the framework's wrapped_invoke handler classifies it
        as an unhandled exception and exits with code 2, which is misleading.
        """
        from clickwork.process import run
        from clickwork._types import CliProcessError

        with pytest.raises(CliProcessError, match="Command not found"):
            run(["definitely-not-a-real-binary-xyz123"])

    def test_run_with_stdin_text_delivers_value(self, capfd):
        """stdin_text should be piped to the child process as a text stream.

        WHY: this is the primary use case for secret-via-stdin tools like
        ``wrangler secret put``, ``gh auth login --with-token``, and
        ``docker login --password-stdin`` -- pass the secret on stdin so
        it never appears in argv (which is visible in ``ps`` output).
        """
        from clickwork.process import run

        # Child process echoes its stdin to stdout. We then use capfd to
        # confirm the child actually received "hello" over its stdin pipe.
        result = run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            stdin_text="hello",
        )
        assert result.returncode == 0
        captured = capfd.readouterr()
        assert captured.out == "hello"

    def test_run_with_stdin_bytes_delivers_value(self, capfdbinary):
        """stdin_bytes should be piped to the child process as a binary stream.

        WHY: some tools want raw bytes on stdin (e.g., binary tokens, keys
        with non-UTF-8 encodings). Keeping a distinct ``stdin_bytes`` kwarg
        -- rather than overloading ``stdin_text`` with ``str | bytes`` --
        makes the mode explicit at the call site.
        """
        from clickwork.process import run

        # Child echoes raw bytes from stdin to stdout via the binary buffer
        # so byte-for-byte fidelity is preserved across the pipe boundary.
        result = run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            stdin_bytes=b"world",
        )
        assert result.returncode == 0
        captured = capfdbinary.readouterr()
        assert captured.out == b"world"

    def test_run_rejects_both_stdin_text_and_stdin_bytes(self):
        """Passing both stdin_text and stdin_bytes is a programming error.

        WHY: there's no coherent semantics for "text and bytes at the same
        time" -- the caller clearly meant one or the other. Raise early
        with a ValueError so the mistake surfaces immediately, rather than
        silently picking one and dropping the other.
        """
        from clickwork.process import run

        with pytest.raises(ValueError, match="stdin_text.*stdin_bytes"):
            run(
                [sys.executable, "-c", "pass"],
                stdin_text="hello",
                stdin_bytes=b"world",
            )

    def test_run_stdin_text_dry_run_does_not_execute(self):
        """With dry_run=True, stdin_text must NOT trigger process execution.

        WHY: dry-run should be safe even when callers pass a real secret as
        stdin_text. If dry-run spawned the process (just to feed it stdin)
        it could leak the secret into the child -- which is the exact
        behavior dry-run is supposed to prevent.
        """
        from clickwork.process import run

        # Sentinel: if the child actually runs, it exits 1, which would
        # raise CliProcessError. Dry-run must short-circuit before that.
        with patch("subprocess.Popen") as mock_popen:
            result = run(
                [sys.executable, "-c", "import sys; sys.exit(1)"],
                dry_run=True,
                stdin_text="secret-value",
            )

        assert result is None
        mock_popen.assert_not_called()


class TestFormatCmd:
    """_format_cmd() renders an argv list as a display string."""

    def test_posix_uses_shlex_quote(self):
        """On POSIX, shlex.quote wraps args with spaces in single quotes."""
        from clickwork.process import _format_cmd

        with patch("os.name", "posix"):
            result = _format_cmd(["echo", "hello world"])
        assert result == "echo 'hello world'"

    def test_windows_uses_list2cmdline(self):
        """On Windows, subprocess.list2cmdline handles quoting."""
        from clickwork.process import _format_cmd

        with patch("os.name", "nt"):
            result = _format_cmd(["echo", "hello world"])
        # list2cmdline wraps args with spaces in double quotes.
        assert result == 'echo "hello world"'


class TestCapture:
    """capture() runs a command and returns its stdout."""

    def test_captures_stdout(self):
        from clickwork.process import capture

        output = capture([sys.executable, "-c", "print('hello world')"])
        assert output.strip() == "hello world"

    def test_raises_on_failure(self):
        from clickwork.process import capture
        from clickwork._types import CliProcessError

        with pytest.raises(CliProcessError):
            capture([sys.executable, "-c", "import sys; sys.exit(1)"])

    def test_always_executes_in_dry_run(self):
        """capture() is read-only, so it runs even in dry-run mode."""
        from clickwork.process import capture

        output = capture([sys.executable, "-c", "print('data')"], dry_run=True)
        assert output == "data"

    def test_returns_stripped_output(self):
        """capture() strips trailing whitespace/newlines from stdout."""
        from clickwork.process import capture

        output = capture([sys.executable, "-c", "print('  hello  ')"])
        assert output == "hello"

    def test_accepts_only_lists_not_strings(self):
        """Prevent shell injection by rejecting string commands."""
        from clickwork.process import capture

        with pytest.raises(TypeError):
            capture("echo hello")  # type: ignore

    def test_missing_binary_raises_cli_process_error(self):
        """A nonexistent binary should raise CliProcessError, not FileNotFoundError.

        Same policy as run(): missing binary is exit 1 (user error), not
        exit 2 (framework bug).
        """
        from clickwork.process import capture
        from clickwork._types import CliProcessError

        with pytest.raises(CliProcessError, match="Command not found"):
            capture(["definitely-not-a-real-binary-xyz123"])


class TestRunWithConfirm:
    """run_with_confirm() prompts before executing destructive commands."""

    def test_executes_when_confirmed(self):
        from clickwork.process import run_with_confirm
        from unittest.mock import patch

        # Patch the imported binding in the process module so the already-
        # resolved _prompt_confirm name is replaced for this test.
        with patch("clickwork.process._prompt_confirm", return_value=True):
            result = run_with_confirm(
                [sys.executable, "-c", "print('hello')"],
                "Delete everything?",
            )
            assert result is not None
            assert result.returncode == 0

    def test_skips_when_denied(self):
        from clickwork.process import run_with_confirm
        from unittest.mock import patch

        # Patch the imported binding in the process module so the already-
        # resolved _prompt_confirm name is replaced for this test.
        with patch("clickwork.process._prompt_confirm", return_value=False):
            result = run_with_confirm(
                [sys.executable, "-c", "print('hello')"],
                "Delete everything?",
            )
            assert result is None

    def test_yes_flag_bypasses_prompt(self):
        from clickwork.process import run_with_confirm

        result = run_with_confirm([sys.executable, "-c", "print('hello')"], "Delete?", yes=True)
        assert result is not None
        assert result.returncode == 0

    def test_dry_run_skips_execution(self):
        from clickwork.process import run_with_confirm

        result = run_with_confirm(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            "Delete?",
            yes=True,
            dry_run=True,
        )
        assert result is None

    def test_stdin_text_happy_path(self, capfd):
        """run_with_confirm should forward stdin_text to the child process.

        WHY: destructive commands that consume secrets on stdin (e.g., a
        "rotate production token" workflow using ``wrangler secret put``)
        still want the confirmation prompt. Forwarding stdin_text from
        run_with_confirm through to run() keeps the call site symmetric
        with run() and avoids a second code path for stdin injection.
        """
        from clickwork.process import run_with_confirm

        # yes=True skips the prompt entirely so we can isolate the stdin
        # delivery behavior from the confirmation logic.
        result = run_with_confirm(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            "Rotate production token?",
            yes=True,
            stdin_text="s3cret-token",
        )
        assert result is not None
        assert result.returncode == 0
        captured = capfd.readouterr()
        assert captured.out == "s3cret-token"

    def test_rejects_both_stdin_text_and_stdin_bytes(self):
        """run_with_confirm should also enforce stdin mutual exclusivity.

        WHY: the validation must happen in both public entry points so
        callers hitting run_with_confirm get the same clear error as
        callers hitting run() directly -- not a confusing error from
        deep inside the process machinery.
        """
        from clickwork.process import run_with_confirm

        with pytest.raises(ValueError, match="stdin_text.*stdin_bytes"):
            run_with_confirm(
                [sys.executable, "-c", "pass"],
                "Do it?",
                yes=True,
                stdin_text="hello",
                stdin_bytes=b"world",
            )

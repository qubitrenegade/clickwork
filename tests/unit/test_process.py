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
        """stdin_text should be UTF-8 encoded and piped to the child's stdin.

        Implementation detail pinned here for future readers: run() always
        opens the child's stdin pipe in **binary** mode and encodes
        stdin_text to UTF-8 itself. We do NOT use ``Popen(text=True)``
        because that would pick up the platform locale encoding and could
        apply Windows "\\n" -> "\\r\\n" newline translation -- both of
        which silently corrupt secret/token payloads. See the WHY-always-
        bytes comment in process.py.

        The child-side Python here uses ``sys.stdin.read()`` which decodes
        using the child's locale. Since our payload "hello" is pure ASCII,
        the round-trip is lossless regardless of what that locale is.

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


class TestRunWithSecrets:
    """run_with_secrets() delivers secrets via env (always) and stdin (optional).

    The helper is a thin, safety-focused wrapper around run(). It exists to
    make the "subprocess needs a secret" contract explicit at every call
    site, and to centralise two guardrails:
      - reject any ``Secret`` instance that appears directly in ``cmd``
        (argv is world-readable in ``ps``);
      - redact secret-sourced env vars in the log line this helper emits
        before delegating to ``run()``.

    Child processes running under this helper always see ``secrets`` in
    their env; optionally, one of those secrets can ALSO be piped through
    stdin (for tools like ``wrangler secret put --env-stdin`` or
    ``docker login --password-stdin``). The dual-channel delivery is
    deliberate: some tools prefer env, some prefer stdin, and this helper
    lets the caller support both without re-plumbing secret handling.
    """

    def test_run_with_secrets_rejects_Secret_in_argv(self):
        """A Secret instance appearing in cmd is a footgun; reject it loudly.

        WHY: argv is visible via ``ps`` / ``/proc/*/cmdline`` to other
        processes on the same host. If a caller accidentally writes
        ``run_with_secrets(["curl", "-H", f"Authorization: Bearer {tok}",
        ...])`` where ``tok`` is a Secret, the token lands in argv and
        leaks. Rejecting the explicit Secret-in-argv case catches the
        most common mistake.

        The error message must name the offending arg by **position**
        (its index in cmd), NOT by value -- the whole point is to avoid
        leaking the secret, and an error message that echoes .get() back
        would undermine that.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        secret = Secret("supersecret-leaky")
        with pytest.raises(ValueError) as exc_info:
            run_with_secrets(["cmd", secret], secrets={})

        # Error message must reference the position (index 1) so the
        # caller knows which arg to fix.
        assert "1" in str(exc_info.value), (
            f"Expected error to name the offending position, got: {exc_info.value!r}"
        )
        # The raw secret value must NOT appear anywhere in the error -- a
        # regression here would mean our "don't leak secrets" helper leaks
        # secrets in its own rejection path.
        assert "supersecret-leaky" not in str(exc_info.value)

    def test_run_with_secrets_routes_via_env(self):
        """Secrets are delivered to the child subprocess via environment variables.

        WHY env-as-default: tools like ``CLOUDFLARE_API_TOKEN`` expect
        credentials in env; forcing every caller to build an env dict
        themselves is error-prone. Giving ``secrets=`` its own channel
        makes the "this is sensitive" signal visible at each call site.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        # Child reads the env var we claim to have set and echoes it to
        # stdout, so we can assert the delivery worked end-to-end.
        result = run_with_secrets(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdout.write(os.environ['TOKEN'])",
            ],
            secrets={"TOKEN": Secret("supersecret")},
        )
        # Capture via subprocess.run-style assertion: rerun capturing.
        # We use capture directly here for clarity, mirroring how the
        # existing stdin_text test uses capfd.
        # But since run_with_secrets delegates to run() (which inherits
        # stdio), we need capfd-style capture. Use capsys via the capfd
        # fixture form when this is called -- see the fixture-based test
        # below. This positive path just asserts the return code.
        assert result is not None
        assert result.returncode == 0

    def test_run_with_secrets_env_value_reaches_child(self, capfd):
        """Second form of the env-delivery test using capfd to verify payload.

        WHY a second test: the first asserts the happy path without
        capturing. This one pins the exact value the child sees,
        guaranteeing Secret.get() was called and the value was placed
        into env under the right key.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        run_with_secrets(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdout.write(os.environ['TOKEN'])",
            ],
            secrets={"TOKEN": Secret("supersecret")},
        )
        captured = capfd.readouterr()
        assert captured.out == "supersecret"

    def test_run_with_secrets_routes_via_stdin_when_stdin_secret_set(self, capfd):
        """stdin_secret="NAME" routes secrets[NAME].get() through the child's stdin.

        WHY dual-channel: tools like ``wrangler secret put --env-stdin``
        and ``docker login --password-stdin`` want the secret on stdin
        (keeping it out of argv AND out of env, where a child process
        inspection might surface it). The same value is ALSO placed in
        env -- that's intentional; some tools read from one channel, some
        from the other, and the caller shouldn't have to pick.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        run_with_secrets(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read())",
            ],
            secrets={"PW": Secret("hunter2")},
            stdin_secret="PW",
        )
        captured = capfd.readouterr()
        assert captured.out == "hunter2"

    def test_run_with_secrets_logs_redacted(self, caplog):
        """The helper's log line shows env-var NAMES but never VALUES.

        WHY: operators debugging a subprocess launch need to see WHICH
        environment variables were set (to spot misspellings, missing
        keys, etc.) but must never see the values. The redaction token
        ``<redacted>`` is the canonical placeholder.
        """
        import logging
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        # caplog captures records from the clickwork logger. INFO level
        # so the helper's own info-level message is retained.
        with caplog.at_level(logging.INFO, logger="clickwork"):
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"K": Secret("v")},
            )

        # Flatten all captured log messages for substring checks.
        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "<redacted>" in all_log_text, (
            f"Expected '<redacted>' marker in log output, got: {all_log_text!r}"
        )
        # The env-var NAME stays visible so operators can see which keys
        # were set.
        assert "K" in all_log_text
        # The value must NOT appear anywhere in the captured log output.
        # (A one-character value like "v" might false-match in other log
        # text, so we check it appears only as part of "<redacted>" --
        # which has no 'v' -- or inside words like "secrets" / "env" /
        # "delegate". For safety, grep for "=v" which would be the shape
        # of a leaked "K=v" pair.)
        assert "=v" not in all_log_text, (
            f"Secret value leaked into log: {all_log_text!r}"
        )

    def test_run_with_secrets_stdin_secret_must_be_in_secrets_dict(self):
        """stdin_secret must name a key that exists in secrets={}.

        WHY: if the caller typos the key name, silently routing None or
        empty through stdin would produce a confusing failure from the
        child process. Raising early with a ValueError makes the mistake
        obvious. The error message must NOT leak any secret value.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        # Case 1: secrets dict is empty.
        with pytest.raises(ValueError) as exc_info:
            run_with_secrets(
                ["cmd"],
                secrets={},
                stdin_secret="MISSING",
            )
        # Name the missing key so the caller knows what to fix.
        assert "MISSING" in str(exc_info.value)

        # Case 2: secrets present but none matching. Ensure existing
        # Secret values don't leak into the rejection message.
        with pytest.raises(ValueError) as exc_info:
            run_with_secrets(
                ["cmd"],
                secrets={"OTHER": Secret("do-not-leak-me")},
                stdin_secret="MISSING",
            )
        assert "do-not-leak-me" not in str(exc_info.value)

    def test_run_with_secrets_respects_dry_run(self):
        """dry_run=True must short-circuit before any subprocess starts.

        WHY: same policy as run(stdin_text=..., dry_run=True) -- dry-run
        is a safety net, and spawning a child just to throw away its
        output would defeat the purpose (and could leak the secret to
        the child even if we never read its output).
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        with patch("subprocess.Popen") as mock_popen:
            result = run_with_secrets(
                [sys.executable, "-c", "import sys; sys.exit(1)"],
                secrets={"TOKEN": Secret("v")},
                dry_run=True,
            )

        assert result is None
        mock_popen.assert_not_called()

    def test_run_with_secrets_merges_caller_env(self, capfd):
        """Caller-supplied env is merged with secrets; secrets win on key conflict.

        WHY: callers often want to set non-secret env vars (region,
        config path) alongside secrets. The helper should layer them,
        and secrets should win if a caller foolishly passes the same
        key in both env and secrets -- the secrets value is what the
        call was set up to deliver.
        """
        from clickwork.process import run_with_secrets
        from clickwork._types import Secret

        run_with_secrets(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdout.write(os.environ['REGION'] + ':' + os.environ['TOKEN'])",
            ],
            secrets={"TOKEN": Secret("t")},
            env={"REGION": "us-east-1"},
        )
        captured = capfd.readouterr()
        assert captured.out == "us-east-1:t"

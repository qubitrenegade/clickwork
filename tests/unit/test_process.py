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

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRun:
    """run() executes a command, streams output, raises on failure."""

    def test_runs_command_and_returns_completed_process(self):
        from clickwork.process import run

        result = run([sys.executable, "-c", "print('hello')"])
        assert result.returncode == 0

    def test_raises_cli_process_error_on_failure(self):
        from clickwork._types import CliProcessError
        from clickwork.process import run

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
        """A nonexistent binary should raise CliProcessError (exit 1), not FileNotFoundError.

        WHY: a missing binary is a user/environment error, not a framework bug.
        Without this catch, the framework's wrapped_invoke handler classifies it
        as an unhandled exception and exits with code 2, which is misleading.
        """
        from clickwork._types import CliProcessError
        from clickwork.process import run

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
        from clickwork._types import CliProcessError
        from clickwork.process import capture

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
        from clickwork._types import CliProcessError
        from clickwork.process import capture

        with pytest.raises(CliProcessError, match="Command not found"):
            capture(["definitely-not-a-real-binary-xyz123"])


class TestRunWithConfirm:
    """run_with_confirm() prompts before executing destructive commands."""

    def test_executes_when_confirmed(self):
        from unittest.mock import patch

        from clickwork.process import run_with_confirm

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
        from unittest.mock import patch

        from clickwork.process import run_with_confirm

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
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        secret = Secret("supersecret-leaky")
        with pytest.raises(ValueError) as exc_info:
            run_with_secrets(["cmd", secret], secrets={})

        # Error message must reference the position (index 1) so the
        # caller knows which arg to fix.
        assert "1" in str(
            exc_info.value
        ), f"Expected error to name the offending position, got: {exc_info.value!r}"
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
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        # Child reads the env var we claim to have set and exits 0 iff
        # the value matches -- we assert exit code here. The companion
        # test below (``..._env_value_reaches_child``) pins the exact
        # child stdout via ``capfd``; this one only verifies the
        # successful-exit path so a regression where the env var never
        # reaches the child still fails loudly (the child would exit
        # non-zero with KeyError before ever writing to stdout).
        result = run_with_secrets(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdout.write(os.environ['TOKEN'])",
            ],
            secrets={"TOKEN": Secret("supersecret")},
        )
        assert result is not None
        assert result.returncode == 0

    def test_run_with_secrets_env_value_reaches_child(self, capfd):
        """Second form of the env-delivery test using capfd to verify payload.

        WHY a second test: the first asserts the happy path without
        capturing. This one pins the exact value the child sees,
        guaranteeing Secret.get() was called and the value was placed
        into env under the right key.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

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
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

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

        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        # caplog captures records from the clickwork logger. INFO level
        # so the helper's own info-level message is retained.
        with caplog.at_level(logging.INFO, logger="clickwork"):
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"K": Secret("v")},
            )

        # Flatten all captured log messages for substring checks.
        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert (
            "<redacted>" in all_log_text
        ), f"Expected '<redacted>' marker in log output, got: {all_log_text!r}"
        # The env-var NAME stays visible so operators can see which keys
        # were set.
        assert "K" in all_log_text
        # The value must NOT appear anywhere in the captured log output.
        # (A one-character value like "v" might false-match in other log
        # text, so we check it appears only as part of "<redacted>" --
        # which has no 'v' -- or inside words like "secrets" / "env" /
        # "delegate". For safety, grep for "=v" which would be the shape
        # of a leaked "K=v" pair.)
        assert "=v" not in all_log_text, f"Secret value leaked into log: {all_log_text!r}"

    def test_run_with_secrets_does_not_leak_non_secret_env_values(self, caplog):
        """Non-secret env values ALSO redacted (tightened after Copilot PR #28).

        WHY: a caller can pass ``env={"REGION": "us-east-1", "API_KEY":
        "accidentally-not-wrapped"}`` -- we can't tell which of those
        the caller considers sensitive. The helper's contract is "this
        invocation carries secrets", so treating the whole env as
        potentially-sensitive is the safer default. Non-secret values
        render as ``<set>`` so the log still shows which keys were set
        (useful for debugging missing/mistyped keys) without exposing
        the values.

        This test pins the behaviour: pass a non-secret env value that
        would be embarrassing to leak, assert it NEVER appears in the
        log output.
        """
        import logging

        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        accidental_plaintext = "ghp_totallyNotWrappedInSecret_12345"

        with caplog.at_level(logging.INFO, logger="clickwork"):
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"REAL_TOKEN": Secret("also-hidden")},
                env={"BAD_TOKEN": accidental_plaintext, "REGION": "us-east-1"},
            )

        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert (
            accidental_plaintext not in all_log_text
        ), f"Non-secret env value leaked into log: {all_log_text!r}"
        # Non-secret VALUES also hidden: "us-east-1" must not appear either.
        assert "us-east-1" not in all_log_text
        # But the NAMES must still be visible so operators can see shape.
        assert "BAD_TOKEN" in all_log_text
        assert "REGION" in all_log_text
        assert "REAL_TOKEN" in all_log_text
        # And the tags differ so the operator can tell WHICH were secret-sourced.
        assert "REAL_TOKEN=<redacted>" in all_log_text
        assert "BAD_TOKEN=<set>" in all_log_text

    def test_run_with_secrets_stdin_secret_must_be_in_secrets_dict(self):
        """stdin_secret must name a key that exists in secrets={}.

        WHY: if the caller typos the key name, silently routing None or
        empty through stdin would produce a confusing failure from the
        child process. Raising early with a ValueError makes the mistake
        obvious. The error message must NOT leak any secret value.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

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
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        with patch("subprocess.Popen") as mock_popen:
            result = run_with_secrets(
                [sys.executable, "-c", "import sys; sys.exit(1)"],
                secrets={"TOKEN": Secret("v")},
                dry_run=True,
            )

        assert result is None
        mock_popen.assert_not_called()

    def test_run_with_secrets_dry_run_does_not_unwrap_secrets(self):
        """dry_run=True must not call Secret.get() on any secret.

        WHY: dry-run is "nothing happens" -- no subprocess, no file
        writes, and (this test) no secret values pulled into memory.
        A Secret whose .get() would otherwise raise (e.g. one backed by
        a lazy source that errors during dry-run) must pass cleanly
        through dry-run. We pin this by wrapping a Secret in a spy that
        tracks every .get() call; the assertion is that it was called
        ZERO times.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        unwrap_count = {"count": 0}

        class _SpySecret(Secret):
            """Secret that records every .get() invocation.

            Inherits Secret so isinstance checks still fire; overrides
            .get to increment the counter. We do NOT want the counter
            touched during dry-run.
            """

            def get(self) -> str:
                unwrap_count["count"] += 1
                return super().get()

        result = run_with_secrets(
            [sys.executable, "-c", "print('never runs')"],
            secrets={"TOKEN": _SpySecret("the-secret-value")},
            stdin_secret="TOKEN",
            dry_run=True,
        )
        assert result is None
        assert unwrap_count["count"] == 0, (
            f"dry_run=True must not unwrap any Secret; got "
            f"{unwrap_count['count']} .get() call(s). A regression here "
            "would mean dry-run is pulling secret values into process "
            "memory, contradicting the docstring contract."
        )

    def test_run_with_secrets_rejects_non_str_non_secret_argv(self):
        """cmd elements that aren't str and aren't Secret raise TypeError.

        WHY: an earlier implementation silently dropped non-str elements
        from cmd via an isinstance-filter, which could change the
        command the child sees (if a PathLike, bytes, or int sneaked
        through). The new guard validates every element is str after
        the Secret-rejection step so the caller gets a loud error at
        the offending index instead of a mysterious "command did the
        wrong thing" bug at runtime.
        """
        from pathlib import Path

        from clickwork.process import run_with_secrets

        with pytest.raises(TypeError) as exc_info:
            run_with_secrets(
                ["echo", Path("/tmp/nope")],
                secrets={},
            )
        # The error must name the offending index so the caller can
        # locate the bad arg without a traceback hunt.
        assert "cmd[1]" in str(exc_info.value)
        # Mention the type so the fix (str(path)) is obvious.
        assert (
            "PosixPath" in str(exc_info.value)
            or "WindowsPath" in str(exc_info.value)
            or "Path" in str(exc_info.value)
        )

    def test_run_with_secrets_rejects_non_str_env_value_before_unwrap(self):
        """Non-str env values must raise BEFORE any Secret.get() runs.

        WHY: if caller's ``env={"X": 1}`` or ``env={1: "x"}`` reaches
        subprocess.Popen, it raises TypeError -- but by that point
        every Secret in ``secrets`` has already been unwrapped into
        memory. Validating env types up front keeps the "minimal
        touch" promise: no Secret ever gets unwrapped on a call that
        was doomed anyway. The Spy-Secret counter pins that.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        unwrap_count = {"count": 0}

        class _SpySecret(Secret):
            def get(self) -> str:
                unwrap_count["count"] += 1
                return super().get()

        with pytest.raises(TypeError) as exc_info:
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"TOKEN": _SpySecret("must-not-leak")},
                env={"REGION": 42},  # type: ignore[dict-item]
            )

        assert "REGION" in str(exc_info.value)
        assert "int" in str(exc_info.value)
        # Most important assertion: no Secret.get() happened. Earlier
        # flow would have unwrapped TOKEN into full_env, then crashed
        # on Popen's env validation. We want to catch this before any
        # secret material leaves the wrapper.
        assert unwrap_count["count"] == 0, (
            f"Expected zero Secret.get() calls before the env validation "
            f"fires; got {unwrap_count['count']}. A regression here would "
            "mean env-type failures unwrap secrets unnecessarily."
        )
        # Secret value must also not appear in the error text.
        assert "must-not-leak" not in str(exc_info.value)

    def test_run_with_secrets_rejects_non_str_env_key(self):
        """Non-str env keys also rejected up front (same rationale)."""
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        with pytest.raises(TypeError, match="env keys must be str"):
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"TOKEN": Secret("v")},
                env={42: "value"},  # type: ignore[dict-item]
            )

    def test_run_with_secrets_rejects_non_str_key_in_secrets_dict(self):
        """secrets keys must be str (env-var names), not int / tuple / etc.

        WHY: an earlier draft only validated the values. A caller passing
        ``secrets={1: Secret("x")}`` would then unwrap the Secret (pulling
        it into memory!) and fail mid-subprocess launch with a confusing
        ``TypeError: expected str, bytes or os.PathLike object, not int``
        far from the real cause. Validate key types up front so the
        Secret.get() never happens on the bad call.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        with pytest.raises(TypeError) as exc_info:
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={1: Secret("value-must-not-leak")},  # type: ignore[dict-item]
            )
        # Error names the offending type but NEVER the value.
        assert "int" in str(exc_info.value)
        assert "value-must-not-leak" not in str(exc_info.value)

    def test_run_with_secrets_rejects_non_Secret_value_in_secrets_dict(self):
        """secrets={"K": "plain-string"} fails with a clear TypeError.

        WHY: if the caller forgets to wrap a value in Secret, .get()
        would raise AttributeError mid-execution. Through clickwork's
        wrapped_invoke that surfaces as exit 2 "Internal error:
        'str' object has no attribute 'get'" -- classified as a
        framework bug when it's really a user-side wrapping miss.
        The up-front TypeError keeps the error close to the cause and
        doesn't echo the value (which might be sensitive even
        unwrapped).
        """
        from clickwork.process import run_with_secrets

        with pytest.raises(TypeError) as exc_info:
            run_with_secrets(
                [sys.executable, "-c", "pass"],
                secrets={"TOKEN": "plain-string-not-wrapped"},  # type: ignore[dict-item]
            )
        # Must name the offending key AND its type so the fix is
        # obvious, but NEVER the value (a caller who passed a token
        # un-Secret-wrapped shouldn't see it echoed in the error
        # message -- same redaction discipline as everywhere else).
        assert "TOKEN" in str(exc_info.value)
        assert "str" in str(exc_info.value)  # type name, not value
        assert "plain-string-not-wrapped" not in str(exc_info.value)

    def test_run_with_secrets_rejects_non_list_cmd(self):
        """cmd must be a list, not a tuple/str/other iterable.

        WHY: matches the _validate_cmd guard run()/capture() already
        enforce. A tuple would iterate fine in plain Python but
        signals the caller is treating argv as something other than a
        mutable list -- often a sign they came from a string.format
        chain that should have been a list[str] to begin with. The
        same list-only rule catches raw string commands (which is the
        shell-injection footgun _validate_cmd was originally written
        to prevent).
        """
        from clickwork.process import run_with_secrets

        with pytest.raises(TypeError, match="cmd must be a list"):
            run_with_secrets(
                "echo hello",  # type: ignore[arg-type] -- the point of the test
                secrets={},
            )

    def test_run_with_secrets_merges_caller_env(self, capfd):
        """Caller-supplied env is merged with secrets; secrets win on key conflict.

        WHY: callers often want to set non-secret env vars (region,
        config path) alongside secrets. The helper should layer them,
        and secrets should win if a caller foolishly passes the same
        key in both env and secrets -- the secrets value is what the
        call was set up to deliver.
        """
        from clickwork._types import Secret
        from clickwork.process import run_with_secrets

        # Inline Python snippet kept on one line so the child shell invocation
        # stays self-contained; noqa since E501 isn't worth splitting a string
        # literal argument here.
        snippet = (
            "import os, sys; sys.stdout.write(os.environ['REGION'] + ':' + os.environ['TOKEN'])"
        )
        run_with_secrets(
            [sys.executable, "-c", snippet],
            secrets={"TOKEN": Secret("t")},
            env={"REGION": "us-east-1"},
        )
        captured = capfd.readouterr()
        assert captured.out == "us-east-1:t"


# ---------------------------------------------------------------------------
# Real-signal end-to-end tests (issue #50)
# ---------------------------------------------------------------------------
#
# The tests above this point mock subprocess.Popen with MagicMock to exercise
# run()'s KeyboardInterrupt-handling branches in isolation. What they DON'T
# verify is that the os-level signal plumbing actually works: that when the
# parent forwards SIGINT, the child's SIGINT handler really fires; that the
# SIGKILL escalation really terminates a wedged child. Those properties are
# what the two tests below pin down by spawning a real subprocess and sending
# a real signal.
#
# Windows caveat: CPython delivers SIGINT to a console process group via a
# different mechanism (CTRL_C_EVENT / GenerateConsoleCtrlEvent). It does not
# deliver signal.SIGINT the way POSIX does, and the "send SIGINT to the
# parent's own PID" trick below behaves differently. Rather than write a
# second Windows-specific test path, we simply skip these tests on Windows
# (the underlying framework still works there -- just with OS-native
# semantics) and rely on the mock-based tests for coverage parity.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only signal semantics")
class TestRealSignalForwarding:
    """End-to-end SIGINT forwarding with a real child process.

    Every test in this class spawns an actual ``python -c "..."`` child so the
    signal handler inside the child runs for real, the parent's KeyboardInterrupt
    path runs for real, and the OS-level wait/kill transitions run for real.
    No ``MagicMock``-based substitution of the subprocess itself (the way the
    earlier ``TestRun.test_forwards_sigint_to_child_and_waits`` does). The
    escalation test DOES monkeypatch ``subprocess.Popen`` with a snooping
    wrapper so we can inspect ``proc.returncode`` after ``KeyboardInterrupt``
    unwinds ``run()`` -- that wrapper calls the real ``Popen`` and captures
    its instance, it doesn't replace subprocess behavior with a mock.
    """

    # The child-side Python program installs a SIGINT handler that writes a
    # "received" marker to a file path passed in argv, then exits cleanly with
    # status 0. Running the handler-installation inline via ``python -c``
    # keeps the test self-contained (no helper scripts to check in) and
    # re-uses the already-available ``sys.executable`` interpreter so the
    # child matches the parent's Python version bit-for-bit.
    #
    # WHY a ready-marker file: the parent needs to wait until the child has
    # actually installed its SIGINT handler before forwarding the signal.
    # Without this sync, a fast test on a slow machine could fire SIGINT at
    # a child that hasn't yet replaced the default handler, and the default
    # handler on CPython raises KeyboardInterrupt which the child then
    # propagates as exit code 130 -- not a "signal-received" observation,
    # just an unhandled SIGINT race. The ready marker eliminates that race.
    _GRACEFUL_CHILD_SCRIPT = textwrap.dedent(
        """
        import os, signal, sys, time
        received_path = sys.argv[1]
        ready_path = sys.argv[2]

        def handler(signum, frame):
            with open(received_path, "w") as fh:
                fh.write("received-sigint")
            # Clean exit on SIGINT. The parent's _wait_with_signal_forwarding
            # then observes returncode 0 and re-raises KeyboardInterrupt.
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        # Mark ourselves ready AFTER the handler is installed -- any sooner
        # and the parent could send SIGINT before we've installed the
        # handler (default handler would raise KeyboardInterrupt, not
        # run our observable handler). The marker write is the sync point.
        with open(ready_path, "w") as fh:
            fh.write("ready")
        # Block until either our handler fires (sys.exit) or the parent
        # escalates to SIGKILL. A long sleep is fine -- the test bounds
        # the wait with its own timeout below.
        while True:
            time.sleep(0.05)
        """
    ).strip()

    # The wedged-child variant installs a SIGINT handler that RECORDS the
    # signal (so the parent can confirm the forward was delivered) but then
    # keeps sleeping forever instead of exiting. This is the shape of a
    # misbehaving child that catches SIGINT and ignores it -- exactly the
    # case SIGINT_TIMEOUT_SECONDS + SIGKILL escalation is designed for.
    _WEDGED_CHILD_SCRIPT = textwrap.dedent(
        """
        import os, signal, sys, time
        received_path = sys.argv[1]
        ready_path = sys.argv[2]

        def handler(signum, frame):
            with open(received_path, "w") as fh:
                fh.write("received-sigint-but-ignoring")
            # Intentionally do NOT exit -- simulate a child that catches
            # SIGINT, logs it, but refuses to shut down. The parent must
            # escalate to SIGKILL to unwedge.

        signal.signal(signal.SIGINT, handler)
        with open(ready_path, "w") as fh:
            fh.write("ready")
        while True:
            time.sleep(0.05)
        """
    ).strip()

    @staticmethod
    def _wait_for_ready(ready_path: Path, timeout: float = 5.0) -> None:
        """Block until the child writes the ready-marker file, or fail the test.

        WHY polling instead of os.pipe()/inotify: keeping the child-side logic
        in an inline ``python -c`` snippet means we can't share a Python object
        (pipe fd, event) across the process boundary without smuggling fd
        numbers through argv. A file-existence poll is the simplest synchronous
        handshake that works on both Linux and macOS without extra deps.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready_path.exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"child never wrote ready marker at {ready_path} within {timeout}s")

    @staticmethod
    def _send_sigint_after_ready(
        ready_path: Path,
        extra_delay: float = 0.0,
    ) -> threading.Thread:
        """Spawn a helper thread that sends SIGINT to the parent once child is ready.

        WHY a background thread (instead of os.kill in the test body): the
        test body needs to be blocked inside run() -> proc.wait() when the
        signal arrives, because that's the codepath we're exercising.
        If we sent SIGINT before calling run(), KeyboardInterrupt would raise
        before the subprocess was even spawned. Issuing the signal from a
        separate thread lets the main thread make forward progress to the
        wait() call and THEN receive the signal at the right moment.

        extra_delay gives the SIGKILL-escalation test a way to let several
        SIGINT_TIMEOUT_SECONDS windows elapse before the signal arrives, if
        that's ever needed -- today it's effectively 0.
        """

        # daemon=True: if the test aborts for an unrelated reason, we don't
        # want a lingering helper thread to keep the interpreter alive.
        def _fire() -> None:
            # Poll for the ready marker but ALWAYS send SIGINT afterwards,
            # even if the marker never appeared. If we raised here instead,
            # the background thread would die silently (threads don't fail
            # the parent test) AND the parent would stay blocked inside
            # run() -> proc.wait() forever because no signal ever arrived.
            # Better: send the signal anyway, let the test body observe
            # what happened, and assert on the marker's existence post-hoc.
            try:
                TestRealSignalForwarding._wait_for_ready(ready_path)
            except AssertionError:
                # Marker never appeared. Either the child is slow to
                # install its handler, or it died before doing so. Send
                # SIGINT anyway to unblock the parent; the test body's
                # post-run assertions will surface the real problem.
                pass
            if extra_delay > 0:
                time.sleep(extra_delay)
            # signal.SIGINT to our own PID is the POSIX equivalent of the user
            # pressing Ctrl-C at the terminal: the kernel routes it to the
            # main thread, which raises KeyboardInterrupt inside proc.wait().
            os.kill(os.getpid(), signal.SIGINT)

        thread = threading.Thread(target=_fire, daemon=True)
        thread.start()
        return thread

    def test_run_forwards_sigint_to_child(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SIGINT received by the parent is forwarded to the child process.

        Pins two properties of the production forwarding path:
          (a) The child's OWN SIGINT handler fires -- proved by the marker
              file the handler writes. If the signal weren't forwarded, the
              child would either outlive the parent (orphan) or be killed
              without running user code.
          (b) The child exits within the forward-timeout window so SIGKILL
              escalation is never reached -- proved by the elapsed time
              being well under the patched timeout.
        """
        import clickwork.process
        from clickwork.process import run

        # Patch the forward timeout to 1s (default is 10s). If signal
        # forwarding ever regresses, the test fails in ~1s instead of
        # ~10s, which keeps CI debug turnaround fast. The graceful path
        # should never hit this timeout anyway -- the child exits on its
        # SIGINT handler in milliseconds -- so the patch is transparent
        # when the production code is correct.
        monkeypatch.setattr(clickwork.process, "SIGINT_TIMEOUT_SECONDS", 1.0)

        # Separate paths for the two independent pieces of state keeps the
        # poll logic trivial: existence alone is a meaningful signal.
        received_path = tmp_path / "child-received-sigint.txt"
        ready_path = tmp_path / "child-ready.txt"

        sender = self._send_sigint_after_ready(ready_path)

        try:
            start = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                # run() always re-raises KeyboardInterrupt after forwarding --
                # that's the semantic we're testing. The child's exit code (0)
                # is observed internally but never surfaced as a return value
                # on this codepath; the raised KI is how the caller knows.
                run(
                    [
                        sys.executable,
                        "-c",
                        self._GRACEFUL_CHILD_SCRIPT,
                        str(received_path),
                        str(ready_path),
                    ]
                )
            elapsed = time.monotonic() - start
        finally:
            # Always wait for the sender thread to exit -- even if the
            # pytest.raises expectation wasn't met. Without the try/finally,
            # an unexpected success path would leave the daemon thread
            # alive and its os.kill(SIGINT) could land on a later test,
            # causing cascading spurious failures.
            sender.join(timeout=1.0)
            assert not sender.is_alive(), "signal-sender thread did not exit cleanly"

        # (a) The child observably received and handled SIGINT.
        assert received_path.exists(), "child never wrote its received-marker file"
        assert received_path.read_text() == "received-sigint"

        # (b) Graceful path -- well below the patched 1s forward timeout.
        # The 0.5s bound is generous: the child's SIGINT handler exits
        # immediately, so this should complete in well under 100ms in
        # practice.
        assert elapsed < 0.9, f"child exit took {elapsed:.2f}s; expected <0.9s on the graceful path"

    def test_run_sigkill_escalation_on_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A child that catches SIGINT but refuses to exit is killed with SIGKILL.

        Pins three properties of the escalation path:
          (a) Parent forwards SIGINT first (the child's handler records it).
          (b) After SIGINT_TIMEOUT_SECONDS with no exit, parent escalates.
          (c) Child's final exit status reflects SIGKILL (negative 9 on POSIX,
              per subprocess convention when a child is terminated by a signal).

        The production timeout is SIGINT_TIMEOUT_SECONDS = 10s. Running the
        test at that value would make this one test a ~10s wallclock spike;
        monkeypatching to a small value keeps the full suite fast while
        still exercising every branch of the escalation code.
        """
        import clickwork.process as process_module
        from clickwork.process import run

        # WHY monkeypatch a module constant instead of passing a kwarg:
        # process.run() reads SIGINT_TIMEOUT_SECONDS from the module at call
        # time; there is no per-call timeout kwarg today (and exposing one
        # is out of scope for this coverage-only issue). Patching the
        # attribute is the least-invasive way to drive the escalation path
        # in bounded time without modifying the production API.
        monkeypatch.setattr(process_module, "SIGINT_TIMEOUT_SECONDS", 0.3)

        received_path = tmp_path / "child-received-sigint.txt"
        ready_path = tmp_path / "child-ready.txt"

        # Capture the real Popen instance so we can inspect returncode after
        # KeyboardInterrupt unwinds. We preserve the real Popen semantics by
        # delegating to the original; we're only snooping, not replacing.
        captured: dict[str, subprocess.Popen[bytes]] = {}
        real_popen = subprocess.Popen

        def _snooping_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            proc: subprocess.Popen[bytes] = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            captured["proc"] = proc
            return proc

        monkeypatch.setattr(subprocess, "Popen", _snooping_popen)

        sender = self._send_sigint_after_ready(ready_path)

        try:
            start = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                run(
                    [
                        sys.executable,
                        "-c",
                        self._WEDGED_CHILD_SCRIPT,
                        str(received_path),
                        str(ready_path),
                    ]
                )
            elapsed = time.monotonic() - start
        finally:
            # Always join the sender thread, even if pytest.raises didn't
            # fire -- otherwise an unexpected success path leaves the
            # daemon alive and its os.kill(SIGINT) could land on a later
            # test, cascading spurious failures.
            sender.join(timeout=1.0)
            assert not sender.is_alive(), "signal-sender thread did not exit cleanly"

        # (a) SIGINT was delivered and the child's handler ran before the
        # escalation kicked in -- otherwise we haven't actually exercised
        # the "child caught SIGINT then ignored it" shape we care about.
        assert received_path.exists(), "child never wrote its received-marker file"
        assert received_path.read_text() == "received-sigint-but-ignoring"

        # (b) Elapsed time sits on the right side of the escalation window:
        # at least the (patched) timeout must have elapsed, and the whole
        # thing must still complete quickly relative to the suite.
        assert elapsed >= 0.3, (
            f"escalation happened too fast ({elapsed:.2f}s < 0.3s timeout) -- "
            "did SIGINT_TIMEOUT_SECONDS fail to apply?"
        )
        assert (
            elapsed < 2.0
        ), f"escalation took {elapsed:.2f}s; expected <2s with a 0.3s patched timeout"

        # (c) The child was terminated by SIGKILL. On POSIX, subprocess
        # reports "killed by signal N" as returncode == -N. SIGKILL is 9.
        proc = captured["proc"]
        # By the time run() re-raises KeyboardInterrupt, the parent has
        # already awaited the killed child -- returncode is populated.
        assert proc.returncode == -signal.SIGKILL, (
            f"child exited with {proc.returncode}, expected -{signal.SIGKILL} "
            "(SIGKILL). If the child exited cleanly, the escalation path "
            "was never reached."
        )

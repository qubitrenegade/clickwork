"""Subprocess helpers for clickwork commands.

Three main functions:
- run(cmd): Execute a mutating command (deploy, build, push). Streams output
  in real-time, raises CliProcessError on failure, respects --dry-run.
- capture(cmd): Execute a read-only command and return stripped stdout. Always
  runs, even in dry-run mode, because commands need the data to proceed.
- run_with_confirm(cmd, message): Prompt before executing a destructive command.
  Combines confirmation + execution in one call.

All accept argv-style lists only (never strings) to prevent shell injection.
Secrets should be passed via the env parameter, not as argv arguments, because
argv is visible in `ps` output.

Signal handling: when the user presses Ctrl-C, the framework forwards SIGINT to
the child process, waits for it to exit, then re-raises KeyboardInterrupt so the
caller sees the interruption only after the child has had a chance to clean up.
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess

from clickwork._types import CliProcessError
from clickwork.prompts import confirm as _prompt_confirm

logger = logging.getLogger("clickwork")

# How long to wait for a child process to exit after forwarding SIGINT
# before escalating to SIGKILL. Long enough for graceful shutdown of most
# deploy/build commands, short enough to not leave the user staring at a
# frozen terminal.
SIGINT_TIMEOUT_SECONDS = 10


def _validate_cmd(cmd: list[str] | str) -> None:
    """Reject string commands to prevent shell injection.

    Accepting a raw string like "echo hello" would require shell=True, which
    opens the door to injection (e.g., "echo hello; rm -rf /"). Enforcing a
    list forces callers to be explicit about each argument boundary.

    Args:
        cmd: The command to validate. Must be a ``list[str]``; raises if it
            is a string, tuple, or any other type.

    Raises:
        TypeError: If cmd is not a list.
    """
    if not isinstance(cmd, list):
        raise TypeError(
            f"cmd must be a list, not {type(cmd).__name__}. Got: {cmd!r}. "
            "Use ['echo', 'hello'] instead of 'echo hello' to prevent shell injection."
        )


def _build_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Merge extra env vars with os.environ, or return None.

    Returning None (not an empty dict) when no extra vars are provided lets
    subprocess inherit the full parent environment via the default env=None
    path, which is what most processes expect (PATH, HOME, etc.).

    Args:
        env: Additional environment variables to layer on top of os.environ,
            or None to use the inherited environment unchanged.

    Returns:
        A merged dict of os.environ plus any caller-supplied vars, or None
        if no extra vars were provided.
    """
    if env is not None:
        # Spread os.environ first so caller-supplied vars win on conflict.
        # This is the safest default: commands see all the usual env vars
        # plus whatever secrets the caller injected.
        return {**os.environ, **env}
    return None


def _format_cmd(cmd: list[str]) -> str:
    """Format a command list as a shell-ready string for display.

    On POSIX platforms, uses ``shlex.quote`` so each argument is rendered
    in a form suitable for pasting into a POSIX shell.  On Windows, uses
    ``subprocess.list2cmdline`` which follows cmd.exe quoting conventions.

    Args:
        cmd: The command as an argv list.

    Returns:
        A single string suitable for logging or dry-run output.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(arg) for arg in cmd)


def _wait_with_signal_forwarding(proc: subprocess.Popen) -> int:
    """Wait for a child process, forwarding SIGINT before re-raising.

    This preserves Ctrl-C semantics for long-running deploy/build commands:
    the child gets a chance to handle SIGINT and clean up before the parent
    aborts. Without this, Python would raise KeyboardInterrupt immediately and
    leave the child running in the background as an orphan.

    If the child does not exit within SIGINT_TIMEOUT_SECONDS after receiving
    SIGINT, the framework escalates to SIGKILL to prevent indefinite hangs
    (e.g., a child that catches and ignores SIGINT).

    Args:
        proc: The running subprocess to wait on.

    Returns:
        The process exit code (0 for success, non-zero for failure).

    Raises:
        KeyboardInterrupt: After forwarding SIGINT to the child and waiting
            for it to exit, so the caller sees the interruption only once
            the child has cleaned up.
    """
    try:
        # Block until the child exits normally.
        return proc.wait()
    except KeyboardInterrupt:
        # User pressed Ctrl-C. Tell the child to stop gracefully via SIGINT
        # (the same signal the terminal sent us), then wait for it to exit
        # before propagating the interruption upward.
        try:
            proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            # The child may already have exited by the time we forward SIGINT.
            pass
        try:
            proc.wait(timeout=SIGINT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # The child ignored SIGINT for too long. Escalate to SIGKILL so
            # we don't hang forever waiting for a process that won't exit.
            proc.kill()
            proc.wait()
        except (ProcessLookupError, OSError):
            # If the process is already gone, the important part is that we
            # still re-raise KeyboardInterrupt for the caller.
            pass
        raise



def _validate_stdin_params(
    stdin_text: str | None, stdin_bytes: bytes | None
) -> None:
    """Enforce mutual exclusivity between stdin_text and stdin_bytes.

    WHY two separate kwargs instead of one polymorphic stdin=str|bytes:
    self-documenting call sites. ``run(cmd, stdin_text=token)`` is
    unambiguous; ``run(cmd, stdin=token)`` forces the reader to check
    the type to know whether the child sees text or bytes. Splitting the
    parameter makes the intent explicit at every call site. The cost is
    this one-time validation that the caller didn't pass both.

    Args:
        stdin_text: Text payload for the child's stdin, or None.
        stdin_bytes: Binary payload for the child's stdin, or None.

    Raises:
        ValueError: If both parameters are set. Passing neither is fine
            (it means "don't attach anything to stdin").
    """
    if stdin_text is not None and stdin_bytes is not None:
        raise ValueError(
            "Pass stdin_text OR stdin_bytes, not both. "
            "Use stdin_text for UTF-8 strings; use stdin_bytes for raw bytes."
        )


def run(
    cmd: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    *,
    stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess | None:
    """Execute a command, streaming output in real-time.

    Args:
        cmd: Command as an argv list. Strings are rejected (TypeError).
        dry_run: If True, print the command but don't execute it.
        env: Extra environment variables merged with os.environ. Use this
            for secrets instead of putting them in cmd (argv is visible in ps).
        stdin_text: If set, pipe this string to the child's stdin (text mode,
            UTF-8 by default). Mutually exclusive with stdin_bytes.
        stdin_bytes: If set, pipe these raw bytes to the child's stdin
            (binary mode). Mutually exclusive with stdin_text.

    Returns:
        subprocess.CompletedProcess on success, or None if dry_run=True.

    Raises:
        CliProcessError: If the command exits with non-zero status.
        TypeError: If cmd is a string instead of a list.
        ValueError: If both stdin_text and stdin_bytes are set.

    Passing data on stdin (secrets-via-stdin):
        Many tools accept a secret on stdin so it never appears in argv
        (which is visible in ``ps`` output). Common examples:

        - ``wrangler secret put API_KEY`` reads the secret from stdin
        - ``gh auth login --with-token`` reads the token from stdin
        - ``docker login --password-stdin`` reads the password from stdin

        Use ``stdin_text`` for UTF-8 text (the common case), or
        ``stdin_bytes`` for raw binary payloads. Never pass secrets via
        ``cmd`` (argv is world-readable in ``ps``); prefer ``env`` for
        env-var-based secrets and ``stdin_text`` for stdin-based ones.

        Example::

            ctx.run(["wrangler", "secret", "put", "API_KEY"], stdin_text=token)
    """
    _validate_cmd(cmd)
    # Validate stdin mutual exclusivity BEFORE the dry_run short-circuit so
    # callers catch the programming mistake in both live and dry-run modes.
    _validate_stdin_params(stdin_text, stdin_bytes)

    if dry_run:
        # Log what would have run so dry-run mode is still informative.
        logger.info("[dry-run] Would execute: %s", _format_cmd(cmd))
        return None

    full_env = _build_env(env)

    # Decide whether we need to attach a stdin pipe. Only one of stdin_text
    # or stdin_bytes can be set (enforced above); "stdin_payload" captures
    # whichever one it is, and "stdin_is_text" tells us what mode Popen
    # should open its stdin stream in.
    stdin_payload: str | bytes | None
    if stdin_text is not None:
        stdin_payload = stdin_text
        stdin_is_text = True
    elif stdin_bytes is not None:
        stdin_payload = stdin_bytes
        stdin_is_text = False
    else:
        stdin_payload = None
        stdin_is_text = False  # unused when stdin_payload is None

    # Only open a pipe when we actually have data to write. When no stdin
    # payload is set, we inherit the parent's stdin (the existing behavior)
    # so interactive tools that read from the TTY still work.
    popen_kwargs: dict = {"env": full_env, "shell": False}
    if stdin_payload is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
        # text=True makes proc.stdin a text stream; text=False (the default)
        # makes it a binary stream. We set it explicitly so it tracks the
        # payload type we're about to write.
        popen_kwargs["text"] = stdin_is_text

    # Use Popen instead of subprocess.run so we can explicitly forward SIGINT
    # to the child and wait for it before propagating KeyboardInterrupt.
    # subprocess.run() has no hook for signal interception.
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        # The binary doesn't exist. This is a user/environment error (like
        # PrerequisiteError), not a framework bug. Surface it as exit code 1
        # via CliProcessError with an actionable message.
        raise CliProcessError(
            subprocess.CalledProcessError(
                returncode=127, cmd=cmd, stderr=f"Command not found: {cmd[0]}"
            )
        )

    # If we opened a stdin pipe, write the payload and close it so the child
    # sees EOF and can proceed. We do this BEFORE _wait_with_signal_forwarding
    # so the child isn't blocked waiting for stdin input we haven't sent yet.
    #
    # WHY manual write-and-close instead of proc.communicate(input=...):
    # communicate() internally calls proc.wait(), which bypasses our
    # _wait_with_signal_forwarding helper and therefore breaks Ctrl-C
    # SIGINT forwarding to the child. A manual write-then-close keeps the
    # existing wait path intact -- the child sees the stdin payload and
    # EOF, and the parent still forwards SIGINT on KeyboardInterrupt.
    #
    # Risk: if the child produces a huge stdout/stderr burst while we're
    # writing to stdin, the OS pipe buffer could fill and deadlock. In
    # practice, we don't capture stdout/stderr (they inherit the parent's
    # file descriptors), so the child's output streams freely to the
    # terminal and never fills a buffer we control. And stdin payloads
    # for this use case (secrets, tokens) are small enough to fit in a
    # single pipe buffer write, so the write itself won't block either.
    if stdin_payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_payload)  # type: ignore[arg-type]
        finally:
            # Close in a finally so a write failure still sends EOF -- without
            # it, a child waiting on stdin would hang forever.
            proc.stdin.close()

    returncode = _wait_with_signal_forwarding(proc)
    if returncode != 0:
        raise CliProcessError(
            subprocess.CalledProcessError(returncode=returncode, cmd=cmd)
        )
    return subprocess.CompletedProcess(cmd, returncode)


def capture(
    cmd: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    """Execute a command and return its stdout as a stripped string.

    Unlike run(), capture() always executes even in dry-run mode because
    commands typically need the captured data to make decisions (e.g.,
    listing resources before deciding what to deploy).

    Args:
        cmd: Command as an argv list.
        dry_run: Ignored -- capture always executes. Parameter exists for
            API consistency so callers can pass ctx.dry_run uniformly.
        env: Extra environment variables merged with os.environ.

    Returns:
        The command's stdout, stripped of leading/trailing whitespace.

    Raises:
        CliProcessError: If the command exits with non-zero status.
        TypeError: If cmd is a string instead of a list.
    """
    _validate_cmd(cmd)

    _ = dry_run  # accepted for API consistency; capture always executes

    full_env = _build_env(env)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, env=full_env,
            shell=False,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        # Same treatment as run(): missing binary is a user/environment
        # error (exit 1), not a framework bug (exit 2).
        raise CliProcessError(
            subprocess.CalledProcessError(
                returncode=127, cmd=cmd, stderr=f"Command not found: {cmd[0]}"
            )
        )
    except subprocess.CalledProcessError as e:
        raise CliProcessError(e) from e


def run_with_confirm(
    cmd: list[str],
    message: str,
    yes: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    *,
    stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess | None:
    """Prompt for confirmation, then execute a destructive command.

    Combines confirmation + execution so command authors don't forget either
    step. Uses prompts.confirm() directly for TTY-aware interactive prompts.
    When yes=True the prompt is bypassed entirely (--yes flag behaviour).

    Args:
        cmd: Command as an argv list.
        message: Confirmation prompt (e.g., "Deploy to production?").
        yes: If True, skip the prompt (--yes flag).
        dry_run: If True, print the command but don't execute it.
        env: Extra environment variables merged with os.environ.
        stdin_text: If set, pipe this string to the child's stdin (text mode).
            Mutually exclusive with stdin_bytes. See ``run()`` for the
            secrets-via-stdin pattern this supports.
        stdin_bytes: If set, pipe these raw bytes to the child's stdin (binary
            mode). Mutually exclusive with stdin_text.

    Returns:
        subprocess.CompletedProcess on success, or None if denied/dry-run.

    Raises:
        ValueError: If both stdin_text and stdin_bytes are set.

    See Also:
        ``run()`` -- full documentation of the secrets-via-stdin pattern
        (``wrangler secret put``, ``gh auth login --with-token``,
        ``docker login --password-stdin``).
    """
    _validate_cmd(cmd)
    # Validate stdin arguments here too so callers get the same early
    # ValueError they'd get from run(), rather than a confusing error that
    # only surfaces after the confirmation prompt has been answered.
    _validate_stdin_params(stdin_text, stdin_bytes)

    # Delegate to the framework's TTY-aware confirm() from prompts.py.
    # When yes=True, confirm() returns True immediately and skips the prompt.
    # When stdin is not a TTY (piped/CI), confirm() returns False (safe deny).
    if not _prompt_confirm(message, yes=yes):
        logger.info("Cancelled: %s", _format_cmd(cmd))
        return None

    # Delegate to run() so dry-run, env passing, stdin piping, and signal
    # forwarding are all handled consistently in one place.
    return run(
        cmd,
        dry_run=dry_run,
        env=env,
        stdin_text=stdin_text,
        stdin_bytes=stdin_bytes,
    )

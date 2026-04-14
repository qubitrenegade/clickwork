"""Subprocess helpers for qbrd-tools commands.

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

from qbrd_tools._types import CliProcessError
from qbrd_tools.prompts import confirm as _prompt_confirm

logger = logging.getLogger("qbrd_tools")

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



def run(
    cmd: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    """Execute a command, streaming output in real-time.

    Args:
        cmd: Command as an argv list. Strings are rejected (TypeError).
        dry_run: If True, print the command but don't execute it.
        env: Extra environment variables merged with os.environ. Use this
            for secrets instead of putting them in cmd (argv is visible in ps).

    Returns:
        subprocess.CompletedProcess on success, or None if dry_run=True.

    Raises:
        CliProcessError: If the command exits with non-zero status.
        TypeError: If cmd is a string instead of a list.
    """
    _validate_cmd(cmd)

    if dry_run:
        # Log what would have run so dry-run mode is still informative.
        logger.info("[dry-run] Would execute: %s", _format_cmd(cmd))
        return None

    full_env = _build_env(env)

    # Use Popen instead of subprocess.run so we can explicitly forward SIGINT
    # to the child and wait for it before propagating KeyboardInterrupt.
    # subprocess.run() has no hook for signal interception.
    try:
        proc = subprocess.Popen(cmd, env=full_env, shell=False)
    except FileNotFoundError:
        # The binary doesn't exist. This is a user/environment error (like
        # PrerequisiteError), not a framework bug. Surface it as exit code 1
        # via CliProcessError with an actionable message.
        raise CliProcessError(
            subprocess.CalledProcessError(
                returncode=127, cmd=cmd, stderr=f"Command not found: {cmd[0]}"
            )
        )
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
    except subprocess.CalledProcessError as e:
        raise CliProcessError(e) from e


def run_with_confirm(
    cmd: list[str],
    message: str,
    yes: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
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

    Returns:
        subprocess.CompletedProcess on success, or None if denied/dry-run.
    """
    _validate_cmd(cmd)

    # Delegate to the framework's TTY-aware confirm() from prompts.py.
    # When yes=True, confirm() returns True immediately and skips the prompt.
    # When stdin is not a TTY (piped/CI), confirm() returns False (safe deny).
    if not _prompt_confirm(message, yes=yes):
        logger.info("Cancelled: %s", _format_cmd(cmd))
        return None

    # Delegate to run() so dry-run, env passing, and signal forwarding are
    # handled consistently in one place.
    return run(cmd, dry_run=dry_run, env=env)

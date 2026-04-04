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
import sys

from qbrd_tools._types import CliProcessError

logger = logging.getLogger("qbrd_tools")


def _validate_cmd(cmd: list[str] | str) -> None:
    """Reject string commands to prevent shell injection.

    Accepting a raw string like "echo hello" would require shell=True, which
    opens the door to injection (e.g., "echo hello; rm -rf /"). Enforcing a
    list forces callers to be explicit about each argument boundary.
    """
    if isinstance(cmd, str):
        raise TypeError(
            f"cmd must be a list, not a string. Got: {cmd!r}. "
            "Use ['echo', 'hello'] instead of 'echo hello' to prevent shell injection."
        )


def _build_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Merge extra env vars with os.environ, or return None.

    Returning None (not an empty dict) when no extra vars are provided lets
    subprocess inherit the full parent environment via the default env=None
    path, which is what most processes expect (PATH, HOME, etc.).
    """
    if env:
        # Spread os.environ first so caller-supplied vars win on conflict.
        # This is the safest default: commands see all the usual env vars
        # plus whatever secrets the caller injected.
        return {**os.environ, **env}
    return None


def _format_cmd(cmd: list[str]) -> str:
    """Format a command for human-readable display (properly quoted).

    shlex.quote wraps each argument in single quotes if it contains spaces or
    special characters, so the printed command can be pasted into a shell and
    reproduce the same invocation.
    """
    return " ".join(shlex.quote(arg) for arg in cmd)


def _wait_with_signal_forwarding(proc: subprocess.Popen) -> int:
    """Wait for a child process, forwarding SIGINT before re-raising.

    This preserves Ctrl-C semantics for long-running deploy/build commands:
    the child gets a chance to handle SIGINT and clean up before the parent
    aborts. Without this, Python would raise KeyboardInterrupt immediately and
    leave the child running in the background as an orphan.
    """
    try:
        # Block until the child exits normally.
        return proc.wait()
    except KeyboardInterrupt:
        # User pressed Ctrl-C. Tell the child to stop gracefully via SIGINT
        # (the same signal the terminal sent us), then wait for it to exit
        # before propagating the interruption upward.
        proc.send_signal(signal.SIGINT)
        proc.wait()
        raise


# Pluggable confirmation function. Overridden by create_cli() to use the
# framework's confirm() with TTY detection. Tests can patch this directly.
def _confirm_fn(message: str, yes: bool = False) -> bool:
    """Default confirmation: always deny unless yes=True.

    This safe default means un-patched calls never unexpectedly execute
    destructive commands. create_cli() replaces this with an interactive
    prompt so real CLI invocations ask the user.
    """
    return yes


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
    proc = subprocess.Popen(cmd, env=full_env, shell=False)
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

    # dry_run is intentionally unused: capturing output is a read-only
    # operation, so it is always safe to execute even in dry-run mode.
    # The parameter exists purely for call-site ergonomics.
    _ = dry_run

    full_env = _build_env(env)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, env=full_env,
            shell=False,
        )
        # strip() removes trailing newlines from commands like `echo` as well
        # as any leading/trailing whitespace that the calling code shouldn't
        # have to clean up itself.
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
    step. The confirmation function is pluggable (_confirm_fn) -- create_cli()
    replaces it with the framework's TTY-aware confirm().

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

    # _confirm_fn is module-level so tests can patch it directly.
    # When yes=True the default implementation returns True immediately,
    # bypassing any interactive prompt that create_cli() might install.
    if not _confirm_fn(message, yes=yes):
        logger.info("Cancelled: %s", _format_cmd(cmd))
        return None

    # Delegate to run() so dry-run, env passing, and signal forwarding are
    # handled consistently in one place.
    return run(cmd, dry_run=dry_run, env=env)

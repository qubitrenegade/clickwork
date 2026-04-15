"""User confirmation prompts with safety defaults.

Two prompt types:
- confirm(): "Continue? [y/N]" -- for reversible actions
- confirm_destructive(): "Type 'yes' to confirm" -- for dangerous operations

Safety rules:
- --yes flag bypasses all prompts (for scripted/CI use)
- Non-TTY stdin (piped, redirected) auto-denies to prevent hangs
- Default answer is always "no" (safe default)
"""
from __future__ import annotations

import sys


def _is_tty() -> bool:
    """Check whether stdin is connected to a real interactive terminal.

    Returns False when input is piped (``echo y | orbit-admin deploy``)
    or when running in CI without a terminal. This is the check that
    prevents confirmation prompts from hanging in automation.

    Returns:
        True if stdin has an ``isatty`` method and it returns True;
        False otherwise.
    """
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def _read_response(prompt: str) -> str | None:
    """Read a prompt response, returning None on EOF.

    EOFError (piped input ended) returns None so the caller treats it as
    a denial. KeyboardInterrupt is NOT caught here -- Ctrl-C should abort
    the entire operation, not silently answer "no" to the current prompt.
    Click's Abort handler will produce a clean "Aborted!" message.
    """
    try:
        return input(prompt)
    except EOFError:
        return None


def confirm(message: str, yes: bool = False) -> bool:
    """Ask the user a yes/no question. Default is no.

    Args:
        message: The question to display.
        yes: If True, skip the prompt and return True (--yes flag).

    Returns:
        True if the user confirmed, False otherwise.
    """
    if yes:
        return True
    if not _is_tty():
        return False

    response = _read_response(f"{message} [y/N] ")
    if response is None:
        return False
    response = response.strip().lower()
    return response in ("y", "yes")


def confirm_destructive(message: str, yes: bool = False) -> bool:
    """Ask the user to type 'yes' to confirm a dangerous operation.

    More strict than confirm() -- requires the full word 'yes', not just 'y'.
    This adds friction for operations like database drops, force pushes, etc.

    Args:
        message: Description of the destructive action.
        yes: If True, skip the prompt and return True (--yes flag).

    Returns:
        True only if the user typed 'yes' (case-insensitive).
    """
    if yes:
        return True
    if not _is_tty():
        return False

    response = _read_response(f"{message}\nType 'yes' to confirm: ")
    if response is None:
        return False
    response = response.strip().lower()
    return response == "yes"

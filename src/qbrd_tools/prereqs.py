"""Prerequisite checking for CLI commands.

Commands call require("docker") or require("gh", authenticated=True)
at the top of their implementation. If the binary isn't on PATH (or isn't
authenticated when requested), the framework exits with a clear error
message telling the user what to fix.

This catches missing/misconfigured tools early -- before the command does
any work -- instead of failing cryptically when a subprocess call fails
halfway through a deploy.
"""
from __future__ import annotations

import shutil
import subprocess
import sys


# Known auth-check commands for common tools. Maps binary name to the
# argv that returns exit 0 when authenticated. Extensible by consumers.
AUTH_CHECKS: dict[str, list[str]] = {
    "gh": ["gh", "auth", "status"],
    "gcloud": ["gcloud", "auth", "print-access-token"],
    "aws": ["aws", "sts", "get-caller-identity"],
}


def require(binary: str, authenticated: bool = False) -> None:
    """Ensure a binary is available on PATH, optionally checking authentication.

    Exits with code 1 (user error) and a descriptive message if the binary
    is not found or not authenticated. Commands call this at the top of
    their function body so failures surface before any work is done:

        def deploy(ctx):
            ctx.require("gh", authenticated=True)
            ...

    Args:
        binary: The name of the binary to check (e.g., ``"docker"``, ``"gh"``).
        authenticated: If True, also verify the tool is authenticated using
            the command registered in AUTH_CHECKS. Tools without a known auth
            check produce a warning but do not cause a hard failure.

    Returns:
        None. Exits the process with code 1 on failure instead of raising,
        because missing prerequisites are a user configuration error, not a
        programmer error.
    """
    if shutil.which(binary) is None:
        print(
            f"Error: required tool '{binary}' is not installed or not on PATH.\n"
            f"Install it and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if authenticated:
        auth_cmd = AUTH_CHECKS.get(binary)
        if auth_cmd is None:
            print(
                f"Warning: no auth check known for '{binary}'. "
                f"Skipping authentication verification.",
                file=sys.stderr,
            )
            return

        try:
            subprocess.run(
                auth_cmd, capture_output=True, check=True, shell=False,
            )
        except subprocess.CalledProcessError:
            print(
                f"Error: '{binary}' is not authenticated.\n"
                f"Run the appropriate login command and try again.",
                file=sys.stderr,
            )
            sys.exit(1)

"""Prerequisite checking for CLI commands.

Commands call require("docker") or require("gh", authenticated=True)
at the top of their implementation. If the binary isn't on PATH (or isn't
authenticated when requested), require() raises PrerequisiteError. The
framework's error handler catches it and exits with a clear message
telling the user what to fix.

This catches missing/misconfigured tools early -- before the command does
any work -- instead of failing cryptically when a subprocess call fails
halfway through a deploy.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from qbrd_tools._types import PrerequisiteError

logger = logging.getLogger("qbrd_tools")

# Known auth-check commands for common tools. Maps binary name to the
# argv that returns exit 0 when authenticated. Extensible by consumers.
AUTH_CHECKS: dict[str, list[str]] = {
    "gh": ["gh", "auth", "status"],
    "gcloud": ["gcloud", "auth", "print-access-token"],
    "aws": ["aws", "sts", "get-caller-identity"],
}


def require(binary: str, authenticated: bool = False) -> None:
    """Ensure a binary is available on PATH, optionally checking authentication.

    Raises PrerequisiteError if the binary is not found or not authenticated.
    Commands call this at the top of their function body so failures surface
    before any work is done:

        def deploy(ctx):
            ctx.require("gh", authenticated=True)
            ...

    Args:
        binary: The name of the binary to check (e.g., ``"docker"``, ``"gh"``).
        authenticated: If True, also verify the tool is authenticated using
            the command registered in AUTH_CHECKS. Tools without a known auth
            check produce a warning but do not cause a hard failure.

    Raises:
        PrerequisiteError: If the binary is missing or not authenticated.
    """
    if shutil.which(binary) is None:
        raise PrerequisiteError(
            f"Required tool '{binary}' is not installed or not on PATH. "
            f"Install it and try again."
        )

    if authenticated:
        auth_cmd = AUTH_CHECKS.get(binary)
        if auth_cmd is None:
            logger.warning(
                "No auth check known for '%s'. "
                "Skipping authentication verification.",
                binary,
            )
            return

        try:
            subprocess.run(
                auth_cmd, capture_output=True, check=True, shell=False,
            )
        except subprocess.CalledProcessError:
            raise PrerequisiteError(
                f"'{binary}' is not authenticated. "
                f"Run the appropriate login command and try again."
            )

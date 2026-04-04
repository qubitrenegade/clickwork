"""Logging setup for qbrd-tools CLIs.

Provides a single setup_logging() function that configures a named logger
with the correct verbosity level and a consistent output format. Color
support is automatic based on terminal detection.

The verbosity mapping:
  - Default (no flags): WARNING -- only problems
  - -v (verbose=1): INFO -- progress updates
  - -vv (verbose=2): DEBUG -- implementation details
  - --quiet: ERROR -- only failures
"""
from __future__ import annotations

import logging
import sys


def setup_logging(
    verbose: int = 0,
    quiet: bool = False,
    name: str = "qbrd_tools",
) -> logging.Logger:
    """Configure and return a logger with the appropriate verbosity level.

    Args:
        verbose: How many -v flags were passed (0, 1, or 2+).
        quiet: Whether --quiet was passed. Overrides verbose.
        name: Logger name, typically the CLI project name (e.g., "orbit-admin").

    Returns:
        A configured logging.Logger instance.
    """
    # --quiet always wins over --verbose (they're mutually exclusive at the
    # CLI level, but we handle it defensively here too).
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        # No handler yet -- create one and attach it.
        # This is the common path on first call (e.g., a fresh CLI invocation).
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)

        # Use color if stderr is a real terminal (not piped/redirected).
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        if use_color:
            fmt = "\033[2m%(name)s\033[0m %(message)s"
        else:
            fmt = "%(name)s %(message)s"

        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
    else:
        # Handler already exists (e.g., setup_logging called again with a
        # different verbosity in the same process, or in tests).  Update the
        # existing handler's level so the new verbosity takes effect rather
        # than being silently ignored.
        for handler in logger.handlers:
            handler.setLevel(level)

    return logger

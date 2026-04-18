"""Logging setup for clickwork CLIs.

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
    name: str = "clickwork",
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

    def configure_logger(logger: logging.Logger) -> None:
        """Attach or update a stderr handler with the requested verbosity."""
        logger.setLevel(level)

        # Use color if stderr is a real terminal (not piped/redirected).
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        if use_color:
            fmt = "\033[2m%(name)s\033[0m %(message)s"
        else:
            fmt = "%(name)s %(message)s"

        formatter = logging.Formatter(fmt)
        handler = next(
            (
                existing_handler
                for existing_handler in logger.handlers
                if isinstance(existing_handler, logging.StreamHandler)
                and getattr(existing_handler, "stream", None) is sys.stderr
            ),
            None,
        )
        if handler is None:
            handler = logging.StreamHandler(sys.stderr)
            logger.addHandler(handler)

        handler.setLevel(level)
        handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    configure_logger(logger)

    # Framework modules log to the shared clickwork namespace, so keep that
    # logger aligned with the CLI logger as well.
    if name != "clickwork":
        configure_logger(logging.getLogger("clickwork"))

    return logger

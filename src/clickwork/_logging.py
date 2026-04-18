"""Logging setup for clickwork CLIs.

This module is the *host-friendly* logging entry point for clickwork. The
name of the game here: **never silently override a host application's
logging configuration.** clickwork is a library as well as a CLI
framework; when a host app (think: an orbit-admin plugin imported from
another tool, or a test harness that calls ``clickwork.http.get`` directly)
has already run ``logging.basicConfig()`` or installed its own root
handler, clickwork must NOT attach a second handler that duplicates every
log line.

## The rules

1.  Every ``clickwork`` (and descendant) logger gets a ``NullHandler``
    baseline so stdlib's "no handler configured" warning never fires and
    records aren't dropped *just because* no handler exists.
2.  ``propagate=True`` (stdlib default) is preserved so records bubble up
    to the root logger, where the host's handler -- if any -- picks them
    up.
3.  ``setup_logging()`` only attaches a ``StreamHandler`` when the root
    logger has zero handlers. If the host has configured root logging,
    we leave it alone. This is the core of the BREAKING change vs 0.2:
    previously clickwork unconditionally attached its own handler,
    causing duplicate output in embedded contexts.
4.  clickwork *never* calls ``logging.basicConfig()`` or touches the root
    logger's handler list. That's the host's prerogative.

## Verbosity mapping (unchanged from 0.2)

  - Default (no flags): WARNING -- only problems
  - -v (verbose=1): INFO -- progress updates
  - -vv (verbose=2): DEBUG -- implementation details
  - --quiet: ERROR -- only failures
"""

from __future__ import annotations

import logging
import sys

# ---------------------------------------------------------------------------
# Module-load baseline
# ---------------------------------------------------------------------------
#
# As soon as this module is imported, attach a ``NullHandler`` to the
# top-level ``clickwork`` logger. This is the pattern recommended by the
# Python logging HOWTO for libraries: it prevents the "No handlers could
# be found for logger X" warning on stderr when a host hasn't configured
# logging at all, without assuming anything about what the host WANTS.
#
# Children (``clickwork.http``, ``clickwork.discovery``, ...) inherit via
# propagation and reach this handler implicitly; we don't need to attach
# a separate ``NullHandler`` to each descendant.
_clickwork_logger = logging.getLogger("clickwork")
# Idempotent NullHandler attach: in reload-heavy test harnesses (or any
# environment that re-imports clickwork), unconditionally appending here
# would accumulate duplicate NullHandlers across imports. A single
# NullHandler is enough -- they're no-op -- so skip the append when one
# is already on the logger.
if not any(isinstance(h, logging.NullHandler) for h in _clickwork_logger.handlers):
    _clickwork_logger.addHandler(logging.NullHandler())
# Explicit for clarity even though ``True`` is the stdlib default -- we
# want readers of this file to see, without spelunking, that records flow
# up to the root logger where a host handler (if installed) can pick them
# up.
_clickwork_logger.propagate = True


def _host_root_is_configured() -> bool:
    """Return True if the root logger already has any handler attached.

    This is the signal that a host app has opted into its own logging
    configuration (e.g., via ``logging.basicConfig()``, an explicit
    ``root.addHandler(...)``, or a framework like ``uvicorn`` /
    ``structlog`` that installs root handlers at import time).

    When this returns True, ``setup_logging()`` must NOT attach its own
    ``StreamHandler`` -- doing so would produce duplicate output for
    every record that propagates up to the configured root handler.
    """
    return len(logging.getLogger().handlers) > 0


def setup_logging(
    verbose: int = 0,
    quiet: bool = False,
    name: str = "clickwork",
) -> logging.Logger:
    """Configure the clickwork logging stack for a CLI invocation.

    Host-preserving behavior (new in 1.0): if the root logger already has
    a handler attached (e.g., the embedding application called
    ``logging.basicConfig()`` or configured its own), this function will
    NOT attach a stderr handler. It will still set the verbosity level
    on the clickwork-namespace loggers so ``-v`` / ``-q`` continue to
    work as users expect, but emission is delegated to the host's root
    handler via propagation.

    If no host handler is installed, ``setup_logging()`` attaches a
    stderr ``StreamHandler`` with clickwork's format so standalone CLI
    use still prints records.

    The public signature is unchanged from 0.2, so 0.2-era call sites
    continue to work -- only the side effects differ.

    Args:
        verbose: How many -v flags were passed (0, 1, or 2+).
        quiet: Whether --quiet was passed. Overrides verbose.
        name: Logger name, typically the CLI project name (e.g.,
            ``"orbit-admin"``). clickwork also mirrors the level onto
            the shared ``"clickwork"`` logger so framework-internal
            modules (``clickwork.http``, ``clickwork.discovery``, ...)
            honor the same verbosity.

    Returns:
        The ``logging.Logger`` instance for ``name``.
    """
    # --quiet always wins over --verbose (they're mutually exclusive at
    # the CLI level, but handle it defensively here too).
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    # Decide ONCE whether the host has configured root. We snapshot this
    # up front because the decision applies symmetrically to the named
    # CLI logger and the shared "clickwork" logger -- we don't want to
    # attach to one and skip the other based on state drift mid-call.
    host_configured = _host_root_is_configured()

    def configure_logger(logger: logging.Logger) -> None:
        """Set level + baseline handlers for a clickwork-namespace logger.

        Always attaches a ``NullHandler`` so the "no handlers" warning
        can't fire even if a host clears the root logger between imports.
        Only attaches a ``StreamHandler`` when the host hasn't taken
        responsibility for root -- see ``_host_root_is_configured()``.
        """
        logger.setLevel(level)
        # Keep propagation on so records reach the host root handler
        # when one exists. This is the default, but restate it in case
        # 0.2-era callers (or tests) disabled propagation manually.
        logger.propagate = True

        # Ensure a NullHandler is present so stdlib never complains about
        # missing handlers. Idempotent: we only add one if there isn't
        # already a NullHandler on this logger.
        has_null = any(isinstance(h, logging.NullHandler) for h in logger.handlers)
        if not has_null:
            logger.addHandler(logging.NullHandler())

        if host_configured:
            # Host owns output. Before returning, evict any
            # clickwork-owned stderr ``StreamHandler`` that an earlier
            # bare-root call to ``setup_logging()`` may have attached.
            # Without this eviction, a sequence of
            #   setup_logging()              # bare root -> attach stderr handler
            #   logging.basicConfig()        # host takes over root
            #   setup_logging()              # still attached to our handler
            # produces duplicate output again (the clickwork stderr handler
            # AND the propagated record reaching the host's root handler).
            # We remove only stderr StreamHandlers -- NullHandler is fine
            # to leave in place (no output) and a host-attached handler on
            # the clickwork logger is not ours to evict.
            for existing in list(logger.handlers):
                if (
                    isinstance(existing, logging.StreamHandler)
                    and not isinstance(existing, logging.NullHandler)
                    and getattr(existing, "stream", None) is sys.stderr
                ):
                    logger.removeHandler(existing)
            # Records propagate up to the host's root handler. Do NOT
            # attach a StreamHandler -- that's what caused the
            # duplicate-output bug this rewrite exists to fix (#43).
            return

        # Standalone CLI mode: no host handler, so clickwork is
        # responsible for actually printing records. Attach a stderr
        # StreamHandler with the usual format, but do it idempotently so
        # repeated calls to ``setup_logging()`` don't stack handlers.
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        if use_color:
            fmt = "\033[2m%(name)s\033[0m %(message)s"
        else:
            fmt = "%(name)s %(message)s"
        formatter = logging.Formatter(fmt)

        # Find an existing stderr StreamHandler we previously attached so
        # re-invocations (e.g., nested test runs, or the CLI callback
        # running twice in-process) just update its level + format
        # rather than doubling up.
        stream_handler = next(
            (
                existing
                for existing in logger.handlers
                if isinstance(existing, logging.StreamHandler)
                and not isinstance(existing, logging.NullHandler)
                and getattr(existing, "stream", None) is sys.stderr
            ),
            None,
        )
        if stream_handler is None:
            stream_handler = logging.StreamHandler(sys.stderr)
            logger.addHandler(stream_handler)

        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    configure_logger(logger)

    # Mirror the level + handler policy onto the shared ``clickwork``
    # logger so framework modules (``clickwork.http``, ...) honor the
    # same verbosity. Skipped when the named CLI logger IS the clickwork
    # logger -- nothing to mirror.
    if name != "clickwork":
        configure_logger(logging.getLogger("clickwork"))

    return logger

"""Logging setup for clickwork CLIs.

This module is the *host-friendly* logging entry point for clickwork. The
name of the game here: **respect the host's root logging configuration.
The loggers we touch are the ``clickwork`` namespace and the CLI-named
logger the caller passes to ``setup_logging()`` (typically the project
name -- e.g. ``"orbit-admin"``).** clickwork is a library as well as a
CLI framework; when a host app (think: an orbit-admin plugin imported
from another tool, or a test harness that calls ``clickwork.http.get``
directly) has already run ``logging.basicConfig()`` or installed its
own root handler, clickwork must NOT attach a second stderr handler
that duplicates every log line.

Scope of clickwork's overrides: we DO set the ``propagate`` flag to
``True`` on the ``clickwork`` logger and any CLI-named logger, because
records flowing up to the root is how the host-preserving design
delivers output. A host that wants to break propagation intentionally
(e.g., it attaches its own handler to ``clickwork`` and sets
``propagate=False`` to avoid double-emission to root) should do that
AFTER calling ``setup_logging()``; the helper is meant for the common
case and documents this tradeoff rather than trying to autodetect
every host configuration pattern.

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
            honor the same verbosity. MUST be a non-empty string --
            ``""`` resolves to the root logger via
            ``logging.getLogger("")`` and configuring root would
            violate the host-preserving contract.

    Returns:
        The ``logging.Logger`` instance for ``name``.

    Raises:
        ValueError: If ``name`` is an empty string. Callers must pass
            a non-empty project name (e.g. ``"orbit-admin"``).
    """
    # Reject empty name up front so the rest of the function can
    # assume it's configuring a named, non-root logger. Without this
    # guard, ``setup_logging(name="")`` would silently start mutating
    # the root logger's handlers/level/propagate -- exactly the
    # embedding-host-owned state we've pledged not to touch.
    if not name:
        raise ValueError(
            "setup_logging(name=...) must be a non-empty string; "
            "logging.getLogger('') resolves to the root logger and "
            "configuring root would violate clickwork's "
            "host-preserving contract. Pass your project name "
            "(e.g. 'orbit-admin') instead."
        )
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
            # We identify OUR handlers by the ``_clickwork_owned``
            # attribute set at attach time below, NOT by ``handler.stream
            # is sys.stderr`` -- the latter is fragile under frameworks
            # that swap ``sys.stderr`` (pytest capture, uvicorn, etc.).
            # NullHandler is fine to leave in place (no output); any host-
            # attached handler on the clickwork logger is not ours to
            # evict (no ``_clickwork_owned`` flag).
            for existing in list(logger.handlers):
                if getattr(existing, "_clickwork_owned", False):
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

        # Find an existing clickwork-owned StreamHandler so re-invocations
        # (nested test runs, or the CLI callback running twice in-process)
        # update its level + format rather than stacking. Identity is
        # tracked via the ``_clickwork_owned`` marker attribute (set when
        # we first attach). Again, we deliberately don't compare
        # ``existing.stream is sys.stderr`` -- pytest capture replaces
        # ``sys.stderr`` so the identity check would miss our own handler.
        stream_handler = next(
            (
                existing
                for existing in logger.handlers
                if getattr(existing, "_clickwork_owned", False)
                and isinstance(existing, logging.StreamHandler)
                and not isinstance(existing, logging.NullHandler)
            ),
            None,
        )
        if stream_handler is None:
            stream_handler = logging.StreamHandler(sys.stderr)
            # Mark this handler as ours so future calls can find/evict it
            # via a robust identity check that survives sys.stderr swaps.
            stream_handler._clickwork_owned = True  # type: ignore[attr-defined]
            logger.addHandler(stream_handler)
        else:
            # Reusing an existing clickwork-owned handler: re-bind its
            # stream to the CURRENT sys.stderr. Pytest capture, uvicorn,
            # and similar tools swap sys.stderr between invocations --
            # the old stream reference inside the handler becomes stale
            # and subsequent records go to a detached stream the test
            # harness isn't watching. Rebinding here keeps the handler
            # writing to whatever "current stderr" means right now.
            stream_handler.stream = sys.stderr

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

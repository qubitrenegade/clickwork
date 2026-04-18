"""Tests for the logging module.

The logging module configures Python's stdlib logging with consistent formatting,
verbosity levels driven by CLI flags, and automatic color detection. It's the
first thing the CLI sets up, so it must be reliable and avoid surprising
import-time side effects; the only expected import-time behavior is attaching
a ``NullHandler`` baseline to the ``clickwork`` logger (library convention --
see the module docstring in ``clickwork/_logging.py`` for the rationale).

## Host-preserving behavior (1.0, #43)

A second cluster of tests below (``TestHostPreservingBehavior``) exercises
the 1.0 semantic change: clickwork must NOT double-emit records when a
host application has already configured root logging. The stdlib logging
module is global mutable state, so these tests rigorously reset handlers
and propagation flags around every case to keep tests order-independent.
"""
import io
import logging

import pytest


@pytest.fixture
def reset_logging():
    """Snapshot-and-restore the clickwork + root logger state per test.

    Python's logging module is global -- handlers installed in one test
    persist into the next unless we actively clean up. This fixture
    captures handlers / level / propagate for the loggers we touch, and
    restores them after the test runs regardless of pass/fail. It's
    opt-in (tests request it explicitly) because the pre-existing
    ``TestSetupLogging`` cases don't need it; they only check return-
    value attributes.
    """
    logger_names = ["clickwork", "clickwork.http", "", "my-cli", "test_default",
                    "test_v1", "test_v2", "test_quiet", "test_host",
                    "test_nullfallback", "test_propagate", "test_no_dup"]
    snapshots = []
    for name in logger_names:
        logger = logging.getLogger(name)
        snapshots.append((
            logger,
            list(logger.handlers),
            logger.level,
            logger.propagate,
        ))

    yield

    for logger, handlers, level, propagate in snapshots:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


class TestSetupLogging:
    """setup_logging() configures the named logger (not root) based on CLI verbosity flags.

    These tests predate the 1.0 host-preserving rewrite. They only check
    return-value attributes (``logger.level``, formatter choice, etc.),
    so they didn't need updating for the new host-preserving behavior.
    See ``TestHostPreservingBehavior`` below for the root-touches-nothing
    assertions.
    """

    def test_default_is_warning_level(self, reset_logging):
        """With no flags, only warnings and above should show."""
        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=False, name="test_default")
        assert logger.level == logging.WARNING

    def test_verbose_1_is_info(self, reset_logging):
        """Single -v flag should show INFO messages."""
        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=1, quiet=False, name="test_v1")
        assert logger.level == logging.INFO

    def test_verbose_2_is_debug(self, reset_logging):
        """Double -vv flag should show DEBUG messages."""
        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=2, quiet=False, name="test_v2")
        assert logger.level == logging.DEBUG

    def test_quiet_is_error_only(self, reset_logging):
        """--quiet should suppress everything below ERROR."""
        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=True, name="test_quiet")
        assert logger.level == logging.ERROR

    def test_returns_named_logger(self, reset_logging):
        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=False, name="my-cli")
        assert logger.name == "my-cli"


class TestHostPreservingBehavior:
    """1.0 #43 -- don't double-emit records when the host configured root logging.

    The key invariant: when a host has installed any handler on the root
    logger (e.g., via ``logging.basicConfig()``), clickwork's
    ``setup_logging()`` must not attach its OWN stderr handler.
    Otherwise a ``clickwork.http`` record emits twice -- once via the
    clickwork stderr handler, once via propagation to the host's root
    handler.
    """

    def test_no_duplicate_logs_when_host_configured_basicconfig(self, reset_logging):
        """Simulates a host that called basicConfig() before importing clickwork.

        After ``setup_logging()``, emitting a record from the shared
        ``clickwork`` logger should reach the host's root handler
        exactly once. If clickwork attached its own StreamHandler we'd
        see two emissions.
        """
        # Reset root's handlers -- pytest's logging plugin installs its
        # own by default, which would count as "host configured" before
        # we want it to. We install our OWN root handler to simulate the
        # embedding-application scenario.
        root = logging.getLogger()
        root.handlers = []

        buffer = io.StringIO()
        host_handler = logging.StreamHandler(buffer)
        host_handler.setLevel(logging.DEBUG)
        host_handler.setFormatter(logging.Formatter("HOST:%(name)s:%(message)s"))
        root.addHandler(host_handler)
        root.setLevel(logging.DEBUG)

        # Host has called basicConfig-equivalent. Now clickwork runs.
        from clickwork._logging import setup_logging
        setup_logging(verbose=1, quiet=False, name="test_no_dup")

        # Emit a record on the shared clickwork logger -- this is what
        # framework modules (http, discovery) do.
        logging.getLogger("clickwork").info("hello from clickwork")

        # Exactly one "hello from clickwork" should appear in the host's
        # buffer. If clickwork installed its own StreamHandler on the
        # "clickwork" logger AND propagation is on, we'd see two. If it
        # installed one and disabled propagation, we'd see one but the
        # host wouldn't get it at all. The correct behavior is: host
        # handler gets exactly one copy.
        output = buffer.getvalue()
        assert output.count("hello from clickwork") == 1, (
            f"Expected exactly one emission; got: {output!r}"
        )
        # And we should also verify no clickwork-attached StreamHandler
        # was installed on the "clickwork" logger when the host was
        # already configured. (NullHandler is fine / expected.)
        clickwork_logger = logging.getLogger("clickwork")
        non_null_stream_handlers = [
            h for h in clickwork_logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.NullHandler)
        ]
        assert non_null_stream_handlers == [], (
            "clickwork should not attach its own StreamHandler when "
            f"the host configured root; found: {non_null_stream_handlers}"
        )

    def test_null_handler_fallback_when_host_unconfigured(self, reset_logging, capsys):
        """With no host config, clickwork must not crash AND must not spam stdout.

        The library-HOWTO pattern is: attach NullHandler so stdlib
        doesn't warn "No handlers could be found", and let the CLI's
        ``setup_logging()`` attach a real stderr handler for standalone
        use. Critically, importing clickwork alone (without calling
        setup_logging) must NEVER print to stdout, because that'd
        corrupt any CLI that produces machine-readable stdout.
        """
        # Clear the root handlers so we simulate a "no host config"
        # environment -- pytest would otherwise have installed its own.
        root = logging.getLogger()
        root.handlers = []

        # Re-import clickwork's logging module fresh so the module-load
        # side effects re-run and we can observe them.
        import importlib
        import clickwork._logging
        importlib.reload(clickwork._logging)

        # After the module reloads, the clickwork logger MUST have a
        # NullHandler attached. This is the "baseline" that prevents
        # stdlib's "no handlers" warning.
        clickwork_logger = logging.getLogger("clickwork")
        has_null = any(isinstance(h, logging.NullHandler) for h in clickwork_logger.handlers)
        assert has_null, (
            f"clickwork logger must have a NullHandler baseline; "
            f"handlers are: {clickwork_logger.handlers}"
        )

        # Emitting a record before setup_logging is ever called should
        # produce no stdout (it can go to stderr via stdlib lastResort
        # for WARNING+, but should never hit stdout). The critical
        # assertion here is "no error raised" -- the NullHandler means
        # the record is accepted and silently dropped.
        logging.getLogger("clickwork").debug("pre-setup debug record")
        logging.getLogger("clickwork").info("pre-setup info record")

        captured = capsys.readouterr()
        assert captured.out == "", (
            f"clickwork must not emit to stdout when no handlers are "
            f"configured; got stdout: {captured.out!r}"
        )

    def test_propagate_is_true_for_clickwork_loggers(self, reset_logging):
        """The clickwork logger must propagate so host root handlers see records.

        If a previous clickwork version set propagate=False (to "own"
        output), embedding that version in a host that configured root
        logging would silently swallow all clickwork records -- they'd
        hit clickwork's private StreamHandler but never reach the
        host's root handler. The 1.0 contract is propagate=True.
        """
        import importlib
        import clickwork._logging
        importlib.reload(clickwork._logging)

        assert logging.getLogger("clickwork").propagate is True

        # Also verify propagate stays True after setup_logging() runs,
        # since setup_logging explicitly restates the value.
        from clickwork._logging import setup_logging
        setup_logging(verbose=0, quiet=False, name="test_propagate")
        assert logging.getLogger("clickwork").propagate is True
        # And the named CLI logger should also propagate.
        assert logging.getLogger("test_propagate").propagate is True

    def test_standalone_mode_attaches_stderr_handler(self, reset_logging, capsys):
        """No host root handlers -> setup_logging attaches a stderr StreamHandler.

        Pins the bare-script path: when a consumer runs a clickwork CLI
        directly (no embedding framework, no ``logging.basicConfig()``),
        clickwork MUST attach its own stderr StreamHandler so records
        actually get printed. Without this path the record would only
        propagate to root, which has no handlers, and stdlib would fall
        back to the "handler of last resort" or silently drop below-WARN
        records.
        """
        # Start from a bare root: no handlers at all.
        root = logging.getLogger()
        root.handlers = []
        # Reload so the module-load baseline runs against the cleared
        # root. Without the reload, module state from an earlier test
        # could pre-attach handlers and mask the "bare root" scenario.
        import importlib
        import clickwork._logging
        importlib.reload(clickwork._logging)

        from clickwork._logging import setup_logging

        logger = setup_logging(verbose=1, quiet=False, name="test_standalone")

        # The named CLI logger should have a clickwork-owned
        # StreamHandler attached. Identified by the marker attribute we
        # set at attach time (more robust than ``handler.stream is
        # sys.stderr``, which breaks under pytest capture).
        owned_handlers = [
            h
            for h in logger.handlers
            if getattr(h, "_clickwork_owned", False)
            and isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.NullHandler)
        ]
        assert len(owned_handlers) == 1, (
            f"standalone mode must attach exactly one clickwork-owned "
            f"StreamHandler; got: {logger.handlers}"
        )

        # Emit a record and confirm the installed handler actually
        # prints it. pytest's capsys captures fd-level stderr so this
        # works even with the _clickwork_owned marker-based seam.
        logger.warning("standalone warn record")
        captured = capsys.readouterr()
        assert "standalone warn record" in captured.err

    def test_stderr_handler_marker_survives_setup_logging_reinvocation(self, reset_logging):
        """setup_logging is idempotent: a second call updates-in-place, not stacks.

        Without the ``_clickwork_owned`` marker, the old
        ``handler.stream is sys.stderr`` identity check would fail under
        frameworks that swap ``sys.stderr`` (pytest capture, uvicorn's
        stream wrapping, etc.). This test confirms a second
        ``setup_logging()`` call finds and updates the original handler
        via the marker instead of stacking a duplicate.
        """
        root = logging.getLogger()
        root.handlers = []
        import importlib
        import clickwork._logging
        importlib.reload(clickwork._logging)

        from clickwork._logging import setup_logging

        name = "test_no_dup"
        setup_logging(verbose=0, quiet=False, name=name)
        setup_logging(verbose=2, quiet=False, name=name)  # second call, -vv
        setup_logging(verbose=1, quiet=False, name=name)  # third call, -v

        logger = logging.getLogger(name)
        owned = [h for h in logger.handlers if getattr(h, "_clickwork_owned", False)]
        assert len(owned) == 1, (
            f"setup_logging must not stack handlers on re-invocation; "
            f"got: {logger.handlers}"
        )
        # Final level should reflect the LAST call (-v -> INFO).
        assert owned[0].level == logging.INFO

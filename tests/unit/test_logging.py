"""Tests for the logging module.

The logging module configures Python's stdlib logging with consistent formatting,
verbosity levels driven by CLI flags, and automatic color detection. It's the
first thing the CLI sets up, so it must be reliable and have no side effects
when imported.
"""
import logging

import pytest


class TestSetupLogging:
    """setup_logging() configures the root logger based on CLI verbosity flags."""

    def test_default_is_warning_level(self):
        """With no flags, only warnings and above should show."""
        from qbrd_tools._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=False, name="test_default")
        assert logger.level == logging.WARNING

    def test_verbose_1_is_info(self):
        """Single -v flag should show INFO messages."""
        from qbrd_tools._logging import setup_logging

        logger = setup_logging(verbose=1, quiet=False, name="test_v1")
        assert logger.level == logging.INFO

    def test_verbose_2_is_debug(self):
        """Double -vv flag should show DEBUG messages."""
        from qbrd_tools._logging import setup_logging

        logger = setup_logging(verbose=2, quiet=False, name="test_v2")
        assert logger.level == logging.DEBUG

    def test_quiet_is_error_only(self):
        """--quiet should suppress everything below ERROR."""
        from qbrd_tools._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=True, name="test_quiet")
        assert logger.level == logging.ERROR

    def test_returns_named_logger(self):
        from qbrd_tools._logging import setup_logging

        logger = setup_logging(verbose=0, quiet=False, name="my-cli")
        assert logger.name == "my-cli"

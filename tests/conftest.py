"""Shared test fixtures for qbrd-tools.

Provides a CliContext factory so tests don't need to construct one with
7 positional args every time. Fixtures are added incrementally as modules
are built.
"""
import logging

import pytest


@pytest.fixture
def make_cli_context():
    """Factory fixture for creating CliContext instances with sensible defaults.

    Usage in tests:
        def test_something(make_cli_context):
            ctx = make_cli_context(dry_run=True)
    """
    def _factory(**overrides):
        from qbrd_tools._types import CliContext

        defaults = {
            "config": {},
            "env": None,
            "dry_run": False,
            "verbose": 0,
            "quiet": False,
            "yes": False,
            "logger": logging.getLogger("test"),
        }
        defaults.update(overrides)
        return CliContext(**defaults)

    return _factory

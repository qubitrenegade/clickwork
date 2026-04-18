"""Tests for the clickwork.testing helper module.

These tests are themselves meta: they exercise the helpers that future test
suites will use to drive clickwork-built CLIs. Each test corresponds to one of
the acceptance criteria in the Wave 4 plan for issue #16.

WHY we keep these tests minimal: the two public helpers (``run_cli`` and
``make_test_cli``) are thin adapters. Their value is in *convention* (pinned
defaults and forwarded kwargs), not logic -- so each test asserts ONE
convention in isolation. That way a future refactor of the helpers produces a
single focused failure, instead of a wall of broken assertions that all
collapse into the same root cause.
"""
from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import Result


class TestRunCli:
    """run_cli() wraps CliRunner().invoke() with pinned defaults."""

    def test_run_cli_invokes_command_and_returns_click_result(self) -> None:
        """run_cli() returns Click's native Result object (exit_code == 0 on --help).

        WHY we assert on the native Result type: plugin authors already know
        Click's testing idioms, so we deliberately DO NOT invent a new result
        type. Anything returned here must be drop-in compatible with
        ``CliRunner.invoke()``'s return value so snippets copy-pasted from
        Click docs keep working.
        """
        from clickwork.testing import run_cli

        @click.command()
        def greet() -> None:
            """Say hi."""
            click.echo("hi")

        result = run_cli(greet, ["--help"])

        # Click's native Result type -- we deliberately don't wrap it.
        assert isinstance(result, Result)
        assert result.exit_code == 0
        # --help text always mentions the "Usage" banner; cheap smoke check
        # that we actually invoked the command rather than, say, returning a
        # stubbed Result.
        assert "Usage" in result.output

    def test_run_cli_catch_exceptions_false_by_default(self) -> None:
        """Unhandled exceptions propagate unless catch_exceptions=True is passed.

        WHY this is the default: with ``catch_exceptions=True`` (Click's own
        default), a bug inside a command surfaces only as ``result.exception``
        and ``result.exit_code == 1`` -- the real traceback is swallowed,
        which routinely hides test failures behind a generic assertion on
        exit code. We flip the default so the traceback lands directly on
        the pytest output and the author sees the exact line that broke.
        Passing ``catch_exceptions=True`` explicitly restores Click's
        original behaviour for tests that WANT to assert on
        ``result.exception``.
        """
        from clickwork.testing import run_cli

        @click.command()
        def boom() -> None:
            raise RuntimeError("explode")

        # Default: catch_exceptions=False, so RuntimeError propagates.
        with pytest.raises(RuntimeError, match="explode"):
            run_cli(boom, [])

        # Explicit catch_exceptions=True overrides and suppresses.
        result = run_cli(boom, [], catch_exceptions=True)
        assert isinstance(result.exception, RuntimeError)
        assert str(result.exception) == "explode"
        assert result.exit_code == 1


class TestMakeTestCli:
    """make_test_cli() wraps create_cli() with test-friendly defaults."""

    def test_make_test_cli_returns_click_group(self, tmp_path: Path) -> None:
        """make_test_cli() returns a click.Group instance.

        WHY we assert on the type: tests downstream rely on methods that only
        Groups expose (``.commands``, ``.add_command``, etc.), so if a future
        refactor accidentally returns a bare ``click.Command`` the failure
        should surface here rather than as a confusing ``AttributeError`` in
        the caller.
        """
        from clickwork.testing import make_test_cli

        cli = make_test_cli(commands_dir=tmp_path)

        assert isinstance(cli, click.Group)

    def test_make_test_cli_accepts_commands_dir(self, tmp_path: Path) -> None:
        """Commands dropped in commands_dir are discovered by the returned CLI.

        WHY we exercise discovery end-to-end: commands_dir is the forward
        path that every other clickwork test will use to scaffold realistic
        CLIs, so a regression that breaks discovery from make_test_cli
        (e.g., forgetting to forward the kwarg) would silently cascade into
        every plugin test suite. Cheaper to catch it here.
        """
        from clickwork.testing import make_test_cli, run_cli

        # Minimal conventional command file: exports `cli` as a Click command.
        (tmp_path / "ping.py").write_text(
            "import click\n"
            "@click.command()\n"
            "def ping():\n"
            "    '''Respond with pong.'''\n"
            "    click.echo('pong')\n"
            "cli = ping\n"
        )

        cli = make_test_cli(commands_dir=tmp_path)
        result = run_cli(cli, ["ping"])

        assert result.exit_code == 0
        assert "pong" in result.output

    def test_make_test_cli_forwards_create_cli_kwargs(self, tmp_path: Path) -> None:
        """Extra kwargs (e.g. description=) reach the underlying create_cli call.

        WHY we test forwarding: **kwargs is easy to forget to pass through
        when refactoring. A failure here means a plugin test that sets
        ``description="..."`` to verify custom help-text behaviour would
        silently drop the argument and produce a Click default.
        """
        from clickwork.testing import make_test_cli, run_cli

        cli = make_test_cli(commands_dir=tmp_path, description="smoke-test description")
        result = run_cli(cli, ["--help"])

        assert result.exit_code == 0
        # create_cli passes description through to Click's help= parameter,
        # which renders it near the top of --help output.
        assert "smoke-test description" in result.output

    def test_make_test_cli_default_name(self, tmp_path: Path) -> None:
        """Without an explicit name= kwarg, the CLI is named 'test-cli'.

        WHY this default is pinned in tests: downstream test authors will
        grep logs and help output for the CLI name when debugging; making
        the default predictable (rather than, say, a random uuid) keeps
        those greps trivial. If someone changes the default, this test
        forces the change to be intentional.
        """
        from clickwork.testing import make_test_cli

        cli = make_test_cli(commands_dir=tmp_path)

        assert cli.name == "test-cli"

    def test_make_test_cli_respects_explicit_name(self, tmp_path: Path) -> None:
        """Passing name= overrides the 'test-cli' default.

        WHY this complements test_make_test_cli_default_name: asserting the
        default is pinned isn't enough -- we also need to know a caller can
        still override it. A setdefault("name", ...) bug that silently
        clobbered an explicit name would pass the "default is test-cli"
        test while breaking this one.
        """
        from clickwork.testing import make_test_cli

        cli = make_test_cli(commands_dir=tmp_path, name="custom-name")

        assert cli.name == "custom-name"


class TestModuleSurface:
    """clickwork.testing is importable via both module and attribute paths."""

    def test_module_importable_as_clickwork_testing(self) -> None:
        """``import clickwork.testing`` resolves the submodule.

        WHY both import forms are tested: we advertise the module in docs
        with ``from clickwork.testing import run_cli`` AND as
        ``clickwork.testing.run_cli`` (attribute access on the package).
        Both must work; a missing re-export in ``clickwork/__init__.py``
        would silently break the second form and users would not discover
        it until a copy-pasted snippet failed.
        """
        import clickwork.testing as testing_module

        assert callable(testing_module.run_cli)
        assert callable(testing_module.make_test_cli)

    def test_testing_attribute_on_clickwork_package(self) -> None:
        """``from clickwork import testing`` works thanks to the re-export."""
        from clickwork import testing

        assert callable(testing.run_cli)
        assert callable(testing.make_test_cli)

"""Test helpers for clickwork-built CLIs.

This module provides two thin wrappers over Click's own testing toolkit so
plugin test suites can stop re-typing the same 4-line setup. It ships on
purpose with a minimal surface -- ``run_cli`` and ``make_test_cli`` only --
because most test authors benefit from keeping the rest of Click's testing
API visible. If you want to stub out subprocess calls, build your own mocks;
we deliberately do not ship ``mock_run`` / ``mock_capture`` context managers.

Why these helpers exist
-----------------------

Before this module existed, every plugin test that exercised a full CLI
looked like::

    from click.testing import CliRunner
    from clickwork import create_cli

    def test_greet(tmp_path):
        (tmp_path / "greet.py").write_text(...)
        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["greet"], catch_exceptions=False)
        assert result.exit_code == 0

Two pieces of boilerplate appeared in every file: constructing the CLI with
a throwaway name, and remembering to pass ``catch_exceptions=False`` so real
tracebacks surface in test output. This module collapses both.

Canonical usage
---------------

::

    from clickwork.testing import make_test_cli, run_cli

    def test_greet_says_hello(tmp_path):
        (tmp_path / "greet.py").write_text(
            "import click\\n"
            "@click.command()\\n"
            "def greet():\\n"
            "    click.echo('hello')\\n"
            "cli = greet\\n"
        )

        cli = make_test_cli(commands_dir=tmp_path)
        result = run_cli(cli, ["greet"])

        assert result.exit_code == 0
        assert "hello" in result.stdout

CliRunner output attributes -- pick the right one
-------------------------------------------------

Click's :class:`click.testing.Result` exposes three stream attributes that
look similar but differ in subtle ways that matter for assertion tests:

* ``result.output`` -- stdout AND stderr **interleaved** in the order the
  command produced them. Convenient for "did the command say X at all"
  smoke checks. Misleading for "did the error go to stderr" tests,
  because the answer is always "yes, and also it's in ``output``".
* ``result.stdout`` -- stdout only. Assert on this when the contract you
  care about is specifically "this goes to the normal output channel".
* ``result.stderr`` -- stderr only. Assert on this when the contract is
  "this is an error / diagnostic / progress line on a side channel".

A test that says "the error message was printed to stderr" should assert
on ``result.stderr``, not ``result.output``. See the GUIDE.md "Testing
commands" section for a worked example.

Historical note: Click 8.2 removed the ``mix_stderr`` kwarg that
``CliRunner.__init__`` used to accept. Post-removal, all three stream
attributes on ``Result`` are populated separately (``output`` is the
interleaved form; ``stdout`` and ``stderr`` are kept independent).
clickwork declares ``click>=8.2`` precisely so this guidance always
applies -- snippets in older tutorials that use
``CliRunner(mix_stderr=False)`` will raise ``TypeError`` against the
supported Click range, and on 8.1 and earlier ``result.stderr`` would
have raised ``ValueError: stderr not separately captured`` under the
default ``CliRunner()`` configuration (where streams were mixed unless
``mix_stderr=False`` was passed explicitly). Flooring at 8.2 gets us
out of documenting that conditional behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from clickwork.cli import create_cli

if TYPE_CHECKING:
    # click.testing is only pulled in for type-checker annotations so the
    # runtime import cost stays opt-in: a project that imports
    # ``clickwork.testing`` for ``make_test_cli`` but never calls
    # ``run_cli`` never loads click.testing's ~dozen-KB module graph.
    from click.testing import Result as _ClickResult
else:
    # At runtime the alias resolves to ``Any`` so ``typing.get_type_hints``
    # (used by IDEs, FastAPI, pydantic, etc.) can still introspect the
    # signature without ``Result`` actually existing in module globals.
    # Without this fallback ``get_type_hints(run_cli)`` would raise
    # ``NameError: name '_ClickResult' is not defined`` -- the forward-
    # reference string ``-> _ClickResult`` can't resolve because
    # TYPE_CHECKING is False at runtime. The real type is still visible
    # to type-checkers thanks to the TYPE_CHECKING branch above.
    _ClickResult = Any


def run_cli(
    cli: click.Command,
    args: str | Sequence[str] | None = None,
    **kwargs: Any,
) -> _ClickResult:
    """Invoke a Click CLI under CliRunner with test-friendly defaults.

    Equivalent to ``click.testing.CliRunner().invoke(cli, args, **kwargs)``
    with one change: ``catch_exceptions`` defaults to ``False`` so bugs in
    the command surface as real tracebacks in pytest output instead of
    being swallowed into ``result.exception``. Pass ``catch_exceptions=True``
    explicitly if you want to assert on the caught exception.

    Args:
        cli: The Click command or group to invoke. Accepts any
            ``click.Command`` (including ``click.Group``) so tests can pass a
            raw ``@click.command``-decorated function or a group built with
            :func:`clickwork.create_cli`. ``click.BaseCommand`` was the
            documented base in earlier Click 8.x but is deprecated in 8.2+
            and slated for removal in 9.0, so we use ``click.Command``.
        args: The command-line arguments to pass, as you would write them
            after the CLI name. The preferred form is a list/tuple of
            already-tokenised strings (``["deploy", "--env", "staging"]``);
            a single string gets shell-tokenised by Click, which matches
            Click's ``CliRunner.invoke`` signature but is error-prone on
            values containing spaces or quotes. ``None`` means "no
            arguments," equivalent to invoking the CLI with no positionals.
        **kwargs: Forwarded verbatim to ``CliRunner.invoke``. Useful
            overrides: ``input=`` to feed stdin, ``env=`` to set
            environment variables, ``catch_exceptions=True`` to restore
            Click's default exception-swallowing behaviour.

    Returns:
        Click's native :class:`click.testing.Result`. We deliberately do
        not wrap this -- plugin authors already know its shape.

    Example::

        result = run_cli(cli, ["deploy", "--dry-run"])
        assert result.exit_code == 0
        assert "would deploy" in result.stdout
    """
    # WHY we import CliRunner inside the function rather than at module
    # top level: click.testing pulls in additional streams/runner
    # machinery that a plugin's production code path never needs. Doing
    # the import here keeps ``import clickwork.testing`` itself cheap --
    # test-only consumers (conftest.py, test modules) pay the cost once
    # the first time ``run_cli`` is called.
    from click.testing import CliRunner

    # WHY setdefault instead of popping + re-adding: setdefault only
    # writes the key when it's absent, so a caller that passes
    # ``catch_exceptions=True`` keeps their override intact. Explicit
    # ``kwargs["catch_exceptions"] = False`` would clobber it.
    kwargs.setdefault("catch_exceptions", False)

    runner = CliRunner()
    return runner.invoke(cli, args, **kwargs)


def make_test_cli(
    *,
    commands_dir: Path | None = None,
    **create_cli_kwargs: Any,
) -> click.Group:
    """Build a clickwork CLI with sensible test-suite defaults.

    Thin convenience wrapper over :func:`clickwork.create_cli`. Fills in a
    default ``name`` (``"test-cli"``) so tests that don't care about the
    CLI name don't have to repeat that argument, and forwards every other
    kwarg through unchanged.

    Args:
        commands_dir: Directory containing command ``.py`` files to
            discover, typically ``tmp_path`` in a pytest test. Optional;
            omit to test the global-flags layer without registering any
            commands.
        **create_cli_kwargs: Forwarded verbatim to ``create_cli``.
            Commonly overridden: ``name=`` to pin the CLI name for help-
            text assertions, ``description=`` to test custom help text,
            ``config_schema=`` to exercise config validation.

    Returns:
        A :class:`click.Group` ready to feed into :func:`run_cli`.

    Example::

        cli = make_test_cli(commands_dir=tmp_path, description="deploy helpers")
        result = run_cli(cli, ["--help"])
        assert "deploy helpers" in result.stdout
    """
    # WHY setdefault for ``name``: callers that DO pass name= (e.g.,
    # "custom-cli") must keep it. setdefault is the one-liner that
    # preserves both behaviours -- provide a sensible default while
    # staying transparent to explicit overrides.
    create_cli_kwargs.setdefault("name", "test-cli")

    # Forward commands_dir as a dedicated keyword rather than folding it
    # into ``create_cli_kwargs`` in the signature. This makes the helper's
    # intent ("opt-in commands directory, everything else passes through")
    # readable at a glance, and prevents a caller from accidentally
    # passing ``commands_dir=`` as both a positional-looking kwarg AND
    # inside ``**create_cli_kwargs`` -- Python would raise TypeError on
    # the duplicate.
    return create_cli(commands_dir=commands_dir, **create_cli_kwargs)

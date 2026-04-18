"""Tests for platform detection and repo root finding.

Platform detection wraps sys.platform checks into readable helpers.
Repo root finding walks up from cwd looking for .git (as directory or file),
with a fallback to git rev-parse. This needs to handle worktrees where .git
is a file pointing at the real gitdir.

Platform dispatch (decorator + functional) routes a command to the right
per-OS implementation at call time. Tests monkeypatch ``sys.platform`` to
the strings clickwork's ``is_linux``/``is_macos``/``is_windows`` helpers
check for: ``"linux"``, ``"darwin"``, and (importantly) ``"win32"``.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import click
import pytest


class TestPlatformDetection:
    """Platform helpers return booleans based on sys.platform."""

    def test_is_linux(self):
        from clickwork.platform import is_linux

        with patch.object(sys, "platform", "linux"):
            assert is_linux() is True
        with patch.object(sys, "platform", "darwin"):
            assert is_linux() is False

    def test_is_macos(self):
        from clickwork.platform import is_macos

        with patch.object(sys, "platform", "darwin"):
            assert is_macos() is True
        with patch.object(sys, "platform", "linux"):
            assert is_macos() is False

    def test_is_windows(self):
        from clickwork.platform import is_windows

        with patch.object(sys, "platform", "win32"):
            assert is_windows() is True
        with patch.object(sys, "platform", "linux"):
            assert is_windows() is False


class TestFindRepoRoot:
    """find_repo_root() walks up from a starting directory looking for .git."""

    def test_finds_git_directory(self, tmp_path: Path):
        """Standard case: .git is a directory at the repo root."""
        from clickwork.platform import find_repo_root

        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        assert find_repo_root(subdir) == tmp_path

    def test_finds_git_file_for_worktrees(self, tmp_path: Path):
        """Worktree case: .git is a file containing 'gitdir: /path/to/...'."""
        from clickwork.platform import find_repo_root

        (tmp_path / ".git").write_text("gitdir: /some/other/path")
        assert find_repo_root(tmp_path) == tmp_path

    def test_returns_none_when_no_git(self, tmp_path: Path):
        """When there's no .git anywhere in the hierarchy, return None."""
        from clickwork.platform import find_repo_root

        subdir = tmp_path / "not" / "a" / "repo"
        subdir.mkdir(parents=True)
        assert find_repo_root(subdir) is None


class TestPlatformDispatchDecorator:
    """@platform_dispatch routes a decorated command to the right per-OS impl.

    The decorator captures linux/windows/macos impls at definition time, then
    at call time detects the current platform via sys.platform and forwards
    the original args/kwargs to the matching impl. Missing impls for the
    detected platform raise ``click.UsageError``.
    """

    def test_platform_dispatch_linux_calls_linux_impl(self, monkeypatch):
        """On linux, the ``linux=`` impl runs with the caller's args/kwargs."""
        from clickwork.platform import platform_dispatch

        calls: list[tuple[tuple, dict]] = []

        def linux_impl(*args, **kwargs):
            calls.append((args, kwargs))

        def other_impl(*args, **kwargs):  # pragma: no cover - not selected
            raise AssertionError("wrong platform impl called")

        @platform_dispatch(linux=linux_impl, windows=other_impl, macos=other_impl)
        def runner_up(name: str) -> None:
            """Runner up command (body unused because dispatch takes over)."""

        monkeypatch.setattr("sys.platform", "linux")
        runner_up("alice")

        assert calls == [(("alice",), {})]

    def test_platform_dispatch_windows_calls_windows_impl(self, monkeypatch):
        """On win32, the ``windows=`` impl is selected (NOT 'windows')."""
        from clickwork.platform import platform_dispatch

        calls: list[tuple[tuple, dict]] = []

        def windows_impl(*args, **kwargs):
            calls.append((args, kwargs))

        def other_impl(*args, **kwargs):  # pragma: no cover - not selected
            raise AssertionError("wrong platform impl called")

        @platform_dispatch(linux=other_impl, windows=windows_impl, macos=other_impl)
        def runner_up(name: str) -> None: ...

        # sys.platform is "win32" on Windows, not "windows".
        monkeypatch.setattr("sys.platform", "win32")
        runner_up("bob")

        assert calls == [(("bob",), {})]

    def test_platform_dispatch_macos_calls_macos_impl(self, monkeypatch):
        """On darwin, the ``macos=`` impl is selected."""
        from clickwork.platform import platform_dispatch

        calls: list[tuple[tuple, dict]] = []

        def macos_impl(*args, **kwargs):
            calls.append((args, kwargs))

        def other_impl(*args, **kwargs):  # pragma: no cover - not selected
            raise AssertionError("wrong platform impl called")

        @platform_dispatch(linux=other_impl, windows=other_impl, macos=macos_impl)
        def runner_up(name: str) -> None: ...

        monkeypatch.setattr("sys.platform", "darwin")
        runner_up("carol")

        assert calls == [(("carol",), {})]

    def test_platform_dispatch_unsupported_platform_raises_usage_error(self, monkeypatch):
        """Any platform that is not linux/win32/darwin raises UsageError."""
        from clickwork.platform import platform_dispatch

        def impl(*args, **kwargs):  # pragma: no cover - never called
            raise AssertionError("should not be called on unsupported platform")

        @platform_dispatch(linux=impl, windows=impl, macos=impl)
        def runner_up() -> None: ...

        monkeypatch.setattr("sys.platform", "freebsd13")
        with pytest.raises(click.UsageError):
            runner_up()

    def test_platform_dispatch_linux_error_kwarg_overrides_message(self, monkeypatch):
        """linux_error= replaces the default 'linux not supported' message."""
        from clickwork.platform import platform_dispatch

        @platform_dispatch(
            linux=None,
            windows=lambda *a, **k: None,
            macos=lambda *a, **k: None,
            linux_error="not yet",
        )
        def runner_up() -> None: ...

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(click.UsageError) as exc_info:
            runner_up()
        assert str(exc_info.value.message) == "not yet"

    def test_platform_dispatch_windows_error_kwarg_overrides_message(self, monkeypatch):
        """windows_error= replaces the default 'windows not supported' message."""
        from clickwork.platform import platform_dispatch

        @platform_dispatch(
            linux=lambda *a, **k: None,
            windows=None,
            macos=lambda *a, **k: None,
            windows_error="windows soon",
        )
        def runner_up() -> None: ...

        monkeypatch.setattr("sys.platform", "win32")
        with pytest.raises(click.UsageError) as exc_info:
            runner_up()
        assert str(exc_info.value.message) == "windows soon"

    def test_platform_dispatch_macos_error_kwarg_overrides_message(self, monkeypatch):
        """macos_error= replaces the default 'macos not supported' message."""
        from clickwork.platform import platform_dispatch

        @platform_dispatch(
            linux=lambda *a, **k: None,
            windows=lambda *a, **k: None,
            macos=None,
            macos_error="macOS not supported yet",
        )
        def runner_up() -> None: ...

        monkeypatch.setattr("sys.platform", "darwin")
        with pytest.raises(click.UsageError) as exc_info:
            runner_up()
        assert str(exc_info.value.message) == "macOS not supported yet"

    def test_platform_dispatch_signature_forwarding(self, monkeypatch):
        """The decorated function's signature is preserved; impls receive the same args."""
        from clickwork.platform import platform_dispatch

        seen: dict = {}

        def linux_impl(name: str, *, flag: bool = False) -> None:
            seen["name"] = name
            seen["flag"] = flag

        @platform_dispatch(
            linux=linux_impl,
            windows=lambda *a, **k: None,
            macos=lambda *a, **k: None,
        )
        def runner_up(name: str, *, flag: bool = False) -> None: ...

        monkeypatch.setattr("sys.platform", "linux")
        runner_up("dave", flag=True)

        assert seen == {"name": "dave", "flag": True}


class TestPlatformDispatchFunctional:
    """clickwork.platform.dispatch() is the escape hatch for pre-dispatch logic.

    Matches @pass_cli_context command structure: the selected impl is called
    with ``ctx`` as its first positional arg, followed by any forwarded kwargs.
    """

    def test_dispatch_functional_linux(self, monkeypatch):
        """On linux, the ``linux=`` impl receives ``ctx`` and forwarded kwargs."""
        from clickwork.platform import dispatch

        calls: list[tuple[tuple, dict]] = []

        def linux_fn(*args, **kwargs):
            calls.append((args, kwargs))

        def other_fn(*args, **kwargs):  # pragma: no cover - not selected
            raise AssertionError("wrong platform impl")

        ctx = object()  # Stand-in for the CliContext.
        monkeypatch.setattr("sys.platform", "linux")
        dispatch(ctx, linux=linux_fn, windows=other_fn, macos=other_fn)

        assert calls == [((ctx,), {})]

    def test_dispatch_functional_forwards_kwargs(self, monkeypatch):
        """Extra kwargs are forwarded alongside ctx to the selected impl."""
        from clickwork.platform import dispatch

        captured: dict = {}

        def linux_fn(ctx, **kwargs):
            captured["ctx"] = ctx
            captured["kwargs"] = kwargs

        ctx = object()
        monkeypatch.setattr("sys.platform", "linux")
        dispatch(
            ctx,
            linux=linux_fn,
            windows=lambda *a, **k: None,
            macos=lambda *a, **k: None,
            extra="x",
        )

        assert captured["ctx"] is ctx
        assert captured["kwargs"] == {"extra": "x"}

    def test_dispatch_functional_raises_for_unsupported(self, monkeypatch):
        """Unsupported platform raises click.UsageError, same as the decorator."""
        from clickwork.platform import dispatch

        ctx = object()
        monkeypatch.setattr("sys.platform", "freebsd13")
        with pytest.raises(click.UsageError):
            dispatch(
                ctx,
                linux=lambda *a, **k: None,
                windows=lambda *a, **k: None,
                macos=lambda *a, **k: None,
            )


class TestPlatformDispatchViaClickRunner:
    """End-to-end test that platform_dispatch works through Click's CliRunner.

    WHY this class exists (separate from the direct-call tests): the
    decorator-order gotcha around ``@pass_cli_context`` only manifests
    when Click actually invokes the command through its own callback
    machinery. Direct-call tests bypass that, so they can pass while a
    real Click invocation would crash. This class pins the working
    decorator stack (platform_dispatch innermost, pass_cli_context
    above, click.command / click.argument on top).
    """

    def test_dispatch_through_clirunner_hits_linux_impl(self, monkeypatch, tmp_path: Path):
        """A Click command decorated with @platform_dispatch runs its linux impl.

        Builds a CLI via ``create_cli`` so the CliContext injection flows
        through ``@pass_cli_context`` and then into the dispatched impl.
        Patches ``sys.platform`` to ``"linux"`` before invocation.
        """
        from click.testing import CliRunner

        from clickwork.cli import create_cli, pass_cli_context
        from clickwork.platform import platform_dispatch

        captured: dict = {}

        # Inner per-platform impls receive the CliContext that
        # @pass_cli_context injected upstream. Prove that by stashing
        # the first arg and a known kwarg into captured[].
        def _linux_up(ctx, *, name):
            captured["impl"] = "linux"
            captured["ctx_has_dry_run"] = hasattr(ctx, "dry_run")
            captured["name"] = name

        def _windows_up(ctx, *, name):
            captured["impl"] = "windows"

        def _macos_up(ctx, *, name):
            captured["impl"] = "macos"

        @click.command("runner-up")
        @click.option("--name", default="test-runner")
        @pass_cli_context
        @platform_dispatch(
            linux=_linux_up,
            windows=_windows_up,
            macos=_macos_up,
        )
        def runner_up(
            ctx, name
        ): ...  # body intentionally empty -- platform_dispatch never calls it

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(runner_up)

        monkeypatch.setattr("sys.platform", "linux")
        runner = CliRunner()
        result = runner.invoke(cli, ["runner-up", "--name", "rabbithole"])

        assert result.exit_code == 0, result.output
        assert captured.get("impl") == "linux"
        # If this is True, @pass_cli_context's injection actually reached
        # the impl (CliContext has a dry_run attribute; a raw click.Context
        # does not). If False, platform_dispatch consumed the @pass_cli_context
        # wrapper without preserving its behaviour -- the decorator-order
        # bug this test exists to catch.
        assert captured.get("ctx_has_dry_run") is True, (
            f"impl did not receive CliContext; got ctx={captured.get('ctx_has_dry_run')!r}. "
            "Check that @platform_dispatch is the INNERMOST decorator on the stack."
        )
        assert captured.get("name") == "rabbithole"

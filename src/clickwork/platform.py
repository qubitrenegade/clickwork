"""Platform detection, dispatch, and repository root finding.

Platform helpers (is_linux, is_macos, is_windows) are thin wrappers around
sys.platform. They exist so command code reads clearly: `if is_macos():`
instead of `if sys.platform == "darwin":`.

platform_dispatch (decorator) and dispatch() (functional helper) route a
single command to the right per-OS implementation at call time. The two
forms share ``_select_impl`` so the detection + error-message logic is
single-sourced and can't drift.

find_repo_root() walks up from a starting directory looking for .git as
either a directory (normal repo) or a file (worktree/submodule). Falls back
to `git rev-parse --show-toplevel` if the walk fails.
"""
from __future__ import annotations

import functools
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click


def is_linux() -> bool:
    """Return True if the current platform is Linux.

    Returns:
        True when sys.platform is ``"linux"``, False otherwise.
    """
    return sys.platform == "linux"


def is_macos() -> bool:
    """Return True if the current platform is macOS.

    Returns:
        True when sys.platform is ``"darwin"``, False otherwise.
    """
    return sys.platform == "darwin"


def is_windows() -> bool:
    """Return True if the current platform is Windows.

    Returns:
        True when sys.platform is ``"win32"``, False otherwise.
    """
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# Platform dispatch (decorator + functional)
# ---------------------------------------------------------------------------
#
# Both forms exist so command authors can pick the ergonomics that fit:
#
#   - ``@platform_dispatch(...)`` is the primary surface. Wrap a Click
#     command and pass the three per-OS impls as kwargs; the decorator
#     routes the call to the matching impl at invocation time.
#
#   - ``dispatch(ctx, ...)`` is the escape hatch for when you need to run
#     pre-dispatch logic (loading config, validating args) *before* branching
#     on platform. It takes the same kwargs and forwards ``ctx`` as the
#     selected impl's first positional argument, matching the
#     ``@pass_cli_context`` command-callback structure.
#
# Both forms share ``_select_impl`` below so the detection + error-message
# logic lives in exactly one place.


def _select_impl(
    kwargs: dict[str, Any],
) -> tuple[Callable[..., Any] | None, str | None, str]:
    """Pick the per-OS impl and error message for the current platform.

    This helper is the single source of truth for platform-dispatch logic.
    Both ``platform_dispatch`` (decorator) and ``dispatch`` (functional)
    call it, so the two forms can never drift on either the detection
    mapping or the error-message defaults.

    Args:
        kwargs: The full kwargs dict passed to the decorator/functional form.
            Expected keys: ``linux``, ``windows``, ``macos`` (the impls) and
            optional ``linux_error``, ``windows_error``, ``macos_error``
            (custom UsageError messages when the matching impl is None).
            Keys are read via ``kwargs.get`` so callers may omit any key.

    Returns:
        A ``(impl, error_message, platform_name)`` triple:

        - ``impl`` is the callable to dispatch to, or None if the matching
          kwarg was None/missing.
        - ``error_message`` is the custom ``<platform>_error`` value if
          provided, otherwise None (callers should fall back to the default
          ``f"{platform_name} not supported"`` string).
        - ``platform_name`` is a human-readable name (``"linux"``, ``"windows"``,
          ``"macos"``, or the raw ``sys.platform`` string for unknown OSes).
          Unsupported platforms return ``(None, None, sys.platform)`` so the
          caller can raise ``UsageError`` uniformly.
    """
    # is_linux / is_macos / is_windows are intentionally reused here rather
    # than re-checking sys.platform inline: if those helpers ever gain new
    # logic (WSL detection, for example), dispatch picks it up for free.
    if is_linux():
        return kwargs.get("linux"), kwargs.get("linux_error"), "linux"
    if is_windows():
        return kwargs.get("windows"), kwargs.get("windows_error"), "windows"
    if is_macos():
        return kwargs.get("macos"), kwargs.get("macos_error"), "macos"
    # Unknown platform (e.g., "freebsd13", "cygwin"): return the raw platform
    # string as the name so the default error message is informative.
    return None, None, sys.platform


def _raise_unsupported(custom_message: str | None, platform_name: str) -> None:
    """Raise click.UsageError with a custom or default 'not supported' message.

    Extracted so decorator and functional forms share the exact same wording
    and exception type. click.UsageError is used (not a plain RuntimeError)
    because platform-unsupported is a user-facing error, not a framework
    bug -- Click prints it cleanly with exit code 2 and no traceback, which
    is what we want.

    Args:
        custom_message: The caller-supplied ``<platform>_error`` string, or
            None if the caller did not override the default message.
        platform_name: The platform name used to build the default message
            when ``custom_message`` is None.

    Raises:
        click.UsageError: Always. The message is either the custom message
            or the default ``f"{platform_name} not supported"`` string.
    """
    message = custom_message if custom_message is not None else f"{platform_name} not supported"
    raise click.UsageError(message)


def platform_dispatch(
    *,
    linux: Callable[..., Any] | None = None,
    windows: Callable[..., Any] | None = None,
    macos: Callable[..., Any] | None = None,
    linux_error: str | None = None,
    windows_error: str | None = None,
    macos_error: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a command so it dispatches to a per-OS implementation at call time.

    The decorated function's body is never executed -- it exists purely to
    carry the Click decorator stack (``@click.command``, ``@click.argument``,
    ``@click.option``, ``@pass_cli_context``, etc.) and define the signature
    that each platform impl must satisfy. At call time, the decorator detects
    the current platform via ``sys.platform`` (using ``is_linux``/
    ``is_windows``/``is_macos``) and forwards the caller's args/kwargs to the
    matching impl.

    The three ``*_error`` kwargs are part of the public API with no macOS
    carve-out -- any platform can opt out of support by passing
    ``<platform>=None`` plus an optional ``<platform>_error="..."`` message.
    When a custom message is not provided, the default is
    ``"<platform> not supported"``. The error is raised as
    ``click.UsageError`` so Click prints it cleanly and exits with code 2,
    matching clickwork's "user error, not framework bug" policy.

    Args:
        linux: Implementation to run on Linux (``sys.platform == "linux"``).
            Pass None to signal "not supported on this platform"; the call
            will raise ``click.UsageError`` when invoked on linux.
        windows: Implementation to run on Windows (``sys.platform == "win32"``,
            NOT ``"windows"``). Same None semantics as ``linux``.
        macos: Implementation to run on macOS (``sys.platform == "darwin"``).
            Same None semantics as ``linux``.
        linux_error: Custom UsageError message when invoked on linux with
            ``linux=None``. Defaults to ``"linux not supported"`` when None.
        windows_error: Custom UsageError message when invoked on windows with
            ``windows=None``. Defaults to ``"windows not supported"`` when None.
        macos_error: Custom UsageError message when invoked on macos with
            ``macos=None``. Defaults to ``"macos not supported"`` when None.

    Returns:
        A decorator that replaces the wrapped function with a dispatcher.
        The dispatcher preserves the wrapped function's metadata via
        ``functools.wraps`` so Click can still read ``__doc__`` / ``__name__``.

    Example::

        @clickwork.platform_dispatch(
            linux=my_lib.linux.up,
            windows=my_lib.windows.up,
            macos=my_lib.macos.up,
            macos_error="macOS not supported yet",
        )
        @click.command()
        @click.argument("name")
        @pass_cli_context
        def runner_up(ctx, name): ...
    """
    # Bundle the kwargs so the inner wrapper can hand them to _select_impl
    # without restating every key. Keeping this dict at decoration time means
    # each call site pays the per-platform lookup cost once per invocation,
    # not once per definition.
    dispatch_kwargs: dict[str, Any] = {
        "linux": linux,
        "windows": windows,
        "macos": macos,
        "linux_error": linux_error,
        "windows_error": windows_error,
        "macos_error": macos_error,
    }

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap ``func`` in a platform-dispatching shim.

        The shim is only invoked when Click calls the command; it does NOT
        execute ``func``'s body because the original function exists only
        to define the signature and carry the Click decorator stack. All
        real work happens in the selected per-OS impl.
        """
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            impl, error_message, platform_name = _select_impl(dispatch_kwargs)
            if impl is None:
                _raise_unsupported(error_message, platform_name)
            # Forward the caller's args/kwargs verbatim. This is what makes
            # signature forwarding "just work" -- Click's parsed options and
            # arguments flow straight through to the per-OS impl with the
            # same names the decorated function declared.
            return impl(*args, **kwargs)
        return wrapper

    return decorator


def dispatch(
    ctx: Any,
    *,
    linux: Callable[..., Any] | None = None,
    windows: Callable[..., Any] | None = None,
    macos: Callable[..., Any] | None = None,
    linux_error: str | None = None,
    windows_error: str | None = None,
    macos_error: str | None = None,
    **kwargs: Any,
) -> Any:
    """Functional escape hatch for platform-dispatching from inside a command.

    Use this form when you need to run pre-dispatch logic (loading config,
    validating args, printing a banner) *before* branching on platform. The
    decorator form (``@platform_dispatch``) is the primary surface; reach
    for ``dispatch`` only when the decorator's "body-is-never-run" semantics
    get in the way.

    The selected impl is called as ``impl(ctx, **kwargs)`` -- ``ctx`` is
    always forwarded as the first positional argument, matching the shape of
    ``@pass_cli_context`` command callbacks. Any extra keyword arguments
    passed to ``dispatch`` are forwarded to the impl alongside ``ctx``.

    The three ``*_error`` kwargs behave exactly like the decorator form:
    when the matching impl kwarg is None, ``click.UsageError`` is raised
    with the custom message if provided or ``f"{platform} not supported"``
    by default.

    Args:
        ctx: The command's context object (typically a ``CliContext``).
            Forwarded to the selected impl as its first positional argument.
        linux: Impl to call when ``sys.platform == "linux"``. None means
            unsupported (raises UsageError).
        windows: Impl to call when ``sys.platform == "win32"``. None means
            unsupported (raises UsageError).
        macos: Impl to call when ``sys.platform == "darwin"``. None means
            unsupported (raises UsageError).
        linux_error: Custom UsageError message for ``linux=None`` on linux.
        windows_error: Custom UsageError message for ``windows=None`` on win32.
        macos_error: Custom UsageError message for ``macos=None`` on darwin.
        **kwargs: Forwarded verbatim to the selected impl as keyword arguments.

    Returns:
        Whatever the selected impl returns.

    Raises:
        click.UsageError: If the current platform is unknown, or if the
            impl kwarg for the current platform is None.

    Example::

        def runner_up(ctx, name: str) -> None:
            ctx.logger.info("starting runner %s", name)
            clickwork.platform.dispatch(
                ctx,
                linux=my_lib.linux.up,
                windows=my_lib.windows.up,
                macos=my_lib.macos.up,
                macos_error="macOS not supported yet",
                name=name,
            )
    """
    # Reuse the shared selector so the detection mapping and error defaults
    # match the decorator form byte-for-byte.
    select_kwargs: dict[str, Any] = {
        "linux": linux,
        "windows": windows,
        "macos": macos,
        "linux_error": linux_error,
        "windows_error": windows_error,
        "macos_error": macos_error,
    }
    impl, error_message, platform_name = _select_impl(select_kwargs)
    if impl is None:
        _raise_unsupported(error_message, platform_name)
    # ctx is forwarded as the first positional arg; this matches the
    # @pass_cli_context callback shape that commands already use.
    return impl(ctx, **kwargs)


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up the directory tree to locate the repository root.

    Searches for a ``.git`` entry (directory or file) starting from ``start``
    and traversing toward the filesystem root. Handles:

    - Normal repos: ``.git`` is a directory.
    - Worktrees and submodules: ``.git`` is a file containing ``gitdir: ...``.

    Falls back to ``git rev-parse --show-toplevel`` if the directory walk
    does not find ``.git``, which covers edge cases like bare repos.

    Args:
        start: Directory to begin the search from. Defaults to the current
            working directory when None.

    Returns:
        The absolute Path to the repository root, or None if not found.
    """
    current = (start or Path.cwd()).resolve()

    # Walk up the directory tree looking for .git (file or directory).
    while current != current.parent:
        git_path = current / ".git"
        if git_path.exists():
            return current
        current = current.parent

    # Check the filesystem root too.
    if (current / ".git").exists():
        return current

    # Fallback: ask git directly. This handles edge cases the walk misses.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=start or Path.cwd(),
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

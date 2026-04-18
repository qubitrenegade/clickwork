"""CLI factory for clickwork.

create_cli() is the single entry point for building a CLI. It:
1. Creates a Click group with global flags (--verbose, --quiet, --dry-run, --env, --yes)
2. Discovers commands from directory and/or entry points
3. Sets up logging and loads config
4. Builds a CliContext and injects it into Click's ctx.obj
5. Binds convenience methods (run, capture, require, etc.) to the context
6. Wraps unhandled exceptions with exit code 2 (framework error)

Plugin authors call this once in their entry point script:

    from clickwork import create_cli
    cli = create_cli(name="orbit-admin", commands_dir=Path(__file__).parent / "commands")
"""

from __future__ import annotations

import functools
import importlib.metadata
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import click

from clickwork import prereqs as _prereqs
from clickwork._logging import setup_logging
from clickwork._types import CliContext, CliProcessError, PrerequisiteError, normalize_prefix
from clickwork.config import ConfigError, load_config
from clickwork.discovery import discover_commands
from clickwork.process import (
    capture as _capture,
)
from clickwork.process import (
    run as _run,
)
from clickwork.process import (
    run_with_confirm as _run_with_confirm,
)
from clickwork.process import (
    run_with_secrets as _run_with_secrets,
)
from clickwork.prompts import confirm as _confirm
from clickwork.prompts import confirm_destructive as _confirm_destructive

# Exit codes per spec:
# 0 = success
# 1 = user error (missing prereq, bad config, command failure)
# 2 = framework internal error (unhandled exception)
EXIT_USER_ERROR = 1
EXIT_FRAMEWORK_ERROR = 2


# Module-level wrapper for the lazy require binding (issue #8).
#
# WHY defined here and not inside create_cli(): the inner cli_group()
# factory runs every time the CLI is constructed, and defining a new
# wrapper function object there creates needless per-invocation garbage.
# A single module-level wrapper is created exactly once, and every
# CliContext reuses the same object.
#
# WHY a wrapper function that dispatches through _prereqs.require at call
# time, rather than binding cli_ctx.require to _prereqs.require directly:
# binding the imported reference freezes it at import time, so tests that
# do ``patch("clickwork.prereqs.require")`` silently have no effect -- the
# CliContext already holds the original function. Resolving the attribute
# through the ``_prereqs`` module object on every call means the patched
# function is picked up naturally, matching what test authors expect.
#
# WHY @functools.wraps(_prereqs.require): copies the signature and docstring
# from the import-time reference onto the wrapper (via __wrapped__), so
# ``inspect.signature(ctx.require)`` shows the real
# ``(binary: str, authenticated: bool = ...)`` parameters and IDE tooling
# can introspect it. The wrapper's *body* still goes through
# ``_prereqs.require`` on every call, so patching remains effective.
@functools.wraps(_prereqs.require)
def _require_via_prereqs(*args: Any, **kwargs: Any) -> None:
    return _prereqs.require(*args, **kwargs)


class MutuallyExclusive(click.Option):
    """Click option that is mutually exclusive with another option.

    Used to enforce that --verbose and --quiet cannot be passed together.
    Both options declare the other in their mutually_exclusive list, so
    whichever is processed second will detect the conflict.

    WHY a custom class instead of a callback: Click processes options in
    the order they appear on the command line, so a callback on --quiet
    would not see --verbose if --verbose appeared after --quiet. This
    class hooks into handle_parse_result() which runs after ALL options
    are parsed, so it sees the complete picture regardless of order.
    """

    def __init__(
        self,
        *args: Any,
        mutually_exclusive: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a Click option that enforces mutual exclusivity.

        Stores the list of conflicting option names for later checking in
        ``handle_parse_result()``, which runs after all CLI options are parsed.

        Args:
            *args: Positional arguments forwarded to click.Option.
            mutually_exclusive: List of other option names (Python identifiers,
                not flag strings) that conflict with this option.
            **kwargs: Keyword arguments forwarded to click.Option.
        """
        # Store the list of option names that this option conflicts with.
        # We'll check them in handle_parse_result() once all parsing is done.
        self._mutually_exclusive = mutually_exclusive or []
        super().__init__(*args, **kwargs)

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: Mapping[str, Any],
        args: list[str],
    ) -> tuple[Any, list[str]]:
        """Check for mutual exclusivity conflicts after all options are parsed.

        Raises a UsageError only when BOTH this option and a conflicting option
        have truthy values. Click includes all options in ``opts`` even when
        they carry falsy defaults (e.g., count=True starts at 0), so we check
        truthiness rather than key presence to avoid false positives.

        Args:
            ctx: The current Click context.
            opts: Dict of all parsed option values for this command.
            args: Remaining unparsed arguments.

        Returns:
            The result of the parent class ``handle_parse_result()`` call.

        Raises:
            click.UsageError: If this option and any option in
                ``_mutually_exclusive`` are both truthy.
        """
        # ``self.name`` is Optional[str] in Click's stubs but always set by
        # the time ``handle_parse_result`` runs (Click assigns it from the
        # declared param decls during Parameter.__init__); the guard below
        # keeps mypy happy without changing runtime behaviour.
        if self.name is None:
            return super().handle_parse_result(ctx, opts, args)
        current_value = opts.get(self.name)
        for other in self._mutually_exclusive:
            other_value = opts.get(other)
            if current_value and other_value:
                raise click.UsageError(f"--{self.name} and --{other} are mutually exclusive.")
        return super().handle_parse_result(ctx, opts, args)


def pass_cli_context(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that injects a CliContext into a Click command function.

    Safer than ``@click.pass_obj`` because it traverses the full Click
    context chain with ``find_object(CliContext)`` (works in deeply nested
    groups) and raises a descriptive UsageError instead of letting commands
    crash with ``AttributeError: 'NoneType' has no attribute 'dry_run'``
    when the CLI was not created through ``create_cli()``.

    Args:
        f: The Click command function to wrap. Its first positional argument
            must be typed as CliContext.

    Returns:
        A wrapped function compatible with Click's decorator stack.

    Usage:
        @click.command()
        @pass_cli_context
        def deploy(ctx: CliContext) -> None:
            ctx.run(["kubectl", "apply", "-f", "manifests/"])
    """

    @click.pass_context
    @functools.wraps(f)
    def wrapper(click_ctx: click.Context, *args: Any, **kwargs: Any) -> Any:
        """Resolve CliContext from the Click context chain and call f.

        Traverses the context chain with ``find_object(CliContext)`` so this
        works in deeply nested command groups. Raises a descriptive UsageError
        if no CliContext is found (i.e., the CLI was not built by create_cli()).

        Args:
            click_ctx: The Click context injected by @click.pass_context.
            *args: Additional positional arguments forwarded to f.
            **kwargs: Keyword arguments forwarded to f.

        Returns:
            Whatever the wrapped command function f returns.

        Raises:
            click.UsageError: If no CliContext object is found in the context
                chain, indicating the command is not running under create_cli().
        """
        # Traverse the Click context chain looking for a CliContext instance.
        # find_object() returns None if no matching object is found anywhere
        # in the parent chain -- not just in the immediate ctx.obj.
        cli_ctx = click_ctx.find_object(CliContext)
        if cli_ctx is None:
            raise click.UsageError(
                "CliContext is missing. Ensure the command is running under "
                "a CLI created by clickwork.create_cli()."
            )
        return f(cli_ctx, *args, **kwargs)

    return wrapper


def create_cli(
    name: str,
    commands_dir: Path | None = None,
    discovery_mode: str = "auto",
    config_schema: dict[str, Any] | None = None,
    repo_config_path: Path | None = None,
    *,
    description: str | None = None,
    enable_parent_package_imports: bool = False,
    strict: bool = False,
    version: str | None = None,
    package_name: str | None = None,
) -> click.Group:
    """Create a Click CLI group with global flags and plugin discovery.

    This is the main entry point for building a clickwork CLI. It returns
    a Click group that can be invoked directly or used as a console_scripts
    entry point.

    The group has these global flags available to every subcommand:
      --verbose / -v  (count, repeatable -- -v is INFO, -vv is DEBUG)
      --quiet / -q    (flag -- suppress all non-error output)
      --dry-run       (flag -- preview without executing)
      --env           (string -- select config environment)
      --yes / -y      (flag -- skip confirmation prompts)

    If ``version`` or ``package_name`` is provided, the CLI also gains
    ``-V`` / ``--version`` at the root level (wired via
    :func:`click.version_option`). When neither kwarg is set, no
    version flag is installed -- existing callers see no change.

    Args:
        name: CLI name (e.g., "orbit-admin"). Used for config paths and logging.
        commands_dir: Path to the commands directory for dev-mode discovery.
        discovery_mode: "dev", "installed", or "auto".
        config_schema: Optional config schema dict for validation.
        repo_config_path: Optional path to repo-level config file.
        description: Short help summary shown at the top of ``<cli> --help``.
            Keyword-only to preserve positional compatibility for existing
            callers that pass ``commands_dir`` positionally. When omitted
            (None), an empty string is passed to Click so it does NOT fall
            back to the inner cli_group callback's docstring, which is a
            developer-only implementation detail. Plugin authors should
            pass something like "Admin CLI for orbit" to give users a
            one-line summary of what the CLI does.
        enable_parent_package_imports: When True (and ``commands_dir`` is
            provided), prepend ``commands_dir.parent.parent`` (resolved)
            to ``sys.path`` so command files can import the parent
            package. For example, with the conventional layout
            ``project_root/tools/commands/*.py`` (where ``commands_dir``
            points at ``project_root/tools/commands``), this makes the
            ``tools`` package importable, so command files can write
            ``from tools.lib.X import Y`` without the CLI entry script
            having to manually poke sys.path. *Note:* we insert the
            **grandparent** of ``commands_dir`` -- the directory that
            *contains* the parent package -- not the parent itself; see
            the implementation comment below for why. Defaults to False
            (opt-in) so existing callers experience no change in import
            resolution. Keyword-only to keep the positional signature
            stable. Dedup uses the resolved path against ``sys.path``'s
            existing entries; repeated calls with the same ``commands_dir``
            don't stack duplicate entries (known limitation: the dedup
            does not normalize *existing* ``sys.path`` entries that were
            added via relative/unresolved spellings elsewhere).
        strict: When True, any command-discovery failure raises
            ``ClickworkDiscoveryError`` at CLI construction time instead of
            silently dropping the command with a warning. Failure modes
            that count: broken import, missing ``cli`` attribute, ``cli``
            not a Click command, duplicate command name WITHIN a single
            discovery mechanism (two files in the same ``commands/`` dir,
            or two installed entry points), and failed entry-point wraps.

            Scope note: in ``discovery_mode="auto"``, a name conflict
            BETWEEN a local command file and an installed entry point is
            intentional shadowing (local wins, the installed command is
            still reachable via fully-qualified import). This cross-
            mechanism shadowing is NOT a strict-mode failure; only
            same-mechanism duplicates are.

            Use this flag for production CLIs and release validation
            where shipping a binary with a missing command is a bug.
            Defaults to False to preserve the forgiving dev-mode
            behaviour so upgraders see no change unless they opt in.
            Keyword-only to keep the positional signature stable. See
            issue #42 for the full rationale.
        version: Explicit version string (e.g. ``"1.2.3"``). If provided,
            it is used verbatim as the value displayed by ``--version``.
            Takes precedence over ``package_name`` when both are set.
        package_name: Installed distribution name to auto-resolve the
            version from via :func:`importlib.metadata.version`. Only
            used when ``version`` is ``None``. Raises ``ValueError`` at
            ``create_cli()`` call time if the named distribution is not
            installed -- we prefer failing loud to silently omitting the
            flag, since a misspelled package name would otherwise go
            unnoticed until someone happened to run ``--version``.

    Returns:
        A configured Click group with all discovered commands registered.

    Raises:
        ClickworkDiscoveryError: If ``strict=True`` and discovery observed
            one or more failures while building the command tree. The
            exception carries a ``.failures`` list describing each
            problem so CI can print them all at once.
        ValueError: If ``package_name`` is provided (and ``version`` is
            ``None``) but the named distribution cannot be found by
            :mod:`importlib.metadata`.

    Example:
        Explicit version string::

            cli = create_cli(name="orbit-admin", version="1.2.3")

        Auto-resolve from the installing package's metadata::

            cli = create_cli(name="orbit-admin", package_name="orbit-admin")
    """

    # Resolve the version string to display via --version, if any.
    #
    # Three cases:
    #   1. ``version`` is set -> use it verbatim. Wins over package_name.
    #   2. ``version`` is None and ``package_name`` is set -> look it up
    #      via importlib.metadata. PackageNotFoundError is wrapped as
    #      ValueError so the caller gets a loud, actionable error at
    #      create_cli() time instead of a silent "no --version flag".
    #   3. Both None -> no --version flag is installed at all. This is
    #      the default and preserves behaviour for pre-#48 callers.
    #
    # WHY eagerly resolve (instead of letting click.version_option do it
    # at --version invocation time via its own ``package_name=``): Click
    # only consults importlib.metadata when the user actually runs
    # ``--version``. A typo in ``package_name`` would go undetected until
    # someone hits that code path (possibly in production). Eager
    # resolution at create_cli() call time surfaces the error at CLI
    # construction, which is always exercised on startup.
    resolved_version: str | None = None
    if version is not None:
        resolved_version = version
    elif package_name is not None:
        try:
            resolved_version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError as e:
            # Re-raise as ValueError with a message that names both the
            # missing distribution and the ``create_cli`` context, so
            # stack traces point at the real problem (a wrong
            # package_name=) rather than the internal metadata lookup.
            raise ValueError(
                f"create_cli(package_name={package_name!r}) could not resolve a version: "
                f"distribution {package_name!r} is not installed. "
                "Pass an explicit version= string, or ensure the package name matches "
                "the distribution name on PyPI / in pyproject.toml's [project] table."
            ) from e

    # Optionally make commands_dir's parent package importable.
    #
    # WHY: plugin authors typically lay out their project as
    #
    #     project_root/
    #       tools/           (commands_dir.parent)
    #         my_cli         (entry script)
    #         commands/      (commands_dir -- per-command .py files)
    #         lib/           (shared helpers)
    #
    # and want their command files to write ``from tools.lib.X import Y``
    # without the entry script prepending project_root to sys.path.
    # Setting ``enable_parent_package_imports=True`` does that prepend here, once,
    # at CLI-construction time.
    #
    # WHY grandparent, not parent: to make ``tools/`` importable *as a
    # package* (enabling ``from tools.lib.X import Y``), the directory
    # that CONTAINS ``tools/`` has to be on sys.path -- that's
    # ``commands_dir.parent.parent`` (project_root). Inserting just
    # ``commands_dir.parent`` would only enable ``import lib`` (sibling
    # top-level imports), which is a different, less useful feature
    # than what issue #15 asked for.
    #
    # WHY .resolve() + dedup: the same directory can be reached via
    # different strings -- ``./project`` vs ``/abs/path/project`` vs a
    # symlinked path -- depending on the caller's CWD. Resolving to the
    # absolute canonical path before comparing against sys.path ensures
    # repeat calls don't stack duplicate entries that would shadow each
    # other during import resolution.
    if enable_parent_package_imports and commands_dir is not None:
        project_root = str(commands_dir.parent.parent.resolve())
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    # Resolve the help text shown by ``<cli> --help``.
    #
    # WHY an explicit fallback to "": Click's @click.group() decorator falls
    # back to the callback function's __doc__ when ``help=`` is None. That
    # behaviour would leak the inner cli_group() docstring -- which documents
    # internal callback args like ctx/verbose/quiet -- to end users. Passing
    # an empty string forces Click to render no description at all, instead
    # of scraping the developer-facing docstring (issue #4).
    group_help = description if description is not None else ""

    # Define the group callback as a local function so that 'name', 'config_schema',
    # and 'repo_config_path' from the outer scope are captured in the closure.
    # This is the standard Click pattern for parameterised group factories.
    @click.group(name=name, help=group_help)
    @click.option(
        "--verbose",
        "-v",
        count=True,
        # count=True means -v gives 1, -vv gives 2, etc.
        # We map these to INFO / DEBUG in setup_logging().
        help="Increase log verbosity (-v for info, -vv for debug).",
        cls=MutuallyExclusive,
        mutually_exclusive=["quiet"],
    )
    @click.option(
        "--quiet",
        "-q",
        is_flag=True,
        default=False,
        help="Suppress non-error output.",
        cls=MutuallyExclusive,
        # NOTE: the name Click uses internally for 'verbose' with count=True
        # is the parameter name 'verbose', not '--verbose'. We must use the
        # Python identifier here, not the flag string.
        mutually_exclusive=["verbose"],
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Preview actions without executing.",
    )
    @click.option(
        "--env",
        default=None,
        help="Select config environment (e.g., staging, production).",
    )
    @click.option(
        "--yes",
        "-y",
        is_flag=True,
        default=False,
        help="Skip confirmation prompts.",
    )
    @click.pass_context
    def cli_group(
        ctx: click.Context,
        verbose: int,
        quiet: bool,
        dry_run: bool,
        env: str | None,
        yes: bool,
    ) -> None:
        """CLI entry point -- configure logging, load config, and build CliContext.

        Runs before every subcommand. Sets up the logger, loads layered config
        from TOML files and environment variables, builds a CliContext, binds
        all convenience helpers to it, and stores it in Click's ctx.obj so
        subcommands can receive it via @pass_cli_context.

        Args:
            ctx: The current Click context (injected by @click.pass_context).
            verbose: Number of -v flags passed (0=WARNING, 1=INFO, 2=DEBUG).
            quiet: Whether --quiet was passed (overrides verbose; ERROR only).
            dry_run: Whether --dry-run was passed.
            env: The selected config environment string, or None.
            yes: Whether --yes was passed to skip confirmation prompts.
        """
        # Setup logging first so any errors below are properly formatted.
        # setup_logging() configures both the log level and output format
        # based on the --verbose / --quiet flags.
        logger = setup_logging(verbose=verbose, quiet=quiet, name=name)

        # Resolve the env var fallback BEFORE constructing CliContext so
        # ctx.env reflects the actual environment in use -- not just the
        # --env flag value.  Without this, ctx.env stays None when the env
        # is selected via {PROJECT_NAME}_ENV, even though load_config()
        # applies the env-specific config section correctly.
        if env is None:
            prefix = normalize_prefix(name)
            env = os.environ.get(f"{prefix}_ENV")

        # Load config from layered sources (user config, repo config, env vars).
        # If loading fails due to a schema violation or bad permissions, we exit
        # with code 1 (user error) with a clear message -- not a traceback.
        try:
            config = load_config(
                project_name=name,
                repo_config_path=repo_config_path,
                env=env,
                schema=config_schema,
            )
        except ConfigError as e:
            logger.error("Config error: %s", e)
            ctx.exit(EXIT_USER_ERROR)

        # Build the CliContext with all resolved state from this invocation.
        # CliContext is a dataclass; all fields have defaults so we only
        # set what create_cli() knows about.
        cli_ctx = CliContext(
            config=config,
            env=env,
            dry_run=dry_run,
            verbose=verbose,
            quiet=quiet,
            yes=yes,
            logger=logger,
        )

        # Bind convenience methods to the CliContext's callable fields.
        # These lambdas close over cli_ctx so they automatically pick up
        # the current dry_run / yes values without the caller passing them.
        #
        # WHY lambdas instead of functools.partial: partial() freezes
        # keyword arguments at bind time, but we want them evaluated at
        # call time (from cli_ctx which could in theory be mutated later).
        # Lambdas defer the lookup to when the method is actually called.
        # stdin_text / stdin_bytes are forwarded through so callers can use
        # ctx.run(cmd, stdin_text=secret) for the secrets-via-stdin pattern
        # (wrangler secret put, gh auth login --with-token, etc.) without
        # reaching around ctx to the underlying process.run() helper.
        cli_ctx.run = lambda cmd, env=None, *, stdin_text=None, stdin_bytes=None: _run(
            cmd,
            dry_run=cli_ctx.dry_run,
            env=env,
            stdin_text=stdin_text,
            stdin_bytes=stdin_bytes,
        )
        cli_ctx.capture = lambda cmd, env=None: _capture(cmd, dry_run=cli_ctx.dry_run, env=env)

        # require() has no dry_run / yes concept -- it's always a live check.
        # The call site is ctx.require("docker") not ctx.require("docker", dry_run=...).
        # Uses the module-level _require_via_prereqs wrapper (defined above)
        # so patching clickwork.prereqs.require takes effect even though the
        # binding here looks like a frozen reference. See that wrapper's
        # comment block for the full "why" (issue #8).
        cli_ctx.require = _require_via_prereqs

        # confirm() and confirm_destructive() close over yes so --yes propagates.
        cli_ctx.confirm = lambda msg: _confirm(msg, yes=cli_ctx.yes)
        cli_ctx.confirm_destructive = lambda msg: _confirm_destructive(msg, yes=cli_ctx.yes)

        # Thin wrapper around the standalone run_with_confirm() that supplies
        # cli_ctx's flags. This keeps the logic single-sourced in process.py
        # (confirmation + execution + dry-run + signal forwarding) while still
        # letting ctx.run_with_confirm(cmd, msg) work without extra args.
        cli_ctx.run_with_confirm = (
            lambda cmd, msg, env=None, *, stdin_text=None, stdin_bytes=None: (  # noqa: E501
                _run_with_confirm(
                    cmd,
                    msg,
                    yes=cli_ctx.yes,
                    dry_run=cli_ctx.dry_run,
                    env=env,
                    stdin_text=stdin_text,
                    stdin_bytes=stdin_bytes,
                )
            )
        )

        # run_with_secrets: safety-focused wrapper for subprocesses that
        # need sensitive input. The forwarding lambda captures cli_ctx.dry_run
        # (so --dry-run at the CLI level short-circuits secret delivery too)
        # and accepts ``env=`` as a non-secret passthrough, matching the shape
        # of the other bindings above. secrets / stdin_secret are keyword-only
        # at the helper level; we mirror that here.
        cli_ctx.run_with_secrets = lambda cmd, *, secrets, stdin_secret=None, env=None: (
            _run_with_secrets(
                cmd,
                secrets=secrets,
                stdin_secret=stdin_secret,
                dry_run=cli_ctx.dry_run,
                env=env,
            )
        )

        # Attach the CliContext to Click's ctx.obj so all subcommands can
        # receive it via @click.pass_obj or @pass_cli_context.
        ctx.obj = cli_ctx

    # Install --version / -V if a version string was resolved above.
    #
    # WHY this lives here (after the @click.group decorator ran, before
    # discover_commands): click.version_option() returns a decorator that
    # wraps a Command. We already have the decorated group object
    # ``cli_group``, so applying the decorator in-place (``cli_group =
    # click.version_option(...)(cli_group)``) installs the option on the
    # exact group we're about to return. Doing this after command
    # discovery would also work, but grouping all group-level option
    # wiring in one place keeps the factory's structure easier to read.
    #
    # WHY we pass the already-resolved string (not package_name) to
    # click.version_option: Click would otherwise defer the
    # importlib.metadata lookup until --version is actually invoked --
    # see the resolution block at the top of this function for why we
    # want the error surfaced at construction time instead.
    #
    # WHY prog_name=name: without it, Click derives the prog name from
    # the command's invocation path (e.g. ``python -m orbit_admin``),
    # which is rarely what the end user expects. Forcing ``name`` makes
    # ``--version`` output read ``<cli-name>, version <version>`` no
    # matter how the CLI was launched.
    if resolved_version is not None:
        cli_group = click.version_option(
            resolved_version,
            "-V",
            "--version",
            prog_name=name,
        )(cli_group)

    # Discover and register commands from the commands directory and/or
    # installed entry points, depending on the discovery_mode setting.
    # This runs at factory time (not at invocation time) so the commands
    # appear in --help output immediately.
    #
    # ``strict=True`` propagates through the discovery layer: any failure
    # becomes a ClickworkDiscoveryError raised from right here, before
    # create_cli() returns. That's the behaviour issue #42 specified --
    # a broken discovery fails at CLI startup rather than at "user runs
    # the missing command and gets 'no such command'" time.
    commands = discover_commands(
        commands_dir=commands_dir,
        discovery_mode=discovery_mode,
        strict=strict,
    )
    for cmd_name, cmd in commands.items():
        cli_group.add_command(cmd, cmd_name)

    # Install a custom exception handler that wraps unhandled exceptions
    # with exit code 2 (framework error) vs exit code 1 (user error).
    #
    # WHY patch invoke() instead of using Click's result_callback or
    # standalone_mode exception_handler: standalone_mode=False removes
    # Click's built-in exception handling entirely. We want Click to still
    # handle UsageError, Exit, and Abort normally -- we only want to intercept
    # unexpected RuntimeError and similar bugs.
    original_invoke = cli_group.invoke

    def wrapped_invoke(ctx: click.Context) -> Any:
        """Invoke the CLI group and classify any unhandled exceptions.

        Known exception types are routed by semantic category:

        - ``CliProcessError`` and ``PrerequisiteError`` are our own user-error
          signals -- emit the message and exit 1 without a traceback.
        - ``click.exceptions.Exit`` and ``click.exceptions.Abort`` are normal
          Click control-flow exceptions and are re-raised so Click handles them.
        - ``click.exceptions.ClickException`` (and subclasses like UsageError,
          FileError, BadParameter) are also user errors. We re-raise them so
          Click's native handler formats the message with its own "Error:"
          prefix (plus a "Usage:" hint for UsageError) and exits with the
          subclass's own ``exit_code`` attribute (1 for most, 2 for UsageError).
          Before issue #5 these fell through to the generic catch-all below
          and got stamped with "Internal error:" + exit 2, hiding the real
          message and implying a framework bug.
        - Any OTHER exception is treated as an unexpected framework bug and
          exits with code 2 (EXIT_FRAMEWORK_ERROR) after printing a short
          message to stderr.

        Args:
            ctx: The current Click context passed to the group's invoke().

        Returns:
            Whatever the original invoke() returns on success.
        """
        try:
            return original_invoke(ctx)
        except (CliProcessError, PrerequisiteError) as e:
            # CliProcessError = a subprocess returned non-zero.
            # PrerequisiteError = a required tool is missing or not authenticated.
            # Both are user errors: emit the message and exit 1 without a traceback.
            click.echo(str(e), err=True)
            ctx.exit(EXIT_USER_ERROR)
        except click.exceptions.Exit:
            # Normal Click exit (e.g., from ctx.exit(0) or ctx.exit(1)).
            # Don't intercept -- let Click propagate the requested code.
            raise
        except click.exceptions.Abort:
            # User pressed Ctrl-C at a confirmation prompt.
            # Don't intercept -- Click handles this with a clean "Aborted!" message.
            raise
        except click.exceptions.ClickException:
            # ClickException covers UsageError, BadParameter, FileError, etc.
            # These are user errors, NOT framework bugs. Re-raise so Click's
            # own standalone_mode handler formats them (with "Error:" prefix
            # and, for UsageError, a "Usage: ... --help" hint) and uses the
            # subclass's ``exit_code`` attribute (default 1; UsageError's is 2).
            #
            # IMPORTANT: this clause MUST come before ``except Exception`` --
            # ClickException inherits from Exception, so the generic clause
            # would otherwise shadow it and we'd be right back where we
            # started (issue #5).
            raise
        except Exception as e:
            # Everything else is an unexpected framework bug.
            # Print to stderr (not stdout) so it doesn't pollute captured output.
            click.echo(f"Internal error: {e}", err=True)
            ctx.exit(EXIT_FRAMEWORK_ERROR)

    # Deliberate method assignment: wrapped_invoke intercepts unhandled
    # exceptions before they reach Click's default handler. Standard library
    # method-assignment is the least-invasive way to splice in the wrapper
    # without subclassing click.Group (which would force every caller into a
    # custom class hierarchy). mypy flags this as [method-assign] in strict
    # mode because instance-level method overrides bypass normal method-
    # resolution -- here that's the point.
    cli_group.invoke = wrapped_invoke  # type: ignore[method-assign]

    return cli_group

"""CLI factory for qbrd-tools.

create_cli() is the single entry point for building a CLI. It:
1. Creates a Click group with global flags (--verbose, --quiet, --dry-run, --env, --yes)
2. Discovers commands from directory and/or entry points
3. Sets up logging and loads config
4. Builds a CliContext and injects it into Click's ctx.obj
5. Binds convenience methods (run, capture, require, etc.) to the context
6. Wraps unhandled exceptions with exit code 2 (framework error)

Plugin authors call this once in their entry point script:

    from qbrd_tools import create_cli
    cli = create_cli(name="orbit-admin", commands_dir=Path(__file__).parent / "commands")
"""
from __future__ import annotations

import functools
from pathlib import Path

import click

from qbrd_tools._logging import setup_logging
from qbrd_tools._types import CliContext, CliProcessError
from qbrd_tools.config import ConfigError, load_config
from qbrd_tools.discovery import discover_commands
from qbrd_tools.process import capture as _capture, run as _run
from qbrd_tools.prereqs import require as _require
from qbrd_tools.prompts import confirm as _confirm, confirm_destructive as _confirm_destructive


# Exit codes per spec:
# 0 = success
# 1 = user error (missing prereq, bad config, command failure)
# 2 = framework internal error (unhandled exception)
EXIT_USER_ERROR = 1
EXIT_FRAMEWORK_ERROR = 2


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

    def __init__(self, *args, mutually_exclusive: list[str] | None = None, **kwargs):
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

    def handle_parse_result(self, ctx, opts, args):
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
        current_value = opts.get(self.name)
        for other in self._mutually_exclusive:
            other_value = opts.get(other)
            if current_value and other_value:
                raise click.UsageError(
                    f"--{self.name} and --{other} are mutually exclusive."
                )
        return super().handle_parse_result(ctx, opts, args)


def pass_cli_context(f):
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
    def wrapper(click_ctx, *args, **kwargs):
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
                "a CLI created by qbrd_tools.create_cli()."
            )
        return f(cli_ctx, *args, **kwargs)
    return wrapper


def create_cli(
    name: str,
    commands_dir: Path | None = None,
    discovery_mode: str = "auto",
    config_schema: dict | None = None,
    repo_config_path: Path | None = None,
) -> click.Group:
    """Create a Click CLI group with global flags and plugin discovery.

    This is the main entry point for building a qbrd-tools CLI. It returns
    a Click group that can be invoked directly or used as a console_scripts
    entry point.

    The group has these global flags available to every subcommand:
      --verbose / -v  (count, repeatable -- -v is INFO, -vv is DEBUG)
      --quiet / -q    (flag -- suppress all non-error output)
      --dry-run       (flag -- preview without executing)
      --env           (string -- select config environment)
      --yes / -y      (flag -- skip confirmation prompts)

    Args:
        name: CLI name (e.g., "orbit-admin"). Used for config paths and logging.
        commands_dir: Path to the commands directory for dev-mode discovery.
        discovery_mode: "dev", "installed", or "auto".
        config_schema: Optional config schema dict for validation.
        repo_config_path: Optional path to repo-level config file.

    Returns:
        A configured Click group with all discovered commands registered.
    """

    # Define the group callback as a local function so that 'name', 'config_schema',
    # and 'repo_config_path' from the outer scope are captured in the closure.
    # This is the standard Click pattern for parameterised group factories.
    @click.group(name=name)
    @click.option(
        "--verbose", "-v",
        count=True,
        # count=True means -v gives 1, -vv gives 2, etc.
        # We map these to INFO / DEBUG in setup_logging().
        help="Increase log verbosity (-v for info, -vv for debug).",
        cls=MutuallyExclusive,
        mutually_exclusive=["quiet"],
    )
    @click.option(
        "--quiet", "-q",
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
        "--yes", "-y",
        is_flag=True,
        default=False,
        help="Skip confirmation prompts.",
    )
    @click.pass_context
    def cli_group(ctx: click.Context, verbose: int, quiet: bool, dry_run: bool, env: str | None, yes: bool) -> None:
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
            return

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
        cli_ctx.run = lambda cmd, env=None: _run(cmd, dry_run=cli_ctx.dry_run, env=env)
        cli_ctx.capture = lambda cmd, env=None: _capture(cmd, dry_run=cli_ctx.dry_run, env=env)

        # require() has no dry_run / yes concept -- it's always a live check.
        # We bind it directly so the call site is ctx.require("docker") not
        # ctx.require("docker", dry_run=...).
        cli_ctx.require = _require

        # confirm() and confirm_destructive() close over yes so --yes propagates.
        cli_ctx.confirm = lambda msg: _confirm(msg, yes=cli_ctx.yes)
        cli_ctx.confirm_destructive = lambda msg: _confirm_destructive(msg, yes=cli_ctx.yes)

        # run_with_confirm on the context uses the framework's TTY-aware
        # confirm() directly through the closure -- no module-level mutation
        # needed.  This means multiple CLI instances in the same process each
        # carry their own confirm function and never share state.
        def _ctx_run_with_confirm(
            cmd: list,
            msg: str,
            env: dict | None = None,
        ):
            """Confirm then run, using the framework's TTY-aware prompt."""
            if not _confirm(msg, yes=cli_ctx.yes):
                return None
            return _run(cmd, dry_run=cli_ctx.dry_run, env=env)

        cli_ctx.run_with_confirm = _ctx_run_with_confirm

        # Attach the CliContext to Click's ctx.obj so all subcommands can
        # receive it via @click.pass_obj or @pass_cli_context.
        ctx.obj = cli_ctx

    # Discover and register commands from the commands directory and/or
    # installed entry points, depending on the discovery_mode setting.
    # This runs at factory time (not at invocation time) so the commands
    # appear in --help output immediately.
    commands = discover_commands(
        commands_dir=commands_dir,
        discovery_mode=discovery_mode,
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

    def wrapped_invoke(ctx: click.Context):
        """Invoke the CLI group and classify any unhandled exceptions.

        Known exception types (CliProcessError, Click's Exit and Abort) are
        handled explicitly so Click's normal handlers surface them with the
        correct exit codes. Any other unexpected exception is treated as a
        framework bug and exits with code 2 (EXIT_FRAMEWORK_ERROR) after
        printing a short message to stderr.

        Args:
            ctx: The current Click context passed to the group's invoke().

        Returns:
            Whatever the original invoke() returns on success.
        """
        try:
            return original_invoke(ctx)
        except CliProcessError as e:
            # CliProcessError = a subprocess returned non-zero.
            # Emit the human-readable message and exit 1 without a traceback.
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
        except Exception as e:
            # Everything else is an unexpected framework bug.
            # Print to stderr (not stdout) so it doesn't pollute captured output.
            click.echo(f"Internal error: {e}", err=True)
            ctx.exit(EXIT_FRAMEWORK_ERROR)

    cli_group.invoke = wrapped_invoke

    return cli_group

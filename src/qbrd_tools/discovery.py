"""Plugin discovery for qbrd-tools CLIs.

Two mechanisms find Click commands:

1. **Directory scanning (dev mode):** Import .py files from a commands/ dir,
   look for a 'cli' attribute (Click command or group). Non-recursive --
   subdirectories like lib/ are skipped. Files without 'cli' produce a warning.

2. **Entry points (installed mode):** Read the 'qbrd_tools.commands' entry
   point group from installed packages.

The discovery_mode parameter controls which are active:
- "dev": directory only
- "installed": entry points only
- "auto" (default): entry points always, PLUS directory scanning when
  commands_dir exists. Both mechanisms run concurrently; local (directory)
  commands win on name conflicts and an info message is logged.

In a typical dev workflow the package is pip-installed (editable or
otherwise), so entry points exist AND a commands/ directory is present.
Running both and letting local commands shadow installed ones is the most
useful default.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import click

logger = logging.getLogger("qbrd_tools")

# The entry point group name that plugin packages use to register commands.
# Plugin authors add this to their pyproject.toml [project.entry-points] table.
ENTRY_POINT_GROUP = "qbrd_tools.commands"


class LazyEntryPointCommand(click.Command):
    """Lightweight proxy that loads the real entry-point command on demand.

    WHY lazy loading: At startup, we only have entry point metadata (name,
    dotted path). Eagerly importing every installed plugin would slow startup
    and import unrelated packages for every command invocation. Instead we
    wrap each entry point in this proxy so the real module loads only when
    the user actually runs that command (or requests --help for it).
    """

    def __init__(self, ep: importlib.metadata.EntryPoint) -> None:
        """Create a lazy proxy for an entry point command.

        Initialises with a stub callback and passthrough context settings so
        Click can register this proxy in a group at startup without importing
        the plugin module. The real command is loaded on first invocation.

        Args:
            ep: The entry point metadata object from importlib.metadata.
        """
        # Initialize with a stub callback and passthrough context settings.
        # The real command takes over in invoke() before any callback runs.
        super().__init__(
            name=ep.name,
            callback=self._invoke_loaded,
            add_help_option=False,
            context_settings={
                "ignore_unknown_options": True,
                "allow_extra_args": True,
            },
        )
        self._entry_point = ep
        # Cache the loaded command so we only import it once per process.
        self._loaded: click.Command | None = None

    def _load(self) -> click.Command:
        """Import and cache the real Click command behind this entry point.

        The result is cached in ``self._loaded`` so subsequent calls do not
        re-import the plugin module. This is the single place where the lazy
        load actually happens.

        Returns:
            The fully-initialised Click command or group from the plugin.

        Raises:
            TypeError: If the entry point loads successfully but the resulting
                object is not a Click BaseCommand.
        """
        if self._loaded is None:
            obj = self._entry_point.load()
            if not isinstance(obj, click.Command):
                raise TypeError(
                    f"Entry point '{self._entry_point.name}' did not load a Click command"
                )
            self._loaded = obj
        return self._loaded

    def _invoke_loaded(self, *args, **kwargs):
        """Stub callback that should never be reached.

        invoke() delegates to the real command before Click reaches the
        callback stage, so this method is a safety net rather than a
        normal execution path.

        Raises:
            RuntimeError: Always -- indicates a bug in the lazy-loading logic.
        """
        raise RuntimeError("LazyEntryPointCommand callback should not be called directly")

    def invoke(self, ctx: click.Context):
        """Load the real command and delegate execution to it.

        The proxy deliberately does not pre-parse the real command's options.
        That keeps installed-mode behavior aligned with the real command,
        which then parses the original argv itself. We pass ``obj=ctx.obj``
        so the CliContext built by ``create_cli()`` is propagated into the
        real command's context and ``@pass_cli_context`` / ``@click.pass_obj``
        keep working.

        Args:
            ctx: The Click context, whose ``ctx.args`` contains the
                unparsed extra arguments collected by the proxy and whose
                ``ctx.obj`` holds the CliContext built by create_cli().

        Returns:
            Whatever the real command's ``main()`` returns.
        """
        loaded = self._load()
        # Pass obj=ctx.obj so the new context created by loaded.main() carries
        # the CliContext forward.  Click forwards **extra kwargs through
        # make_context() -> Context(), and Context accepts obj as a keyword arg.
        return loaded.main(
            args=list(ctx.args),
            prog_name=ctx.command_path,
            standalone_mode=False,
            obj=ctx.obj,
        )

    def get_short_help_str(self, limit: int = 45) -> str:
        """Return the short help string from the real command.

        Called by Click when rendering the parent group's help listing, so
        the lazy proxy shows the plugin's actual help text rather than a stub.

        Args:
            limit: Maximum character width for the short help string.

        Returns:
            The real command's short help string, truncated to limit chars.
        """
        return self._load().get_short_help_str(limit)

    def get_help(self, ctx: click.Context) -> str:
        """Return the full help text from the real command.

        Called when the user runs ``qbrd <cmd> --help``. Loading the real
        command here ensures the full docstring and option list are shown.

        Args:
            ctx: The current Click context.

        Returns:
            The real command's full formatted help string.
        """
        return self._load().get_help(ctx)


def discover_commands_from_dir(commands_dir: Path) -> dict[str, click.Command]:
    """Scan a directory for .py files that export a 'cli' Click command.

    Only top-level .py files are checked -- subdirectories are skipped.
    __init__.py files are skipped (they start with '_'). Files without a
    'cli' attribute produce a stderr warning (they probably shouldn't be
    in commands/). Import errors also produce a stderr warning so the CLI
    remains usable even if one plugin is broken.

    WHY stderr warnings instead of exceptions: A broken or incomplete command
    file should not prevent the rest of the CLI from loading. Users get a
    clear signal without a hard crash.

    Args:
        commands_dir: Path to the commands directory.

    Returns:
        Dict mapping command name -> Click command/group.
    """
    commands: dict[str, click.Command] = {}

    # Guard: if the directory doesn't exist, return empty without error.
    # discover_commands() already handles the "auto" fallback logic.
    if not commands_dir.is_dir():
        return commands

    # Make the private discovery namespace behave like a real package so
    # modules inside commands/ can use sibling-relative imports such as
    # `from .helper import ...`.
    package_name = "qbrd_tools._discovered"
    package = sys.modules.get(package_name)
    dir_path = str(commands_dir)
    if package is None:
        package = ModuleType(package_name)
        package.__path__ = [dir_path]  # type: ignore[attr-defined]
        sys.modules[package_name] = package
    else:
        package_path = list(getattr(package, "__path__", []))
        if dir_path not in package_path:
            package_path.append(dir_path)
            package.__path__ = package_path  # type: ignore[attr-defined]

    for py_file in sorted(commands_dir.glob("*.py")):
        # Skip __init__.py, __main__.py, and any other dunder files.
        # These are package plumbing, not command entry points.
        if py_file.name.startswith("_"):
            continue

        # Build a unique module name to avoid collisions in sys.modules.
        # Using a private sub-namespace means these never clash with real
        # installed packages, even if the filename matches one.
        module_name = f"qbrd_tools._discovered.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            # spec is None for paths Python can't interpret as modules.
            continue

        module = importlib.util.module_from_spec(spec)
        # Register under the private discovery package before exec so
        # relative imports can resolve sibling helper modules.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as e:
            # Covers ImportError, SyntaxError, and any runtime error at
            # module-top-level. Keep going so other commands still load.
            print(
                f"Warning: failed to import {py_file.name}: {e}",
                file=sys.stderr,
            )
            # Clean up the partial entry to avoid stale modules in sys.modules.
            sys.modules.pop(module_name, None)
            continue

        cli_attr = getattr(module, "cli", None)
        if cli_attr is None:
            # Warn: the file is in the commands dir but doesn't export 'cli'.
            # This is likely a helper that should live in lib/ instead.
            print(
                f"Warning: {py_file.name} has no 'cli' attribute. "
                f"Command files must export a Click command or group as 'cli'. "
                f"If this is a helper module, move it to lib/.",
                file=sys.stderr,
            )
            continue

        if not isinstance(cli_attr, click.Command):
            # Warn: the file has a 'cli' attribute but it's not a Click command.
            print(
                f"Warning: {py_file.name} 'cli' attribute is not a Click command "
                f"(got {type(cli_attr).__name__}). Skipping.",
                file=sys.stderr,
            )
            continue

        # Key by the exposed Click command name so discovery is consistent
        # with installed entry points. Fall back to the filename only if the
        # command object has no explicit name.
        cmd_name = cli_attr.name or py_file.stem
        commands[cmd_name] = cli_attr

    return commands


def discover_commands_from_entrypoints() -> dict[str, click.Command]:
    """Discover commands from installed packages via the entry points mechanism.

    Reads the ``qbrd_tools.commands`` entry point group from all installed
    packages. Each entry point should reference a Click command or group.
    Entry points are wrapped in ``LazyEntryPointCommand`` proxies so startup
    does not trigger imports of every installed plugin.

    WHY entry points: This is the standard Python plugin mechanism. Plugin
    authors declare their commands in ``pyproject.toml`` and pip handles the
    wiring -- no config file or explicit registration is needed. Consumers
    install a plugin package and its commands immediately appear in the CLI.

    Returns:
        Dict mapping command name to a lazy-loading Click command/group proxy.
    """
    commands: dict[str, click.Command] = {}

    try:
        # Python 3.12+ API: entry_points(group=...) returns a sequence.
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        # Python 3.9 compat: older API signature returns a dict of lists.
        all_eps = importlib.metadata.entry_points()
        eps = all_eps.get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]

    for ep in eps:
        try:
            # Wrap in a lazy proxy -- don't actually import the plugin yet.
            commands[ep.name] = LazyEntryPointCommand(ep)
        except Exception as e:
            logger.warning("Failed to load entry point '%s': %s", ep.name, e)

    return commands


def discover_commands(
    commands_dir: Path | None = None,
    discovery_mode: str = "auto",
) -> dict[str, click.Command]:
    """Discover commands using the selected mechanism.

    This is the main entry point for the discovery system. It orchestrates
    directory scanning and/or entry point discovery based on the caller's
    chosen mode, then merges the results with the conflict resolution policy:
    local (directory) commands always win over installed (entry point) commands.

    WHY local wins: During development you often want to test a new version of
    a command that's also installed system-wide. The local file should shadow
    the installed one without requiring an uninstall/reinstall cycle.

    Args:
        commands_dir: Path for directory scanning (dev/auto mode).
        discovery_mode: Controls which mechanism(s) are used.
            "dev"       -- directory scanning only (ignores entry points)
            "installed" -- entry points only (ignores commands_dir)
            "auto"      -- entry points always, plus directory if commands_dir exists

    Returns:
        Dict mapping command name -> Click command/group.

    Raises:
        ValueError: If discovery_mode is not one of the accepted values.
    """
    commands: dict[str, click.Command] = {}

    # Resolve which mechanisms to activate before doing any I/O.
    use_dir = False
    use_ep = False

    if discovery_mode == "dev":
        # Dev mode: directory scanning only. Entry points are ignored so
        # installed plugins don't interfere with local development.
        use_dir = True
    elif discovery_mode == "installed":
        # Installed mode: entry points only. For production deploys where
        # a commands/ directory doesn't exist on the file system.
        use_ep = True
    elif discovery_mode == "auto":
        # Auto mode: always check entry points, and ALSO scan the directory
        # when it exists. This lets local commands shadow installed ones with
        # a conflict warning -- the primary use case for the shadow log.
        # WHY both: In a typical dev workflow the package is also pip-installed
        # (editable or otherwise), so entry points exist AND the commands/ dir
        # exists. Running both and logging conflicts is the most useful behavior.
        use_ep = True
        if commands_dir and commands_dir.is_dir():
            use_dir = True
    else:
        raise ValueError(f"Invalid discovery_mode: {discovery_mode!r}")

    # Load entry-point commands first so directory commands can shadow them.
    if use_ep:
        commands.update(discover_commands_from_entrypoints())

    if use_dir and commands_dir:
        dir_commands = discover_commands_from_dir(commands_dir)

        for name, cmd in dir_commands.items():
            if name in commands:
                # Log at INFO (not WARNING) because shadowing is expected and
                # intentional during dev. It's not a problem, just informational.
                logger.info(
                    "Local command '%s' shadows installed plugin command. "
                    "The local version will be used.",
                    name,
                )
            commands[name] = cmd

    return commands

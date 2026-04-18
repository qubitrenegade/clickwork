"""Plugin discovery for clickwork CLIs.

Two mechanisms find Click commands:

1. **Directory scanning (dev mode):** Import .py files from a commands/ dir,
   look for a 'cli' attribute (Click command or group). Non-recursive --
   subdirectories like lib/ are skipped. Files without 'cli' produce a warning.

2. **Entry points (installed mode):** Read the 'clickwork.commands' entry
   point group from installed packages.

The discovery_mode parameter controls which are active:
- "dev": directory only
- "installed": entry points only
- "auto" (default): entry points always, PLUS directory scanning when
  commands_dir exists. Local commands shadow installed ones on name
  conflicts (with an info log).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import click

logger = logging.getLogger("clickwork")

# The entry point group name that plugin packages use to register commands.
# Plugin authors add this to their pyproject.toml [project.entry-points] table.
ENTRY_POINT_GROUP = "clickwork.commands"


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
                object is not a Click Command.
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

        We also pass ``parent=ctx.parent`` (NOT ``parent=ctx``) so the
        loaded command's context joins the existing context chain without
        becoming a child of the proxy. WHY this matters: anything that
        walks to the root via ``ctx.find_root()`` -- notably
        ``clickwork.add_global_option`` writing to ``ctx.find_root().meta``
        -- needs to see the REAL root's meta dict, not a detached one. If
        we passed no parent at all, the loaded plugin command's
        ``find_root()`` would return its own fresh context and global-
        option values written at the root level would be silently invisible
        to the plugin. If we passed ``parent=ctx`` (the proxy itself),
        Click would double-count the command name in ``command_path``
        (proxy + loaded both contribute the same info_name to the chain),
        producing duplicated Usage/help text like "myapp plugin-cmd
        plugin-cmd". Passing ``ctx.parent`` threads the chain correctly:
        the loaded command *replaces* the proxy at the plugin-cmd level,
        inheriting the proxy's own parent so ``find_root()`` still reaches
        the true root.

        Args:
            ctx: The Click context, whose ``ctx.args`` contains the
                unparsed extra arguments collected by the proxy and whose
                ``ctx.obj`` holds the CliContext built by create_cli().

        Returns:
            Whatever the real command's ``main()`` returns.
        """
        loaded = self._load()

        # Defensive flag-collision check for entry-point plugins.
        #
        # WHY this check exists: ``clickwork.add_global_option`` installs
        # options on this proxy at CLI-build time, but the proxy has no
        # way to introspect the plugin's own options until the plugin
        # module is actually loaded (the whole point of laziness). So
        # add_global_option's conflict detector cannot see a plugin's
        # private ``--json`` (or whatever) -- it only sees the proxy's
        # ``self.params``. If the plugin's loaded command declares the
        # same flag, Click would parse that flag at the PROXY level
        # first, consume the token, and the plugin would never see its
        # own option. Behaviour would look like "the flag is silently
        # ignored by the plugin", which is a nasty debugging experience.
        #
        # Now that we actually have ``loaded``, compare its declared
        # flag strings against the proxy's. Any overlap is a genuine
        # conflict between a plugin-declared option and a globally-
        # installed option; surface it as a ``click.UsageError`` (user-
        # classification, matches the rest of clickwork's error policy)
        # with a pointer to both sides so the caller can fix whichever
        # makes sense for their setup.
        #
        # WHY we walk the FULL loaded tree (not just loaded.params): if
        # the plugin's entry-point target is a ``click.Group``, the
        # group's own params don't include the options declared on its
        # subcommands. A nested subcommand that declares ``--json``
        # still has its token consumed at the proxy level because the
        # proxy installed the ``--json`` option via add_global_option
        # and Click's parser greedily matches it at whichever level
        # declares it (the proxy, here) before descending. Walking the
        # full tree catches those deeper collisions.
        proxy_flag_strings: set[str] = set()
        for proxy_param in self.params:
            proxy_flag_strings.update(getattr(proxy_param, "opts", ()))
            proxy_flag_strings.update(getattr(proxy_param, "secondary_opts", ()))

        # Collect (qualified_path, flag_string) pairs from loaded and any
        # nested commands it contains. Group membership check uses the
        # public isinstance on click.Group; Groups always expose their
        # subcommands via ``.commands``.
        #
        # WHY we track the REGISTERED name (the dict key in Group.commands)
        # rather than ``cmd.name``: a plugin can register a command under
        # an alias with ``Group.add_command(cmd, name="alias")``, in which
        # case ``cmd.name`` differs from the name the user actually types
        # on the command line. The error message needs the invocation
        # path, so we thread the dict key down through the recursion
        # instead of relying on ``cmd.name``. For the top-level call we
        # use ``self.name`` -- the proxy IS registered under that name
        # at the CLI root, so starting the walk with just that name
        # (and NOT appending ``loaded.name`` again) avoids the
        # "plugin plugin" duplication an earlier draft produced.
        collisions: list[tuple[str, str]] = []

        def _walk(cmd: click.Command, path: str) -> None:
            # path is the FULL qualified path (registered names, space-
            # separated) for ``cmd`` as the user would type it. Every
            # param declared directly on ``cmd`` that collides with a
            # proxy flag gets appended with this path.
            for p in cmd.params:
                cmd_flags = set(getattr(p, "opts", ()))
                cmd_flags.update(getattr(p, "secondary_opts", ()))
                for flag in cmd_flags & proxy_flag_strings:
                    collisions.append((path, flag))
            if isinstance(cmd, click.Group):
                # Iterate .items() so we get the REGISTERED name (dict
                # key), not the underlying cmd.name attribute -- these
                # can differ when a plugin aliased the command.
                for registered_name, sub in cmd.commands.items():
                    sub_path = f"{path} {registered_name}".strip()
                    _walk(sub, sub_path)

        # Start with the proxy's own registered name. We do NOT append
        # loaded.name to this: the proxy IS the loaded command's entry
        # point, and Click parses from the proxy's registered name, so
        # "self.name" alone is the correct root of the path.
        _walk(loaded, self.name or "")

        if collisions:
            # Group collisions by flag so the error message names each
            # conflicting flag once with all the command paths that
            # declare it -- easier to read than one line per occurrence.
            by_flag: dict[str, list[str]] = {}
            for cmd_path, flag in collisions:
                by_flag.setdefault(flag, []).append(cmd_path)
            details = "; ".join(
                f"{flag!r} declared on " + ", ".join(sorted(paths))
                for flag, paths in sorted(by_flag.items())
            )
            raise click.UsageError(
                f"Entry-point plugin {self.name!r} contains option(s) that "
                f"collide with a globally-installed option on the CLI root: "
                f"{details}. The global option consumes these flags before "
                f"the plugin command/subcommand sees them. Either rename "
                f"the plugin-side option(s) or omit the global install for "
                f"the colliding flag(s)."
            )

        # Pass obj=ctx.obj so the new context created by loaded.main() carries
        # the CliContext forward.  Click forwards **extra kwargs through
        # make_context() -> Context(), and Context accepts obj as a keyword arg.
        #
        # WHY parent=ctx.parent (NOT parent=ctx): Click builds
        # ``command_path`` as ``parent.command_path + " " + info_name`` by
        # walking the parent chain. The proxy ctx ALREADY represents the
        # plugin-cmd level in the chain. If we passed parent=ctx the loaded
        # command would become a *child* of the proxy and its command_path
        # would be "myapp plugin-cmd" (proxy's path) + " " + "plugin-cmd"
        # (loaded's info_name) = "myapp plugin-cmd plugin-cmd" -- duplicated
        # in Usage / help / error messages.
        #
        # Passing parent=ctx.parent instead makes the loaded command's
        # context a *sibling* of the proxy in the tree: it replaces the
        # proxy in the chain rather than appending to it. That gives
        # command_path = "myapp" + " " + "plugin-cmd" = "myapp plugin-cmd"
        # (correct) while keeping ctx.find_root() reachable from the loaded
        # ctx -- the whole reason we wire the chain at all, so
        # clickwork.add_global_option values live on a shared root.meta.
        #
        # WHY prog_name=ctx.info_name: once parent is ctx.parent, info_name
        # needs to be just the command's own name (e.g. "plugin-cmd"),
        # not the full command path -- Click rebuilds the path from the
        # chain.
        return loaded.main(
            args=list(ctx.args),
            prog_name=ctx.info_name,
            standalone_mode=False,
            obj=ctx.obj,
            parent=ctx.parent,
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

        Called when the user runs ``<cli> <cmd> --help``. Loading the real
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
    'cli' attribute produce a log warning (they probably shouldn't be in
    commands/). Import errors also produce a log warning so the CLI remains
    usable even if one plugin is broken.

    WHY warnings instead of exceptions: A broken or incomplete command file
    should not prevent the rest of the CLI from loading. Users get a clear
    signal without a hard crash.

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
    #
    # Include a hash of the directory path in the package name so that two
    # different command directories with identically-named files get separate
    # module objects instead of colliding in sys.modules.
    dir_path = str(commands_dir.resolve())
    dir_hash = hashlib.sha256(dir_path.encode()).hexdigest()[:12]
    package_name = f"clickwork._discovered_{dir_hash}"
    package = sys.modules.get(package_name)
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
        # The directory hash ensures that identically-named files in
        # different command dirs get separate module entries.
        module_name = f"{package_name}.{py_file.stem}"
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
            logger.warning("Failed to import %s: %s", py_file.name, e)
            # Clean up the partial entry to avoid stale modules in sys.modules.
            sys.modules.pop(module_name, None)
            continue

        cli_attr = getattr(module, "cli", None)
        if cli_attr is None:
            # The file is in the commands dir but doesn't export 'cli'.
            # This is likely a helper that should live in lib/ instead.
            logger.warning(
                "%s has no 'cli' attribute. Command files must export a "
                "Click command or group as 'cli'. If this is a helper "
                "module, move it to lib/.",
                py_file.name,
            )
            continue

        if not isinstance(cli_attr, click.Command):
            logger.warning(
                "%s 'cli' attribute is not a Click command (got %s). Skipping.",
                py_file.name,
                type(cli_attr).__name__,
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

    Reads the ``clickwork.commands`` entry point group from all installed
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

    # The group keyword has been stable since Python 3.10; this project
    # requires 3.11+ (see pyproject.toml), so no compat fallback is needed.
    eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)

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
            "auto"      -- entry points always, plus directory scanning when
                           commands_dir exists; local commands shadow installed
                           ones on name conflicts (with an info log)

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
        # Auto mode: always check entry points so installed plugins are
        # visible, and ALSO scan the directory when it exists. Local
        # commands shadow installed ones on name conflicts, with an INFO
        # log so stale local files don't silently hide installed plugins.
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

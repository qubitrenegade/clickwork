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
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import click

logger = logging.getLogger("clickwork")

# The entry point group name that plugin packages use to register commands.
# Plugin authors add this to their pyproject.toml [project.entry-points] table.
ENTRY_POINT_GROUP = "clickwork.commands"


# ---------------------------------------------------------------------------
# Strict-discovery error surface
# ---------------------------------------------------------------------------
#
# Discovery is intentionally forgiving by default: a single broken command
# file should not bring down the whole CLI during local development, so
# failures log a warning and the offending command is dropped. For production
# CLIs that mindset inverts -- shipping a binary with a silently-missing
# command is a release-validation bug, not a friendly degradation. Issue #42
# adds an opt-in ``strict=True`` mode that raises instead of warning.
#
# The failure reporter below is structured rather than a plain string so that
# strict mode can collect EVERY failure in one pass and raise a single
# exception listing all of them, rather than short-circuiting on the first.
# That matches how linters / type checkers behave: fixing issues one-at-a-
# time is frustrating when the tool could have told you about all five.


# WHY a string-valued category instead of an enum: the set of failure modes
# is small and unlikely to grow, callers rarely care to switch on it
# programmatically, and plain strings keep the exception trivially
# serialisable (logging, JSON error reporters, subprocess output). If
# downstream code needs to branch on the category, the current values are
# documented here and stable across minor versions.
#
# The five known categories map 1:1 to the silent-drop branches that strict
# mode promotes to raises:
#   - "import_error":  module failed to import (ImportError, SyntaxError,
#                      anything that bubbles out of exec_module).
#   - "missing_cli":   the module imported fine but did not expose ``cli``.
#   - "invalid_cli":   the module's ``cli`` attribute is not a click.Command.
#   - "entrypoint_load": LazyEntryPointCommand construction or metadata load
#                        for an installed entry point raised.
#   - "duplicate_command": two discovered sources produced the same command
#                          name at the same discovery level (two files in
#                          commands/, or two entry points), which without
#                          strict mode would silently overwrite whichever
#                          was loaded first.


@dataclass
class DiscoveryFailure:
    """Structured record of a single discovery failure.

    Discovery can fail in a handful of distinct ways; collecting them into
    a dataclass rather than one-off strings lets ``ClickworkDiscoveryError``
    expose a machine-readable ``.failures`` list that tests and callers can
    assert against without resorting to string matching.

    Attributes:
        category: Short tag naming WHICH failure mode tripped -- see the
            module-level comment above for the full set of known values.
        message: Human-readable description. Already localised-ish; used
            directly in the combined exception's ``str()`` output.
        cause_path: Filesystem path to the file that broke discovery, when
            known. ``None`` for entry-point failures that don't map back to
            a single file (e.g., an entry point metadata error).
        exception: The underlying exception, when the failure was caused by
            one. Useful for callers that want the full traceback.
    """

    category: str
    message: str
    cause_path: Path | None = None
    exception: BaseException | None = None


class ClickworkDiscoveryError(Exception):
    """Raised when discovery fails under ``strict=True``.

    Carries a ``.failures`` list of structured ``DiscoveryFailure`` records so
    a single run can surface every problem it found, not just the first. The
    convenience ``.cause_path`` attribute points at the first failure's file
    (useful for test assertions and one-line error logging); inspect
    ``.failures`` directly for the full picture.

    WHY an exception type of our own rather than reusing ``click.UsageError``
    or ``ImportError``: these are classification bugs at the CLI-wiring
    layer, not user input mistakes or plain import problems. Giving them a
    dedicated type lets consumers catch only this class without accidentally
    swallowing unrelated errors, and lets our own ``except`` clauses in the
    CLI startup path treat strict-discovery failures distinctly from
    runtime user errors.
    """

    def __init__(self, failures: list[DiscoveryFailure]) -> None:
        """Build a discovery error that aggregates one or more failure records.

        Args:
            failures: Non-empty list of structured failure records. The
                constructor formats a combined message for ``str(exc)``;
                callers that want the raw records read ``exc.failures``.
        """
        # We don't reject an empty list here -- callers are expected to only
        # build a ClickworkDiscoveryError when at least one failure happened,
        # and an accidental empty would surface as an odd "[]" in the
        # message, which is self-describing. Keeping the constructor
        # forgiving avoids an extra guard at every call site.
        self.failures = list(failures)
        # Keep a convenience pointer to the first failure's path so simple
        # "where did it break" consumers don't have to index into .failures
        # themselves. Falls back to None if the first failure has no path
        # (entry-point case).
        self.cause_path: Path | None = (
            self.failures[0].cause_path if self.failures else None
        )
        # Format the combined message once at construction time so the
        # usual ``str(exc)`` / ``repr(exc)`` paths show every failure
        # without recomputation.
        summary = self._format_summary(self.failures)
        super().__init__(summary)

    @staticmethod
    def _format_summary(failures: list[DiscoveryFailure]) -> str:
        """Render ``failures`` into a human-readable multi-line summary.

        One line per failure, prefixed by a bullet. The category is included
        so downstream log greppers can filter on it, and the file path (when
        known) helps the reader jump straight to the offender.

        Args:
            failures: The failure list to render.

        Returns:
            A newline-separated summary string suitable for ``str(exc)``.
        """
        if not failures:
            return "Discovery failed with no recorded failures."
        header = f"Discovery failed with {len(failures)} error(s):"
        lines = [header]
        for f in failures:
            # Path prefix only when we actually have one; entry-point
            # failures without a file path just show the category + message.
            path_part = f" [{f.cause_path}]" if f.cause_path is not None else ""
            lines.append(f"  - [{f.category}]{path_part} {f.message}")
        return "\n".join(lines)


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


def discover_commands_from_dir(
    commands_dir: Path,
    *,
    strict: bool = False,
) -> dict[str, click.Command]:
    """Scan a directory for .py files that export a 'cli' Click command.

    Only top-level .py files are checked -- subdirectories are skipped.
    __init__.py files are skipped (they start with '_'). Files without a
    'cli' attribute produce a log warning (they probably shouldn't be in
    commands/). Import errors also produce a log warning so the CLI remains
    usable even if one plugin is broken.

    WHY warnings instead of exceptions (default): A broken or incomplete
    command file should not prevent the rest of the CLI from loading during
    local development. Users get a clear signal without a hard crash.

    WHY an opt-in strict mode: for production CLIs and release validation,
    silently shipping a binary that dropped a broken command is a release
    bug, not a friendly degradation. Passing ``strict=True`` promotes every
    warning/drop path in this function to a ``ClickworkDiscoveryError``
    that aggregates every failure in the scan so the release engineer
    sees the full picture on one pass rather than fixing them one at a
    time. See issue #42 for the full rationale.

    Args:
        commands_dir: Path to the commands directory.
        strict: When True, discovery failures raise
            ``ClickworkDiscoveryError`` after the scan instead of logging
            warnings. Default False preserves the forgiving dev-mode
            behaviour. Keyword-only to keep the positional signature
            stable for existing callers.

    Returns:
        Dict mapping command name -> Click command/group.

    Raises:
        ClickworkDiscoveryError: If ``strict=True`` and one or more
            failures were observed during the scan. The exception
            carries a structured ``.failures`` list naming each one.
    """
    commands: dict[str, click.Command] = {}
    # Collect every failure we hit so strict mode can raise with the full
    # list. Permissive mode ignores this list -- the warnings we log as we
    # go are the user-visible signal.
    failures: list[DiscoveryFailure] = []

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
            # Record for strict-mode aggregation. We still continue past
            # this failure so strict mode can report multiple issues in
            # one pass, matching the dev-mode behaviour of "keep going
            # and let the user fix them all at once".
            failures.append(
                DiscoveryFailure(
                    category="import_error",
                    message=f"Failed to import {py_file.name}: {e}",
                    cause_path=py_file,
                    exception=e,
                )
            )
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
            failures.append(
                DiscoveryFailure(
                    category="missing_cli",
                    message=(
                        f"{py_file.name} has no 'cli' attribute. Command "
                        "files must export a Click command or group as "
                        "'cli'. If this is a helper module, move it to lib/."
                    ),
                    cause_path=py_file,
                )
            )
            continue

        if not isinstance(cli_attr, click.Command):
            logger.warning(
                "%s 'cli' attribute is not a Click command (got %s). Skipping.",
                py_file.name,
                type(cli_attr).__name__,
            )
            failures.append(
                DiscoveryFailure(
                    category="invalid_cli",
                    message=(
                        f"{py_file.name} 'cli' attribute is not a Click "
                        f"command (got {type(cli_attr).__name__})."
                    ),
                    cause_path=py_file,
                )
            )
            continue

        # Key by the exposed Click command name so discovery is consistent
        # with installed entry points. Fall back to the filename only if the
        # command object has no explicit name.
        cmd_name = cli_attr.name or py_file.stem

        # Duplicate-name guard: two files registering the same command name
        # used to silently overwrite each other (last-write-wins via dict
        # assignment). In strict mode that's a release-engineering bug.
        # In permissive mode we now warn AND keep the first-loaded
        # command rather than the last -- matching
        # ``discover_commands_from_entrypoints()``'s keep-first policy
        # so both discovery mechanisms handle duplicates consistently.
        # Keep-first is also deterministic across filesystems (dir
        # iteration order is consistently sorted above), whereas
        # last-write-wins made behaviour depend on which file happened
        # to sort alphabetically later.
        if cmd_name in commands:
            logger.warning(
                "Duplicate command name %r discovered in %s; keeping the "
                "first-loaded command and dropping this one. Rename one "
                "of the files or the Click command name to resolve the "
                "conflict.",
                cmd_name,
                py_file.name,
            )
            failures.append(
                DiscoveryFailure(
                    category="duplicate_command",
                    message=(
                        f"Duplicate command name {cmd_name!r} discovered "
                        f"in {py_file.name}; keeping the first-loaded "
                        "command with the same name."
                    ),
                    cause_path=py_file,
                )
            )
            # Keep the first; skip assignment for this (duplicate) entry.
            # Strict mode raises after the loop so all duplicates surface
            # in one error, not a fix-run-fix cycle.
            continue

        commands[cmd_name] = cli_attr

    # Strict mode: if ANY failure was recorded, raise with the whole list.
    # We deliberately run this AFTER the loop so that all files in the dir
    # are inspected and every problem surfaces in a single error. Raising
    # on the first failure would force the user to fix-run-fix-run in a
    # loop; release engineers want the full list up front.
    if strict and failures:
        raise ClickworkDiscoveryError(failures)

    return commands


def discover_commands_from_entrypoints(
    *,
    strict: bool = False,
) -> dict[str, click.Command]:
    """Discover commands from installed packages via the entry points mechanism.

    Reads the ``clickwork.commands`` entry point group from all installed
    packages. Each entry point should reference a Click command or group.
    Entry points are wrapped in ``LazyEntryPointCommand`` proxies so startup
    does not trigger imports of every installed plugin.

    WHY entry points: This is the standard Python plugin mechanism. Plugin
    authors declare their commands in ``pyproject.toml`` and pip handles the
    wiring -- no config file or explicit registration is needed. Consumers
    install a plugin package and its commands immediately appear in the CLI.

    Args:
        strict: When True, failures observed while wrapping an entry point
            raise ``ClickworkDiscoveryError`` after the scan instead of
            logging warnings. Default False preserves the forgiving
            behaviour. Keyword-only to keep the positional signature stable.

    Returns:
        Dict mapping command name to a lazy-loading Click command/group proxy.

    Raises:
        ClickworkDiscoveryError: If ``strict=True`` and one or more
            entry-point failures were observed during the scan. Note that
            plugin-internal flag collisions are still surfaced lazily at
            invocation time via ``LazyEntryPointCommand.invoke`` -- they
            cannot be detected at discovery time without loading every
            plugin, which would defeat the lazy-loading design.
    """
    commands: dict[str, click.Command] = {}
    # Aggregate failures for strict mode. Entry-point failures are rare
    # (LazyEntryPointCommand.__init__ is near-trivial) but any failure here
    # today is silent-drop-plus-warning, which strict mode must promote to
    # a raise to honour the "no silent drops in production" contract.
    failures: list[DiscoveryFailure] = []

    # The group keyword has been stable since Python 3.10; this project
    # requires 3.11+ (see pyproject.toml), so no compat fallback is needed.
    eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)

    for ep in eps:
        try:
            # Detect duplicate entry-point command names BEFORE overwriting
            # the dict slot. Two installed plugins can register the same
            # command name -- previously this was a silent last-write-wins
            # via dict assignment. Mirror the directory-scan duplicate
            # detection so strict mode catches entry-point collisions too,
            # and non-strict mode at least logs a warning so the drop isn't
            # invisible.
            if ep.name in commands:
                prior = commands[ep.name]
                # LazyEntryPointCommand carries its origin in `.name` /
                # metadata; surface both entry points in the message so the
                # release engineer can tell which packages conflicted.
                prior_origin = getattr(prior, "_entry_point", None)
                prior_name = (
                    f"{prior_origin.value}" if prior_origin is not None else "(unknown)"
                )
                current_name = ep.value
                msg = (
                    f"Duplicate entry-point command name {ep.name!r}: "
                    f"both {prior_name!r} and {current_name!r} register it. "
                    f"Keeping the first; last-write-wins semantics otherwise "
                    f"would silently drop one of the plugins."
                )
                logger.warning(msg)
                failures.append(
                    DiscoveryFailure(
                        category="duplicate_command",
                        message=msg,
                        cause_path=None,
                        exception=None,
                    )
                )
                # Keep the first-loaded entry point to be deterministic
                # (matches directory-scan behaviour). Strict mode raises
                # after the full scan so all duplicates surface at once.
                continue
            # Wrap in a lazy proxy -- don't actually import the plugin yet.
            commands[ep.name] = LazyEntryPointCommand(ep)
        except Exception as e:
            logger.warning("Failed to load entry point '%s': %s", ep.name, e)
            # cause_path is None here because an entry point failure doesn't
            # map cleanly to a single file -- the metadata came from a
            # package's pyproject.toml but the failure could be in any
            # module imported transitively. The entry point name (carried
            # in the message) is the useful locator.
            failures.append(
                DiscoveryFailure(
                    category="entrypoint_load",
                    message=f"Failed to load entry point {ep.name!r}: {e}",
                    cause_path=None,
                    exception=e,
                )
            )

    if strict and failures:
        raise ClickworkDiscoveryError(failures)

    return commands


def discover_commands(
    commands_dir: Path | None = None,
    discovery_mode: str = "auto",
    *,
    strict: bool = False,
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
        strict: When True, any discovery failure in EITHER mechanism raises
            ``ClickworkDiscoveryError`` after BOTH mechanisms have run, so
            a single run can surface every problem in one pass. Default
            False preserves the forgiving warn-and-drop behaviour.
            Keyword-only to keep the positional signature stable for
            existing callers. Note that "local shadows installed" is
            NOT promoted to a failure under strict mode -- shadowing is
            an intentional feature of auto mode, not a discovery error.

    Returns:
        Dict mapping command name -> Click command/group.

    Raises:
        ValueError: If discovery_mode is not one of the accepted values.
        ClickworkDiscoveryError: If ``strict=True`` and either the directory
            scan or the entry-point scan recorded at least one failure.
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

    # Under strict mode we want to AGGREGATE failures across both mechanisms
    # before raising, so callers see a complete report in one pass. We do
    # that by letting each per-mechanism helper return its failure list via
    # the exception it would have raised, then combining lists. The
    # ``strict=False`` path we pass down causes the helpers to swallow
    # failures (log-only), so we capture them here by re-running the
    # equivalent bookkeeping at this level -- except that's redundant.
    # Simpler: call each helper with the SAME ``strict`` value, but when
    # strict=True catch its ``ClickworkDiscoveryError``, extend our local
    # list with its failures, and keep going. Raise at the end if anything
    # was recorded.
    aggregated_failures: list[DiscoveryFailure] = []

    # Load entry-point commands first so directory commands can shadow them.
    if use_ep:
        try:
            commands.update(
                discover_commands_from_entrypoints(strict=strict)
            )
        except ClickworkDiscoveryError as e:
            # strict=True raised here. Collect failures and keep going so
            # the directory scan can still surface its own problems. We'll
            # raise a combined exception at the end.
            aggregated_failures.extend(e.failures)

    if use_dir and commands_dir:
        try:
            dir_commands = discover_commands_from_dir(
                commands_dir, strict=strict
            )
        except ClickworkDiscoveryError as e:
            # Same pattern: collect and continue. The helper still returned
            # whatever partial results it had before raising? Actually no --
            # ``raise`` aborts the return. That means under strict the
            # directory commands we merge below would be missing. But
            # that's fine: under strict we're going to raise at the
            # bottom anyway, so the partial merge doesn't matter; callers
            # never see the return value.
            aggregated_failures.extend(e.failures)
            dir_commands = {}

        for name, cmd in dir_commands.items():
            if name in commands:
                # Log at INFO (not WARNING) because shadowing is expected and
                # intentional during dev. It's not a problem, just informational.
                #
                # WHY this is NOT a strict-mode failure: shadowing is the
                # documented, intentional behaviour of auto mode. Elevating
                # it to an error would make strict-mode auto discovery
                # incompatible with any CLI that has BOTH a local commands
                # dir AND an installed plugin using the same name -- which
                # is exactly the case "local wins" was designed for.
                logger.info(
                    "Local command '%s' shadows installed plugin command. "
                    "The local version will be used.",
                    name,
                )
            commands[name] = cmd

    # Aggregate-raise: if strict mode recorded any failure across either
    # mechanism, surface them all now. We deferred the raise until both
    # scans completed so a release engineer sees the full list, not just
    # the first mechanism's failures.
    if strict and aggregated_failures:
        raise ClickworkDiscoveryError(aggregated_failures)

    return commands

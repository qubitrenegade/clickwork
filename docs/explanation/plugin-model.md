# The plugin model

Why clickwork's plugin system is entry-point based, how discovery
works conceptually, and how the local-wins rule plays out.

## The shape

Plugins are regular Python packages. They contribute commands by
declaring an entry point in the `clickwork.commands` group of their
`pyproject.toml`.

```toml
[project.entry-points."clickwork.commands"]
deploy = "projectctl_deploy:cli"
```

When `create_cli()` runs, clickwork:

1. Iterates `importlib.metadata.entry_points(group="clickwork.commands")`
   and wraps each in a `LazyEntryPointCommand` keyed by the entry-point
   name (the LHS — `deploy` above).
2. Reads the local `commands_dir` (when it exists and auto mode is
   active) and registers every file that exposes a `cli` attribute,
   keyed by the Click command's `.name` attribute (with fallback to
   the filename stem only if `.name` is unset).
3. Overlays directory commands on top of entry-point commands, so
   local files win any name collision. clickwork emits an INFO log
   when a local file shadows an installed command — visibility
   depends on the host's logging setup though (see the caveat
   below).

**Caveat on visibility of these log messages.** Discovery runs
during `create_cli()` — often at module import time, before the
host application has configured logging. clickwork attaches a
`NullHandler` on its own logger at import time, so discovery-time
records don't produce "no handlers" complaints, but they may also
not reach any handler the host installs later. For WARNING+
records, Python's "last resort" stderr fallback usually kicks in;
INFO records typically do not. If you need collisions / import
errors surfaced reliably regardless of logging config, pass
`strict=True` to `create_cli()` — discovery failures become a
`ClickworkDiscoveryError` raised at CLI construction time.

## Why entry points

Three alternatives got rejected in the 0.x cycle:

1. **A central registry config file** (e.g. `plugins.toml` listing
   which packages contribute). Rejected because it's an extra thing
   to keep in sync on every plugin install, and it turns plugin
   discovery into "did someone update the config" instead of "did
   someone install the package."
2. **Directory scanning** (look at `site-packages/*/plugin.json`).
   Rejected because it's coupled to filesystem layout — breaks on
   editable installs, zipped installs, and namespace packages.
3. **Manual registration** (`@cli.register_plugin(X)` at runtime).
   Rejected because it requires the main CLI to know about every
   plugin, defeating the purpose.

Entry points are the Python ecosystem's native plugin mechanism.
`pip install` registers them; `pip uninstall` unregisters them; the
tooling already knows how to introspect them.

To see the entry points a distribution declares, read the
distribution's `*.dist-info/entry_points.txt` directly, or use the
programmatic API:

```python
import importlib.metadata
for ep in importlib.metadata.entry_points(group="clickwork.commands"):
    print(ep.name, "->", ep.value, "from", ep.dist.name)
```

(`pip show -f <package>` lists installed files but does not parse
entry-point metadata, so it's not the right tool for this question.)

## No per-CLI scoping today

The entry-point group is `clickwork.commands` — a single global group
for every clickwork-built CLI. Every CLI running in the same Python
environment sees every plugin published under this group. There is
no `.<cli-name>` suffix or other per-CLI scoping in the current
implementation.

The practical consequences:

- When authoring a plugin, choose command names that are unlikely to
  collide with other plugins or with local command-directory files
  in target CLIs.
- **Within-mechanism duplicates** — two plugins both registering the
  same entry-point name, or two directory files producing the same
  Click command name — are a *bug*. clickwork keeps the first-loaded
  one (directory scan sorted alphabetically; entry points in
  `importlib.metadata` iteration order), emits a warning, and strict
  mode promotes the duplicate to a hard error via
  `ClickworkDiscoveryError`.
- **Cross-mechanism "collisions"** — a local `commands/foo.py` with
  the same name as an installed-plugin entry point — are the
  *intentional* shadowing feature: the local command wins, clickwork
  logs at INFO (not WARNING) so you can tell the shadowing happened,
  and strict mode explicitly does NOT treat this as a failure. See
  the "Why local wins on collision" section below.
- If you're planning to ship an ecosystem of distinct CLIs in the
  same venv, be deliberate about namespacing command names at
  design time (`projectctl-deploy`, `dataops-deploy`, not just
  `deploy`, `deploy`).

Per-CLI scoping is a credible future feature (it'd scope a group
name like `clickwork.commands.projectctl`), but it's not shipped.
Don't publish plugins under a scoped group name today — clickwork
won't read them.

## Why local wins on collision

Scenario: a plugin you installed six months ago exposes a command
named `deploy`. You later write a local `commands/deploy.py` because
your project's deploy story diverged. clickwork picks the local file.

Rationale:

- **Local code is what the project maintainer is actively editing.**
  A plugin winning would silently shadow work-in-progress.
- **Plugins are easy to replace; hand-written code is not.** If a
  plugin's `deploy` no longer fits, `pip uninstall` + `rm` is one
  command; rewriting a local command to match a plugin's shape is
  weeks.
- **The override is visible.** `projectctl deploy --help` shows the
  local file's docstring, not the plugin's. You can tell by reading
  the help which one ran.

See [plugins reference](../reference/plugins.md) for the exact
discovery algorithm, including strict mode behaviour and diagnostic
hooks.

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
   when a local file shadows an installed command, so stale local
   files don't silently hide plugin updates.

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
- If two plugins (or a plugin and a local file) register the same
  command name, clickwork keeps the first-loaded one deterministically
  (directory scan is sorted alphabetically, entry-point iteration
  order is `importlib.metadata`'s) and emits a warning on the
  others. Strict mode promotes the duplicate to a hard error.
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

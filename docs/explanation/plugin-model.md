# The plugin model

Why clickwork's plugin system is entry-point based, how discovery
works conceptually, and how the local-wins rule plays out.

## The shape

Plugins are regular Python packages. They contribute commands by
declaring an entry point in the `clickwork.commands.<cli-name>` group
of their `pyproject.toml`.

```toml
[project.entry-points."clickwork.commands.projectctl"]
deploy = "projectctl_deploy:cli"
```

When `create_cli(name="projectctl")` runs, clickwork:

1. Iterates `importlib.metadata.entry_points(group=
   "clickwork.commands.projectctl")` and loads each entry point's
   `cli` object.
2. Reads the local `commands_dir` (when it exists and auto mode is
   active) and registers every file that exposes a `cli` attribute.
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
tooling already knows how to introspect them
(`pip show -f <package>` lists entry points).

## Why the `.<cli-name>` suffix

A sibling CLI named `dataops` in the same venv shouldn't see
`projectctl`'s deploy plugin. Scoping by CLI name keeps plugins
targeted:

- `clickwork.commands.projectctl` → plugins for the `projectctl` CLI
- `clickwork.commands.dataops` → plugins for the `dataops` CLI

Without this, every plugin would appear in every CLI, and names
would collide in minutes.

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

# 2. Adding a plugin

Plugins ship separately from the main CLI and contribute commands via
entry points. By the end of this page you'll have `projectctl-deploy`
installed and showing up as `projectctl deploy`.

## Why a plugin and not just another `commands/` file

Use a plugin when:

- The command ships on a different release cadence than the main CLI.
- A separate team owns it.
- You want it installable standalone (`pip install projectctl-deploy`
  without installing the whole project).

Use a local `commands/` file when the command is part of this
project's lifecycle and versioning.

## Scaffold the plugin

From the parent directory (not inside `projectctl/`):

```bash
uv init --package projectctl-deploy
cd projectctl-deploy
```

## Add the clickwork entry point

Edit `projectctl-deploy/pyproject.toml` and add the
`clickwork.commands` entry-point group:

```toml
[project.entry-points."clickwork.commands.projectctl"]
deploy = "projectctl_deploy:cli"
```

Two things are happening here:

- `clickwork.commands.projectctl` is the entry-point group — the
  `.projectctl` suffix tells clickwork this plugin contributes
  commands to the CLI named `projectctl`. Other CLIs (e.g. a sibling
  `dataops` CLI) have their own suffix and won't see these.
- `deploy = "projectctl_deploy:cli"` says "expose a `deploy` command
  whose Click object lives at `projectctl_deploy.cli`".

## Write the command

Create `projectctl-deploy/src/projectctl_deploy/__init__.py`:

```python
import click


@click.command()
@click.option("--env", default="staging", show_default=True,
              help="Target environment.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print what would happen without doing it.")
def cli(env: str, dry_run: bool) -> None:
    """Deploy the project to <env>."""
    prefix = "[dry-run] " if dry_run else ""
    click.echo(f"{prefix}Deploying to {env}...")
```

## Install the plugin into the main CLI's venv

Back in the `projectctl/` directory:

```bash
cd ../projectctl
uv add --dev ../projectctl-deploy  # or drop --dev for a runtime dep
```

`uv add` with a local path installs in editable mode — edits to the
plugin reflect immediately.

## Verify discovery

```bash
uv run python -m projectctl --help
```

You should see:

```
Commands:
  deploy     Deploy the project to <env>.
  tail-logs  Tail a log file.
```

And run it:

```bash
uv run python -m projectctl deploy --env production --dry-run
```

Expected:

```
[dry-run] Deploying to production...
```

## Conflict handling: local wins

If a plugin ships a `tail-logs` command and you have
`commands/tail_logs.py` locally, the local file wins. Install-time
collisions never overwrite hand-maintained local commands. clickwork
emits an `INFO` log when a local file shadows an installed command
so stale local files don't silently hide plugin updates.

## Next

In [Packaging](03-packaging.md) we'll build both projects as wheels
and install them in a fresh venv to confirm the setup is
reproducible.

# Write a plugin

When a command should ship on its own release cadence, publish it as a
separate package. See also: the [plugin reference](../reference/plugins.md)
for the full spec.

## Scaffold

```bash
uv init --package my-cli-deploy  # replace "my-cli" with your target CLI name
cd my-cli-deploy
```

## Entry point in pyproject.toml

```toml
[project.entry-points."clickwork.commands.<target-cli-name>"]
deploy = "my_cli_deploy:cli"
```

Breakdown:

- `clickwork.commands.<target-cli-name>` — the entry-point group.
  The `.<target-cli-name>` suffix scopes the plugin to a specific
  CLI. If your plugin ships commands for `projectctl`, it's
  `clickwork.commands.projectctl`.
- `deploy` — the command name as it appears on the command line.
- `my_cli_deploy:cli` — the import path of the Click object.

## Write the command

```python
# src/my_cli_deploy/__init__.py
import click


@click.command()
@click.option("--env", default="staging", show_default=True)
def cli(env: str) -> None:
    """Deploy."""
    click.echo(f"Deploying to {env}...")
```

## Install into the target CLI's venv

From the target CLI's directory:

```bash
uv add --dev ../my-cli-deploy  # editable install during development
```

## Verify

```bash
uv run python -m <target-cli> deploy --env production
```

## Ship

- `uv build` produces the wheel.
- Upload to PyPI (or a private index).
- `pip install my-cli-deploy` alongside the target CLI and your
  command shows up.

## Collision: local wins

If the target CLI has a `commands/deploy.py` file, that wins over the
plugin's `deploy`. This is by design — hand-maintained local commands
are never silently overwritten by a plugin install. clickwork emits
an `INFO` log when the shadowing happens so stale local files don't
silently hide plugin updates.

## Testing plugins independently

```python
# tests/test_deploy.py in the plugin repo
from click.testing import CliRunner

from my_cli_deploy import cli


def test_deploy_defaults_to_staging() -> None:
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "staging" in result.output
```

No clickwork dependency in the test — plugins are just Click commands
that clickwork happens to discover.

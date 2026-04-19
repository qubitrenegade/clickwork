# Add a new command

For an existing clickwork project. Three minutes.

## Add the file

In `src/<project>/commands/`, create a new module with a `cli`
attribute that's a Click command:

```python
# src/<project>/commands/status.py
import click


@click.command(name="status")
@click.option("--json", "as_json", is_flag=True,
              help="Emit machine-readable JSON.")
def cli(as_json: bool) -> None:
    """Show project status."""
    if as_json:
        click.echo('{"status": "ok"}')
    else:
        click.echo("ok")
```

## Verify discovery

```bash
uv run python -m <project> --help
```

The command shows up under the name set in `@click.command(name=...)`.
Without that explicit `name=`, Click derives the command's `.name`
from the decorated function — which for `def cli(...)` is `cli`.
clickwork keys registered commands off the Click command's `.name`
(falling back to the filename stem only when `.name` is unset),
so always set `name=` explicitly to avoid collisions between files.
For multi-word commands, use `-` in the name (`@click.command(name="tail-logs")`).

## Ship it

Add a test (`tests/test_status.py`), commit, push, PR.

```python
# tests/test_status.py
from click.testing import CliRunner

from <project>.commands.status import cli


def test_status_plain() -> None:
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert result.output.strip() == "ok"


def test_status_json() -> None:
    result = CliRunner().invoke(cli, ["--json"])
    assert result.exit_code == 0
    assert '"status": "ok"' in result.output
```

## Gotchas

- The attribute MUST be named `cli` (not `command`, not `main`).
- `@click.command()` returns a Click `Command`, not a function you
  can call directly. Always test via `CliRunner`.
- If the file imports a missing dependency, discovery fails for that
  file alone — the other commands still register. Pass `strict=True`
  to `create_cli()` if you want discovery failures to be fatal.

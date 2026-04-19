# 1. Your first command

By the end of this page you'll have a working `projectctl tail-logs`
command and understand how clickwork finds commands on disk.

## Scaffold the project

```bash
mkdir -p projectctl/commands
cd projectctl
uv init --package .
```

`uv init --package .` gives you a modern `pyproject.toml` and a
`src/projectctl/` layout. Move the `commands/` dir inside the package:

```bash
mkdir -p src/projectctl/commands
rmdir projectctl/commands
```

## Wire up the CLI

Create `src/projectctl/__main__.py`:

```python
from projectctl.cli import cli

if __name__ == "__main__":
    cli()
```

And `src/projectctl/cli.py`:

```python
from pathlib import Path

from clickwork import create_cli

cli = create_cli(
    name="projectctl",
    commands_dir=Path(__file__).parent / "commands",
)
```

`commands_dir` is typed as `pathlib.Path` (discovery calls `.is_dir()`
and `.glob()` on it). `Path(__file__).parent / "commands"` resolves
relative to this `cli.py` so the command works regardless of what
directory you run `python -m projectctl` from.

## Write the first command

Create `src/projectctl/commands/tail_logs.py`:

```python
from pathlib import Path

import click


@click.command(name="tail-logs")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option("-n", "--lines", default=20, show_default=True,
              help="How many lines from the tail.")
def cli(path: Path, lines: int) -> None:
    """Tail a log file."""
    content = path.read_text().splitlines()
    for line in content[-lines:]:
        click.echo(line)
```

**Note:** the attribute MUST be named `cli` — that's what clickwork's
discovery looks for. The Click `name=` kwarg controls how the
subcommand appears on the command line.

## Install the project into the venv

```bash
uv sync
```

This creates `.venv/` and installs `projectctl` in editable mode plus
clickwork.

Add clickwork as a dep if `uv init` didn't:

```bash
uv add clickwork
```

## Run it

Create a sample log:

```bash
printf 'line 1\nline 2\nline 3\nline 4\nline 5\n' > sample.log
```

Then:

```bash
uv run python -m projectctl tail-logs sample.log --lines 2
```

Expected:

```
line 4
line 5
```

## Next

In [Adding a plugin](02-adding-a-plugin.md) you'll add a second
command, but via a separate installable package rather than another
file in `commands/`.

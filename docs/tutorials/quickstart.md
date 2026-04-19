# Quickstart

Five minutes from `pip install` to your first clickwork command.

## What you'll build

A minimal CLI named `greet` with one command, `greet hello`, that
takes a `--name` flag.

## Prerequisites

- Python 3.11 or newer
- A fresh directory to work in

## Step 1 — install

```bash
pip install clickwork
```

Verify:

```bash
python -c "import clickwork; print(clickwork.__version__)"
```

You should see `1.0.0` (or whatever the latest is).

## Step 2 — create the project layout

```bash
mkdir -p greet/commands
cd greet
```

Then create the entry point `greet/cli.py`:

```python
from pathlib import Path

from clickwork import create_cli

cli = create_cli(
    name="greet",
    commands_dir=Path(__file__).parent / "commands",
)

if __name__ == "__main__":
    cli()
```

`commands_dir` is typed as `pathlib.Path`, not `str` — clickwork's
discovery calls `.is_dir()` and `.glob()` on it directly. Using
`Path(__file__).parent / "commands"` makes the path resolve relative
to the `cli.py` file, so `python -m greet.cli` works from any working
directory.

And your first command `greet/commands/hello.py`:

```python
import click


@click.command()
@click.option("--name", default="world", help="Who to greet.")
def cli(name: str) -> None:
    """Say hello."""
    click.echo(f"Hello, {name}!")
```

## Step 3 — run it

From the `greet/` directory:

```bash
python -m greet.cli hello --name "clickwork"
```

Expected:

```
Hello, clickwork!
```

You've just written a clickwork CLI.

## What just happened

- `create_cli()` returned a Click `Group` configured to load commands
  from `greet/commands/`.
- Each file in that directory that exposes a `cli` attribute becomes
  a subcommand, named after the file (so `hello.py` becomes
  `greet hello`).
- The `--name` option is plain Click — clickwork doesn't get in
  Click's way.

## Where to next

- **[Practical Walkthrough](walkthrough/index.md)** — build a realistic
  multi-command CLI with a plugin.
- **[User Guide](../reference/guide.md)** — the full reference.
- **[How-To: Tame a script directory](../how-to/tame-a-script-directory.md)**
  — if you arrived with an existing pile of scripts rather than a
  blank slate.

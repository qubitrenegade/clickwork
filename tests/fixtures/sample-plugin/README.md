# sample-commands

A minimal but complete plugin for [qbrd-tools](https://github.com/qubitrenegade/qbrd-tools).
This serves triple duty:

1. **Test fixture** -- integration tests install this plugin and verify
   entry point discovery works end-to-end
2. **Reference implementation** -- shows the patterns for building your own
   CLI commands on top of qbrd-tools
3. **Tutorial** -- walk through this code to understand how the framework works

## Structure

```
sample-plugin/
  pyproject.toml                 # Package metadata + entry point registration
  src/
    sample_commands/
      __init__.py
      hello.py                   # The actual command group
```

## How It Works

### 1. Register your commands via entry points

In `pyproject.toml`, declare which Click commands your package provides:

```toml
[project.entry-points."qbrd_tools.commands"]
hello = "sample_commands.hello:cli"
```

This tells the framework: "when discovering installed plugins, register the
`cli` object from `sample_commands.hello` under the name `hello`."

### 2. Write your command module

Each command module exports a `cli` attribute -- either a `@click.command()`
or a `@click.group()`:

```python
# hello.py
import click

@click.group()
def hello():
    """Sample commands for testing and demonstration."""
    pass

@hello.command()
@click.argument("name", default="world")
@click.pass_obj
def greet(ctx, name: str):
    """Say hello to someone."""
    click.echo(f"Hello, {name}!")

@hello.command()
@click.pass_obj
def info(ctx):
    """Show current config and flags."""
    click.echo(f"env: {ctx.env}")
    click.echo(f"dry_run: {ctx.dry_run}")

# This export is how the framework discovers the command
cli = hello
```

Key points:
- **`cli = hello`** at the bottom is required -- the framework looks for this attribute
- **`@click.pass_obj`** gives you the `CliContext` with config, flags, and helpers
- A **group** (`@click.group()`) creates subcommands: `my-tool hello greet World`
- A **command** (`@click.command()`) registers as a top-level command: `my-tool deploy`

### 3. Install and use

```bash
# Install the framework and this plugin into the same venv
uv pip install -e /path/to/qbrd-tools
uv pip install -e /path/to/sample-plugin

# Now 'hello' is available as a subcommand
my-tool hello greet World    # "Hello, World!"
my-tool hello info           # Shows env, dry_run, verbose
my-tool --dry-run hello info # dry_run: True
```

For **dev mode** (no install needed), just put your command files in a
`commands/` directory and point `create_cli()` at it:

```python
from pathlib import Path
from qbrd_tools import create_cli

cli = create_cli(name="my-tool", commands_dir=Path(__file__).parent / "commands")
```

## Building Your Own Plugin

1. Copy this directory as a starting point
2. Rename `sample_commands` to your package name
3. Replace `hello.py` with your actual commands
4. Update `pyproject.toml` entry points
5. Install alongside `qbrd-tools` -- your commands appear automatically

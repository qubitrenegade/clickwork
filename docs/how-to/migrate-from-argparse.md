# Migrate from argparse (or plain Click)

Pattern-by-pattern conversion. Start with the structural shape, then
move individual commands.

## Map the existing CLI

If you have a single-file argparse CLI:

```python
# old_cli.py
import argparse

def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    greet = sub.add_parser("greet")
    greet.add_argument("--name", default="world")

    count = sub.add_parser("count")
    count.add_argument("n", type=int)

    args = ap.parse_args()
    if args.cmd == "greet":
        print(f"Hello, {args.name}!")
    elif args.cmd == "count":
        for i in range(args.n):
            print(i)
```

Each subparser becomes one file under `commands/`.

## Convert a subparser to a clickwork command

**Before** (argparse subparser):

```python
greet = sub.add_parser("greet")
greet.add_argument("--name", default="world")
# ...
if args.cmd == "greet":
    print(f"Hello, {args.name}!")
```

**After** (`commands/greet.py`):

```python
import click


@click.command(name="greet")
@click.option("--name", default="world", show_default=True)
def cli(name: str) -> None:
    """Say hello."""
    click.echo(f"Hello, {name}!")
```

`name="greet"` is important: clickwork keys commands off the Click
command's `.name` attribute, and `@click.command()` without `name=`
derives it from the function — which here is `cli`. Without
`name="greet"`, every file doing the `def cli(...)` pattern would
collide on the name `cli`.

## Convert a positional

**argparse:**

```python
count.add_argument("n", type=int)
```

**Click:**

```python
@click.argument("n", type=int)
```

## Convert `store_true` / `store_false`

**argparse:**

```python
ap.add_argument("--verbose", action="store_true")
ap.add_argument("--no-progress", dest="progress", action="store_false")
```

**Click:**

```python
@click.option("--verbose", is_flag=True)
@click.option("--progress/--no-progress", default=True)
```

## Convert `choices`

**argparse:**

```python
ap.add_argument("--env", choices=["dev", "staging", "prod"])
```

**Click:**

```python
@click.option("--env", type=click.Choice(["dev", "staging", "prod"]))
```

## If you're already using Click

If you already have a multi-command Click CLI with manual
`@cli.group()` / `cli.add_command()` wiring, clickwork's job is to
replace that plumbing with auto-discovery:

**Before:**

```python
# cli.py
import click

from .commands import greet, count


@click.group()
def cli():
    pass


cli.add_command(greet.cli)
cli.add_command(count.cli)
```

**After:**

```python
from pathlib import Path

from clickwork import create_cli

cli = create_cli(
    name="<project>",
    commands_dir=Path(__file__).parent / "commands",
)
```

No `add_command` calls. Dropping a new file in `commands/` is the only
action needed to add a command. `commands_dir` is a `pathlib.Path`;
`Path(__file__).parent / "commands"` resolves relative to the cli
module so the command works from any cwd.

## What doesn't translate

- **argparse's `--help` formatting** is different from Click's. You
  can customise Click's via `@click.command(context_settings=...)`
  but it won't be identical.
- **argparse's `parents=` composition** has no direct equivalent. Use
  Click decorators as shared modules (`from _shared_options import
  verbose_opt`; then `@verbose_opt` above each command).

## Tests

Click's `CliRunner` replaces `argparse.parse_args()` for tests. No
need to construct `sys.argv`.

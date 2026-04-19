# Tame an out-of-control script directory

You have `scripts/` with `cleanup.sh`, `deploy.py`, `oldstuff.py`,
`thing_v3_final.py`, some of them still work, some don't, and nobody
remembers what `do_the_thing.sh` does.

The goal: one `projectctl` CLI where each script becomes a discoverable
subcommand, with `--help` that actually tells you what each one does.

## Step 1 — inventory

List what you have and roughly what each script does:

```bash
ls scripts/ > /tmp/inventory.txt
# Manually annotate the file with one-line descriptions.
```

Drop the scripts you don't need anymore. This is the most valuable
step and nobody does it.

## Step 2 — scaffold clickwork

```bash
uv init --package .
uv add clickwork
mkdir -p src/<project>/commands
```

Create `src/<project>/cli.py`:

```python
from pathlib import Path

from clickwork import create_cli

cli = create_cli(
    name="<project>",
    commands_dir=Path(__file__).parent / "commands",
)
```

`commands_dir` is typed as `pathlib.Path`. Resolving via
`Path(__file__).parent` makes the path work from any cwd.

And `src/<project>/__main__.py`:

```python
from <project>.cli import cli

if __name__ == "__main__":
    cli()
```

## Step 3 — convert one script

Pick the simplest script. For a Python one:

**Before** (`scripts/cleanup.py`):

```python
import sys, os

path = sys.argv[1]
for f in os.listdir(path):
    if f.endswith(".tmp"):
        os.remove(os.path.join(path, f))
```

**After** (`src/<project>/commands/cleanup.py`):

```python
from pathlib import Path

import click


@click.command(name="cleanup")
@click.argument("path", type=click.Path(path_type=Path, exists=True,
                                         file_okay=False))
@click.option("--pattern", default="*.tmp", show_default=True)
def cli(path: Path, pattern: str) -> None:
    """Remove matching files from PATH."""
    for f in path.glob(pattern):
        f.unlink()
        click.echo(f"removed {f}")
```

**Why `name="cleanup"`:** clickwork keys registered commands off the
Click command's `.name` attribute. `@click.command()` without `name=`
derives the name from the decorated function, which here is `cli` —
so without the explicit `name="cleanup"`, every command file doing
this pattern would collide on the name `cli`.

Benefits: proper `--help`, validated path, the pattern is now
discoverable, the command prints what it did.

## Step 4 — convert a shell script

Shell scripts become Python commands that shell out. Use clickwork's
process helpers for correct signal forwarding:

```python
import click

from clickwork.process import run


@click.command(name="do-the-thing")
def cli() -> None:
    """What this actually does (write ONE sentence)."""
    run(["bash", "scripts/do_the_thing.sh"])
```

`clickwork.process.run()` streams output in real time and raises
`CliProcessError` on non-zero exit — no `check=` kwarg needed
(non-zero always raises). If you want the output captured into a
string instead of streamed, use `clickwork.process.capture()`
instead of `run()`.

You can keep the original `.sh` file and wrap it, OR rewrite the
logic in Python. Wrap first, rewrite later if it stays.

## Step 5 — repeat and delete

One command per commit. After each, run `projectctl --help` and verify
the new command shows up. When `scripts/` is empty, delete the
directory.

## Tips

- Naming: set it explicitly with `@click.command(name="foo-bar")`
  on every command. clickwork keys commands off the Click command's
  `.name` (with filename fallback only when `.name` is unset), and
  `@click.command()` without an explicit name derives the name from
  the decorated function — so the common `def cli(...)` pattern
  registers as the command `cli` unless you override it. Pick kebab-
  case names (`foo-bar`, not `foo_bar`) to match standard CLI
  conventions.
- Shared helpers go in `src/<project>/_lib.py` (or similar); commands
  import from there. Don't put helper modules in `commands/` — they
  get treated as commands and clickwork will complain.
- Tests: `tests/test_cleanup.py` with `click.testing.CliRunner`
  gives you command-level coverage without shelling out.

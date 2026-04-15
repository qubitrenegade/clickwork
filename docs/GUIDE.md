# Guide

A step-by-step guide to building project automation CLIs with clickwork.
Each section builds on the last, starting from a single command and
progressing to a full-featured CLI with config, environments, and
distributed plugins.

For the design decisions behind the framework, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Who This Is For

You have a project with automation tasks -- deploying, packaging, setting
up CI runners, generating release manifests, running benchmarks. Maybe
these live in scattered bash scripts, maybe they are ad-hoc commands you
run from memory. You want to unify them into a single CLI with consistent
flags, configuration, and error handling.

clickwork gives you the scaffolding. You write the commands.

## Prerequisites

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
uv pip install "git+https://github.com/qubitrenegade/clickwork.git@v0.1.0"
```

For local development alongside your project:

```bash
git clone https://github.com/qubitrenegade/clickwork.git
cd your-project
uv pip install -e ../clickwork
```

## Your First CLI

### Step 1: Create the Entry Point

Create a Python script that will be your CLI. This is the only
boilerplate -- everything else is commands.

```python
#!/usr/bin/env python3
"""my-tool: Project automation CLI."""
from pathlib import Path
from clickwork import create_cli

# Resolve commands_dir relative to this script so it works
# regardless of the current working directory.
commands_dir = Path(__file__).resolve().parent / "commands"
cli = create_cli(name="my-tool", commands_dir=commands_dir)

if __name__ == "__main__":
    cli()
```

### Step 2: Write a Command

Create a `commands/` directory next to your entry point. Drop a `.py`
file in it. The only requirement: export a Click command or group as
`cli`.

```python
# commands/greet.py
import click

@click.command()
@click.argument("name", default="world")
def greet(name: str):
    """Say hello to someone."""
    click.echo(f"Hello, {name}!")

# The framework discovers commands via this export.
cli = greet
```

### Step 3: Run It

```bash
python my-tool.py greet
# Hello, world!

python my-tool.py greet Alice
# Hello, Alice!

python my-tool.py --help
# Shows all discovered commands

python my-tool.py greet --help
# Shows greet's help text
```

You get `--verbose`, `--quiet`, `--dry-run`, `--env`, and `--yes` for
free. Every command inherits these global flags.

## Using the Context

Most commands need access to config, flags, or subprocess helpers. The
`CliContext` dataclass carries all of this. Use `@pass_cli_context` to
receive it:

```python
# commands/deploy.py
import click
from clickwork import pass_cli_context, CliContext

@click.command()
@click.argument("target")
@pass_cli_context
def deploy(ctx: CliContext, target: str):
    """Deploy a component."""
    ctx.require("wrangler")
    account_id = ctx.config.get("cloudflare.account_id")
    ctx.run(["wrangler", "deploy", "--account-id", account_id])

cli = deploy
```

The context gives you:

| Attribute/Method | What It Does |
|-----------------|-------------|
| `ctx.config` | Merged config dict from all sources |
| `ctx.env` | Selected environment string (e.g., `"staging"`) |
| `ctx.dry_run` | `True` if `--dry-run` was passed |
| `ctx.verbose` | Verbosity level (0, 1, or 2) |
| `ctx.yes` | `True` if `--yes` was passed |
| `ctx.logger` | Configured logger instance |
| `ctx.run(cmd)` | Execute a mutating command |
| `ctx.capture(cmd)` | Execute and return stdout |
| `ctx.require(binary)` | Check a binary is on PATH |
| `ctx.confirm(msg)` | Ask yes/no, respects `--yes` |
| `ctx.confirm_destructive(msg)` | Requires typing "yes" |
| `ctx.run_with_confirm(cmd, msg)` | Confirm then execute |

## Subcommand Groups

When a command file exports a `click.Group` instead of a `click.Command`,
it becomes a subcommand group:

```python
# commands/runner.py
import click
from clickwork import pass_cli_context

@click.group()
def runner():
    """Manage CI runners."""
    pass

@runner.command()
@pass_cli_context
def setup(ctx):
    """Set up a new runner."""
    ctx.require("docker")
    ctx.run(["docker", "compose", "up", "-d"])

@runner.command()
@pass_cli_context
def teardown(ctx):
    """Remove a runner."""
    ctx.run_with_confirm(
        ["docker", "compose", "down", "-v"],
        "This will delete all runner data. Continue?"
    )

cli = runner
```

Usage:

```bash
my-tool runner setup
my-tool runner teardown
my-tool runner --help
```

## Configuration

### Repo Config

Create a `.my-tool.toml` file in your project root. The `[default]`
section provides baseline values:

```toml
# .my-tool.toml
[default]
r2.bucket = "releases-staging"
region = "us-east-1"
```

Commands access these via `ctx.config`:

```python
bucket = ctx.config.get("r2.bucket")       # "releases-staging"
region = ctx.config.get("region")           # "us-east-1"
missing = ctx.config.get("nonexistent")     # None
```

### Environment-Specific Config

Add `[env.*]` sections to override values per environment. Keys not
present in the env section fall through to `[default]`:

```toml
[default]
r2.bucket = "releases-staging"
region = "us-east-1"

[env.staging]
cloudflare.account_id = "staging-abc"

[env.production]
cloudflare.account_id = "prod-xyz"
r2.bucket = "releases-prod"
```

Select the environment with `--env`:

```bash
my-tool --env staging deploy site
# r2.bucket = "releases-staging" (from default, not overridden)
# cloudflare.account_id = "staging-abc" (from env.staging)

my-tool --env production deploy site
# r2.bucket = "releases-prod" (overridden in env.production)
# cloudflare.account_id = "prod-xyz" (from env.production)
```

CI pipelines can set `MY_TOOL_ENV=staging` instead of passing `--env`
on every command.

### User Config

Personal settings (credentials, local overrides) go in
`~/.config/my-tool/config.toml`. This file has the **lowest** priority
-- repo config overrides it. To override a repo value locally, use an
environment variable instead.

User config may contain secrets, so the framework enforces owner-only
permissions on Unix (`chmod 600`). Files that are group- or
world-readable are refused.

### Environment Variables

Environment variables have the **highest** priority. Two mechanisms:

**Auto-prefix:** Every config key is automatically checked against
`{PROJECT_NAME}_{KEY}`. Dots become underscores, everything uppercased:

```bash
export MY_TOOL_R2_BUCKET="from-env"
# Overrides r2.bucket from any config file
```

**Explicit mapping:** For third-party env var names, declare the mapping
in a config schema:

```python
CONFIG_SCHEMA = {
    "cloudflare.account_id": {
        "env": "CLOUDFLARE_ACCOUNT_ID",
    },
}

cli = create_cli(name="my-tool", commands_dir=..., config_schema=CONFIG_SCHEMA)
```

When both an explicit mapping and an auto-prefixed var could provide
the same key, the explicit mapping wins.

### Config Schema

Schemas are optional but recommended for production CLIs. They provide
validation at startup so commands do not fail halfway through a deploy
because of a missing key:

```python
CONFIG_SCHEMA = {
    "cloudflare.account_id": {
        "type": str,
        "required": True,
        "env": "CLOUDFLARE_ACCOUNT_ID",
        "description": "Cloudflare account ID for deployments",
    },
    "r2.bucket": {
        "type": str,
        "default": "releases-staging",
    },
    "api_token": {
        "secret": True,
        "env": "MY_TOOL_API_TOKEN",
    },
}
```

Schema features:

- **`required: True`** -- Raises `ConfigError` if the key is missing
  after all layers merge.
- **`type: str`** (or `int`, `bool`, etc.) -- Validates the resolved
  value matches the expected type.
- **`default: "value"`** -- Fills missing keys after all layers merge
  but before validation.
- **`env: "VAR_NAME"`** -- Explicit env var mapping (overrides auto-prefix).
- **`secret: True`** -- Refuses the key if found in repo config (which
  is checked into git). The resolved value is automatically wrapped in a
  `Secret()` instance that redacts itself in logs and string formatting.
- **`description: "..."`** -- Documentation only, ignored by the framework.

## Subprocess Helpers

### run() -- Execute Mutating Commands

Streams output in real-time. Raises `CliProcessError` on non-zero exit.
Respects `--dry-run`:

```python
# Normal execution
ctx.run(["wrangler", "deploy"])

# In --dry-run mode, prints the command without executing
# [dry-run] Would execute: wrangler deploy
```

### capture() -- Execute Read-Only Commands

Returns stripped stdout. Always executes, even in `--dry-run` mode,
because commands need the data to proceed:

```python
sha = ctx.capture(["git", "rev-parse", "HEAD"])
version = ctx.capture(["node", "--version"])
```

### run_with_confirm() -- Destructive Commands

Prompts for confirmation before executing. Respects `--yes` (skips the
prompt) and `--dry-run` (skips execution):

```python
ctx.run_with_confirm(
    ["rm", "-rf", "dist/"],
    "Delete build artifacts?"
)
```

### Passing Secrets to Subprocesses

Never put secrets in the command's argv -- they are visible in `ps`
output. Pass them as environment variables instead:

```python
token = ctx.config["api_token"]  # This is a Secret instance

# WRONG: visible in ps output
ctx.run(["curl", "-H", f"Authorization: Bearer {token.get()}", url])

# RIGHT: only readable by the process owner
ctx.run(["curl", "-H", "Authorization: Bearer $TOKEN", url],
        env={"TOKEN": token.get()})
```

## Prerequisite Checking

Check that required tools exist before doing any work:

```python
ctx.require("docker")                     # Is it on PATH?
ctx.require("gh", authenticated=True)     # On PATH AND authenticated?
```

If the check fails, `PrerequisiteError` is raised with a clear message
and the CLI exits with code 1. This catches missing tools at the top of
a command, not halfway through a deploy.

Built-in auth checks exist for `gh`, `gcloud`, and `aws`. Add your own:

```python
from clickwork.prereqs import AUTH_CHECKS
AUTH_CHECKS["my-tool"] = ["my-tool", "auth", "verify"]
```

## Confirmation Prompts

Two levels of confirmation:

```python
# Standard: "Continue? [y/N]" -- accepts y or yes
if ctx.confirm("Deploy to staging?"):
    ctx.run(["deploy", "--env", "staging"])

# Destructive: requires typing the full word "yes"
if ctx.confirm_destructive("Drop the production database?"):
    ctx.run(["dropdb", "production"])
```

Both respect `--yes` (auto-confirm for CI) and auto-deny when stdin is
not a TTY (prevents hangs in piped/automated contexts).

## Distributing as a Package

Once your CLI is stable, you can distribute it as an installable package.
Commands are registered via Python entry points so they are discoverable
without a `commands/` directory on disk.

### Package Structure

```
my-tool/
  pyproject.toml
  src/
    my_tool/
      __init__.py
      commands/
        deploy.py       # exports cli = click.command()(deploy)
        runner.py        # exports cli = click.group()(runner)
```

### pyproject.toml

```toml
[project]
name = "my-tool"
dependencies = ["clickwork"]

[project.scripts]
my-tool = "my_tool:cli"

[project.entry-points."clickwork.commands"]
deploy = "my_tool.commands.deploy:cli"
runner = "my_tool.commands.runner:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### Entry Point in __init__.py

```python
# src/my_tool/__init__.py
from clickwork import create_cli
cli = create_cli(name="my-tool", discovery_mode="installed")
```

After installation (`pip install my-tool`), users run `my-tool deploy`
directly. The framework discovers commands from entry points -- no
`commands/` directory needed.

## Testing Your Commands

### Unit Testing with CliRunner

Click's `CliRunner` invokes your CLI in-process without spawning a
subprocess:

```python
from click.testing import CliRunner
from clickwork import create_cli

def test_deploy_dry_run(tmp_path):
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir()
    (cmd_dir / "deploy.py").write_text(
        "import click\n"
        "@click.command()\n"
        "@click.pass_obj\n"
        "def deploy(ctx):\n"
        "    click.echo(f'dry_run={ctx.dry_run}')\n"
        "cli = deploy\n"
    )

    cli = create_cli(name="test-cli", commands_dir=cmd_dir)
    result = CliRunner().invoke(cli, ["--dry-run", "deploy"])

    assert result.exit_code == 0
    assert "dry_run=True" in result.output
```

### Testing with a Mock Context

For testing command logic without the CLI harness, construct a
`CliContext` directly:

```python
from clickwork import CliContext

def test_deploy_logic():
    commands_run = []
    ctx = CliContext(
        config={"cloudflare.account_id": "test-123"},
        dry_run=False,
    )
    ctx.run = lambda cmd, env=None: commands_run.append(cmd)
    ctx.require = lambda binary, **kw: None

    # Call your command logic directly
    deploy_impl(ctx, target="site")

    assert ["wrangler", "deploy", "--account-id", "test-123"] in commands_run
```

### Using the conftest Fixture

The test suite provides a `make_cli_context` fixture for constructing
contexts with sensible defaults:

```python
def test_something(make_cli_context):
    ctx = make_cli_context(dry_run=True, config={"key": "value"})
    assert ctx.dry_run is True
```

## Reference

### Config Resolution Order

Highest priority wins:

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | Explicit env var mapping | `CLOUDFLARE_ACCOUNT_ID` |
| 1 | Auto-prefixed env var | `MY_TOOL_BUCKET` |
| 2 | Env-specific section | `[env.staging]` in `.my-tool.toml` |
| 3 | Default section | `[default]` in `.my-tool.toml` |
| 4 (lowest) | User config | `~/.config/my-tool/config.toml` |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | User/environment error (missing tool, bad config, command failure) |
| 2 | Framework internal error (unhandled exception -- report as a bug) |

### Global Flags

| Flag | Description |
|------|-------------|
| `--verbose` / `-v` | Increase log verbosity (`-v` = INFO, `-vv` = DEBUG) |
| `--quiet` / `-q` | Suppress non-error output (mutually exclusive with `-v`) |
| `--dry-run` | Preview actions without executing |
| `--env NAME` | Select config environment |
| `--yes` / `-y` | Skip confirmation prompts |

### Public API

Everything you need is re-exported from `clickwork`:

```python
from clickwork import (
    create_cli,          # Build a CLI with global flags and discovery
    load_config,         # Load layered TOML config directly
    CliContext,          # Typed context passed to every command
    pass_cli_context,    # Decorator for receiving CliContext
    Secret,              # Redacted wrapper for sensitive values
    CliProcessError,     # Raised when a subprocess fails
    PrerequisiteError,   # Raised when a required tool is missing
    ConfigError,         # Raised when config validation fails
    normalize_prefix,    # Convert project name to env-var prefix
)
```

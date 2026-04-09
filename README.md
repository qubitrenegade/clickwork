# qbrd-tools

Reusable CLI framework for project automation. Build project-specific CLIs
with plugin discovery, layered config, subprocess helpers, and common
utilities -- so your commands focus on business logic, not boilerplate.

> **Status:** Pre-1.0 (`0.x`). API is unstable. All features are driven by
> real [orbit-admin](https://github.com/qubitrenegade/qbrd-orbit-widener)
> needs -- no speculative abstractions.

## Installation

```bash
# Pin to a tag or SHA for reproducibility
uv pip install "git+https://github.com/qubitrenegade/qbrd-tools.git@v0.1.0"
```

For local development alongside a consumer project:

```bash
git clone https://github.com/qubitrenegade/qbrd-tools.git
cd your-project
uv pip install -e ../qbrd-tools
```

## Quick Start

### 1. Create your entry point

```python
#!/usr/bin/env python3
"""my-tool: Project automation CLI."""
from pathlib import Path
from qbrd_tools import create_cli

commands_dir = Path(__file__).resolve().parent / "commands"
cli = create_cli(name="my-tool", commands_dir=commands_dir)

if __name__ == "__main__":
    cli()
```

### 2. Write a command

Drop a `.py` file in your `commands/` directory. Export a Click command or
group as `cli`:

```python
# commands/deploy.py
import click
from qbrd_tools import pass_cli_context, CliContext

@click.command()
@click.argument("target")
@pass_cli_context
def deploy(ctx: CliContext, target: str):
    """Deploy a component to the active environment."""
    ctx.require("wrangler")
    account_id = ctx.config.get("cloudflare.account_id")
    ctx.run(["wrangler", "deploy", "--account-id", account_id])

cli = deploy
```

### 3. Run it

```bash
# Dev mode (directory scanning)
python tools/my-tool.py deploy site

# With flags
python tools/my-tool.py --env staging --dry-run deploy site

# Help
python tools/my-tool.py --help
python tools/my-tool.py deploy --help
```

See the [sample plugin](tests/fixtures/sample-plugin/) for a complete
working example with subcommand groups.

## Features

### Plugin Discovery

Two mechanisms, selected via `discovery_mode`:

- **Directory scanning (`dev`):** Imports `.py` files from `commands_dir`,
  registers any that export a `cli` attribute (Click command or group).
  Subdirectories like `lib/` are skipped. Used for local development.

- **Entry points (`installed`):** Reads the `qbrd_tools.commands` entry point
  group from installed packages. Used for distributed plugins.

- **Auto mode (default):** Uses directory scanning if `commands_dir` exists
  on disk, plus entry points from installed packages. Local commands win on
  name conflicts (with an info log).

### Layered Config

TOML-based configuration with cascading precedence (highest wins):

1. **Environment variables** -- explicit mappings (`CLOUDFLARE_ACCOUNT_ID`)
   or auto-prefixed (`MY_TOOL_BUCKET`)
2. **Env-specific section** -- `[env.staging]` in repo config, selected via
   `--env` flag or `{PROJECT_NAME}_ENV` env var
3. **Default section** -- `[default]` in repo config (`.my-tool.toml`)
4. **User-level config** -- `~/.config/my-tool/config.toml`

```toml
# .my-tool.toml
[default]
r2.bucket = "releases-staging"

[env.production]
r2.bucket = "releases-prod"
cloudflare.account_id = "prod-xyz"
```

Optional schema validation with required keys, defaults, type checking, and
secret-in-repo-config rejection:

```python
CONFIG_SCHEMA = {
    "cloudflare.account_id": {
        "required": True,
        "env": "CLOUDFLARE_ACCOUNT_ID",
    },
    "api_token": {
        "secret": True,  # Rejected if found in repo config
        "env": "MY_TOOL_API_TOKEN",
    },
}

cli = create_cli(name="my-tool", commands_dir=..., config_schema=CONFIG_SCHEMA)
```

### Subprocess Helpers

Commands get `ctx.run()`, `ctx.capture()`, and `ctx.run_with_confirm()`:

```python
# Mutating command -- respects --dry-run
ctx.run(["wrangler", "deploy"])

# Read-only -- always executes, even in dry-run
output = ctx.capture(["git", "rev-parse", "HEAD"])

# Destructive -- prompts for confirmation first
ctx.run_with_confirm(["rm", "-rf", "dist/"], "Delete build artifacts?")
```

- All helpers accept **argv lists only** (never strings) to prevent shell injection
- `run()` streams output in real-time, raises `CliProcessError` on failure
- `capture()` returns stripped stdout, always executes (read-only convention)
- Secrets passed via `env=` parameter, not argv (visible in `ps`)
- SIGINT forwarded to child processes before propagating

### Global Flags

Every CLI built with `create_cli()` gets these flags automatically:

| Flag | Description |
|------|-------------|
| `--verbose` / `-v` | Increase log verbosity (`-vv` for debug) |
| `--quiet` / `-q` | Suppress non-error output (mutually exclusive with `-v`) |
| `--dry-run` | Preview actions without executing |
| `--env` | Select config environment (e.g., `--env staging`) |
| `--yes` / `-y` | Skip confirmation prompts (for CI) |

### Typed Context

`CliContext` is a dataclass passed to every command via `@pass_cli_context`.
It holds config, flags, logger, and convenience methods:

```python
@click.command()
@pass_cli_context
def my_command(ctx: CliContext):
    ctx.config.get("some.key")    # Resolved config value
    ctx.env                        # Selected environment
    ctx.dry_run                    # True if --dry-run
    ctx.run(["echo", "hello"])     # Subprocess helper
    ctx.require("docker")          # Prerequisite check
    ctx.confirm("Continue?")       # TTY-aware prompt
```

### Secret Safety

- `Secret` wrapper type redacts values in `str()`, `repr()`, `f-strings`,
  `vars()`, and pickle
- User config files checked for owner-only permissions (0o600)
- Keys tagged `secret: True` in schema are rejected if found in repo config
- Subprocess secrets passed via env vars, not argv

### Prerequisite Checking

```python
ctx.require("docker")                    # Is it on PATH?
ctx.require("gh", authenticated=True)    # Is it on PATH AND authenticated?
```

Known auth checks are built in for `gh`, `gcloud`, and `aws`. Extensible via
`qbrd_tools.prereqs.AUTH_CHECKS`.

## Architecture

```
your-project/
  tools/
    my-tool.py          # Entry point: create_cli(name="my-tool", commands_dir=...)
    commands/
      deploy.py         # cli = click.command()(deploy_fn)
      runner.py         # cli = click.group()(runner_group)
    lib/
      helpers.py        # Shared code (not auto-discovered)
    .my-tool.toml       # Repo-level config
```

The framework (`qbrd-tools`) provides:

| Module | Responsibility |
|--------|---------------|
| `cli.py` | `create_cli()` factory, global flags, context wiring |
| `discovery.py` | Plugin discovery (directory + entry points) |
| `config.py` | Layered TOML config, schema validation |
| `process.py` | `run()`, `capture()`, `run_with_confirm()` |
| `prereqs.py` | `require()` binary/auth checks |
| `prompts.py` | `confirm()`, TTY detection |
| `_logging.py` | Logging setup, verbosity levels |
| `platform.py` | Platform detection, repo root finding |
| `_types.py` | `CliContext`, `Secret`, `CliProcessError` |

## Development

```bash
git clone https://github.com/qubitrenegade/qbrd-tools.git
cd qbrd-tools
uv venv && uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/unit/ -v          # Fast unit tests
uv run pytest tests/integration/ -v   # Slower integration tests (creates venvs)
uv run pytest tests/ -v               # Everything
```

## License

MIT

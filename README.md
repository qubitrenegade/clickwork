# clickwork

[![PyPI](https://img.shields.io/pypi/v/clickwork.svg)](https://pypi.org/project/clickwork/) [![Python Versions](https://img.shields.io/pypi/pyversions/clickwork.svg)](https://pypi.org/project/clickwork/) [![Docs](https://img.shields.io/badge/docs-clickwork.readthedocs.io-blue)](https://clickwork.readthedocs.io/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/qubitrenegade/clickwork/blob/main/LICENSE)

**Docs:** <https://clickwork.readthedocs.io/> — full tutorials, how-to recipes, API reference, and LLM-oriented reference.

Reusable CLI framework for project automation. Build project-specific CLIs
with plugin discovery, layered config, subprocess helpers, and common
utilities -- so your commands focus on business logic, not boilerplate.

> **Status:** 1.0 stable. The public API is documented in the
> [API Policy](https://clickwork.readthedocs.io/explanation/api-policy/)
> and covered by SemVer: breaking changes require a major bump and
> removals carry a one-minor deprecation runway. All features are
> driven by real
> [orbit-admin](https://github.com/qubitrenegade/qbrd-orbit-widener)
> needs -- no speculative abstractions.

Upgrading from 0.2.x? See the
[Migrating guide](https://clickwork.readthedocs.io/reference/migrating/)
for the complete before/after diff.

## Installation

```bash
# From PyPI (preferred)
uv pip install "clickwork>=1.0,<2"
# or, pinning to a git tag if you need a ref PyPI doesn't expose
uv pip install "git+https://github.com/qubitrenegade/clickwork.git@v1.0.0"
```

For local development alongside a consumer project:

```bash
git clone https://github.com/qubitrenegade/clickwork.git
cd your-project
uv pip install -e ../clickwork
```

## Quick Start

### 1. Create your entry point

```python
#!/usr/bin/env python3
"""my-tool: Project automation CLI."""
from pathlib import Path
from clickwork import create_cli

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
from clickwork import pass_cli_context, CliContext

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

## Documentation

The full site lives at
**<https://clickwork.readthedocs.io/>**. Highlights:

### Start here

- **[Quickstart](https://clickwork.readthedocs.io/tutorials/quickstart/)**
  -- install to first working command in about 5 minutes.
- **[Practical Walkthrough](https://clickwork.readthedocs.io/tutorials/walkthrough/)**
  -- build a realistic CLI with a local command, an installed plugin,
  and a publishable wheel.

### Cookbook

- **[How-To recipes](https://clickwork.readthedocs.io/how-to/)**
  -- tame an out-of-control script directory, add a command, write a
  plugin, migrate from argparse.

### Reference

- **[User Guide](https://clickwork.readthedocs.io/reference/guide/)**
  -- Step-by-step tutorial: building a CLI, adding config, using
  subprocess helpers, distributing as a package, testing your
  commands.
- **[Plugins](https://clickwork.readthedocs.io/reference/plugins/)**
  -- 15-minute walkthrough for shipping a pip-installable plugin via
  the `clickwork.commands` entry-point group.
- **[Security](https://clickwork.readthedocs.io/reference/security/)**
  -- What clickwork defends against, what it leaves to the CLI
  author, threat model assumptions, and how to report
  vulnerabilities.
- **[Migrating 0.2.x to 1.0](https://clickwork.readthedocs.io/reference/migrating/)**
  -- Breaking changes, new opt-in surfaces, and concrete before/after
  diffs for upgraders.
- **[API Reference](https://clickwork.readthedocs.io/reference/api/)**
  -- Auto-generated from docstrings.
- **[LLM Reference](https://clickwork.readthedocs.io/reference/llm-reference/)**
  -- Compact, LLM-oriented cheat sheet of the public surface with a
  "Common Footguns" section. Useful whether you're an AI agent
  generating clickwork code or a human skimming for gotchas.

### Explanation

- **[Architecture](https://clickwork.readthedocs.io/explanation/architecture/)**
  -- Design decisions, module responsibilities, security model, and
  the reasoning behind non-obvious choices.
- **[API Policy](https://clickwork.readthedocs.io/explanation/api-policy/)**
  -- The 1.0 public surface: which symbols are covered by SemVer,
  deprecation runway, supported Python and Click ranges.
- **[Plugin Model](https://clickwork.readthedocs.io/explanation/plugin-model/)**
  -- Why entry points, why local files win on collision, and how
  discovery actually works.

## Features

### Plugin Discovery

Two mechanisms, selected via `discovery_mode`:

- **Directory scanning (`dev`):** Imports `.py` files from `commands_dir`,
  registers any that export a `cli` attribute (Click command or group).
  Subdirectories like `lib/` are skipped. Used for local development.

- **Entry points (`installed`):** Reads the `clickwork.commands` entry point
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
`clickwork.prereqs.AUTH_CHECKS`.

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

The framework (`clickwork`) provides:

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the canonical local setup
(`uv sync --extra dev`), the four-command verification suite that
matches CI, test-writing pointers, PR conventions, and review
expectations. The section below is a quick pytest reference for
contributors who already have a venv.

```bash
# Run tests (after uv sync --extra dev)
uv run pytest tests/unit/ -v          # Fast unit tests
uv run pytest tests/integration/ -v   # Slower integration tests (creates venvs)
uv run pytest tests/ -v               # Everything
```

## License

MIT

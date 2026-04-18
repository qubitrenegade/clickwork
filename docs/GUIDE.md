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
# From PyPI (preferred)
uv pip install "clickwork==0.2.0"
# or, pinning to a git tag if you need a ref PyPI doesn't expose
uv pip install "git+https://github.com/qubitrenegade/clickwork.git@v0.2.0"
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
free. Every command inherits these global flags. Pass `version=` or
`package_name=` to `create_cli()` to also install `--version` / `-V`
(see "Version flag" below).

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

### Config Precedence

clickwork merges config from six ordered sources. The table below is
the authoritative precedence contract -- it is part of the public 1.0
surface (see [`API_POLICY.md`](API_POLICY.md#protocol-level-surfaces)),
so changing the order is a breaking change requiring a major version
bump. Highest priority wins; when the same key is set in multiple
sources, the higher row's value is what `ctx.config[key]` returns.

| # | Source | How it's set | Notes / gotchas |
|---|--------|--------------|-----------------|
| 1 (highest) | Explicit env-var mapping | `CLOUDFLARE_ACCOUNT_ID=abc` with `{"env": "CLOUDFLARE_ACCOUNT_ID"}` in schema | Only applies to keys whose schema entry declares an `env:` name. Beats the auto-prefix form for the same key. |
| 2 | Auto-prefixed env var | `MY_TOOL_R2_BUCKET=bucket-x` | Prefix is the project name uppercased with hyphens replaced by `_`; suffix is the dotted config key with `.` and `-` replaced by `_`, uppercased. `my-tool` + `r2.bucket` -> `MY_TOOL_R2_BUCKET`. |
| 3 | `[env.<selected>]` section | `[env.staging]` in `.my-tool.toml`, selected via `--env staging` or `MY_TOOL_ENV=staging` | Only the section matching the selected env applies; other `[env.*]` sections are ignored. Selecting an undefined env raises `ConfigError` when the repo config file exists but has no matching `[env.<name>]` section. If there is no repo config file at all, the env selection is silently a no-op. |
| 4 | `[default]` section | `[default]` in `.my-tool.toml` | Shared defaults for all envs. Keys absent from the selected `[env.<x>]` fall through to here. |
| 5 | User config | `~/.config/my-tool/config.toml` | Must be `chmod 600` (or stricter) on POSIX -- any group/other bit raises `ConfigError`. On Windows the permission check is skipped. |
| 6 (lowest) | Schema default | `{"port": {"default": 8080}}` in the schema passed to `create_cli()` | Applied only if the key is declared in the schema AND still absent after all higher layers merge. No schema => no defaults. |

The rationale for this order:

- **Env vars win** because they are the canonical escape hatch for
  one-off overrides and per-process CI injection. An operator should
  be able to override any checked-in value without editing a file.
- **Repo config beats user config** because clickwork targets project
  automation: the `.my-tool.toml` committed to the repo defines the
  project's canonical behaviour, so teammates and CI get the same
  result. If you want a *personal* override, use an env var (layer 1
  or 2), not your user config.
- **Schema defaults sit at the bottom** so they only fire when no
  real source provided a value. This matches the "documented
  fallback" role defaults play -- a schema default should never
  shadow a value the operator explicitly set anywhere else.

#### Worked example

Given the schema, repo file, user file, and env below:

```python
# in create_cli(config_schema=...)
CONFIG_SCHEMA = {
    "r2.bucket": {"type": str, "default": "releases-fallback"},
    "region":    {"type": str, "default": "us-east-1"},
}
```

```toml
# .my-tool.toml (checked in)
[default]
r2.bucket = "releases-staging"
region    = "us-east-1"

[env.production]
r2.bucket = "releases-prod"
```

```toml
# ~/.config/my-tool/config.toml
r2.bucket = "releases-personal"
region    = "eu-west-1"
```

```bash
MY_TOOL_REGION=ap-south-1 my-tool --env production deploy
```

Resolution:

| Key | Value | Chosen by |
|-----|-------|-----------|
| `r2.bucket` | `"releases-prod"` | Layer 3 (`[env.production]`) beats user config (layer 5) and `[default]` (layer 4). |
| `region` | `"ap-south-1"` | Layer 2 (auto-prefixed `MY_TOOL_REGION`) beats every TOML layer and the schema default. |

Drop the env var (`unset MY_TOOL_REGION`) and `region` falls to
`"us-east-1"` from `[default]` (layer 4), NOT `"eu-west-1"` from the
user file (layer 5) -- because repo config overrides user config. Drop
`[env.production]` and `r2.bucket` falls to `"releases-staging"` from
`[default]`. Drop every source for a key entirely and it resolves to
its schema default (`"releases-fallback"`) or raises `ConfigError` if
the schema marks it `required: True`.

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
`~/.config/my-tool/config.toml`. This file sits **below** repo config
in the precedence order -- repo config overrides it. (Schema-declared
defaults are the only thing lower; see [Config
Precedence](#config-precedence) for the full table.) To override a
repo value locally, use an environment variable instead.

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

Env-var values always arrive as strings from the OS. If your schema
declares `type: int`, `type: float`, or `type: bool` for a key that
might be set via env var, the loader coerces the string into the
declared type automatically. See
[Environment Variable Types](#environment-variable-types) below for
the coercion rules -- bool parsing in particular has a fixed
allowlist that avoids the classic `bool("false") == True`
foot-cannon.

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
- **`type: str`** (or `int`, `bool`, `float`) -- Validates the resolved
  value matches the expected type. For **any string-sourced value**
  (env var or TOML string literal), also
  performs coercion to the declared type -- see
  [Environment Variable Types](#environment-variable-types) below for
  the exact rules and the bool allowlist.
- **`default: "value"`** -- Fills missing keys after all layers merge
  but before validation.
- **`env: "VAR_NAME"`** -- Explicit env var mapping (overrides auto-prefix).
- **`secret: True`** -- Refuses the key if found in repo config (which
  is checked into git). The resolved value is automatically wrapped in a
  `Secret()` instance that redacts itself in logs and string formatting.
- **`description: "..."`** -- Documentation only, ignored by the framework.

### Environment Variable Types

Environment variables at the OS level are **always strings**.
`os.environ` is `dict[str, str]`, and the kernel-level `environ`
array is a list of `NAME=value` byte strings -- there is no such
thing as an "integer environment variable." That means when a
plugin author declares a schema key like `{"port": {"type": int}}`
and the value arrives via `MY_TOOL_PORT=8080`, *something* has to
convert the string `"8080"` into the integer `8080` before the
command code uses it.

clickwork pins that conversion at the **schema layer**. When the
loader finishes merging all layers and the schema declares a non-
`str` `type`, the loader coerces any string value in the merged
config dict to the declared type before returning it in
`ctx.config`. The rule is uniform across sources: the coercion
applies to env vars, TOML string literals (`port = "8080"`), and
TOML string literals alike -- whichever source produced the
string, the same coercion fires. The caller never has to write
`int(os.environ["PORT"])` by hand, and a TOML author who quoted the
value by mistake still gets a usable int.

Pinning coercion at the schema layer (rather than the caller or the
env-var reader) means:

- Env vars and TOML values behave the same at the call site.
  `ctx.config["port"]` is an int whether it came from
  `port = 8080` in TOML or `MY_TOOL_PORT=8080` in the shell.
- String literals in TOML coerce too. A `.test-cli.toml` that
  contains `port = "8080"` under `type: int` produces the int
  `8080` in `ctx.config["port"]`, matching what an env var would
  have delivered.
- Conversion errors surface at CLI startup (during `load_config`)
  rather than halfway through a deploy when a command does
  arithmetic on a string. You get a `ConfigError` naming the key
  and the offending value (redacted to `<redacted>` if the schema
  marks the key as `secret: True`).
- The coercion table is small, stdlib-only, and deliberately
  explicit about bools so Python's classic `bool("false") == True`
  foot-cannon never bites you.

The supported `type` values and their string-source coercion rules:

| Schema `type` | String input | Result | Failure mode |
|---------------|--------------|--------|--------------|
| `str` | `"hello"` | `"hello"` (unchanged) | Never fails -- strings are strings. |
| `int` | `"8080"` | `8080` (base 10) | `ConfigError` on non-integer text (`"3.14"`, `"abc"`). |
| `float` | `"3.14"` | `3.14` | `ConfigError` on non-numeric text. |
| `bool` | `"true"`, `"1"`, `"yes"`, `"on"` | `True` | `ConfigError` on anything outside the allowlist. |
| `bool` | `"false"`, `"0"`, `"no"`, `"off"` | `False` | See above. |

Boolean parsing is **case-insensitive** (`"TRUE"`, `"True"`, and
`"true"` all produce `True`) but the allowlist is fixed. Tokens like
`"maybe"`, `"enabled"`, or `"y"` raise `ConfigError` rather than
silently defaulting either way. If you need looser parsing, do it
in your command code before feeding the value to clickwork.

Values that already carry the declared type pass through unchanged.
TOML's native typing means `port = 8080` parses as `int` and skips
coercion entirely -- the schema `type` check still runs, but
there's nothing to convert. Only *string* values in the merged
config dict take the coercion path.

Worked TOML-string example: given the schema
`{"port": {"type": int}}` and a repo config containing
`port = "8080"` (quoted string literal), the loader coerces the
string and `ctx.config["port"]` is the int `8080` -- identical to
what `port = 8080` (unquoted int) would have produced.

Without a schema, string values stay as strings in `ctx.config`.
The schema's `type` declaration is the explicit opt-in for
coercion; there is no heuristic "looks like a number, must be a
number" detection.

Example combining all of the above:

```python
CONFIG_SCHEMA = {
    "port": {
        "type": int,
        "default": 8080,
        "description": "HTTP listener port; honours MY_TOOL_PORT.",
    },
    "debug": {
        "type": bool,
        "default": False,
    },
    "api_token": {
        "secret": True,
        "env": "MY_TOOL_API_TOKEN",
    },
}
```

With `MY_TOOL_PORT=9090 MY_TOOL_DEBUG=true my-tool deploy`, the
command sees `ctx.config["port"] == 9090` (int) and
`ctx.config["debug"] is True`.

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

### Testing commands with `clickwork.testing`

The `clickwork.testing` module ships two thin helpers that collapse the
boilerplate of constructing a test CLI and invoking it through Click's
`CliRunner`:

```python
from clickwork.testing import make_test_cli, run_cli

def test_greet_says_hello(tmp_path):
    (tmp_path / "greet.py").write_text(
        "import click\n"
        "@click.command()\n"
        "def greet():\n"
        "    click.echo('hello')\n"
        "cli = greet\n"
    )

    cli = make_test_cli(commands_dir=tmp_path)
    result = run_cli(cli, ["greet"])

    assert result.exit_code == 0
    assert "hello" in result.stdout
```

What the helpers do:

- `make_test_cli(*, commands_dir=None, **kwargs)` wraps
  `create_cli()` with a default `name="test-cli"`. Every other kwarg
  forwards unchanged, so you still get `description=`, `config_schema=`,
  etc. when you need them.
- `run_cli(cli, args, **kwargs)` wraps `CliRunner().invoke()` with
  `catch_exceptions=False` pinned by default. This means a bug in your
  command surfaces as a real traceback in pytest output instead of being
  quietly captured into `result.exception`. Pass `catch_exceptions=True`
  explicitly when you want Click's default swallow-and-report behaviour.

`run_cli` returns Click's native `click.testing.Result` -- the helpers
deliberately do not invent a new result type, so any idiom you already
know from Click docs keeps working.

### `result.output` vs `result.stdout` vs `result.stderr`

Click's `Result` exposes three stream attributes and they are **not
interchangeable**:

| Attribute | Contents |
|-----------|----------|
| `result.output` | stdout **and** stderr interleaved in the order the command produced them |
| `result.stdout` | stdout only |
| `result.stderr` | stderr only |

The rule of thumb: if a test says "the error message was printed to
stderr", it should assert on `result.stderr` -- asserting on
`result.output` would pass even if the command wrote the error to
stdout by mistake, because `output` contains both streams.

```python
@click.command()
def noisy():
    click.echo("normal line")
    click.echo("error line", err=True)

result = run_cli(noisy, [])
assert "normal line" in result.stdout        # yes
assert "error line" in result.stderr          # yes
assert "error line" in result.output          # ALSO yes (interleaved)
assert "normal line" in result.stderr         # NO -- would fail
```

> **Footgun:** Click 8.2 removed the `mix_stderr` kwarg on
> `CliRunner.__init__` that used to toggle whether stderr was folded
> into `output`. Post-removal, `result.stdout` / `result.stderr` are
> populated independently and `result.output` keeps providing the
> interleaved form. clickwork declares `click>=8.2` so this guidance
> always applies: snippets in older tutorials that use
> `CliRunner(mix_stderr=False)` will raise `TypeError`, and the
> `result.stderr` advice above cannot fall back to Click 8.1 where,
> under the default `CliRunner()` configuration (streams mixed unless
> `mix_stderr=False` was passed), it would have raised
> `ValueError: stderr not separately captured`.

### Unit Testing with CliRunner

If you need finer control than `run_cli` gives -- custom `CliRunner`
configuration, isolated filesystems via `runner.isolated_filesystem()`,
and so on -- reach for Click's `CliRunner` directly:

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
    # Pass ``catch_exceptions=False`` here for the same reason
    # ``run_cli`` pins it above: without it, a bug inside the command
    # surfaces only as ``result.exception`` with a generic exit code,
    # and the real traceback is swallowed.
    result = CliRunner().invoke(cli, ["--dry-run", "deploy"], catch_exceptions=False)

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

See [Config Precedence](#config-precedence) above for the authoritative
ordered table (layers 1-6, from highest to lowest priority) and a
worked example showing which source wins each tiebreaker. The ordering
is part of the 1.0 stability contract -- see
[`API_POLICY.md`](API_POLICY.md#protocol-level-surfaces).

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
| `--version` / `-V` | Print the CLI's version string and exit (only installed if `create_cli()` receives `version=` or `package_name=`) |

### Overriding a global option in a subcommand

`add_global_option(cli, "--flag", ...)` installs the same option on the
root group AND every subcommand that exists at call time, so users can
pass the flag at any level. Occasionally a single subcommand needs to
reclaim a flag name for different semantics — for example, a plugin
subcommand that wants its own `--env` with a plugin-specific default
instead of the framework's `--env` that selects the config environment.

**The rule:** inside the overriding subcommand's scope, the subcommand's
option wins — the value flows into that subcommand's own kwarg, and the
global's merge callback does not run there. Outside that subcommand
(i.e. on other subcommands that did NOT redeclare the flag, or at the
root group level), the global remains active and continues to populate
`ctx.find_root().meta`.

**The pattern:** `add_global_option` takes a call-time snapshot of the
command tree — only commands attached BEFORE the call get the global
installed on them. Commands attached AFTER the call don't. So the
right ordering is:

1. Attach every subcommand that SHOULD inherit the global (or that
   simply doesn't care about the flag).
2. Call `add_global_option`.
3. Attach the overriding subcommand(s) — these won't get the global
   installed, so their own `@click.option("--region", ...)` owns the
   flag inside their scope.

```python
import click
from clickwork import create_cli, add_global_option

cli = create_cli(name="myapp", commands_dir=None)

# Step 1: attach subcommands that should inherit the global.
@cli.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    # --region is accessible via ctx.find_root().meta because the
    # global was installed on this subcommand (step 2 ran after the
    # attachment).
    region = ctx.find_root().meta.get("region")
    click.echo(f"status for {region!r}")

# Step 2: install the global AFTER every inheriting subcommand is
# attached. Pick a name that does NOT collide with create_cli's
# framework builtins (--verbose, --quiet, --dry-run, --env, --yes) --
# we use --region in this example.
add_global_option(cli, "--region", default=None, help="Target region.")

# Step 3: attach the overriding subcommand. Its own --region wins
# inside its scope; `status` above still sees the global via
# ctx.find_root().meta because it was attached before add_global_option ran.
@cli.command("deploy")
@click.option("--region", default="us-east-1", help="Deploy target.")
@click.pass_context
def deploy(ctx: click.Context, region: str) -> None:
    # `region` here is the subcommand's own kwarg -- "us-east-1" by
    # default, or whatever the user passed after "deploy" on the CLI.
    # The global's ctx.find_root().meta["region"] reflects the
    # ROOT-level parse only; it is NOT touched by the inner --region.
    click.echo(f"deploying to {region}")
```

The **reverse order** — subcommand declares `--region` **first**, then
`add_global_option(cli, "--region", ...)` is called — is rejected at
install time with `ValueError` naming the colliding flag string (e.g.
`--region`). That failure mode is deliberate: silently picking a
winner would make override behaviour order-dependent. If you hit this
error, either rename one side or reorder your setup so
`add_global_option` runs before the overriding subcommand is attached.

### Version flag

`create_cli()` accepts two kwargs that opt into a `--version` / `-V`
flag on the resulting group:

- `version="1.2.3"` — the literal string to print.
- `package_name="your-pypi-name"` — resolve via
  `importlib.metadata.version(...)` at `create_cli()` call time; a
  missing distribution raises `ValueError` so typos fail loud.

When both are set, `version=` wins. When neither is set, `--version`
is NOT installed so existing clickwork consumers see no change on
upgrade.

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

## Release notes

This section is for clickwork maintainers cutting a release; framework users
can skip it.

Release notes for clickwork itself are produced by GitHub's built-in
auto-generated release notes feature, configured via
[`.github/release.yml`](../.github/release.yml). The config is only
consulted when auto-generated notes are explicitly requested — clicking
"Generate release notes" in the GitHub UI's Release form, or passing
`--generate-notes` to `gh release create`. A bare `gh release create`
uses the body you provide directly.

To make sure a PR lands in the right section, apply one of these labels
before merging:

| Label | Section |
|-------|---------|
| `enhancement` | Features |
| `bug` | Bug fixes |
| `documentation` | Documentation |
| (no label, or any other label) | Other changes |

PRs labeled `duplicate`, `invalid`, or `wontfix` are excluded from the notes
entirely.

Maintainers can still tweak the generated notes in the GitHub Release UI
before publishing -- the auto-generated text is a starting point, not a
finished artifact. This is the right place to call out breaking changes,
highlight the most user-visible work, or add an upgrade blurb.

For the underlying mechanism and full config grammar, see GitHub's docs on
[automatically-generated release notes](https://docs.github.com/en/repositories/releasing-projects-on-github/automatically-generated-release-notes#configuring-automatically-generated-release-notes).

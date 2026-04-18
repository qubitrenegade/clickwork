# clickwork -- LLM Reference

Reference for AI agents building CLI commands with clickwork. Read this
before writing or migrating commands.

For deeper context: [GUIDE.md](GUIDE.md) (tutorial), [ARCHITECTURE.md](ARCHITECTURE.md) (design decisions).

## What clickwork Is

A Python CLI framework built on Click. You write command files, drop them
in a `commands/` directory, and the framework handles discovery, config,
subprocess management, global flags, and error handling.

## Writing a Command

Every command file exports a Click command or group as `cli`:

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

### Subcommand Groups

Export a `click.Group` instead of a `click.Command`:

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

cli = runner
```

## CliContext API

Commands receive `CliContext` via `@pass_cli_context`. Available attributes:

| Method/Attribute | Purpose |
|-----------------|---------|
| `ctx.run(cmd)` | Execute mutating command (respects --dry-run) |
| `ctx.capture(cmd)` | Execute and return stdout (always runs, even dry-run) |
| `ctx.run_with_confirm(cmd, msg)` | Confirm then execute |
| `ctx.require(binary)` | Assert binary on PATH |
| `ctx.require(binary, authenticated=True)` | Assert binary + auth |
| `ctx.confirm(msg)` | Yes/no prompt (respects --yes) |
| `ctx.confirm_destructive(msg)` | Requires typing "yes" |
| `ctx.config` | Merged config dict |
| `ctx.env` | Selected environment string |
| `ctx.dry_run` | True if --dry-run |
| `ctx.verbose` | Verbosity level (0/1/2) |
| `ctx.yes` | True if --yes |
| `ctx.logger` | Configured logger |

## Rules

### Never Do

- **Never pass strings to run/capture.** Always `list[str]`:
  `ctx.run(["echo", "hello"])` not `ctx.run("echo hello")`.
- **Never put secrets in argv.** Use `ctx.run(cmd, env={"TOKEN": secret.get()})`.
- **Never hardcode config values.** Use `ctx.config.get("key")`.
- **Never import from `clickwork._types` or other private modules in command code.**
  Import from `clickwork` directly.

### Always Do

- **Export `cli`** at module level. The framework discovers it by this name.
- **Call `ctx.require()` at the top** of commands that need external tools.
- **Use `ctx.run()` for mutations, `ctx.capture()` for reads.**
  This is how --dry-run works correctly.
- **Use `@pass_cli_context`** not `@click.pass_obj` (handles nested groups safely).
- **Add docstrings to commands.** Click uses them for --help text.

## Config

TOML files with layered resolution (highest priority wins):

1. Environment variables (explicit mapping or auto-prefixed `PROJECT_NAME_KEY`)
2. `[env.staging]` section in repo config
3. `[default]` section in repo config (`.project-name.toml`)
4. User config (`~/.config/project-name/config.toml`)

Schema example:

```python
CONFIG_SCHEMA = {
    "cloudflare.account_id": {
        "type": str,
        "required": True,
        "env": "CLOUDFLARE_ACCOUNT_ID",
    },
    "api_token": {
        "secret": True,
        "env": "MY_TOOL_API_TOKEN",
    },
}
```

Keys with `secret: True` are auto-wrapped in `Secret()` -- use `.get()` to unwrap.

## Entry Point

```python
#!/usr/bin/env python3
from pathlib import Path
from clickwork import create_cli

commands_dir = Path(__file__).resolve().parent / "commands"
cli = create_cli(name="my-tool", commands_dir=commands_dir)

if __name__ == "__main__":
    cli()
```

## Testing Commands

Prefer `clickwork.testing.run_cli` / `clickwork.testing.make_test_cli` for new
test code -- they pin `catch_exceptions=False` and default `name="test-cli"`
so real tracebacks surface in pytest output. Note that `result.output`
contains stdout AND stderr interleaved; use `result.stdout` / `result.stderr`
when asserting on a specific stream. See
[GUIDE.md "Testing commands with `clickwork.testing`"](GUIDE.md#testing-commands-with-clickworktesting).

```python
from clickwork.testing import make_test_cli, run_cli

def test_deploy_dry_run(tmp_path):
    (tmp_path / "deploy.py").write_text(...)
    cli = make_test_cli(commands_dir=tmp_path)
    result = run_cli(cli, ["--dry-run", "deploy"])
    assert result.exit_code == 0
```

## Common Patterns

### Migrating a Bash Script

1. Identify what the script does (prereqs, config values, subprocess calls)
2. Create `commands/script_name.py`
3. Move prereq checks to `ctx.require()` calls at the top
4. Move config/env vars to the TOML config + schema
5. Replace subprocess calls with `ctx.run()` / `ctx.capture()`
6. Replace any confirmation prompts with `ctx.confirm()` / `ctx.confirm_destructive()`
7. Export as `cli = command_function`

### Wrapping an Existing Tool

```python
@click.command()
@click.argument("args", nargs=-1)
@pass_cli_context
def tool(ctx: CliContext, args: tuple):
    """Run some-tool with project config."""
    ctx.require("some-tool")
    base_cmd = ["some-tool", "--config", ctx.config.get("tool.config_path")]
    ctx.run([*base_cmd, *args])
```

## Common Footguns

Quick hits for "why is my thing broken?" Each entry is Pitfall / Instead /
Why. Link out to helper docstrings or [GUIDE.md](GUIDE.md) for depth.

### 1. Patching `ctx.require`

**Pitfall:** patching a private `clickwork.cli` helper to fake out prereq checks.
**Instead:** `patch("clickwork.prereqs.require")`.
**Why:** `CliContext.require` routes through `clickwork.cli._require_via_prereqs`, which dispatches to `clickwork.prereqs.require` at call time. Patch the public prereqs function so your mock intercepts the actual lookup, not a stale internal alias.

### 2. Signalling user errors

**Pitfall:** `sys.exit(1)` with manual `click.echo`.
**Instead:** `raise click.ClickException("message")`.
**Why:** Wave 1 #5 made `ClickException` route correctly; ad-hoc exits bypass that.

### 3. CliRunner mixed output

**Pitfall:** asserting on `result.output` when you specifically want stdout-only or stderr-only content (`result.output` interleaves BOTH streams).
**Instead:** assert on `result.stdout` or `result.stderr` directly.
**Why:** clickwork declares `click>=8.2`, where `result.output` is stdout+stderr interleaved while `result.stdout` and `result.stderr` are populated independently. The older `CliRunner(mix_stderr=False)` kwarg referenced in some online snippets was removed in 8.2 -- don't copy those. See [GUIDE.md](GUIDE.md) "Testing commands with `clickwork.testing`".

### 4. URL-encoding query params

**Pitfall:** string-concatenating user values into URL query strings.
**Instead:** build params as a dict and use `urllib.parse.urlencode`.
**Why:** spaces, `&`, `#` in user values silently break URLs or enable injection.

### 5. Secrets in argv

**Pitfall:** `ctx.run(["wrangler", "secret", "put", name, token.get()])`.
**Instead:** `ctx.run_with_secrets(...)` (Wave 3 #11).
**Why:** argv is world-readable in `ps`; the helper enforces this and routes via env/stdin.

### 6. Shell-sourceable config files

**Pitfall:** hand-rolling a `.env` parser.
**Instead:** `clickwork.config.load_env_file(path)` (Wave 2 #9).
**Why:** parser gotchas are solved once; the helper also enforces owner-only permissions.

### 7. Platform dispatch

**Pitfall:** repeating `if sys.platform == "linux": ... elif sys.platform == "win32": ...`.
**Instead:** `@clickwork.platform_dispatch(linux=..., windows=..., macos=...)` (Wave 2 #12).
**Why:** the helper handles "unsupported platform" errors consistently.

### 8. HTTP calls

**Pitfall:** building a `urllib.request` helper in each command, or adding `requests`.
**Instead:** `clickwork.http.get/post/put/delete` (Wave 3 #13).
**Why:** stdlib-only helper with URL allowlist, JSON auto-parse, structured `HttpError`. See `clickwork.http` docstring.

### 9. Missing `import sys`

**Pitfall:** calling `sys.exit()` or `sys.stdin` without importing.
**Instead:** explicit `import sys`.
**Why:** easy to forget; `sys` is not a builtin.

### 10. `bash -c`

**Pitfall:** `ctx.run(["bash", "-c", "command $VAR"])`.
**Instead:** use Python stdlib directly, or `ctx.run(["command"], env={...})`.
**Why:** `bash -c` opens a shell-injection vector if any part of the command string is user-influenced, and creates a cross-platform dependency on `bash`.

### 11. `Secret.get()` at module scope

**Pitfall:** `TOKEN = Secret(...).get()` at module import.
**Instead:** call `.get()` at the call site when you actually need the value.
**Why:** module-scope unwrap defeats the "value stays wrapped until used" invariant.

## Lessons Learned

_This section is updated as we migrate commands and discover patterns._

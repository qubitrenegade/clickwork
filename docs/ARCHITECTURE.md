# Architecture

This document explains the design decisions behind clickwork: why the
framework exists, how the pieces fit together, and the reasoning behind
the non-obvious choices. Read this before contributing to the framework
or making architectural decisions in a project built on it.

For a practical guide to building CLIs with clickwork, see
[GUIDE.md](GUIDE.md).

## Problem

Most projects accumulate automation scripts over time. A deploy script
here, a packaging script there, a runner setup script in PowerShell for
the Windows CI. Eventually you have 20+ scripts across bash, Python, and
PowerShell with no shared infrastructure for repo root detection, error
handling, prerequisite checks, or configuration management.

clickwork extracts the common scaffolding into a reusable framework so
each new project starts with plugin discovery, layered config, subprocess
helpers, and consistent CLI flags -- and command authors only write the
business logic.

## Design Principles

**Data-driven, not hardcoded.** Configuration lives in TOML files and
environment variables, not in source code. The framework never hardcodes
project names, paths, or credentials.

**No global mutable state.** All runtime state flows through a `CliContext`
dataclass attached to Click's `ctx.obj`. Multiple CLI instances in the
same process do not share or leak state.

**Argv lists, never shell strings.** Every subprocess helper accepts a
`list[str]` and rejects raw strings. This eliminates shell injection by
construction -- there is no code path that invokes a shell.

**Fail fast, fail clearly.** Missing prerequisites are caught before any
work begins. Config errors surface at startup with actionable messages.
Secrets in repo config are refused, not warned about.

**Independently testable modules.** Each module (`process.py`, `config.py`,
`prereqs.py`, etc.) exposes standalone functions that accept explicit
arguments. The `CliContext` convenience methods (`ctx.run()`, `ctx.require()`)
are thin wrappers injected at runtime. Tests can call the standalone functions
directly without constructing a full CLI harness.

## Two-Package Strategy

The framework is split into two packages across two repositories:

| Package | Repository | Purpose |
|---------|-----------|---------|
| **clickwork** | `qubitrenegade/clickwork` | Generic CLI framework. Zero business logic. |
| **orbit-admin** | `qubitrenegade/qbrd-orbit-widener` | Orbit Widener-specific commands built on clickwork. |

This separation exists so other projects can depend on the framework
without pulling in Orbit Widener's commands or config. The tradeoff is
cross-repo coordination during development: framework API changes require
coordinated commits and a dependency pin update. This friction is managed
with editable local installs (`uv pip install -e ../clickwork`) during
development and SHA-pinned `requirements-ci.txt` for CI.

## Module Architecture

```
src/clickwork/
  __init__.py       Public API re-exports
  _types.py         CliContext, Secret, CliProcessError, PrerequisiteError
  cli.py            create_cli() factory, global flags, context wiring
  discovery.py      Plugin discovery (directory + entry points)
  config.py         Layered TOML config, schema validation
  process.py        run(), capture(), run_with_confirm()
  prereqs.py        require() binary/auth checks
  prompts.py        confirm(), confirm_destructive(), TTY detection
  _logging.py       Log setup, verbosity levels, color detection
  platform.py       Platform detection, repo root finding
```

**Dependency flow:** `_types.py` is the foundation -- stdlib only, no
external imports. Every other module may import from `_types.py`. The
`cli.py` module imports from all others (it wires them together). No
circular dependencies exist.

**Underscore-prefixed modules** (`_types.py`, `_logging.py`) are internal.
`_types.py` uses an underscore because it is the internal home of types
that are re-exported through `__init__.py`. `_logging.py` uses an
underscore to avoid shadowing Python's stdlib `logging` module.

## Discovery System

Commands are found through two mechanisms:

### Directory Scanning (dev mode)

Imports every `.py` file in a `commands/` directory (non-recursive) and
looks for a `cli` attribute -- a Click command or group. Files without
`cli` produce a warning. Files starting with `_` are skipped. Helper
modules should live in `lib/` or another directory outside `commands/`.

Each scanned directory gets a unique namespace in `sys.modules` (keyed
by a SHA-256 hash of the directory path) so identically-named files in
different directories do not collide. This also enables relative imports
between command files in the same directory (`from .helper import VALUE`).

### Entry Points (installed mode)

Reads the `clickwork.commands` entry point group from installed packages.
Plugin authors declare their commands in `pyproject.toml`:

```toml
[project.entry-points."clickwork.commands"]
hello = "my_plugin.commands.hello:cli"
```

Entry points are wrapped in `LazyEntryPointCommand` proxies so startup
does not import every installed plugin. The real command loads on first
invocation or when `--help` requests its metadata.

### Auto Mode

The default `discovery_mode="auto"` runs **both** mechanisms: entry points
are always queried, and directory scanning is added when `commands_dir`
exists on disk. If a local command and an installed command share a name,
the local one wins and an INFO-level message is logged.

This AND behavior (not XOR) is deliberate. During development, the package
is typically pip-installed (editable or otherwise), so entry points exist
AND a `commands/` directory is present. If auto mode only used directory
scanning when a directory existed, installed plugins from other packages
would silently vanish during local development.

## Config System

### Layered Resolution

Config is loaded from four sources with cascading precedence (highest wins):

1. **Environment variables** -- explicit mappings or auto-prefixed
2. **Env-specific section** -- `[env.staging]` in repo config
3. **Default section** -- `[default]` in repo config
4. **User-level config** -- `~/.config/{project}/config.toml`

Repo config overrides user config intentionally: this is a project
automation tool where checked-in config should define default behavior
so teammates and CI get consistent results. To override a repo value
locally, use an environment variable (highest priority).

### Environment Variables

Two mechanisms coexist:

- **Auto-prefix:** Every config key is checked against
  `{PROJECT_NAME}_{KEY}` (dots become underscores, uppercased). For
  example, `my-tool` + `cloudflare.account_id` checks
  `MY_TOOL_CLOUDFLARE_ACCOUNT_ID`.

- **Explicit mapping:** Schema entries can declare `"env": "CF_ACCOUNT_ID"`
  for third-party env var names that do not follow the auto-prefix
  convention. Explicit mappings win over auto-prefix when both are set.

### Environment Selection

The `--env` flag selects a config environment (`--env staging` loads
`[env.staging]`). When `--env` is omitted, the framework checks
`{PROJECT_NAME}_ENV` as a fallback (e.g., `MY_TOOL_ENV=staging`). This
lets CI pipelines select environments without modifying every command.

Env-specific sections *overlay* `[default]` -- keys present in the env
section override `[default]`, but absent keys fall through.

### Schema Validation

Optional. When a schema is provided to `create_cli()`, the framework
validates the merged config at startup:

- **Required keys:** Missing keys raise `ConfigError` with an actionable message.
- **Type checking:** Values that do not match the declared type are refused.
- **Defaults:** Schema-declared defaults fill missing keys after all layers merge.
- **Secret safety:** Keys tagged `secret: True` are refused if found in
  repo config (which is checked into git).

### Secret Wrapping

After validation, values whose schema entry has `secret: True` are
automatically wrapped in a `Secret()` instance. This means
`ctx.config["api_token"]` returns a `Secret`, not a plain string.

The `Secret` type redacts the value in every repr path:

- `str()`, `repr()`, `__format__()` all return `"***"`
- `__slots__` prevents `vars()` / `__dict__` from exposing the value
- `__reduce__` blocks pickling
- `__copy__` / `__deepcopy__` return new `Secret` instances

The only way to retrieve the value is the explicit `.get()` method,
which signals intent at call sites and is easy to grep-audit.

### User Config Permissions

User config (`~/.config/{project}/config.toml`) may contain secrets. On
Unix, the framework refuses to load it if group or other read bits are
set (i.e., anything more permissive than `0o600`). The check uses
`fstat()` on an already-open file descriptor -- not `os.stat()` on the
path -- to avoid a TOCTOU race between the permission check and the read.
The file contents are read from the same fd, so no substitution can happen
between the check and the parse.

On Windows, this check is skipped because the Unix permission model does
not apply.

## Subprocess Model

### run() -- Mutating Commands

`run()` executes a command via `Popen`, streams output in real-time
(stdout and stderr are inherited, not captured), and raises
`CliProcessError` on non-zero exit. In `--dry-run` mode, it logs the
command without executing it.

### capture() -- Read-Only Commands

`capture()` executes a command and returns stripped stdout. It **always**
runs, even in `--dry-run` mode, because commands typically need the
captured data to make decisions (e.g., listing resources before deploying).
Convention: `capture()` = read, `run()` = mutate.

### Signal Forwarding

When the user presses Ctrl-C, the framework forwards SIGINT to the child
process and waits up to 10 seconds for it to exit gracefully. If the child
ignores SIGINT, the framework escalates to SIGKILL. This prevents two
failure modes: orphaned child processes (if Python exits without
forwarding) and indefinite hangs (if the child ignores SIGINT).

### Missing Binaries

If `Popen` raises `FileNotFoundError` (the binary does not exist), the
framework catches it and raises `CliProcessError` with an actionable
message and exit code 1 (user error). Without this, a missing binary
would surface as an unhandled exception with exit code 2 (framework bug).

### Secret Safety

Secrets must be passed to subprocesses via environment variables
(`ctx.run(cmd, env={"API_TOKEN": token.get()})`), not as argv arguments.
Argv is visible in `ps` output; environment variables are only readable
by the process owner.

## Context Model

`CliContext` is a dataclass that holds all resolved runtime state:

- **Config:** Merged dict from all TOML layers and env vars.
- **Flags:** `dry_run`, `verbose`, `quiet`, `yes`, `env`.
- **Logger:** Configured with the correct verbosity level.
- **Convenience methods:** `run()`, `capture()`, `require()`, `confirm()`,
  `confirm_destructive()`, `run_with_confirm()`.

The convenience methods are declared as typed `Callable` fields (not
regular methods) so the dataclass can be constructed cheaply in tests
with all callables set to `None`. The CLI harness injects concrete
implementations as lambda closures that capture the context's flags.

`@pass_cli_context` is the recommended decorator for receiving the
context. It wraps `@click.pass_context` with `find_object(CliContext)`,
which traverses the full Click context chain (works in nested groups)
and raises a descriptive `UsageError` if no `CliContext` is found. This
is safer than `@click.pass_obj`, which silently passes `None` when the
group callback did not set `ctx.obj`.

## Error Model

Three exit codes:

| Code | Meaning | Examples |
|------|---------|---------|
| 0 | Success | Command completed normally |
| 1 | User/environment error | Missing prerequisite, bad config, subprocess failure |
| 2 | Framework internal error | Unhandled exception (bug in clickwork) |

The exception hierarchy:

- `CliProcessError` -- a subprocess returned non-zero. Exit 1.
- `PrerequisiteError` -- a required tool is missing or not authenticated. Exit 1.
- `ConfigError` -- config validation failed. Exit 1.
- `click.UsageError` -- bad CLI arguments. Exit 2 (Click default).
- Any other `Exception` -- framework bug. Exit 2.

The `create_cli()` factory installs a custom `invoke()` wrapper that
catches known exception types and maps them to the correct exit codes.
Click's built-in handling of `UsageError`, `Exit`, and `Abort` is
preserved.

## Security Model

### Shell Injection Prevention

Every subprocess helper rejects string commands (`TypeError` if cmd is
not a `list`). There is no `shell=True` anywhere in the codebase.
`_validate_cmd()` enforces this at the entry point of `run()`,
`capture()`, and `run_with_confirm()`.

### Secret Redaction

The `Secret` type prevents accidental leakage through every common Python
repr path (str, repr, format, f-strings, vars, pickle). Config values
tagged as secrets are automatically wrapped after config loading. The
redaction is not bypassable through normal attribute access -- `__slots__`
removes `__dict__` entirely.

### File Permission Enforcement

User config files are checked for owner-only permissions before loading.
The check is TOCTOU-safe (fstat on open fd, not stat on path). Files
that are group- or world-readable are refused with a clear error message.

### Subprocess Secret Passing

The framework provides `env=` parameters on all subprocess helpers for
passing secrets. Argv is explicitly documented as unsafe for secrets
(visible in `ps` output). The `Secret.get()` method name signals intent
at call sites.

## Testing Strategy

### Unit Tests

Each module is tested in isolation. Filesystem and subprocess calls are
mocked where needed for speed. Tests cover config merging, discovery
logic, prerequisite checking, platform detection, prompt behavior, and
logging setup.

### Integration Tests

Real temporary directories, real subprocess calls. CLI invocations use
Click's `CliRunner` for isolated testing without spawning processes.

### Sample Plugin

`tests/fixtures/sample-plugin/` is a minimal but complete plugin that
exercises framework features. It serves three purposes:

1. Test fixture for integration tests (installed into a temp venv)
2. Reference implementation for plugin authors
3. Tutorial walkthrough in the guide

### Test Markers

- `@pytest.mark.network` -- tests that need PyPI access (pip install).
  Skip with `pytest -m "not network"` in offline environments.
- `@pytest.mark.skipif(sys.platform == "win32")` -- tests that depend on
  Unix permission semantics.

## What Is Out of Scope

- **PyPI publishing:** Install via git URL for now. Publish once the API
  stabilizes post-1.0.
- **Shell completion:** Click supports it natively. One-liner to include;
  deferred until the CLI surface is stable.
- **Config file versioning:** Not needed at 0.x scale.
- **Package directories in commands/:** Only flat `.py` files are scanned.
  Define behavior if package-style commands are needed later.
- **Lazy --help via manifest cache:** Acceptable to import all commands
  for `--help` at 0.x.
- **pydantic/msgspec for config:** Evaluate if config grows beyond flat
  key-value pairs.

# Migrating from clickwork 0.2.x to 1.0

## Who this is for

You are already running clickwork 0.2.x in a project and want to upgrade to
1.0. This guide assumes you know the 0.2.x API; it calls out only what
changes. If you are new to clickwork, start with [GUIDE.md](GUIDE.md).

## tl;dr

```bash
uv pip install 'clickwork>=1.0,<2'
# or: pip install 'clickwork>=1.0,<2'
uv run pytest                 # rerun your test suite
```

Then walk the [Breaking changes](#breaking-changes) below. Most callers
need one or zero code changes. Callers who patched private symbols,
asserted on `ConfigError` for env-var type mismatches, or relied on
clickwork logs appearing twice (once via clickwork's own handler, once
via propagation) need targeted fixes.

The post-1.0 compatibility promise is defined in
[API_POLICY.md](API_POLICY.md); the full change ledger lives in
[CHANGELOG.md](../CHANGELOG.md).

## Breaking changes

### 1. Config env-vars coerce to the schema type

**What changed.** In 0.2.x, an env var (always a string at the OS level)
whose schema declared `type: int` / `float` / `bool` raised
`ConfigError` during `load_config()` itself -- schema validation runs
once as part of loading, not on each `ctx.config.get()` call
(`ctx.config` is a plain `dict`, so `.get()` never raises on type
mismatch). In 1.0, `load_config()` coerces the string to the declared
type before it writes to the merged dict, so downstream
`ctx.config.get()` calls just see the already-typed value.

Before:

```python
CONFIG_SCHEMA = {
    "port": {"type": int, "env": "MY_TOOL_PORT"},
}
# MY_TOOL_PORT=8080 in the environment
load_config(schema=CONFIG_SCHEMA)
# clickwork 0.2.x: ConfigError("port: expected int, got str")
# clickwork 1.0:   returns {"port": 8080}  (int, coerced)
```

The coercion table (stdlib-only, no new dependencies):

- `int`: `int(value)` base 10. Parse failure raises `ConfigError`.
- `float`: `float(value)`. Parse failure raises `ConfigError`.
- `bool`: case-insensitive allowlist. `true`, `1`, `yes`, `on` become
  `True`; `false`, `0`, `no`, `off` become `False`. Anything else (for
  example `maybe`) raises `ConfigError`. The allowlist is deliberate:
  Python's built-in `bool("false")` returns `True`, which is a
  well-known foot-cannon.
- `str`: unchanged.

There is also a related tightening worth knowing about even though it is
unlikely to affect real code: a TOML literal of the wrong type is still
rejected, and because `bool` is a subclass of `int` in Python, the
schema validator now explicitly rejects `port = true` for a `type: int`
key (and the reverse, an integer literal for a `type: bool` key). In
0.2.x this edge case silently passed the `isinstance(value, int)`
check.

**How to detect it.** Your test suite asserts `ConfigError` for an
env-string plus numeric schema combination and the assertion no longer
fires. Or a caller's code had a `try: ... except ConfigError:` fallback
that converted the value manually; the fallback branch now stops
running because the happy path succeeds with the coerced value.

**How to migrate.** Remove the `ConfigError` assertion. If you
worked around the coercion by using `type: str` plus a manual
`int(ctx.config["port"])`, the workaround keeps working unchanged;
switching to native `type: int` in the schema is optional cleanup.

### 2. `setup_logging()` no longer attaches its own stderr handler under host config

**What changed.** In 0.2.x, `setup_logging()` attached a
`StreamHandler` to clickwork's logger unconditionally. If the embedding
host application had already configured the root logger (via
`logging.basicConfig()`, `structlog`, or similar), every `clickwork`
log record was emitted twice: once through clickwork's own handler and
once after propagating to the host's root handler.

In 1.0, `setup_logging()` only attaches a `StreamHandler` when the root
logger has no handlers (the bare-script case). The clickwork logger
also gets a `NullHandler` baseline at import time and
`propagate=True`, so hosts that have configured root logging keep full
control of formatting and destination.

Before (host calls `logging.basicConfig()`, then clickwork is used):

```
2026-04-18 12:00 INFO     clickwork.cli Discovered command: deploy    # via host handler
clickwork.cli: Discovered command: deploy                             # via clickwork's own handler (duplicate)
```

After:

```
2026-04-18 12:00 INFO     clickwork.cli Discovered command: deploy    # single line, host's format
```

**How to detect it.** Log output from embedded consumers drops from two
lines per record to one. Tests that asserted on the count of records
captured via a host-installed handler now see half as many.

**How to migrate.** If you actively wanted the duplicate output (rare),
explicitly install your own `StreamHandler` on the `clickwork` logger:

```python
import logging
logging.getLogger("clickwork").addHandler(logging.StreamHandler())
```

More often, the fix is to update the test expectation: assert on one
record per emit, not two. The public signature of `setup_logging` is
unchanged, so construction-site code needs no edits.

### 3. `click>=8.2` is required

**What changed.** clickwork 0.2.0 already raised the `click` floor to
`>=8.2`, and 1.0 keeps it there with no upper bound. If you are coming
from a very early 0.2.x release where you pinned `click<8.2` in your
own project to avoid the `mix_stderr` removal, you need to drop that
pin now.

Before:

```python
# in a consumer project's test suite on an old Click
runner = CliRunner(mix_stderr=False)  # removed in Click 8.2
result = runner.invoke(cli, [...])
assert result.stderr == "expected error"
```

After:

```python
from clickwork.testing import run_cli  # or a plain CliRunner()
result = run_cli(cli, [...])
assert result.stderr == "expected error"  # 8.2 populates .stderr directly
```

**How to detect it.** `TypeError: __init__() got an unexpected keyword
argument 'mix_stderr'` at test collection time.

**How to migrate.** Remove the `mix_stderr=False` kwarg. On Click 8.2
and later, `result.stdout` and `result.stderr` are populated
independently by default, so the canonical assertion pattern works
with a plain `CliRunner()`. Prefer `clickwork.testing.run_cli`, which
also pins `catch_exceptions=False` so bugs surface as real tracebacks
instead of being swallowed. See also
[LLM_REFERENCE.md](LLM_REFERENCE.md) footgun #3.

### 4. `ctx.require` patch target moved to `clickwork.prereqs.require`

**What changed.** 0.2.0 removed the internal alias
`clickwork.cli._require`. Tests that patched it to stub prereq checks
need to patch `clickwork.prereqs.require` instead. This change landed
inside the 0.2.0 release, so if you migrated cleanly to 0.2.x you have
already handled it. It is documented here because the roadmap for 1.0
calls it out and because some 0.2.x consumers may have skipped the
patch-target fix by not running tests that exercised the mock.

Before:

```python
from unittest.mock import patch

with patch("clickwork.cli._require") as mock_require:
    mock_require.return_value = None
    result = run_cli(cli, ["deploy", "site"])
```

After:

```python
from unittest.mock import patch

with patch("clickwork.prereqs.require") as mock_require:
    mock_require.return_value = None
    result = run_cli(cli, ["deploy", "site"])
```

**How to detect it.** `AttributeError: module 'clickwork.cli' has no
attribute '_require'` at the start of a test that uses the patch, or a
mock that silently never fires because the patched symbol exists
somewhere in the import graph but no longer maps to the lookup
`CliContext.require` performs.

**How to migrate.** Replace `clickwork.cli._require` with
`clickwork.prereqs.require` in every `patch()` / `patch.object()`
call. The public surface is `clickwork.prereqs.require`; the lookup
now goes through a module-level wrapper that re-resolves the name on
every call, so intuitive patching works. Listed in
[LLM_REFERENCE.md](LLM_REFERENCE.md) as footgun #1.

### 5. `click` upper bound policy (no pin, by design)

**What changed.** Nothing has been added. `pyproject.toml` declares
`click>=8.2` with no upper bound, and that is deliberate. The 1.0
release commits to this position in writing; it is called out here so
consumers who were considering pinning `clickwork` alongside
`click<9` in their own `requirements.txt` understand the rationale
before they do.

An upper bound on `click` creates a dependency-resolution ratchet. The
day Click ships a new major, every resolver trying to install
clickwork alongside another package that already moved to the new
major gets an unsolvable constraint and clickwork becomes
uninstallable in that environment until we cut a fix release. That
outcome is strictly worse than the alternative (silent breakage on a
new Click major), because silent breakage surfaces as a real test
failure or bug report, while a ratchet surfaces as "I can't install
your library at all."

**How to detect it.** No detection needed; this is a policy statement.

**How to migrate.** If your downstream project pinned
`click<SOMETHING` for the sake of clickwork, you can drop that pin.
CI (see `.github/workflows/`) runs a "latest Click" matrix job on
every clickwork PR, so Click-major breakage lands on clickwork's own
CI the moment it hits PyPI rather than waiting for a user to file a
bug. See [API_POLICY.md](API_POLICY.md#click-version-range) for the
full rationale.

## New opt-in surfaces worth adopting

None of these break existing code. They are new capabilities the 0.2.x
reader may not know exist yet.

### `--version` / `-V` flag

`create_cli()` gained two keyword-only kwargs: `version=` and
`package_name=`. When either is set, the resulting CLI gets a
`--version` / `-V` flag via `click.version_option`.

```python
# Resolve automatically from your package's installed metadata:
cli = create_cli(name="my-tool", commands_dir=..., package_name="my-tool")

# Or pass the version literal yourself:
cli = create_cli(name="my-tool", commands_dir=..., version="2.3.1")
```

Precedence: `version=` wins if both are passed. If `package_name=`
cannot be resolved via `importlib.metadata`, `create_cli()` raises
`ValueError` at construction time so typos fail loudly instead of
silently disappearing until `--version` runs in production. When
neither kwarg is set, no `--version` flag is installed, so existing
CLIs see no change.

### `strict=True` command discovery

`create_cli()` gained a keyword-only `strict=` flag. With
`strict=True`, discovery failures (broken import, missing `cli`
attribute, invalid `cli` value, duplicate command name, entry-point
load error) aggregate into a single `ClickworkDiscoveryError` raised
after the full scan. The default stays `False` so 0.2.x consumers
upgrade with no observable difference.

```python
from clickwork import create_cli, ClickworkDiscoveryError

try:
    cli = create_cli(name="my-tool", commands_dir=..., strict=True)
except ClickworkDiscoveryError as e:
    for failure in e.failures:
        print(f"{failure.category}: {failure.message} ({failure.cause_path})")
    raise
```

Want this in production CIs where a silently-dropped command is worse
than a loud crash? Turn it on. Leave it off for dev-mode REPLs where
you want to keep iterating even with one broken command file.

### `py.typed` marker

clickwork is now a [PEP 561](https://peps.python.org/pep-0561/) typed
package. Downstream mypy / pyright / pyre users automatically pick up
clickwork's inline annotations. No action required; if you previously
had a `mypy.ini` entry to ignore-missing-imports for `clickwork`, you
can remove it.

### `clickwork._deprecated` (internal, but flagged)

clickwork 1.0 introduces an internal `@deprecated(since, removed_in,
reason)` decorator at `clickwork._deprecated.deprecated`. It is
underscore-prefixed and NOT re-exported; plugin authors should not
import from it. The reason it matters to you: starting in 1.0, any
symbol we deprecate will emit a `DeprecationWarning` the first time
you call it, with a `clickwork:` prefix you can filter on. The 0.x
pattern of "a future release silently changes behavior" is no longer
how we evolve the public API. See
[API_POLICY.md](API_POLICY.md#deprecation-policy) for the runway
guarantee (one full minor release of overlap before removal).

To filter clickwork deprecation warnings in your own test runs:

```toml
# pyproject.toml
[tool.pytest.ini_options]
filterwarnings = ["ignore:clickwork\\::DeprecationWarning"]
```

## Cross-references

- [CHANGELOG.md](../CHANGELOG.md) -- full per-release change ledger.
- [API_POLICY.md](API_POLICY.md) -- the post-1.0 compatibility promise,
  Click and Python version-range policy, deprecation runway length.
- [LLM_REFERENCE.md](LLM_REFERENCE.md) "Common Footguns" section --
  every-day mistakes (patching prereqs, `ClickException` routing,
  `CliRunner` streams, URL-encoding, secrets-in-argv, and more).
- [GUIDE.md](GUIDE.md) -- full tutorial for a clean-slate setup, kept up
  to date with the 1.0 API.

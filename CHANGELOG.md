# Changelog

All notable changes to clickwork will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-18

First stable release. The public surface is now covered by
[`docs/API_POLICY.md`](docs/API_POLICY.md): everything in the
documented API set is committed to semver stability, and removals
follow a one-minor-release deprecation runway with
`DeprecationWarning`.

Numbers in parens are **issue numbers** unless explicitly noted as
`(PR #NN)`. See [`docs/MIGRATING.md`](docs/MIGRATING.md) for the
complete 0.2.x → 1.0 upgrade guide including before/after diffs.

### Breaking

- **Env-sourced config values are coerced per the schema's `type:`.**
  `port = "8080"` with `type: int` now returns `8080` as an `int` on
  both environment variables and TOML. Projects that caught a
  `ConfigError` for this case or did manual `int(...)` on the result
  can drop those workarounds. (#41)
- **`setup_logging()` no longer attaches its own `StreamHandler` when
  the host has already configured logging.** Host-side
  `logging.basicConfig`, `structlog`, etc. are now preserved -- no
  more duplicate log lines. Hosts that relied on the handler being
  installed unconditionally should call `setup_logging(force=True)`
  or attach their own handler. (#43)
- **`load_config(env=...)` fails fast on an undefined env name.**
  When the repo config file exists but has no matching
  `[env.<name>]` section, `ConfigError` is raised naming the missing
  env and listing the defined ones. Previously this silently
  returned the `[default]` merge. Missing repo config file is still
  a no-op. (#52)
- **Python floor bumped to 3.11+.** This is a hard floor:
  `tomllib` is now imported unconditionally from the standard
  library. Consumers on 3.9/3.10 must upgrade Python. (#46)
- **Click floor pinned to `>=8.2,<9`.** 0.2.0 already required
  `click>=8.2`; 1.0 makes this explicit in metadata and caps at `<9`
  so a future Click major doesn't break consumers without a
  clickwork release. (#46, PR #33)

### Added

- **`docs/API_POLICY.md`** -- the public API surface, compatibility
  policy, and deprecation runway. Everything re-exported from
  `clickwork/__init__.py` and everything in the
  `clickwork.{config,discovery,http,platform,process,testing}`
  modules listed there is stable. Leading-underscore modules
  (`_deprecated`, `_logging`, `_types`) are private and may change
  without a major bump. (#36, #46, #49)
- **`docs/GUIDE.md` "Config Precedence" section** -- explicit,
  ordered 6-row precedence table covering every config source from
  explicit env-var mapping (highest) through schema defaults
  (lowest), plus worked examples and a rationale. The table is part
  of the public 1.0 contract. (#54)
- **`docs/MIGRATING.md`** -- the 0.2.x → 1.0 upgrade guide with
  before/after diffs for every breaking change and a quick checklist
  for consumers. (#56)
- **`docs/PLUGINS.md`** -- a 15-minute walkthrough for shipping a
  clickwork plugin on PyPI via the `clickwork.commands` entry point
  group. (#53)
- **`docs/SECURITY.md`** -- what clickwork defends against, what it
  leaves to the CLI author, threat-model assumptions, and the
  vulnerability-reporting process. Wired into GitHub's Security
  tab via a root-level `SECURITY.md` stub. (#55)
- **`CONTRIBUTING.md`** -- contributor setup, dev workflow, and
  review expectations for external contributors. (#58)
- **`strict=True` opt-in for `create_cli()` and
  `discover_commands(...)`.** Promotes every silent-drop branch in
  discovery (missing `cli`, import error, invalid `cli` type,
  duplicate command names, entry-point metadata failures) to a
  single `ClickworkDiscoveryError` aggregating all issues. Default
  `False` preserves the 0.2.x warn-and-drop behavior. Production
  CLIs should turn it on. (#42)
- **`package_name=` / `version=` kwargs on `create_cli()`** install a
  standardized `--version` flag that resolves the string via
  `importlib.metadata` (or the explicit override) so consumers get a
  consistent `--version` without writing their own handler. (#48)
- **`clickwork._deprecated` decorator** for post-1.0 evolution. Wrap
  a public symbol that should stay available for one more minor; it
  emits `DeprecationWarning` on first use with a configurable
  message and a pointer to the replacement. Thread-safe
  once-per-symbol firing. (#47) (Note: `clickwork._deprecated` is a
  private module -- its *decorator output* is public and stable, the
  module path itself is not.)
- **PEP 561 `py.typed` marker.** Consumers' `mypy --strict` now sees
  clickwork's inline annotations directly; no more third-party stub
  packages or hacks required. (#37)

### Changed

- **`add_global_option()` override semantics are now part of the
  public API.** Invoking a command with an explicit flag value
  overrides the group-level option at invocation time; docs and
  tests pin the behavior so downstream CLIs can rely on it. (#60)
- **Logger propagation**. Clickwork loggers now rely on standard
  Python propagation to the host's root logger rather than attaching
  a duplicate stderr handler. Hosts configuring logging before
  calling clickwork see their settings honored. (#43)

### CI / Release engineering

- **Strict mypy gate** on `src/clickwork` with fully annotated
  source. (#38)
- **Ruff lint + format gate** (pinned to 0.6.9) applied to the full
  tree. (#38)
- **Cross-platform test matrix**: linux/macos/windows on Python
  3.11/3.12/3.13 plus a click-latest canary. (#39)
- **Wheel + sdist smoke test** installs the built artifacts into a
  fresh venv and runs the suite against the installed package (#40);
  release-smoke workflow added. (#40)
- **Cold-start import benchmark** with a 20% regression gate, so
  accidentally pulling `requests` or similar into the import path
  fails CI. (#59)
- **Auto-generated release notes** via GitHub's release-notes
  config; reviewers curate from there rather than hand-writing from
  the commit list. (#57)

### Docs

- **LLM_REFERENCE.md "Common Footguns"** section collecting every
  gotcha that external reviewers and auto-review tools have caught
  on clickwork PRs (rewritten throughout the 1.0 cycle).
- **GUIDE.md "Testing commands with clickwork.testing"** section
  consolidated the testing story.
- **README.md** rebuilt around the documented public surface with
  direct links to `GUIDE`, `API_POLICY`, `PLUGINS`, `SECURITY`, and
  `MIGRATING`.

## [0.2.0] - 2026-04-18

A broad expansion of the framework closing 12 issues (#4-#17), plus one
PR-only follow-up (PR #33) that raised the `click` dependency floor during
release prep. Numbers in parens on each entry below are **issue
numbers**, except where explicitly noted as `(PR #NN)`. Major new
modules: `clickwork.http`, `clickwork.platform`, `clickwork.testing`.
New helpers on `CliContext`: `run_with_secrets`, stdin forwarding. New
public API for docs-level CLIs: `add_global_option`. New dotenv helper:
`clickwork.config.load_env_file`. Plus docs: LLM_REFERENCE "Common
Footguns" section and GUIDE "Testing commands" subsection.

### Added

- `clickwork.http` — stdlib-only HTTP client with URL allowlist,
  no-redirect security, JSON auto-parse, and structured `HttpError`.
  `get` / `post` / `put` / `delete` helpers, host allowlist enforced
  before any network activity, userinfo/query/fragment stripped from
  all log lines and exception messages. (#13)
- `clickwork.platform` — `@platform_dispatch(linux=, windows=, macos=)`
  decorator and `dispatch(ctx, linux=, ...)` helper so commands that
  behave differently per OS stop repeating `if sys.platform == ...`
  chains. (#12)
- `clickwork.testing` — `run_cli()` + `make_test_cli()` helpers for
  plugin test suites. `run_cli` pins `catch_exceptions=False` so bugs
  surface as real pytest tracebacks instead of being swallowed;
  `make_test_cli` wraps `create_cli` with a sensible default name.
  (#16)
- `clickwork.add_global_option()` — installs a Click option at the
  root CLI and every nested group/subcommand in one call, with
  OR-semantics for boolean flags and innermost-wins for value
  options. (#14)
- `clickwork.config.load_env_file(path)` — owner-only-permissions
  dotenv parser, no variable substitution, no shell interpretation.
  (#9)
- `CliContext.run_with_secrets(cmd, secrets=, stdin_secret=, ...)` —
  safe subprocess secret delivery via env or stdin, never argv. Type
  checks + validation ensure `Secret` objects can't leak into argv or
  logs. (#11)
- `ctx.run()` now accepts `stdin_text=` / `stdin_bytes=` kwargs while
  preserving SIGINT forwarding. (#10)
- `create_cli()` now accepts `enable_parent_package_imports=True` to
  opt into importing commands as relative to a parent package. (#15)
- LLM_REFERENCE.md — new "Common Footguns" section (11 entries:
  patching prereqs, `ClickException` routing, CliRunner streams,
  URL-encoding, secrets-in-argv, `.env` parsing, platform dispatch,
  HTTP calls, `import sys`, `bash -c` risk, module-scope
  `Secret.get()`). (#17)
- GUIDE.md — new "Testing commands with `clickwork.testing`"
  subsection covering the new helpers and `result.output` /
  `.stdout` / `.stderr` semantics on Click 8.2+.
- CHANGELOG.md (this file).

### Changed

- **Breaking: raised `click` dependency floor from `>=8.1` to
  `>=8.2`.** On Click 8.1 a default `CliRunner()` mixed streams,
  causing `result.stderr` to raise `ValueError: stderr not separately
  captured`. 0.2.0's testing guidance ("assert on `result.stdout` /
  `result.stderr` directly") crashes on 8.1 with the default runner.
  Flooring at 8.2 -- where `mix_stderr` was removed and the three
  stream attributes are populated independently -- makes the
  canonical guidance always applicable. (PR #33)
- `ctx.require` is now patchable via
  `patch("clickwork.prereqs.require")` — the internal
  `clickwork.cli._require` alias was removed, so tests that targeted
  it must update the patch path. (#8)
- `click.exceptions.ClickException` subclasses now re-raise correctly
  instead of being swallowed, so user errors exit with the intended
  non-zero code. (#5)

### Fixed

- `--help` text no longer leaks the full docstring when a `description`
  is passed to `create_cli`. (#4)

### Security

- `clickwork.http` refuses to follow redirects (a custom
  `HTTPRedirectHandler` raises on 3xx so callers control any redirect
  themselves). Prevents cross-host credential forwarding via
  `Authorization`, and prevents allowlist bypass via `Location`
  header. (#13)
- `clickwork.http` sanitizes URLs in every log line and every
  `HttpError.url` / message by stripping userinfo, query, and
  fragment. URLs with malformed ports or missing hostnames are
  rejected with clean `ValueError` messages that never echo the raw
  URL back into an exception. (#13; security hardening landed in PR
  #29 during the Copilot review loop.)
- `load_env_file` rejects any file not owner-only via a TOCTOU-safe
  `os.fstat` check on the already-opened file descriptor. (#9)
- `run_with_secrets` refuses to place `Secret` objects in argv, and
  redacts all env values (secret-sourced as `<redacted>`, other env
  entries as `<set>`) from the single INFO-level log line. (#11)

[1.0.0]: https://github.com/qubitrenegade/clickwork/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/qubitrenegade/clickwork/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/qubitrenegade/clickwork/releases/tag/v0.1.0

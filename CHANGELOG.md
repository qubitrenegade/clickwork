# Changelog

All notable changes to clickwork will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-04-18

A broad expansion of the framework closing 12 issues (#4-#17), plus one
PR-only follow-up (#33) that raised the `click` dependency floor during
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

[0.2.0]: https://github.com/qubitrenegade/clickwork/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/qubitrenegade/clickwork/releases/tag/v0.1.0

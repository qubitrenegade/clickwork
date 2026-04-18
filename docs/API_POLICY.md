# API and compatibility policy

## Scope

This document defines clickwork's public API surface and the
compatibility guarantees that apply to it once version 1.0.0 ships. It
covers which symbols callers can depend on, what constitutes a breaking
change, how long deprecated symbols stick around, and which dependency
and interpreter versions clickwork commits to supporting. The policy
below starts applying on 1.0.0; pre-1.0 releases (0.x) carry no
compatibility promise by semver convention, and the 1.0 release is
explicitly allowed to break 0.2.x where the break corrects a genuine
design mistake.

Related docs that will cross-reference this policy once they land: see
`MIGRATING.md` (tracked by Wave 4 issue #56) for the 0.x to 1.0 upgrade
path, and `SECURITY.md` (tracked by Wave 4 issue #55) for the security
properties clickwork asserts about its public surface.

## Public API surface

The public surface is defined by two things: the names re-exported from
`clickwork/__init__.py`'s `__all__`, and the documented submodules that
callers are expected to import directly. Anything else is private, even
if Python's import system happens to make it reachable.

### Top-level names (re-exported via `clickwork.__all__`)

| Name | Kind | One-line description |
|------|------|----------------------|
| `create_cli` | function | Build a CLI with global flags and plugin discovery. |
| `add_global_option` | function | Install a Click option at root plus every nested group and subcommand. |
| `load_config` | function | Load layered TOML config for custom config scenarios. |
| `CliContext` | dataclass | Typed context object passed to every command. |
| `pass_cli_context` | decorator | Decorator for commands; handles the nested-group footgun. |
| `Secret` | class | Redacted wrapper for sensitive config values. |
| `CliProcessError` | exception | Raised when a subprocess exits non-zero. |
| `ConfigError` | exception | Raised when config loading or schema validation fails. |
| `PrerequisiteError` | exception | Raised when a required tool is missing or unauthenticated. |
| `HttpError` | exception | Raised when an HTTP call returns non-2xx or fails validation. |
| `normalize_prefix` | function | Normalize a CLI name to an env-var prefix (e.g. `my-tool` to `MY_TOOL`). |
| `platform_dispatch` | decorator | Route a command to a per-OS implementation. |
| `get`, `post`, `put`, `delete` | functions | HTTP verb helpers re-exported from `clickwork.http`. |
| `platform`, `http`, `testing` | submodules | Documented submodules exposed on the package. |

### Documented submodules

`clickwork.http`, `clickwork.platform`, and `clickwork.testing` are
re-exported from the top-level package, so callers can reach them as
attributes (`clickwork.http.get`, etc.) after `import clickwork`.
`clickwork.config` is NOT re-exported at the top level for historical
reasons; reach it via an explicit `import clickwork.config` or
`from clickwork import config` / `from clickwork.config import load_config`.
Names exported by each submodule are part of the public surface when
they appear in the submodule's own docstring and public symbols; names
prefixed with an underscore are private.

| Submodule | Public surface | Import style |
|-----------|----------------|---------------|
| `clickwork.http` | `get`, `post`, `put`, `delete`, `HttpError`. URL allowlist, no-redirect security, JSON auto-parse. | attribute on `clickwork` |
| `clickwork.platform` | `platform_dispatch`, `dispatch`, `is_linux`, `is_macos`, `is_windows`, `find_repo_root`. | attribute on `clickwork` |
| `clickwork.testing` | `run_cli`, `make_test_cli`. Helpers for writing plugin test suites against a real CLI. | attribute on `clickwork` |
| `clickwork.config` | `load_config`, `load_env_file`, `ConfigError`. Layered TOML and dotenv helpers. | explicit import required |

### Protocol-level surfaces

Some contracts are not Python symbols but still count as public because
external code depends on their shape. These get the same semver promise
as the symbol surface.

- The `clickwork.commands` entry-point group used by installed plugins.
  The group name and the expected shape of a registered entry point (a
  Click `Command` or `Group` exposed as `cli`) are stable.
- The layered config precedence order (environment variables, then
  `[env.<name>]`, then `[default]`, then user-level config). Changing
  the precedence is a breaking change. See `GUIDE.md` (to be updated by
  Wave 4 issue #54) for the precedence table.
- Global flag names installed by `create_cli()` (`--verbose`, `--quiet`,
  `--dry-run`, `--env`, `--yes`). Removing or renaming one is breaking.

## Private and unstable

The compatibility promise does **not** cover:

- **Leading-underscore names at any level.** `_types`, `_logging`, any
  module-private helper. If you imported it, you are on your own. This
  follows standard Python convention and exists so the framework can
  refactor its internals without needing a major-version bump every
  time.
- **The reserved `clickwork._internal` namespace.** This submodule does
  not exist yet. The name is reserved for future internal-only helpers
  that happen to live in a dedicated namespace (rather than as
  underscore-prefixed symbols). Anything that ever lands under
  `clickwork._internal` is explicitly private regardless of whether the
  submodule or its contents carry an underscore prefix.
- **Symbols not re-exported by `__all__`.** Even if a name is importable
  because it happens to live in a public submodule, if it is not listed
  in that submodule's documented public surface or the package-level
  `__all__`, assume it is an implementation detail. Example: the
  internal loader path behind the entry-point protocol is not API.
- **Error message wording.** Exception types are stable; the exact
  human-readable text is not. Do not assert against error strings in
  downstream tests.

## Compatibility promise (post-1.0)

clickwork follows [semantic versioning](https://semver.org). Once
1.0.0 ships, breaking changes to the public surface require a major
version bump. This section defines precisely what "breaking" means so
neither we nor callers are guessing.

### What counts as breaking (major bump required)

- **Removal.** A public symbol goes away.
- **Rename.** A public symbol's import path changes without a shim.
- **Signature change.** Adding a required positional argument, removing
  an argument, changing an argument's type in a way the existing caller
  can't satisfy, or reordering positional arguments.
- **Semantic change.** The same call with the same arguments starts
  doing something observably different (different return value shape,
  different side effects, different exceptions raised).
- **Protocol change.** Renaming the entry-point group, changing the
  precedence order of layered config, or renaming a global flag.

### What does **not** count as breaking (minor or patch is fine)

- Docstring edits, type-stub tightening that is still structurally
  compatible, internal refactors that leave the public surface alone.
- New public symbols, new optional keyword arguments with safe
  defaults, new submodules.
- Bug fixes that align behavior with documented intent. If the docs
  said "raises `ConfigError` on unknown key" and the code didn't, the
  fix is not a breaking change even though some caller's test changed.
  The reverse also holds: if the docs are wrong and the code is right,
  fixing the docs is not a breaking change.
- New global flags on `create_cli()` as long as they do not collide
  with names a subcommand might reasonably use. (`add_global_option`'s
  collision semantics cover this case for consumer-defined flags.)

## Deprecation policy

Public symbols do not disappear without warning. A deprecated symbol
stays available for at least **one full minor release cycle** before
removal. Concretely: a symbol deprecated in 1.1 is removed no earlier
than 1.2, giving callers at least one version of overlap where the
symbol still works and also emits a `DeprecationWarning`.

Deprecations use the `deprecated(since, removed_in, reason)` decorator
that lives at `clickwork._deprecated.deprecated`. That module is a
Wave 2 deliverable tracked by issue #47; it does not exist in the
codebase yet. Plugin authors should not import from
`clickwork._deprecated` directly (the underscore makes it private).
The decorator is an internal tool clickwork uses on its own public
surface; it exists so every deprecation emits a consistent warning
with a pointer to the replacement.

The 1.0 release itself may deprecate symbols that existed in 0.2.x,
but it won't remove them in the same release. Anything removed in 1.0
had to have been deprecated in a 0.x release or was never public to
begin with. Breaking changes in the 0.x to 1.0 transition are
catalogued in `MIGRATING.md` (Wave 4, issue #56).

## Click version range

clickwork declares `click>=8.2` with **no upper bound**. The floor is
8.2 because the testing guidance in `GUIDE.md` (assert on
`result.stdout` and `result.stderr` directly) requires Click 8.2's
independent stream population; on 8.1 a default `CliRunner()` mixes
streams and `result.stderr` raises `ValueError`. See Click's
[changelog](https://click.palletsprojects.com/en/stable/changes/) for
the 8.2 release notes.

There is no upper bound, and this is a deliberate choice. Pinning
`click<9` (or any future major) creates a **dependency-resolution
ratchet**: the day Click ships a new major, every resolver trying to
install clickwork alongside another package that has already moved to
the new major gets an unsolvable constraint, and clickwork becomes
uninstallable in that environment until we ship a fix release. That
is strictly worse than the alternative, which is silent breakage we
didn't predict. Silent breakage surfaces as a real test failure or a
real bug report; a ratchet surfaces as "I can't install your library
at all," which is harder to diagnose and blocks downstream work.

What we do instead:

- CI (see issue #39) runs a "latest Click" matrix job separate from
  the pinned-Click lockfile job, so a breaking Click release surfaces
  on clickwork's own CI the moment it lands on PyPI.
- When a Click major does break us, we ship a fix release (a patch or
  minor, depending on whether the break required API changes on our
  side), not a retroactive upper bound. The upper bound stays off.

Reference: the [Click documentation](https://click.palletsprojects.com/en/stable/)
is the authoritative source for what Click itself guarantees across
majors.

## Python version support

**Floor:** `requires-python = ">=3.11"`.

The floor is 3.11 because that is the oldest still-supported CPython
release that provides all of the following, which clickwork's code
and type annotations rely on:

- PEP 654 exception groups (`ExceptionGroup`, `except*`). We use these
  for aggregating discovery errors cleanly.
- `tomllib` in the standard library (PEP 680). We do not want to take
  a dependency on `tomli` just to parse config.
- PEP 673 `Self` type for fluent APIs and dataclass methods that
  return an instance of their own class.
- The post-3.10 typing improvements that let us write precise
  annotations without reaching for `typing_extensions`.

3.10 lacks all four. Supporting it would mean either carrying a
backport dependency or writing more awkward code to route around the
gaps, and the marginal gain in user coverage does not justify that
cost given 3.10 is already on its EOL glide path.

**Ceiling:** none. clickwork follows the same "no upper bound, CI
covers the matrix" discipline as with Click. Each new CPython release
is added to CI (see issue #39) so regressions surface immediately.

**Deprecation runway for dropping a Python minor:** we will not drop
support for a Python minor earlier than **18 months after CPython's
own EOL for that minor**. Concretely, CPython 3.11's EOL is scheduled
for 2027-10, so the earliest clickwork release that drops 3.11 is
2029-04. Callers also get at least **two clickwork minor releases**
of warning (via deprecation notices in the changelog and a
`DeprecationWarning` emitted from `create_cli()` / the first public
API call, **not** at package import time) before the drop lands.

Warning-emission discipline: we avoid emitting `DeprecationWarning`
at import time because many downstream test suites run with
`filterwarnings = ["error"]`, and an import-time warning would break
those suites even for callers who aren't touching the deprecated
surface. Warnings fire from the specific entry points that trigger
the deprecated behavior (e.g. inside `create_cli()` once per CLI,
or from the deprecated function itself). Callers who want to silence
deprecations in their own test runs can add a targeted
`filterwarnings = ["ignore::DeprecationWarning:clickwork"]` entry,
or narrow further by message text.

The 18-month window is deliberately generous. Enterprise and
Linux-distribution Python environments lag upstream by years; a
shorter runway punishes the callers most likely to have other
constraints blocking a Python upgrade. Eighteen months past EOL is
long enough that staying on the dropped minor is a conscious choice,
not an oversight.

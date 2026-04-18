# Wave 2 plan — medium features

**Date:** 2026-04-17
**Roadmap:** [docs/superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md](../superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md)
**Scope:** Issues #9, #12, #14 — three independent medium features run in parallel worktrees.
**Depends on:** Wave 1 PRs merged. Wave 2 worktrees rebase onto latest main before agent dispatch.

## API shape decisions

| Issue | Decision |
|-------|----------|
| #9 | Standalone helper: `clickwork.config.load_env_file(path: Path) -> dict[str, str]`. Callers decide how to use the returned dict (inject into `os.environ`, pass as `env=` to `ctx.run`, etc.). **Not** integrated into `load_config()` — keeps the TOML pipeline focused on structured data. Shell-semantics scope is capped at: `KEY=value`, `export KEY=value`, `KEY="double-quoted"`, `KEY='single-quoted'`, `# comments`, blank lines. **No** variable substitution (`$OTHER`), no backticks, no heredocs, no multiline values. Same owner-only TOCTOU-safe permission check clickwork already applies to user config. |
| #12 | Primary surface: decorator `@clickwork.platform_dispatch(linux=fn, windows=fn, macos=fn, linux_error="...", windows_error="...", macos_error="...")`. Also export the functional form `clickwork.platform.dispatch(ctx, *, linux=fn, windows=fn, macos=fn, linux_error="...", windows_error="...", macos_error="...")` as a public helper for the 20% of cases that need pre-dispatch logic in the command body. Any platform whose impl kwarg is `None` or omitted: raise `click.UsageError(f"{platform} not supported")` unless that platform's matching `*_error` string kwarg provides a custom message. All three `*_error` kwargs are part of the public API (consistent surface; no "macOS is special" carve-out). |
| #14 | Functional: `clickwork.add_global_option(cli, *param_decls, **option_kwargs)`. Installs the option at the root group + every group + every subcommand currently attached to `cli`. Value stashed on Click's `ctx.meta[<option_name>]`. **Resolution semantics:** flags (`is_flag=True`) OR across levels (any truthy wins); value options (string/int/etc.) innermost-wins. Both live under the same `ctx.meta[name]` key. |

## Branch + worktree layout

| Issue | Branch | Worktree path |
|-------|--------|---------------|
| #9 | `feat/dotenv-config-9` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-dotenv-9` |
| #12 | `feat/platform-dispatch-12` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-platform-dispatch-12` |
| #14 | `feat/global-flags-14` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-global-flags-14` |

*(Worktrees already prepped from main during Wave 1 Copilot waits; will rebase onto post-Wave-1 main before dispatching agents.)*

## Per-issue tasks

### #9 — `clickwork.config.load_env_file(path)` dotenv helper

**Files:** `src/clickwork/config.py` (add `load_env_file` alongside existing config helpers; expose via `clickwork.__init__` if there's a public re-export pattern), `tests/unit/test_config.py`.

**TDD:**
1. Red: add tests in `tests/unit/test_config.py`:
   - `test_load_env_file_parses_simple_key_value` — `K=v` → `{"K": "v"}`
   - `test_load_env_file_strips_export_prefix` — `export K=v` → `{"K": "v"}`
   - `test_load_env_file_handles_double_quotes` — `K="v with spaces"` → `{"K": "v with spaces"}`
   - `test_load_env_file_handles_single_quotes` — `K='v'` → `{"K": "v"}`
   - `test_load_env_file_skips_comments` — `# comment\nK=v` → `{"K": "v"}`
   - `test_load_env_file_skips_blank_lines`
   - `test_load_env_file_handles_multiple_keys`
   - `test_load_env_file_raises_on_missing_file` — clear `FileNotFoundError` or custom `ConfigError` (match existing pattern in `config.py`)
   - `test_load_env_file_rejects_world_readable_file` — file with mode `0o644` → owner-only check fails with actionable error matching existing user-config permission pattern
   - `test_load_env_file_ignores_malformed_lines` — OR `test_load_env_file_raises_on_malformed_line` — pick whichever is cleaner. **Recommend raising** with the line number so callers know which line is bad.
   - `test_load_env_file_does_not_expand_variables` — `K=$OTHER` → literal `"$OTHER"`, not empty or substituted (explicit anti-test so no one "helpfully" adds substitution later)
2. Green: implement `load_env_file(path: Path) -> dict[str, str]`:
   - Apply the same owner-only permission check used elsewhere in `config.py` (look for the `fstat`/TOCTOU-safe helper that already exists — reuse, don't re-invent).
   - Parse line-by-line: strip, skip empty/comment, strip optional `export ` prefix, split on first `=`, unquote double or single quotes if the entire value is wrapped, store as-is otherwise.
   - Raise `ConfigError(f"line {n}: ...")` (or the existing error type) for malformed lines so debugging is easy.
3. Refactor: docstring with the supported-syntax list + explicit out-of-scope items ("no variable substitution by design; if you need that, shell out to `sh -c 'source file; env'`"). Add a teaching comment on `_strip_quotes` (or wherever unquoting lives) explaining the single-vs-double semantics we chose.

**Constraints:**
- **Must close issue #9.** `Fixes #9`.
- Keep the parser simple; no shell-substitution features. Issue #17 will reference this scope cut as a documented limitation.
- Permission check pattern must match existing config helpers — do not invent a new one.
- Strong typing.
- Zero warnings.

### #12 — `@platform_dispatch` decorator + `clickwork.platform.dispatch` helper

**Files:** `src/clickwork/platform.py` (existing — extend it), `tests/unit/test_platform.py`.

**TDD:**
1. Red: add tests in `tests/unit/test_platform.py`:
   - Decorator form (use the `sys.platform` strings clickwork's own `is_linux/is_windows/is_macos` helpers check for — `"linux"`, `"win32"`, `"darwin"` respectively):
     - `test_platform_dispatch_linux_calls_linux_impl` — patch `sys.platform` to `"linux"`, decorated command calls `linux=fn`, assert `fn` received the expected args.
     - `test_platform_dispatch_windows_calls_windows_impl` — patch `sys.platform` to `"win32"` (not `"windows"` — that's what `is_windows()` checks for).
     - `test_platform_dispatch_macos_calls_macos_impl` — patch `sys.platform` to `"darwin"`.
     - `test_platform_dispatch_unsupported_platform_raises_usage_error` — patch `sys.platform` to `"freebsd13"` (or any platform we don't wire), assert `click.UsageError`.
     - `test_platform_dispatch_linux_error_kwarg_overrides_message` — `linux=None, linux_error="not yet"`, `sys.platform="linux"`, assert `UsageError` with the custom message.
     - Same pattern for `windows_error` and `macos_error`.
   - Functional form:
     - `test_dispatch_functional_linux` — `dispatch(ctx, linux=fn, windows=other, **kwargs)` on linux, assert `fn(**kwargs)` called.
     - `test_dispatch_functional_forwards_kwargs` — pass `extra="x"`, assert the selected impl received it.
   - Signature forwarding for the decorator: a decorated Click command with `@click.argument("name")` must still receive `name` through to the dispatched impl (impls have the same signature as the Click callback).
2. Green: implement both forms. The decorator wraps the original function: at call time, detect platform, route to the right impl. The functional form is thin — just the platform detection + kwarg dispatch. Share the platform-detection logic so they can't drift.
3. Refactor: docstrings on both forms with a code example. Add the existing `is_linux/is_windows/is_macos` helpers as the reference for platform detection.

**Constraints:**
- **Must close issue #12.** `Fixes #12`.
- Both forms ship in this PR (decorator is primary; functional is the escape hatch).
- `click.UsageError` for unsupported platforms — matches clickwork's "user error, not framework bug" policy.
- Strong typing. Decorator type signature is tricky (`Callable[P, R]` forwarding) — reference: https://docs.python.org/3/library/typing.html#typing.ParamSpec. If it gets too hairy, `Callable[..., Any]` with a comment is acceptable.
- Zero warnings.

### #14 — `clickwork.add_global_option(cli, ...)` flags-at-all-levels

**Files:** `src/clickwork/cli.py` or a new `src/clickwork/global_options.py` (pick whichever is smaller — probably a new file since it's a distinct concern), `tests/unit/test_global_options.py`.

**TDD:**
1. Red: add tests:
   - `test_add_global_option_root_level_parses` — `cli --json sub-cmd` → handler sees `ctx.meta["json"] is True`.
   - `test_add_global_option_subcommand_level_parses` — `cli sub-cmd --json` → same result.
   - `test_add_global_option_group_level_parses` — for a nested group, `cli group --json sub-cmd` → same.
   - `test_add_global_option_flag_or_semantics_across_levels` — `cli --json sub-cmd` (root True only), `cli sub-cmd --json` (sub True only), and `cli --json sub-cmd --json` (both True) all resolve to `ctx.meta["json"] is True`.
   - `test_add_global_option_value_innermost_wins` — `cli --env=prod sub-cmd --env=staging` → `ctx.meta["env"] == "staging"`. And `cli --env=prod sub-cmd` → `"prod"`.
   - `test_add_global_option_not_passed_is_falsy_or_none` — no flag set anywhere → `ctx.meta["json"] is False` (for flag) or `ctx.meta["env"] is None` (for value).
   - `test_add_global_option_registered_before_subcommands_still_applies` — call `add_global_option` then add a subcommand; the new subcommand does NOT retroactively get the option (document current behavior — registration is at call time).
2. Green: implement `add_global_option(cli, *param_decls, **option_kwargs)`:
   - Walk the existing commands attached to `cli` at call time (root + all groups recursively + all subcommands). Attach the option to each.
   - Each command's callback wrapper updates `ctx.meta[<name>]` based on the semantics: flag (`is_flag=True`) uses OR (`ctx.meta[name] = ctx.meta.get(name) or current`); value option uses innermost-wins (`ctx.meta[name] = current if current is not None else ctx.meta.get(name)`).
   - Option name derived from `param_decls` the same way Click derives it (`--foo-bar` → `foo_bar`). Use Click's internal helper if reachable, else mirror the logic.
3. Refactor: docstring with examples for both flag and value cases. Comment on the "registration is snapshot at call time" behavior and why (dynamic traversal each call would be fragile and surprising).

**Constraints:**
- **Must close issue #14.** `Fixes #14`.
- Resolution semantics: flag → OR, value → innermost-wins. Both via `ctx.meta[name]`.
- Call-time snapshot — no retroactive registration on commands added later (explicit design choice; document in the docstring).
- Works with nested `click.Group`s. Traverse with `Group.commands.values()` recursively.
- Strong typing.
- Zero warnings.

## Per-wave execution checklist

- [ ] Wave 1 PRs merged; Wave 2 worktrees rebased onto latest main
- [ ] Baseline `pytest -q` passes in each worktree (exact count depends on post-Wave-1 state)
- [ ] Three parallel subagents dispatched
- [ ] Diffs reviewed in main session
- [ ] Commit + push + PRs with `Fixes #N`
- [ ] Copilot review loop per PR
- [ ] Merges (independent — no inter-dependencies within Wave 2)
- [ ] Worktrees + local branches cleaned up

## Out of scope for Wave 2

- **#9** shell-variable substitution, backticks, heredocs, multiline values — documented limitation; refer users to `sh -c 'source file; env'` if they need those semantics
- **#12** platform-specific impl module auto-discovery — issue only asks for dispatch, not module loading
- **#14** retroactive registration on commands added after `add_global_option` call — explicit design choice; register after all commands are attached

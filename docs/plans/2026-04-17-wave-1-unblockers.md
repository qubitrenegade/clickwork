# Wave 1 plan — small unblockers

**Date:** 2026-04-17
**Roadmap:** [docs/superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md](../superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md)
**Scope:** Issues #8, #10, #15 — three independent bug fixes / small features run in parallel worktrees.

## API shape decisions

| Issue | Decision |
|-------|----------|
| #8 | Lazy resolution via property on `CliContext`. One test asserting `patch("clickwork.prereqs.require")` works as intuitively expected. |
| #10 | Two keyword-only params on `ctx.run()` and `ctx.run_with_confirm()`: `stdin_text: str \| None = None` and `stdin_bytes: bytes \| None = None`. Passing both raises `ValueError`. Text/binary mode chosen per the provided value; data delivered via `Popen.communicate()` so existing SIGINT-forwarding semantics are preserved (no switch to `subprocess.run`). |
| #15 | New kwarg `add_parent_to_path: bool = False` on `create_cli()`. When `True` and `commands_dir` is provided, `str(commands_dir.parent.resolve())` is inserted at `sys.path[0]` if that resolved path is not already present. Opt-in to avoid surprising existing consumers. |

## Branch + worktree layout

| Issue | Branch | Worktree path |
|-------|--------|---------------|
| #8 | `fix/require-mock-footgun-8` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-require-mock-8` |
| #10 | `feat/ctx-run-stdin-10` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-ctx-run-stdin-10` |
| #15 | `feat/auto-sys-path-15` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-auto-sys-path-15` |

## Per-issue tasks

### #8 — `ctx.require()` mocking footgun (lazy resolution)

**Files:** `src/clickwork/cli.py`, `src/clickwork/_types.py`, `tests/unit/test_cli.py` (or a new `test_require_mock.py` if cleaner).

**TDD:**
1. Red: add `test_ctx_require_is_patchable_via_prereqs_module` — create a CLI, `with patch("clickwork.prereqs.require")` as mock_req, invoke a command that calls `ctx.require("git")`. Assert `mock_req.called`. Current code fails because `cli.py` does `from clickwork.prereqs import require as _require` at import time and binds `cli_ctx.require = _require` — the patch never affects the bound reference.
2. Green: change the binding so `ctx.require` resolves through `clickwork.prereqs.require` at call time. Options:
   - Make `CliContext.require` a `@property` that imports and returns `clickwork.prereqs.require` on access.
   - Or a plain `lambda *a, **kw: clickwork.prereqs.require(*a, **kw)` bound at context build time.
   - Picking between these is up to the agent, but the property is cleaner for typing.
3. Refactor: ensure existing tests still pass. Document the change in the `CliContext` docstring and add a teaching-style comment on the property pointing at issue #8.

**Constraints:**
- Must close issue #8 (include `Fixes #8` in commit/PR).
- No API break — `ctx.require(name)` signature unchanged at the call site.
- Existing callers in tests that don't patch should keep working.

### #10 — `ctx.run(stdin_text=..., stdin_bytes=...)` (subprocess stdin data)

**Files:** `src/clickwork/process.py`, `src/clickwork/cli.py` (the lambdas wired into `CliContext`), `tests/unit/test_process.py`.

**TDD:**
1. Red: add tests using a portable `[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]` (matches the existing `test_process.py` pattern — no reliance on `cat`/`echo` which differ on Windows). Assert `stdin_text="hello"` is received on stdin and echoed on stdout. Same for `stdin_bytes=b"world"` (note: route through a separate binary-mode helper script). Add a test that passing both raises `ValueError`. Add a test that `stdin_text` works under `dry_run=True` (should NOT execute, just log).
2. Green: add `stdin_text: str | None = None` and `stdin_bytes: bytes | None = None` keyword-only params to `clickwork.process.run()` and `run_with_confirm()`. **Keep the existing `subprocess.Popen`-based execution path** — do not switch to `subprocess.run(input=...)`, because the current implementation uses `Popen` specifically to forward SIGINT to the child before re-raising `KeyboardInterrupt` (see `_wait_with_signal_forwarding` in `process.py`). When a stdin payload is provided: validate mutual exclusivity, set `stdin=subprocess.PIPE`, write the data via `proc.communicate(input=...)` (or equivalent write/close on `proc.stdin`) with text/binary mode chosen to match the provided value. Mode must be consistent: pick `text=True` with `stdin_text` and `text=False` with `stdin_bytes`.
3. Refactor: update the lambdas in `cli.py` that bind `cli_ctx.run` / `cli_ctx.run_with_confirm` to forward the new kwargs. Update the `run()` / `run_with_confirm()` docstrings with a section on stdin data passing, including the "never pass secrets via argv" use case.

**Constraints:**
- Keyword-only — position them behind `*`.
- `capture()` can stay as-is for now — extending it is out of scope for #10. (Note in PR if the agent wants to flag follow-up.)
- Must close issue #10.

### #15 — `add_parent_to_path=True` on `create_cli()`

**Files:** `src/clickwork/cli.py`, `tests/unit/test_cli.py`.

**TDD:**
1. Red: write `test_add_parent_to_path_false_by_default_does_not_modify_sys_path` (creates a CLI, snapshots `sys.path`, builds CLI, asserts unchanged) and `test_add_parent_to_path_true_inserts_commands_dir_parent` (creates a `commands_dir`, passes `add_parent_to_path=True`, asserts `str(commands_dir.parent.resolve())` is at `sys.path[0]` after construction — match the resolved absolute path since the implementation uses `.resolve()` for dedup). Plus `test_add_parent_to_path_idempotent` (call twice with the same commands_dir, path only appears once).
2. Green: add the kwarg to `create_cli()`. In the factory body, before or after discovery:
   ```python
   if add_parent_to_path and commands_dir is not None:
       parent = str(commands_dir.parent.resolve())
       if parent not in sys.path:
           sys.path.insert(0, parent)
   ```
3. Refactor: update `create_cli()` docstring to document the kwarg and its use case ("lets command files `from your_project.lib.X import Y` without boilerplate in the entry script").

**Constraints:**
- Opt-in default `False` — don't surprise existing consumers.
- Keyword-only (behind the `*` already in the signature from PR #6).
- `commands_dir.parent.resolve()` — use resolved absolute path so repeated invocations from different CWDs don't add duplicates.
- Must close issue #15.

## Per-wave execution checklist

- [ ] Wave 1 plan merged (this doc)
- [ ] Three worktrees created from `main`
- [ ] Baseline `pytest -q` passes in each worktree
- [ ] Three parallel agents dispatched (one per issue) with TDD instructions
- [ ] Diffs reviewed in main session
- [ ] Commit + push + open PRs with `Fixes #N`
- [ ] Copilot review loop per PR
- [ ] Merges in order as each clears Copilot (can merge independently — no inter-dependencies within Wave 1)
- [ ] Worktrees + local branches cleaned up
- [ ] Ready to start Wave 2 per parallelism policy B (prep during Copilot waits)

## Out of scope for Wave 1

- `capture(stdin_text=...)` — extending stdout-capturing subprocess to accept stdin data. Not needed for any of the gating use cases. Revisit in Wave 3 if #11 needs it.
- Path validation for `add_parent_to_path` — we trust `commands_dir` since it's already required to exist for discovery.
- Any changes to `ctx.require` semantics beyond making it patchable. Behavior of `require()` itself stays identical.

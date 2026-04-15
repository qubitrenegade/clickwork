# Rename qbrd-tools to clickwork -- Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the project from `qbrd-tools` / `qbrd_tools` to `clickwork` across all source, tests, docs, packaging, CI, and GitHub infrastructure.

**Architecture:** Mechanical find-and-replace with verification. The Python package directory moves from `src/qbrd_tools/` to `src/clickwork/`. All imports, entry point group names, config references, and documentation are updated. The GitHub repo is renamed via the API. No logic changes.

**Tech Stack:** Python, git, GitHub API (gh CLI)

**Mapping:**
- `qbrd-tools` (hyphenated, PyPI/CLI name) -> `clickwork`
- `qbrd_tools` (underscored, Python import) -> `clickwork`
- `QBRD_TOOLS` (uppercased, env var prefix in tests) -> `CLICKWORK`
- Entry point group `qbrd_tools.commands` -> `clickwork.commands`
- Discovery namespace `qbrd_tools._discovered_` -> `clickwork._discovered_`
- Logger name `qbrd_tools` -> `clickwork`

---

## File Map

All files are relative to the repo root (`qbrd-tools/`, soon to be `clickwork/`).

**Source (rename directory + update contents):**
- Rename: `src/qbrd_tools/` -> `src/clickwork/`
- Update: `src/clickwork/__init__.py`
- Update: `src/clickwork/_types.py`
- Update: `src/clickwork/cli.py`
- Update: `src/clickwork/config.py`
- Update: `src/clickwork/discovery.py`
- Update: `src/clickwork/_logging.py`
- Update: `src/clickwork/platform.py`
- Update: `src/clickwork/prereqs.py`
- Update: `src/clickwork/process.py`
- Update: `src/clickwork/prompts.py`

**Tests (update imports and string references):**
- Update: `tests/conftest.py`
- Update: `tests/unit/test_cli.py`
- Update: `tests/unit/test_config.py`
- Update: `tests/unit/test_discovery.py`
- Update: `tests/unit/test_logging.py`
- Update: `tests/unit/test_platform.py`
- Update: `tests/unit/test_prereqs.py`
- Update: `tests/unit/test_process.py`
- Update: `tests/unit/test_prompts.py`
- Update: `tests/unit/test_types.py`
- Update: `tests/integration/test_cli_e2e.py`
- Update: `tests/integration/test_sample_plugin.py`

**Sample plugin fixture:**
- Update: `tests/fixtures/sample-plugin/pyproject.toml`
- Update: `tests/fixtures/sample-plugin/README.md`
- Update: `tests/fixtures/sample-plugin/src/sample_commands/__init__.py`
- Update: `tests/fixtures/sample-plugin/src/sample_commands/hello.py`

**Packaging and CI:**
- Update: `pyproject.toml`
- Update: `.github/workflows/test.yml` (no qbrd refs, but verify)
- Update: `.github/workflows/publish.yml` (no qbrd refs, but verify)
- Update: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Update: `.github/ISSUE_TEMPLATE/config.yml`

**Documentation:**
- Update: `README.md`
- Update: `docs/ARCHITECTURE.md`
- Update: `docs/GUIDE.md`

---

### Task 1: Rename the Source Directory

**Files:**
- Rename: `src/qbrd_tools/` -> `src/clickwork/`

- [ ] **Step 1: Move the source directory**

```bash
git mv src/qbrd_tools src/clickwork
```

- [ ] **Step 2: Verify the move**

```bash
ls src/clickwork/__init__.py src/clickwork/cli.py src/clickwork/_types.py
```

Expected: all files listed, no errors.

- [ ] **Step 3: Commit the directory rename**

```bash
git add -A
git commit -m "refactor: rename src/qbrd_tools/ to src/clickwork/"
```

---

### Task 2: Update All Source File Contents

**Files:**
- Update: every `.py` file in `src/clickwork/`

Every source file contains `qbrd_tools` in imports, logger names, docstrings, or module-level strings. Replace all occurrences.

- [ ] **Step 1: Replace all `qbrd_tools` references in source files**

```bash
find src/clickwork -name '*.py' -exec sed -i 's/qbrd_tools/clickwork/g' {} +
find src/clickwork -name '*.py' -exec sed -i 's/qbrd-tools/clickwork/g' {} +
```

- [ ] **Step 2: Verify no qbrd references remain in source**

```bash
grep -r 'qbrd' src/clickwork/
```

Expected: no output (zero matches).

- [ ] **Step 3: Spot-check key files**

Verify these specific lines are correct:

- `src/clickwork/__init__.py`: `from clickwork._types import ...`
- `src/clickwork/cli.py`: `from clickwork._logging import setup_logging`
- `src/clickwork/discovery.py`: `ENTRY_POINT_GROUP = "clickwork.commands"`
- `src/clickwork/discovery.py`: `package_name = f"clickwork._discovered_{dir_hash}"`
- `src/clickwork/_logging.py`: `logging.getLogger("clickwork")`
- `src/clickwork/prereqs.py`: `logging.getLogger("clickwork")`
- `src/clickwork/process.py`: `logging.getLogger("clickwork")`

- [ ] **Step 4: Commit source content updates**

```bash
git add src/clickwork/
git commit -m "refactor: update all source imports and references to clickwork"
```

---

### Task 3: Update pyproject.toml

**Files:**
- Update: `pyproject.toml`

- [ ] **Step 1: Update package metadata**

Change these fields:
- `name = "qbrd-tools"` -> `name = "clickwork"`
- `description` -> update if it mentions qbrd
- `[tool.hatch.build.targets.wheel] packages` -> `["src/clickwork"]`

- [ ] **Step 2: Verify pyproject.toml**

```bash
cat pyproject.toml
```

Confirm `name = "clickwork"` and `packages = ["src/clickwork"]`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "refactor: update pyproject.toml for clickwork"
```

---

### Task 4: Update All Test Files

**Files:**
- Update: `tests/conftest.py`
- Update: every file in `tests/unit/` and `tests/integration/`

Tests import from `qbrd_tools` and reference the module name in logger assertions, monkeypatch targets, and string comparisons.

- [ ] **Step 1: Replace all `qbrd_tools` and `qbrd-tools` references in tests**

```bash
find tests -name '*.py' -exec sed -i 's/qbrd_tools/clickwork/g' {} +
find tests -name '*.py' -exec sed -i 's/qbrd-tools/clickwork/g' {} +
```

- [ ] **Step 2: Verify no qbrd references remain in tests**

```bash
grep -r 'qbrd' tests/ --include='*.py'
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "refactor: update all test imports and references to clickwork"
```

---

### Task 5: Update Sample Plugin Fixture

**Files:**
- Update: `tests/fixtures/sample-plugin/pyproject.toml`
- Update: `tests/fixtures/sample-plugin/README.md`
- Update: `tests/fixtures/sample-plugin/src/sample_commands/__init__.py`
- Update: `tests/fixtures/sample-plugin/src/sample_commands/hello.py`

The sample plugin declares `qbrd_tools.commands` as its entry point group and references `qbrd-tools` as a dependency.

- [ ] **Step 1: Update sample plugin files**

```bash
find tests/fixtures/sample-plugin -type f \( -name '*.py' -o -name '*.toml' -o -name '*.md' \) \
  -exec sed -i 's/qbrd_tools/clickwork/g' {} + \
  -exec sed -i 's/qbrd-tools/clickwork/g' {} +
```

- [ ] **Step 2: Verify the entry point group name**

```bash
grep 'clickwork.commands' tests/fixtures/sample-plugin/pyproject.toml
```

Expected: `[project.entry-points."clickwork.commands"]`

- [ ] **Step 3: Verify the dependency name**

```bash
grep 'clickwork' tests/fixtures/sample-plugin/pyproject.toml
```

Expected: `"clickwork"` appears in `dependencies`.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/
git commit -m "refactor: update sample plugin fixture for clickwork"
```

---

### Task 6: Update Documentation

**Files:**
- Update: `README.md`
- Update: `docs/ARCHITECTURE.md`
- Update: `docs/GUIDE.md`

- [ ] **Step 1: Replace all references in docs**

```bash
sed -i 's/qbrd_tools/clickwork/g; s/qbrd-tools/clickwork/g; s/qbrd\.tools/clickwork/g' \
  README.md docs/ARCHITECTURE.md docs/GUIDE.md
```

- [ ] **Step 2: Update GitHub URLs in docs**

```bash
sed -i 's|qubitrenegade/qbrd-tools|qubitrenegade/clickwork|g' \
  README.md docs/ARCHITECTURE.md docs/GUIDE.md
```

- [ ] **Step 3: Update the README title and description**

Change the top of `README.md`:
- `# qbrd-tools` -> `# clickwork`

- [ ] **Step 4: Verify no qbrd references remain in docs**

```bash
grep -r 'qbrd' README.md docs/
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/
git commit -m "docs: update all documentation for clickwork rename"
```

---

### Task 7: Update CI and Issue Templates

**Files:**
- Update: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Update: `.github/ISSUE_TEMPLATE/config.yml`

- [ ] **Step 1: Replace references in issue templates**

```bash
find .github -type f -name '*.yml' -exec sed -i 's/qbrd_tools/clickwork/g; s/qbrd-tools/clickwork/g' {} +
find .github -type f -name '*.yml' -exec sed -i 's|qubitrenegade/qbrd-tools|qubitrenegade/clickwork|g' {} +
```

- [ ] **Step 2: Verify no qbrd references remain in .github/**

```bash
grep -r 'qbrd' .github/
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/
git commit -m "ci: update issue templates for clickwork rename"
```

---

### Task 8: Reinstall and Run Tests

**Files:** None (verification only)

- [ ] **Step 1: Reinstall the package in the dev venv**

```bash
uv pip install -e ".[dev]"
```

This picks up the renamed package directory and new pyproject.toml.

- [ ] **Step 2: Run the full unit test suite**

```bash
uv run pytest tests/unit/ -v --tb=short
```

Expected: all tests pass (currently 116 unit tests).

- [ ] **Step 3: Run the integration tests (non-network)**

```bash
uv run pytest tests/integration/test_cli_e2e.py -v --tb=short
```

Expected: all 3 tests pass.

- [ ] **Step 4: Verify imports work from a clean Python**

```bash
uv run python -c "from clickwork import create_cli, CliContext, Secret; print('OK')"
```

Expected: prints `OK`.

- [ ] **Step 5: Do a final sweep for any remaining qbrd references**

```bash
grep -r 'qbrd' --include='*.py' --include='*.toml' --include='*.md' --include='*.yml' . | grep -v .venv | grep -v .git/
```

Expected: no output (or only this plan file and the spec in docs/).

---

### Task 9: Rename the GitHub Repository

**Files:** None (GitHub API only)

- [ ] **Step 1: Rename the repo via gh CLI**

```bash
gh repo rename clickwork
```

This updates the repo name on GitHub. GitHub automatically redirects the old URL to the new one.

- [ ] **Step 2: Update the local remote URL**

```bash
git remote set-url origin https://github.com/qubitrenegade/clickwork.git
```

- [ ] **Step 3: Push all commits**

```bash
git push
```

- [ ] **Step 4: Verify the rename**

```bash
gh repo view qubitrenegade/clickwork --json name,url --jq '.name + " " + .url'
```

Expected: `clickwork https://github.com/qubitrenegade/clickwork`

---

### Task 10: Update the orbit-widener spec references

**Files:**
- Update: `orbit-widener-plugin/docs/superpowers/specs/2026-04-03-qbrd-tools-cli-framework-design.md`
- Update: `orbit-widener-plugin/docs/superpowers/plans/2026-04-03-qbrd-tools-framework.md`

These files in the orbit-widener repo reference `qbrd-tools` and `qbrd_tools` throughout. Update them so future agent sessions don't use the old name.

- [ ] **Step 1: Update the spec file**

```bash
cd /home/qbrd/qbrd-orbit-widener/orbit-widener-plugin
sed -i 's/qbrd_tools/clickwork/g; s/qbrd-tools/clickwork/g' \
  docs/superpowers/specs/2026-04-03-qbrd-tools-cli-framework-design.md
sed -i 's|qubitrenegade/qbrd-tools|qubitrenegade/clickwork|g' \
  docs/superpowers/specs/2026-04-03-qbrd-tools-cli-framework-design.md
```

- [ ] **Step 2: Update the plan file**

```bash
sed -i 's/qbrd_tools/clickwork/g; s/qbrd-tools/clickwork/g' \
  docs/superpowers/plans/2026-04-03-qbrd-tools-framework.md
sed -i 's|qubitrenegade/qbrd-tools|qubitrenegade/clickwork|g' \
  docs/superpowers/plans/2026-04-03-qbrd-tools-framework.md
```

- [ ] **Step 3: Commit in the orbit-widener repo**

```bash
git add docs/superpowers/specs/ docs/superpowers/plans/
git commit -m "docs: update qbrd-tools references to clickwork after rename"
```

---

### Task 11: Close the Issue

- [ ] **Step 1: Comment on issue #2 with the resolution**

```bash
gh issue comment 2 --body "Renamed to \`clickwork\`. Repo, package, imports, docs, CI all updated."
```

- [ ] **Step 2: Close issue #2**

```bash
gh issue close 2
```

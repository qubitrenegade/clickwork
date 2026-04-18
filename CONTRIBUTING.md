# Contributing to clickwork

## Welcome

Thanks for taking an interest. The contributions that move the project
forward the most are: precise bug reports (ideally with a reproducer), doc
clarifications, failing test cases that pin down a behavior, and feature
ideas opened as issues **before** code is written. Drive-by pull requests
for large features without a prior discussion tend to stall, because 1.0
is a stabilization release and new surface area needs to be justified
against the scope captured in
[`docs/superpowers/specs/2026-04-18-clickwork-1.0-roadmap.md`](docs/superpowers/specs/2026-04-18-clickwork-1.0-roadmap.md).
When in doubt, open an issue first. Small PRs for typos, small doc fixes,
and clear one-line bug fixes are welcome without a preceding issue.

## Local dev setup

clickwork targets Python 3.11+ (see `requires-python` in
[`pyproject.toml`](pyproject.toml)). `uv` is the supported installer and
environment manager.

```bash
# Install uv. See https://docs.astral.sh/uv/getting-started/installation/
# for the full list of platform-appropriate install methods (Homebrew,
# apt, winget, pipx, standalone installer, etc.). Picking the installer
# your project already trusts is safer than piping a remote script into
# a shell.

# Clone and enter the repo
git clone https://github.com/qubitrenegade/clickwork.git
cd clickwork

# Install runtime + dev dependencies into a local venv
uv sync --extra dev
```

`uv sync --extra dev` resolves from `uv.lock`, so every contributor gets
the same dependency set CI uses. If you need to add or bump a dependency,
edit `pyproject.toml` and re-run `uv sync --extra dev` to regenerate
`uv.lock`.

## Running the verification suite

Run these four commands locally before you push. CI runs the same tools
with the same pins, though split across separate workflows (test, types,
lint) that may execute in parallel. A green local run of all four should
mean a green CI run across every workflow.

```bash
uv run pytest tests/ -q
uv run mypy --strict src/clickwork
uvx --from ruff==0.6.9 ruff check .
uvx --from ruff==0.6.9 ruff format --check .
```

The Ruff version pin matches
[`.github/workflows/lint.yml`](.github/workflows/lint.yml). Bumping it is
a deliberate, reviewed change in its own PR. If you need to autofix
formatting locally, drop the `--check` from the last command.

## Writing a test

Tests live under `tests/`, split by scope.

- `tests/unit/` -- must run without network. Fast, deterministic, no
  subprocess calls that reach out. These run on every push.
- `tests/integration/` -- allowed to create venvs, spawn subprocesses,
  touch the filesystem. Tests that need network must be marked with
  `@pytest.mark.network` (the marker is declared in `pyproject.toml` and
  is the signal to CI gates / contributors about what the test needs).

Pytest configuration lives in `pyproject.toml` under
`[tool.pytest.ini_options]`. Two things to know before writing a test:

- `filterwarnings = ["error"]` -- any unexpected warning fails the test.
  If you intentionally want to assert on a warning, wrap the call in
  `pytest.warns(...)`. Don't silence warnings globally.
- `pythonpath = ["src"]` -- you can `import clickwork` directly in tests
  without an editable install.

For tests that invoke a CLI built via `create_cli()`, use the helpers in
`clickwork.testing` rather than hand-rolling `CliRunner` plumbing. See
existing tests under `tests/integration/` for patterns.

## Commit and PR conventions

Commit messages: a one-line summary (imperative mood, under ~72 chars),
a blank line, and a body explaining the *why* when it isn't obvious. We
don't enforce Conventional Commits, but prefix style like `fix:`,
`feat:`, `docs:`, `ci:`, `refactor:`, `test:`, `chore:` keeps the log
scannable and matches the project's existing history.

PR titles should match the commit tone. Include `Fixes #<n>` (or
`Closes #<n>`) in the title or body where a PR fully resolves an issue.
This closes the issue automatically on merge.

**Labels matter for release notes.** GitHub's auto-generated release
notes read [`.github/release.yml`](.github/release.yml) and bucket PRs
by label. Apply one of these before a PR merges:

| Label | Release-notes section |
|-------|-----------------------|
| `enhancement` | Features |
| `bug` | Bug fixes |
| `documentation` | Documentation |
| (none, or other) | Other changes |

**Breaking changes** must be called out. Prefix the PR title or the
first line of the PR body with `BREAKING:` so the release-notes
automation and the 0.x → 1.0 migration guide can pick them up. A PR
that silently changes a documented behavior without the marker will be
sent back for a title edit.

Prefer smaller PRs. Anything over about 200 lines of diff is going to
cost a review round; splitting it usually doesn't.

## Review expectations

Every PR gets at least one automated pass from GitHub Copilot's code
review plus a maintainer review. Copilot reviews run on the first push;
subsequent pushes need a manual re-review request from the PR's
**Reviewers** panel. We genuinely use Copilot's feedback as a first-pass
filter, so leaving its comments unanswered is not an option.

Expect at least one round of review comments on anything non-trivial.
Small, focused PRs turn around quickly. Monolithic PRs tend to stall in
review and are usually asked to be split.

## When Copilot flags something you disagree with

The policy is: **fix it, or push back with empirical justification.**
Silent ignores aren't acceptable. If a suggestion is wrong, reply to the
comment explaining why (a failing test case, a profile result, a spec
reference, a link to the Click source, whatever the evidence is) and
use the "Dismiss" action with that reason. Reviewers can follow the
reasoning; a dismissed comment with no reply can't be reviewed.

If you aren't sure whether a suggestion is right, ask in the PR thread.
Maintainers would rather weigh in than have you guess.

## Release process

Maintainer-only. The cadence and release mechanics live in
[`docs/GUIDE.md`](docs/GUIDE.md) under **Release notes**, and the
current wave structure for the 1.0 cycle is in
[`docs/superpowers/specs/2026-04-18-clickwork-1.0-roadmap.md`](docs/superpowers/specs/2026-04-18-clickwork-1.0-roadmap.md).
The multi-wave plan only applies to major work; point releases are
single-PR cuts that bump version and changelog. If you're unsure
whether a change you want to make warrants a roadmap-style rollout,
open an issue and ask.

## Code of conduct

This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/). Treat
each other with respect; disagreements on technical matters are fine,
personal attacks are not. Maintainers will act on reports of abusive
behavior. If a standalone `CODE_OF_CONDUCT.md` is added later, it will
supersede this paragraph.

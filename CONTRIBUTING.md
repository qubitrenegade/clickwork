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

### Cutting a release (recommended: workflow-driven)

The tag is signed by a dedicated release-signing GPG key that lives
in the `pypi` environment as a secret. No local GPG setup required.
Prerequisite: the `RELEASE_GPG_PRIVATE_KEY`, `RELEASE_GPG_FINGERPRINT`,
and `RELEASE_TAG_PUSH_TOKEN` secrets must already exist in the `pypi`
environment (see "Release-signing key + PAT rotation" below for how
they were created and how to rotate).

1. Merge a release PR that bumps `version` in `pyproject.toml`,
   adds the new entry to `CHANGELOG.md`, and (if relevant) updates
   the trove classifier from Beta -> Production/Stable.
2. Go to **Actions → Sign release tag → Run workflow**. Fill in:
   - `version`: e.g. `1.0.1` (no leading `v`). PEP 440 prereleases
     like `1.0.1rc0` and hyphenated forms like `1.0.1-rc0` are both
     accepted.
   - `commit_sha`: leave blank for `main` HEAD, or paste a SHA if
     `main` has moved since the release PR merged and you want to
     tag a specific commit.
   - `headline`: short description for the tag annotation. The
     workflow templates the full message as
     `clickwork X.Y.Z — <headline>` so you only need to supply the
     trailing bit.
3. Click **Run workflow**. The `pypi` environment's approval gate
   fires — open the run, click "Review deployments", approve
   `pypi`. The workflow imports the release-signing GPG key,
   creates and pushes a signed `vX.Y.Z` tag using the PAT
   (`RELEASE_TAG_PUSH_TOKEN`) so the tag push fires
   `publish.yml` normally.
4. The push fires `.github/workflows/publish.yml`: build wheel +
   sdist, Sigstore-sign them, create the GitHub Release with
   auto-generated notes (from `.github/release.yml` label
   grouping) and the dist + `.sigstore` bundle files attached,
   then publish to PyPI via Trusted Publishing with PEP 740
   attestations.
5. Approve the `pypi` environment a **second time** for
   `publish.yml`'s PyPI job. (Two approvals per release by design
   — one for tag signing, one for PyPI publish.) The publish job
   finishes shortly after (typically under a minute).

### Cutting a release (fallback: local GPG)

Use this path if the release-signing secrets aren't configured yet,
or if you need to tag from a machine-local build for some reason
(e.g., emergency cut while the signing workflow is broken).

1. Merge a release PR that bumps `version` in `pyproject.toml`,
   adds the new entry to `CHANGELOG.md`, and (if relevant) updates
   the trove classifier from Beta -> Production/Stable.
2. Pull main, then sign and push the version tag. Write a proper
   tag annotation -- `git show v1.0.0` and the tag detail page on
   GitHub surface it; the GitHub Release body itself is separately
   auto-generated from `.github/release.yml` (see step 3):

   ```bash
   git checkout main && git pull
   git tag -s vX.Y.Z -m "clickwork X.Y.Z — <headline>"
   git push origin vX.Y.Z
   ```

   **GPG on a headless terminal:** if `git tag -s` fails with
   `gpg failed to sign the data` / `Inappropriate ioctl for
   device`, `gpg-agent` can't find a TTY for the pinentry
   passphrase prompt. Fix before retrying:

   ```bash
   export GPG_TTY=$(tty)
   ```

   Adding that to your shell rc makes the fix permanent for
   future releases.

3. The push fires `.github/workflows/publish.yml`: build wheel +
   sdist, Sigstore-sign them, create the GitHub Release with
   auto-generated notes and the dist + `.sigstore` bundle files
   attached, then publish to PyPI via Trusted Publishing with
   PEP 740 attestations.
4. The `pypi` environment is gated on maintainer approval. After
   the tag push, open the Actions tab, find the Publish run, click
   "Review deployments", approve `pypi`. The publish job finishes
   shortly after (typically under a minute; longer if runner load
   or the PyPI upload API is slow).

### Release-signing key + PAT rotation

The dedicated release-signing GPG key and the `RELEASE_TAG_PUSH_TOKEN`
PAT rotate **yearly, or immediately on any suspected exposure**.

**One-time setup (if the secrets don't exist yet):**

1. Generate the release-signing GPG key. This is a dedicated key,
   NOT the maintainer's personal identity:

   ```bash
   gpg --batch --pinentry-mode loopback --passphrase '' \
       --quick-gen-key 'clickwork-release-bot <release@clickwork.invalid>' rsa4096 sign 0
   ```

   `--pinentry-mode loopback` is required on GnuPG 2.1+ for
   `--passphrase ''` to be honored in batch mode.

2. Export and upload the public half to the maintainer's GitHub
   account (Settings → SSH and GPG keys → New GPG key). This is
   what lets GitHub show tags signed with this key as "Verified"
   on the maintainer's account:

   ```bash
   gpg --armor --export 'clickwork-release-bot <release@clickwork.invalid>'
   ```

3. Export the private half (ASCII-armored block):

   ```bash
   gpg --armor --export-secret-keys 'clickwork-release-bot <release@clickwork.invalid>'
   ```

4. Capture the full 40-character fingerprint (filter by UID so
   multiple secret keys in the keyring don't cause a wrong pickup):

   ```bash
   gpg --list-secret-keys --with-colons 'clickwork-release-bot <release@clickwork.invalid>' \
     | awk -F: '$1=="fpr" {print $10; exit}'
   ```

5. Create a fine-scoped PAT (Settings → Developer settings →
   Personal access tokens → Fine-grained):
   - Repository access: Only `qubitrenegade/clickwork`.
   - Repository permissions: **Contents: Read and write**.
   - Expiration: one year out.

6. Store the three secrets in the `pypi` environment (Settings →
   Environments → pypi → Secrets):
   - `RELEASE_GPG_PRIVATE_KEY` — armored private block from step 3.
   - `RELEASE_GPG_FINGERPRINT` — 40-character fingerprint from step 4.
   - `RELEASE_TAG_PUSH_TOKEN` — PAT from step 5.

**Yearly rotation:**

1. Generate a new GPG key + PAT, same procedure as steps 1-5
   above.
2. Upload the new public GPG key to your GitHub account. Do NOT
   delete the old public key yet — existing tag signatures
   reference it.
3. Update `RELEASE_GPG_PRIVATE_KEY`, `RELEASE_GPG_FINGERPRINT`,
   and `RELEASE_TAG_PUSH_TOKEN` in the `pypi` environment.
4. Run the next release through the workflow to confirm it signs
   cleanly against the new key.
5. After a clean release with the new key: revoke the old GPG key
   (upload the revocation certificate to your GitHub account) and
   delete the old PAT.

## Code of conduct

This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/). Treat
each other with respect; disagreements on technical matters are fine,
personal attacks are not. Maintainers will act on reports of abusive
behavior. If a standalone `CODE_OF_CONDUCT.md` is added later, it will
supersede this paragraph.

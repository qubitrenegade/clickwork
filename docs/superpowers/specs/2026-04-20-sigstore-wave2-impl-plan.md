# Implementation plan — Sigstore Wave 2 (workflow-driven tag signing)

**Date:** 2026-04-20
**Milestone:** 1.0.x
**Parent plan:** [2026-04-19-sigstore-signing-plan.md](2026-04-19-sigstore-signing-plan.md) (locked Q4=B Path 1, M1 mitigation: PAT-authenticated push)
**Parent issue:** [#61](https://github.com/qubitrenegade/clickwork/issues/61)
**Scope:** Wave 2 only — add `.github/workflows/sign-release-tag.yml` and update `CONTRIBUTING.md` with the new release-cutting runbook. Wave 3 (verify docs) and Wave 4 (cut 1.0.1) are separate plans/PRs.
**Relevant files:** [.github/workflows/publish.yml](../../../.github/workflows/publish.yml) (current tag-push-driven release flow; unchanged by this plan), [CONTRIBUTING.md](../../../CONTRIBUTING.md#cutting-a-release) (existing runbook using maintainer's local GPG key)

## Goal

Move git-tag signing from the maintainer's local GPG keyring into the release workflow, so (1) future maintainers don't need their own GPG key on their own machine, (2) the signing key is revocable without touching the maintainer's personal identity, and (3) the existing `publish.yml` fires normally on the pushed tag without special-casing.

## Non-goals (Wave 2)

- Sigstore artifact bundle signing (Wave 1, already merged in #108).
- Verification documentation (Wave 3).
- Cutting 1.0.1 (Wave 4).
- Removing the local-GPG fallback from `CONTRIBUTING.md`. Both paths stay documented during the 1.0.x cycle so a maintainer who forgot to configure the secrets can still ship.
- Adding `.github/dependabot.yml` (tracked out-of-scope on the Wave 1 implementation plan).

## Current state

Tag signing today (per `CONTRIBUTING.md` "Cutting a release"):

1. Maintainer locally: `git tag -s vX.Y.Z -m "..." && git push origin vX.Y.Z`.
2. Tag push fires `publish.yml` (3-job pipeline: build → create-release → publish, now including Sigstore artifact signing from #108).

Requires the maintainer to have a GPG key in their local keyring and (in some environments) `GPG_TTY=$(tty)` exported for pinentry.

No workflow-driven tag signing exists.

## Scope of this plan

Two deliverables:

1. **New workflow file** `.github/workflows/sign-release-tag.yml` triggered by `workflow_dispatch`:
   - Checks out `main` at a specified commit SHA (default: HEAD).
   - Imports the dedicated release-signing GPG key from a secret.
   - Configures `git` with the signing identity + fingerprint.
   - Creates a signed annotated tag `vX.Y.Z`.
   - Pushes the tag using a PAT-authenticated URL so the push fires `publish.yml` (per parent plan's M1 mitigation — `GITHUB_TOKEN`-pushed tags don't trigger sibling workflows).
2. **CONTRIBUTING.md update** — new "Cutting a release (workflow path)" subsection describing the `workflow_dispatch` flow as the recommended path for 1.0.x. Existing local-GPG runbook retained below it as a labelled "Fallback: local GPG" subsection.

Prerequisite work (one-time, maintainer-side, NOT in this PR): generate the dedicated GPG key, upload public half to the maintainer's GitHub, store private half + fingerprint + PAT as repo secrets in the `pypi` environment. The plan's "Prerequisites" section below documents the exact steps; the implementation PR references them.

## Design questions

### Q1. `workflow_dispatch` inputs — version only, or version + commit SHA?

- **A) Version input only** — the workflow tags the current `main` HEAD. Simple, one field in the UI.
- **B) Version + optional commit SHA (default main HEAD)** — lets the maintainer tag a specific commit if a late fix landed after HEAD moved. Commit defaults to HEAD so the common case is still one field.
- **C) Version + branch/ref** — richer but strays from "tag a release commit"; most tags come from main HEAD or near it.

**Recommendation:** B. The cost of the extra optional input is near zero (GitHub's dispatch UI shows it below version), and the safety benefit is real: releasing `1.0.1` from an arbitrary `main` is a race condition if an unrelated PR merges between the decision to release and the dispatch. Explicit SHA pin eliminates that.

**Open question for maintainer:** confirm B (version + optional SHA), or A (simpler, HEAD-only)?

### Q2. Tag annotation message — template, or free-form input?

Annotated tags need a `-m` message. Options:

- **A) Template it from inputs: `clickwork X.Y.Z`** — simple, machine-readable. No release-headline prose.
- **B) Template a short line, let maintainer paste a headline in a second input field** — `clickwork X.Y.Z — <headline>`. Matches the existing CONTRIBUTING.md pattern.
- **C) Free-form: one input field for the full `-m` message** — maximum flexibility, but the maintainer has to remember the convention.

**Recommendation:** B. The current runbook uses `"clickwork X.Y.Z — <headline>"`, and dropping the headline would be a visible regression in `git show vX.Y.Z` / the tag detail page. Templating the prefix from the version input keeps the convention stable; the maintainer only has to type the headline.

**Open question for maintainer:** confirm B (template prefix + headline input), or A (template-only, simpler but loses the headline)?

### Q3. Rollback on mid-workflow failure?

If the workflow fails partway (e.g., import-key step succeeds, tag-creation succeeds, push fails), state to clean up:

- GPG key temporarily in the runner (ephemeral, self-heals).
- A local tag in the workflow's checkout (also ephemeral, self-heals).
- **Nothing pushed to `origin`** in the failure case.

- **A) No rollback logic — the workflow fails, nothing was pushed, maintainer re-runs after fixing the underlying issue.** The runner is ephemeral; there's no state to clean up.
- **B) Add an explicit cleanup step that runs `always:`** — delete the local tag, unload the GPG key. Belt-and-suspenders; protects against a hypothetical self-hosted runner that persists state.
- **C) Two-phase: tag in one job, push in a second job that needs the first** — isolates failure modes. Heavier structure.

**Recommendation:** A. We're on ephemeral `ubuntu-latest` runners, nothing persists. Adding cleanup is dead code for our actual deployment topology.

**Open question for maintainer:** confirm A (no rollback logic), or push to B (defensive cleanup for hypothetical future runner changes)?

### Q4. Runbook cutover strategy?

Once this PR merges, the maintainer can sign either locally or via the workflow. How is this presented in `CONTRIBUTING.md`?

- **A) Workflow path is the recommended path, local path is a documented fallback** — "Cutting a release (recommended)" + "Cutting a release (fallback: local GPG)". Both paths stay functional; new contributors see the workflow path first.
- **B) Workflow path fully replaces local path** — delete the local-GPG section entirely. Cleaner doc, but any maintainer who skipped the one-time secret setup can't ship.
- **C) Local path remains primary; workflow path is documented as "if you don't have local GPG available"** — inverse of A. Keeps the current maintainer's muscle memory intact.

**Recommendation:** A. The workflow path is strictly better (no local GPG dependency, signing key is revocable) but the fallback guards against a secrets-configuration mistake derailing a release cut. The local path should wither away when someone does a planned purge of the "fallback" content in a future docs pass — but not now, not as a Wave 2 deliverable.

**Open question for maintainer:** confirm A (workflow primary, local fallback), or C (local primary until you're comfortable)?

## Prerequisites (one-time, before Wave 2 implementation PR merges or the first workflow run)

These are maintainer-side setup tasks, documented here for visibility. They're NOT commits in the Wave 2 implementation PR.

1. **Generate a dedicated release-signing GPG key** (not the maintainer's personal identity):
   ```bash
   gpg --batch --pinentry-mode loopback --passphrase '' \
       --quick-gen-key 'clickwork-release-bot <release@clickwork.invalid>' rsa4096 sign 0
   ```
   Passwordless (`--passphrase ''`) — the workflow can't type a passphrase. On GnuPG 2.1+ the `--passphrase` flag is ignored without `--pinentry-mode loopback`, so include both to keep the one-time batch setup deterministic across environments. The identity is `clickwork-release-bot`, clearly NOT `qubitrenegade`.
2. **Export the public half + upload to the maintainer's GitHub** (Settings → SSH and GPG keys → New GPG key). This is what lets GitHub show signed tags from this key as "Verified" on the maintainer's account.
   ```bash
   gpg --armor --export <fingerprint>
   ```
3. **Export the private half** (ASCII-armored block) to paste into the repo secret:
   ```bash
   gpg --armor --export-secret-keys <fingerprint>
   ```
4. **Capture the full 40-character fingerprint** for the `git config user.signingkey` step. Filter by the new key's UID so multiple secret keys in the keyring don't cause the wrong fingerprint to be picked up:
   ```bash
   gpg --list-secret-keys --with-colons 'clickwork-release-bot <release@clickwork.invalid>' \
     | awk -F: '$1=="fpr" {print $10; exit}'
   ```
5. **Create a fine-scoped PAT** for pushing the signed tag (GitHub Settings → Developer settings → Personal access tokens → Fine-grained):
   - Repository access: Only `qubitrenegade/clickwork`.
   - Repository permissions: `Contents: Read and write`.
   - Expiration: one year out; rotate yearly.
6. **Store the three secrets in the `pypi` environment** (Settings → Environments → pypi → Secrets):
   - `RELEASE_GPG_PRIVATE_KEY` — armored private block from step 3.
   - `RELEASE_GPG_FINGERPRINT` — 40-character fingerprint from step 4.
   - `RELEASE_TAG_PUSH_TOKEN` — PAT from step 5.

## Proposed implementation

### Step 1. `.github/workflows/sign-release-tag.yml`

New file, ~70 lines, `workflow_dispatch` triggered. Sketch:

```yaml
name: Sign release tag

on:
  workflow_dispatch:
    inputs:
      version:
        description: 'Version to tag (without leading v), e.g. 1.0.1'
        required: true
        type: string
      commit_sha:
        description: 'Commit to tag (default: main HEAD)'
        required: false
        type: string
      headline:
        description: 'Release headline for tag annotation'
        required: true
        type: string

permissions: {}

jobs:
  sign-and-push:
    runs-on: ubuntu-latest
    environment: pypi  # gate on same approval as publish
    permissions:
      contents: read  # checkout only; push uses PAT, not GITHUB_TOKEN

    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.commit_sha || 'main' }}
          fetch-depth: 0  # full history so the tag points at a real commit

      - name: Import GPG key + configure git identity
        # crazy-max/ghaction-import-gpg sets user.name, user.email,
        # user.signingkey, and enables tag/commit GPG signing from the
        # imported key's UID. The runner starts with an unconfigured
        # git identity, so without this step `git tag -s` would fail
        # with "please tell me who you are".
        uses: crazy-max/ghaction-import-gpg@<pinned-sha>  # v6.x or similar
        with:
          gpg_private_key: ${{ secrets.RELEASE_GPG_PRIVATE_KEY }}
          git_user_signingkey: true
          git_tag_gpgsign: true
          # git_committer_name and git_committer_email are derived
          # from the key's UID automatically.

      - name: Pin signing key by fingerprint
        # Belt-and-suspenders: the import step sets user.signingkey
        # to the key's ID, but we override with the full 40-char
        # fingerprint so collisions against any other key in the
        # runner's ephemeral keyring are impossible.
        run: git config user.signingkey ${{ secrets.RELEASE_GPG_FINGERPRINT }}

      - name: Create signed tag
        # Shell-injection hardening: pass inputs.version and inputs.headline
        # via env vars rather than direct ${{ }} interpolation inside the
        # shell command. Validate version before use (digits + dots only,
        # no more than 3 dots). Write the annotation body to a temp file
        # and `git tag -F` from it so any punctuation in the headline
        # (quotes, newlines) can't break out of the shell context.
        env:
          VERSION: ${{ inputs.version }}
          HEADLINE: ${{ inputs.headline }}
        run: |
          set -eu
          case "$VERSION" in
            ''|*[!0-9.]*|*.*.*.*)
              echo "Invalid version: $VERSION" >&2
              exit 1
              ;;
          esac
          if printf '%s' "$HEADLINE" | grep -q '[[:cntrl:]]'; then
            echo "Headline must not contain control characters" >&2
            exit 1
          fi
          TAG="v$VERSION"
          msg_file="$(mktemp)"
          trap 'rm -f "$msg_file"' EXIT
          printf 'clickwork %s — %s\n' "$VERSION" "$HEADLINE" >"$msg_file"
          git tag -s "$TAG" -F "$msg_file"

      - name: Push tag with PAT
        # Same hardening: VERSION via env, re-validate (the tag-create step
        # and the push step may execute in different shells on some
        # runners, so defense in depth rather than relying on the earlier
        # check alone).
        env:
          PAT: ${{ secrets.RELEASE_TAG_PUSH_TOKEN }}
          VERSION: ${{ inputs.version }}
        run: |
          set -eu
          case "$VERSION" in
            ''|*[!0-9.]*|*.*.*.*)
              echo "Invalid version: $VERSION" >&2
              exit 1
              ;;
          esac
          TAG="v$VERSION"
          git push "https://x-access-token:${PAT}@github.com/${{ github.repository }}.git" "$TAG"
```

Key properties:
- **`environment: pypi`** — same approval gate as `publish.yml`, so tag signing is not automatic on dispatch.
- **PAT-authenticated push** — the raw git URL with `x-access-token:$PAT` authenticates as the PAT owner, not as `GITHUB_TOKEN`. Push fires `publish.yml` normally (per parent plan M1).
- **`permissions: contents: read`** — only what's needed for checkout; push is handled by the PAT, not by `GITHUB_TOKEN`.
- **`fetch-depth: 0`** — ensures the commit exists locally for the tag reference. Shallow clone could miss older commits.

Open implementation detail for the PR: which GPG-import action to pin. Options: `crazy-max/ghaction-import-gpg` (widely used, mature), or a hand-rolled `gpg --import` step (fewer dependencies, slightly more error-prone). The PR will evaluate and SHA-pin whichever is chosen.

### Step 2. `CONTRIBUTING.md` update

Add a new subsection **above** the existing "Cutting a release" block. Structure:

```markdown
### Cutting a release (recommended: workflow-driven)

1. Merge a release PR that bumps `version` in `pyproject.toml`,
   adds the `CHANGELOG.md` entry, and (if relevant) updates the
   trove classifier from Beta → Production/Stable.
2. Go to Actions → "Sign release tag" → "Run workflow".
3. Fill in:
   - `version`: e.g. `1.0.1` (no leading `v`).
   - `commit_sha`: leave blank for main HEAD, or paste a SHA if
     main has moved since the release PR merged.
   - `headline`: short description (e.g. "Sigstore signing
     end-to-end").
4. Click "Run workflow". The `pypi` environment's approval gate
   fires; approve to proceed.
5. The workflow creates and pushes a signed `vX.Y.Z` tag, which
   fires `publish.yml` and runs the existing 3-job pipeline.
6. Approve the `pypi` environment **again** (for `publish.yml`'s
   PyPI job). That final approval ships to PyPI.

### Cutting a release (fallback: local GPG)

Use this path if the workflow's secrets aren't configured yet, or
if you need to tag from a machine-local build for some reason.

<existing 4-step runbook, moved under this header, unchanged>
```

~30 lines added.

### Step 3. Rotation runbook

Append to `CONTRIBUTING.md` after the "Cutting a release" subsections:

```markdown
### Release-signing key + PAT rotation

The dedicated release-signing GPG key and the `RELEASE_TAG_PUSH_TOKEN`
PAT rotate **yearly, or immediately on any suspected exposure**. Rotation
procedure:

1. Generate a new GPG key + PAT (same scopes as the originals).
2. Upload the new public key to your GitHub account's "SSH and GPG
   keys" page. Do NOT delete the old public key yet — existing tag
   signatures reference it.
3. Update `RELEASE_GPG_PRIVATE_KEY`, `RELEASE_GPG_FINGERPRINT`, and
   `RELEASE_TAG_PUSH_TOKEN` secrets in the `pypi` environment.
4. After the next release signs cleanly against the new key,
   revoke the old GPG key (upload the revocation certificate) and
   delete the old PAT.
```

~15 lines added.

## Smoke-test plan

After this PR's implementation merges:

1. Maintainer completes the prerequisite steps (generate key + PAT + secrets), per the Prerequisites section.
2. Dispatch the new workflow against a throwaway version like `0.0.0-wave2-smoke` pointing at main HEAD. Approve the `pypi` environment gate for the **signing workflow** so it can read the release secrets.
3. Verify:
   - Workflow run succeeds end-to-end.
   - Tag `v0.0.0-wave2-smoke` exists on the repo with a GPG "Verified" badge on the tag detail page.
   - `publish.yml` fires on the tag push (check Actions tab).
   - `publish.yml`'s build job succeeds, and the workflow then waits at its gated PyPI publish job.
4. **Do NOT approve `publish.yml`'s PyPI job** for this smoke test. Cancel it or let the gate expire. Critical: the built artifact's version comes from `pyproject.toml` (currently `1.0.0`), NOT from the throwaway tag name, so approving PyPI here would attempt a re-upload of `1.0.0` and fail — or, worse, publish an unintended real version if `pyproject.toml` had been bumped. This smoke test verifies tag-signing + tag-push-triggers-publish only; it must not touch PyPI.
5. Delete the smoke-test tag: `git push --delete origin v0.0.0-wave2-smoke` + delete the Release.
6. Repeat the real RC flow (`v1.0.1-rc0`, with `pyproject.toml` bumped to `1.0.1rc0` first) once Wave 3 docs are ready — that RC exercises the full Sigstore + tag-signing + PyPI path end-to-end.

If anything fails, the failure is in the workflow file or the secrets; fix, push another commit, dispatch again.

## Target diff size

- `.github/workflows/sign-release-tag.yml`: ~70 new lines (single new file).
- `CONTRIBUTING.md`: ~45 new lines (cutover subsection ~30 + rotation runbook ~15), 0 removed (existing runbook stays as the fallback path).

Total: ~115 lines net. One PR.

## Merge-order constraints

- Wave 2 landed after Wave 1 (#108 merged 2026-04-20). No file conflict — Wave 1 only touched `publish.yml`; Wave 2 adds a new workflow file and updates `CONTRIBUTING.md`.
- Wave 3 (verify docs) + Wave 4 (cut 1.0.1) come after Wave 2.

## Success criteria

- After this PR merges + prerequisites completed + smoke test passes: a dispatched `Sign release tag` run creates a Verified signed tag that fires `publish.yml` normally and flows through to a PyPI release.
- `CONTRIBUTING.md` documents the workflow path as recommended, with the local-GPG path as an explicit fallback.
- Rotation runbook is documented alongside the release-cutting subsections.
- No regression in `publish.yml`: an existing local-GPG tag push continues to ship end-to-end unchanged.

## Risks / open

- **GPG-import action choice locks us in.** Picking `crazy-max/ghaction-import-gpg` vs a hand-rolled approach is a one-way door for the maintenance story. The implementation PR validates against a smoke-test RC before the choice ships.
- **PAT expiration silently breaks the workflow.** A rotated PAT must land in the secret before the old one expires, or the `git push` step 401s. Rotation runbook in CONTRIBUTING.md mitigates, but it's operational discipline not automation.
- **Environment-approval UX.** `environment: pypi` on both `sign-release-tag.yml` and `publish.yml` means the maintainer approves twice per release (once to sign+push the tag, once to publish to PyPI). This is intentional (two checkpoints for two distinct concerns) but documented explicitly in the runbook so it's not a surprise.

## Out of scope for this plan

- Code outside `.github/workflows/sign-release-tag.yml` and `CONTRIBUTING.md`.
- Automating PAT rotation (e.g., via GitHub App instead of PAT) — evaluated in the parent plan's Q4 mitigations, rejected as operational overkill for a solo-maintained repo.
- Removing the local-GPG fallback (future, opportunistic docs pass).
- Signing tags on older releases (locked Q6=A: first signed release is 1.0.1; historical tags stay as-is).
- Adding `.github/dependabot.yml` for the new sigstore/GPG-import action pins (tracked out-of-scope on Wave 1 plan).

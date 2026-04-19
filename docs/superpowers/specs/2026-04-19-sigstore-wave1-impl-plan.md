# Implementation plan — Sigstore Wave 1 (Sigstore bundle signing on release artifacts)

**Date:** 2026-04-19
**Milestone:** 1.0.x
**Parent plan:** [2026-04-19-sigstore-signing-plan.md](2026-04-19-sigstore-signing-plan.md) (locked decisions: Q1=A, Q2=A, Q3=B, Q4=B Path 1, Q5=C, Q6=A)
**Parent issue:** [#61](https://github.com/qubitrenegade/clickwork/issues/61)
**Scope:** Wave 1 only — Sigstore bundle signing + PyPI attestations on release artifacts. Waves 2-4 are separate plans/PRs.
**Relevant files:** [.github/workflows/publish.yml](../../../.github/workflows/publish.yml) (current 3-job pipeline), [.github/workflows/release-smoke.yml](../../../.github/workflows/release-smoke.yml) (existing post-release smoke test pattern)

## Goal

Wire Sigstore keyless signing into the existing release pipeline so that `v1.0.1` ships with `.sigstore` bundles on both the GitHub Release assets AND PyPI's attestation endpoint — without changing what a consumer has to do to `pip install clickwork` or re-architecting the 3-job structure.

## Non-goals (Wave 1)

- Tag signing (that's Wave 2 with the `RELEASE_GPG_FINGERPRINT` + `RELEASE_TAG_PUSH_TOKEN` + `RELEASE_GPG_PRIVATE_KEY` secrets flow).
- Verification documentation (that's Wave 3).
- Cutting the actual 1.0.1 release (that's Wave 4).
- Retroactive signing of 1.0.0 or 0.2.x (locked Q6=A).

## Current state

`.github/workflows/publish.yml` is a 3-job pipeline triggered by `push: tags: v*`:

1. `build` (ubuntu-latest, permissions: `contents: read`, `actions: write`) — checkout → install uv → `uv build` → `actions/upload-artifact@v4` uploads entire `dist/` as `dist` artifact.
2. `create-release` (needs: build, permissions: `contents: write`, `actions: read`) — downloads `dist` artifact → `softprops/action-gh-release` (pinned SHA `153bb8e0...` for v2) creates the Release and attaches `dist/*.whl` + `dist/*.tar.gz`.
3. `publish` (needs: create-release, environment: pypi, permissions: `id-token: write`, `actions: read`) — downloads `dist` artifact → `pypa/gh-action-pypi-publish@release/v1` uploads to PyPI via Trusted Publishing.

No signing anywhere. PyPI sees unsigned wheels + sdist; GitHub Release has unsigned assets.

## Scope of this plan

Three concrete changes, one commit per logical change:

1. **`build` job:** grant `id-token: write`, add a Sigstore signing step after `uv build` that signs `dist/*` in place.
2. **`create-release` job:** extend `files:` glob to include `dist/*.sigstore` so bundles appear as Release assets alongside wheel+sdist.
3. **`publish` job:** add `attestations: true` to `pypa/gh-action-pypi-publish`.

All three land in a single PR (small, cohesive diff ~30 lines net).

## Design questions

### Q1. Pin `sigstore/gh-action-sigstore-python` by SHA or moving ref?

- **A) Pin by commit SHA with a trailing `# vX.Y.Z` comment** — matches the pattern already used for `softprops/action-gh-release` in this same workflow. Supply-chain hardened; Dependabot can bump.
- **B) Use the moving `@v3` tag** — shorter, matches the pattern used for `astral-sh/setup-uv@v4` and `pypa/gh-action-pypi-publish@release/v1` in this same workflow. Trusts GitHub's tag immutability (which can be force-pushed by the action author in rare cases).
- **C) Use `@main`** — pinning to upstream HEAD. Definitively rejected: breaks supply-chain guarantees and would fail a reasonable supply-chain audit.

**Recommendation:** A. The existing workflow is already inconsistent (softprops pinned by SHA, others by moving ref). Pinning this addition by SHA nudges the workflow toward the safer pattern without touching the others in this PR. The tradeoff is one extra Dependabot PR per sigstore-action release, which we want anyway.

**Open question for maintainer:** A (SHA pin), or B (match the other unpinned actions for consistency with current style)?

### Q2. How do we smoke-test Wave 1 before cutting 1.0.1?

The workflow only runs on `push: tags: v*`. We need verification it works before committing to 1.0.1.

- **A) Cut a throwaway prerelease tag like `v1.0.1-rc0`** — runs the full pipeline including Trusted Publishing to PyPI. Test appears on PyPI permanently (can yank but not delete). Exercises everything.
- **B) Add a `workflow_dispatch` trigger that skips the `publish` job** — dry-run: build → sign → create release → STOP. Validates the signing path without poking PyPI. Release gets deleted after. New workflow conditional increases complexity.
- **C) Test on a fork with its own PyPI Trusted Publisher** — clean-room, no risk to production. High setup cost and fork drift risk.
- **D) Just ship it on 1.0.1 and roll forward on failure** — cheapest; yanking a failed 1.0.1 and cutting 1.0.2 is tolerable.

**Recommendation:** A with prerelease flag (so it doesn't advance the "latest" pointer). We already have `prerelease: ${{ contains(github.ref_name, '-') }}` in the workflow. A pre-release RC is a real end-to-end test without compromising 1.0.1 itself; if the RC fails we yank and try again without consumer-visible damage. B is tempting but adding a dry-run trigger is a new surface we'd have to maintain.

**Open question for maintainer:** confirm A (ship an `v1.0.1-rc0` first), or D (skip the RC and fix forward)?

### Q3. PyPI attestation edge cases — wheel-only or wheel + sdist?

`pypa/gh-action-pypi-publish` with `attestations: true` publishes to PyPI's attestation endpoint per [PEP 740](https://peps.python.org/pep-0740/). The question is whether it attests both the wheel AND the sdist, or just the wheel.

- **A) Attest both wheel + sdist** — the action's default in recent versions; covers every artifact PyPI serves.
- **B) Attest wheel only** — older versions of the action only attested wheels, since many consumers only pull the wheel. Would require explicit config or an older action.
- **C) Attest neither** — disables `attestations: true`; Wave 1 ships without PyPI attestations (only Release-side bundles). Gives up half of Q3=B's promise.

**Recommendation:** A. The action (recent versions) attests both by default; `.sigstore` bundles for each artifact are published to PyPI's `/simple/` index per PEP 740. Every consumer path (wheel-only install, sdist-only install, or manual tarball) gets attestation coverage.

**Open question for maintainer:** is there a known issue in the sigstore-action ↔ pypa-publish interplay for sdists that you'd want me to hedge around?

## Proposed implementation

### Step 1. `build` job changes

**File:** `.github/workflows/publish.yml`
**Current:**
```yaml
  build:
    name: Build distribution
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 1
      - name: Install uv
        uses: astral-sh/setup-uv@v4
      - name: Set up Python 3.13
        run: |
          uv python install 3.13
          uv python pin 3.13
      - name: Build package
        run: uv build
      - name: Upload distribution artifacts
        uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
```

**Change:**
1. Add `id-token: write` to `permissions:` (required for OIDC → Fulcio).
2. Insert a signing step between `Build package` and `Upload distribution artifacts`:
```yaml
      - name: Sign artifacts with Sigstore
        uses: sigstore/gh-action-sigstore-python@<pinned-sha>  # v3.x
        with:
          inputs: |
            dist/*.whl
            dist/*.tar.gz
```

The action emits `<artifact>.sigstore` bundle files alongside each input in `dist/`. Because `Upload distribution artifacts` uploads the entire `dist/` directory, no change to that step is needed — the bundles come along for free.

**Permissions final shape:**
```yaml
    permissions:
      contents: read
      actions: write
      id-token: write  # NEW: OIDC → Fulcio for keyless signing
```

### Step 2. `create-release` job changes

**File:** `.github/workflows/publish.yml`
**Change:** one-line extension to the `files:` glob in the softprops step:
```yaml
          files: |
            dist/*.whl
            dist/*.tar.gz
            dist/*.sigstore  # NEW: Sigstore bundles as Release assets
```

No permission changes (already has `contents: write`).

### Step 3. `publish` job changes

**File:** `.github/workflows/publish.yml`
**Change:** add `attestations: true` to the pypa-publish step:
```yaml
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          attestations: true  # NEW: PEP 740 attestations via action's OIDC flow
```

No permission changes (`id-token: write` already present for Trusted Publishing).

## Smoke-test plan

Assuming Q2=A (cut an RC):

1. Merge this PR into `main`.
2. Locally: `git tag v1.0.1-rc0 && git push origin v1.0.1-rc0` (signed with the maintainer's local GPG key per current runbook; Wave 2 will move this into the workflow).
3. Watch Actions → verify:
   - `build` job: Sigstore step completes, `dist/*.sigstore` exists in the uploaded artifact.
   - `create-release` job: GitHub Release page shows `.whl`, `.tar.gz`, and `.sigstore` files as assets.
   - `publish` job: PyPI package page for `clickwork 1.0.1rc0` shows "Attestations" section with a `.sigstore` entry per artifact.
4. Manual verify: `sigstore verify identity dist/clickwork-1.0.1rc0-py3-none-any.whl --bundle dist/clickwork-1.0.1rc0-py3-none-any.whl.sigstore --cert-identity https://github.com/qubitrenegade/clickwork/.github/workflows/publish.yml@refs/tags/v1.0.1-rc0 --cert-oidc-issuer https://token.actions.githubusercontent.com`
5. Manual verify: `pypi-attestations verify pypi clickwork==1.0.1rc0`
6. If all green: yank the RC from PyPI, move to Wave 2 without cutting 1.0.1 yet (Wave 2 is a prereq for Wave 4).

If anything fails, the RC is throwaway — yank on PyPI, delete the GitHub Release + tag, fix, re-cut `rc1`.

## Target diff size

- `publish.yml`: +~10 lines net (one new permission, one new step with 5 lines, one `files:` extension with 1 line, one `with:` option with 1 line).
- No new files, no changes to `CONTRIBUTING.md` in Wave 1 (documentation is Wave 3).

## Merge-order constraints

- Wave 1 merges independently of Wave 2 (both touch `.github/workflows/` but non-overlapping files).
- Wave 4 (cut 1.0.1) is gated on Wave 1 merging + the RC smoke-test passing.
- No dependency on #62 (conda-forge).

## Success criteria

- After this PR merges and an RC tag is pushed: a `pypi-attestations verify` (or equivalent) against the PyPI-hosted RC wheel and sdist returns success, using the workflow identity on Fulcio.
- `sigstore verify identity --bundle …--cert-identity … --cert-oidc-issuer …` against the GitHub Release-attached `.sigstore` bundle returns success.
- GitHub Actions for the RC tag shows all three jobs green end-to-end.
- No regression: `publish.yml` continues to successfully upload unsigned consumers through the normal `pip install clickwork==<older version>` path (i.e., existing releases are unaffected).

## Risks / open

- **Sigstore action breakage mid-flow.** The action could fail (upstream regression, Fulcio outage). Mitigation: workflow fails loudly, release doesn't proceed, we retry. No silent failure mode.
- **PyPI attestation endpoint returns unexpected shape for sdist.** Q3's concern. Mitigation: the RC exposes this before 1.0.1.
- **OIDC identity string drift between `refs/tags/v1.0.1-rc0` and `refs/tags/v1.0.1`.** Verifiers need to accept the specific tag form. Mitigation: document the exact identity string in Wave 3's `VERIFYING.md`; downstream verifiers glob on `refs/tags/v*` if needed.

## Out of scope for this plan

- Any code change outside `.github/workflows/publish.yml`.
- Documentation updates in `CONTRIBUTING.md` or `SECURITY.md` (Wave 3).
- Tag signing (Wave 2).
- Release cut (Wave 4).

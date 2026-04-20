# Implementation plan — Sigstore Wave 4 (cut 1.0.1 + exercise the full pipeline)

**Date:** 2026-04-20
**Milestone:** 1.0.x
**Parent plan:** [2026-04-19-sigstore-signing-plan.md](2026-04-19-sigstore-signing-plan.md) (locked Q1–Q6; Q6=A: first signed release is 1.0.1)
**Parent issue:** [#61](https://github.com/qubitrenegade/clickwork/issues/61)
**Scope:** Wave 4 — cut the 1.0.1 release, exercise the full Sigstore + tag-signing + PyPI pipeline end-to-end against a real release. Final wave of the Sigstore work; closes #61.
**Relevant files:** [pyproject.toml](../../../pyproject.toml) (version = "1.0.0" today), [CHANGELOG.md](../../../CHANGELOG.md) (latest entry: 1.0.0 2026-04-18), [.github/workflows/sign-release-tag.yml](../../../.github/workflows/sign-release-tag.yml) (Wave 2, workflow_dispatch), [.github/workflows/publish.yml](../../../.github/workflows/publish.yml) (Waves 1+2, tag-triggered), [CONTRIBUTING.md](../../../CONTRIBUTING.md) (release-cutting runbook), [docs/reference/verifying.md](../../reference/verifying.md) (Wave 3, the verify commands)

## Decisions (locked 2026-04-20)

After review on this PR the maintainer confirmed:

| # | Question | Decision |
|---|---|---|
| Q1 | RC first, or straight to 1.0.1 final? | **B (revised from C)** — cut `v1.0.1` directly as the first real signed release. Q1=C (throwaway `v0.0.0-wave2-smoke` smoke dispatch) was incompatible with the Wave 2 preflight check (`sign-release-tag.yml` enforces `PKG_VERSION == pyproject.toml.version` and a matching CHANGELOG entry — both blocks dispatching any version that doesn't live on main). Rather than weaken the preflight with a smoke-mode bypass, drop the smoke step; the preflight + CHANGELOG + commit_sha validation already provide the safety rails the smoke was supposed to supply |
| Q2 | CHANGELOG 1.0.1 entry framing? | **A** — "Release-infrastructure hardening" headline: Sigstore bundle signing, PEP 740 attestations on PyPI, workflow-driven signed tags, verification docs. No user-facing API changes |
| Q3 | Smoke-test tag name? | **N/A (superseded by Q1=B)** — no smoke step with the revised Q1 |
| Q4 | 1.0.1 release notes body? | **A** — auto-generated from PR labels via `.github/release.yml`, with a documented escape hatch (optional `body:` addition to `publish.yml`'s create-release step) for future releases that want a custom headline |
| Q5 | Gate the release-cut PR on secrets-ready? | **C** — write the PR (version bump + CHANGELOG entry) and leave it open for maintainer review. Maintainer merges + tags in one session when ready. Avoids the awkward "main shows 1.0.1 but PyPI still shows 1.0.0" window |

Implementation waves below assume these decisions are final.

**Supersession note for the 1.0.1 release session:** this Wave 4 plan overrides older in-repo instructions that still mention cutting a prerelease smoke/RC (for example `v1.0.1-rc0` in the earlier Wave 1 plan at `docs/superpowers/specs/2026-04-19-sigstore-wave1-impl-plan.md` or the release runbook's smoke-test references). Follow Wave 4's direct `v1.0.1` flow documented here: the smoke/RC step was dropped because the current Wave 2 preflight blocks off-main or version-mismatched tags, so adding a special smoke release would require weakening those checks or adding bypass-only logic for a one-off path.

## Goal

Ship clickwork 1.0.1 as the first release signed through the full pipeline built in Waves 1–3, and use it to prove the three verify commands documented in `docs/reference/verifying.md` actually work against a real PyPI-hosted release. After this wave, #61 closes.

## Non-goals (Wave 4)

- Adding any new features to clickwork. 1.0.1 is a release-infrastructure release; no src/clickwork/ changes.
- Bumping the trove classifier (already `Development Status :: 5 - Production/Stable` from 1.0.0).
- Hardcoding `__version__` — it's derived from `importlib.metadata` per `src/clickwork/__init__.py`, so the pyproject.toml version is the single source of truth.
- Updating consumer-facing docs that describe feature behavior. Verify docs landed in Wave 3; the 1.0.1 cut exercises them but doesn't modify them.
- Releasing 1.0.2 or later in this wave. If 1.0.1 surfaces issues, fix-forward in a separate cycle.

## Current state

- clickwork 1.0.0 live on PyPI since 2026-04-18.
- `pyproject.toml` version: `1.0.0`.
- `CHANGELOG.md` latest entry: `## [1.0.0] - 2026-04-18`.
- `.github/workflows/publish.yml`: Sigstore signing + PyPI attestations (Wave 1, #108).
- `.github/workflows/sign-release-tag.yml`: `workflow_dispatch`-triggered, gated on `pypi` env, PAT-authenticated push (Wave 2, #110).
- `docs/reference/verifying.md`: three verify-path documentation (Wave 3, #112).
- `CONTRIBUTING.md`: recommended (workflow-driven) + fallback (local-GPG) release runbooks.
- **Prerequisite status**: the three secrets in the `pypi` environment (`RELEASE_GPG_PRIVATE_KEY`, `RELEASE_GPG_FINGERPRINT`, `RELEASE_TAG_PUSH_TOKEN`) are NOT configured yet per the maintainer. Wave 2's implementation doc covers the one-time setup; Wave 4c below gates on completing it.

## Scope of this plan

Three sub-waves — one PR (4a) merged into this release cycle, plus two maintainer-executed steps (4b–4c) at release time:

1. **Wave 4a (PR)**: release-cut PR — bump `pyproject.toml` to `1.0.1`, add `## [1.0.1] - <date>` entry to `CHANGELOG.md` framing release-infra hardening. Left OPEN for maintainer review; merged by the maintainer in the release session.
2. **Wave 4b (maintainer, one-time)**: complete the Wave 2 prereq — generate the dedicated release-signing GPG key, upload public half to GitHub, store the three secrets in the `pypi` environment. Full procedure in [CONTRIBUTING.md](../../../CONTRIBUTING.md#release-signing-key--pat-rotation).
3. **Wave 4c (maintainer, real)**: merge 4a's PR, dispatch `sign-release-tag.yml` with `version=1.0.1`, approve `pypi` twice (once for tag signing, once for publish), verify artifacts on GitHub Release + PyPI, run all three verify commands from `docs/reference/verifying.md` against the real 1.0.1 release.

No smoke-test step: the Wave 2 preflight + CHANGELOG-entry check + commit_sha validation already catch the "tag version doesn't match artifact version" / "CHANGELOG not updated" / "wrong commit tagged" failure modes that a smoke step was meant to surface. See Q1 decision above for the reasoning.

## Proposed implementation

### Wave 4a (PR) — `pyproject.toml` + `CHANGELOG.md`

**Change 1: `pyproject.toml`**

```diff
-version = "1.0.0"
+version = "1.0.1"
```

One line. No classifier change (already `Production/Stable`), no dependency bump.

**Change 2: `CHANGELOG.md`**

Prepend new entry above `## [1.0.0]`:

```markdown
## [1.0.1] - <release-date>

Release-infrastructure hardening. No user-facing API changes; every
consumer who was running 1.0.0 can upgrade to 1.0.1 as a drop-in.

### Added

- **Sigstore keyless signing** of release artifacts (PR #108,
  part of #61). Wheel and sdist are signed inside the `build` job
  of `publish.yml` using `sigstore/gh-action-sigstore-python`; the
  resulting `.sigstore` bundles appear as GitHub Release assets
  alongside the wheel/sdist.
- **PEP 740 attestations on PyPI** (PR #108, part of #61).
  `pypa/gh-action-pypi-publish` now publishes attestations via the
  existing Trusted Publishing OIDC exchange; consumers can verify
  with `pypi-attestations verify pypi clickwork==1.0.1` (see
  [docs/reference/verifying.md](docs/reference/verifying.md)).
- **Workflow-driven signed git tags** (PR #110, part of #61). A
  new `sign-release-tag.yml` workflow signs release tags from a
  dedicated workflow-only GPG key (not the maintainer's personal
  key) with defense-in-depth input validation and a PAT-based push
  that triggers `publish.yml`. The local-GPG fallback path stays
  documented in `CONTRIBUTING.md` for emergencies.
- **Verification documentation** (PR #112, part of #61). New
  [docs/reference/verifying.md](docs/reference/verifying.md) with
  concrete worked examples for all three verify paths, plus
  troubleshooting for common failure modes. Cross-linked from
  `README.md` and `CONTRIBUTING.md`.

### Changed

- **`docs/reference/security.md`** "Verifying release artifacts"
  section (PR #112, part of #61) rewritten from pre-Sigstore
  hash-pinning-only to a summary of the three verify paths with a
  link to `verifying.md` for the full examples. Hash-pinning
  retained as a fallback for pre-1.0.1 releases and
  tooling-unavailable scenarios.
```

The date in `- <release-date>` is filled in at merge time.

**Change 3: Optional — custom release headline escape hatch**

Locked Q4=A ships 1.0.1 with auto-generated release notes only. For future releases that want a custom headline in the GitHub Release body, the escape hatch is `publish.yml`'s create-release step:

```yaml
  - name: Create GitHub Release
    uses: softprops/action-gh-release@<pinned-sha>
    with:
      # ... existing ...
      generate_release_notes: true
      body: |
        <Custom headline paragraph appears ABOVE the auto-generated section.>
      append_body: true  # append the generate_release_notes output
```

This is NOT part of the 1.0.1 release-cut PR — it's documented here as a design note for future tightening. If Wave 4c's auto-generated 1.0.1 release notes look weak, a follow-up PR can add this pattern.

### Wave 4b — one-time secret setup

Full procedure in [CONTRIBUTING.md](../../../CONTRIBUTING.md#release-signing-key--pat-rotation). Summary:

1. Generate passwordless GPG key with 1-year expiry: `gpg --batch --pinentry-mode loopback --passphrase '' --quick-gen-key 'clickwork-release-bot <release@clickwork.invalid>' rsa4096 sign 1y`
2. Upload public half to maintainer's GitHub (Settings → SSH and GPG keys).
3. Export private half + full 40-char fingerprint.
4. Generate fine-scoped PAT with `contents: write` on `qubitrenegade/clickwork` only, 1-year expiry.
5. Store all three as secrets in the `pypi` environment: `RELEASE_GPG_PRIVATE_KEY`, `RELEASE_GPG_FINGERPRINT`, `RELEASE_TAG_PUSH_TOKEN`.

Gates all subsequent Wave 4 steps.

### Wave 4c — cut the real 1.0.1 release

In one maintainer session:

1. Review Wave 4a's PR (the `pyproject.toml` + `CHANGELOG.md` change), fill in `<release-date>` with today, merge it, and copy the merge commit SHA from the merged PR.
2. Go to **Actions → Sign release tag → Run workflow**.
3. Fill in:
   - `version`: `1.0.1`
   - `commit_sha`: paste the merge commit SHA from step 1 so the tag is pinned to the reviewed release-cut commit. (Leaving it blank tags whatever `main` HEAD is at dispatch time, which is fine for a solo maintainer but exposes a race if `main` advances between 4a's merge and the dispatch.)
   - `headline`: a short description — suggested: "Release infrastructure: Sigstore signing, attestations, verify docs."
4. Click Run workflow. Approve the `pypi` environment gate **twice**:
   - First approval: `sign-release-tag.yml` reads the GPG key + PAT, creates signed `v1.0.1` tag, pushes via PAT → fires `publish.yml`.
   - Second approval: `publish.yml`'s PyPI publish job (after build + create-release + sign have succeeded).
5. Verify on GitHub Release page:
   - `v1.0.1` exists with Verified badge.
   - Release assets include `clickwork-1.0.1-py3-none-any.whl`, `clickwork-1.0.1.tar.gz`, and the matching `.sigstore` bundle files.
   - Release notes auto-generated per `.github/release.yml`.
6. Verify on PyPI:
   - `clickwork 1.0.1` visible.
   - Attestations section present.
7. Run all three commands from `docs/reference/verifying.md` against the real 1.0.1:
   - `pypi-attestations verify pypi clickwork==1.0.1` → expect OK.
   - `sigstore verify identity ./clickwork-1.0.1-py3-none-any.whl --bundle ./clickwork-1.0.1-py3-none-any.whl.sigstore --cert-identity https://github.com/qubitrenegade/clickwork/.github/workflows/publish.yml@refs/tags/v1.0.1 --cert-oidc-issuer https://token.actions.githubusercontent.com` → expect OK.
   - `git verify-tag v1.0.1` → expect "Good signature from clickwork-release-bot...".
8. If all three verify: close #61 referencing this wave. If any fail, file a follow-up issue against the specific verify path; 1.0.1 still shipped but the doc/workflow combination has a bug.

## Target diff size

- `pyproject.toml`: 1 line changed.
- `CHANGELOG.md`: ~30 lines added.

Total: Wave 4a's PR is ~31 lines. Waves 4b–4c are maintainer activity, not committed code.

## Merge-order constraints

- Wave 4a depends on Waves 1–3 (all merged: #108, #110, #112). ✓
- Wave 4c gates on Wave 4a merged + Wave 4b complete. All maintainer-side; sequenced in the release session.
- No dependency on #62 (conda-forge). conda-forge's timeline is separate; `v1.0.1` on PyPI is a prerequisite for the conda-forge recipe's initial grayskull generation, but conda-forge submission is on a post-Wave-4 track anyway.

## Success criteria

- `clickwork 1.0.1` on PyPI with PEP 740 attestations visible on the project page.
- GitHub Release `v1.0.1` has `.whl` + `.tar.gz` + `.sigstore` bundles as assets.
- Tag `v1.0.1` has a Verified badge on the tag detail page, signed by the dedicated release-signing GPG key.
- All three verify commands from `docs/reference/verifying.md` pass against the real 1.0.1 artifacts.
- `pyproject.toml` on main: `version = "1.0.1"`.
- `CHANGELOG.md` on main: contains the `## [1.0.1]` entry with the correct release date.
- #61 closed referencing this wave.

## Risks / open

- **Secrets-setup friction delays the cut.** Wave 4b is a one-time ~10-minute task for the maintainer; if GPG keyring or PAT generation trips up, the release slips. Mitigated by the detailed runbook in CONTRIBUTING.md. Any secrets-wiring bug surfaces in 4c's first workflow dispatch with a clear error message from the fingerprint-validation / PAT-empty checks in `.github/workflows/sign-release-tag.yml` (introduced in #110).
- **Auto-generated release notes don't cover all the Sigstore-related PRs.** `#108`/`#110`/`#112` should be labeled `enhancement` or `documentation`; if they landed without labels, they fall under "Other changes" in the auto-generated body. Can be fixed with a post-merge label sweep before Wave 4c, or accepted (the CHANGELOG entry is the authoritative changelog anyway).
- **Verify commands diverge between `docs/reference/verifying.md` (written pre-1.0.1) and reality (observed at 4c).** If any divergence surfaces, file a follow-up against verifying.md. Low-probability since the commands were derived from Wave 1+2 implementation shapes, not guessed.
- **PyPI attestation endpoint is flaky at the moment of publish.** Outside our control; `publish.yml` fails loudly if attestation upload fails. Retry by re-running the publish job (the sign-release-tag workflow doesn't need to re-run).

## Out of scope for this plan

- Retroactively signing 1.0.0 or 0.2.x (locked Q6=A).
- Cutting 1.1.0 or adding features.
- Supplementing release notes with custom headline text on 1.0.1 specifically (Q4=A; escape hatch documented above as a follow-up).

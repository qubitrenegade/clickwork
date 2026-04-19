# Plan — Sigstore signing + workflow-driven tag signing (issue #61)

**Date:** 2026-04-19
**Milestone:** 1.0.x
**Parent issue:** [#61](https://github.com/qubitrenegade/clickwork/issues/61)
**Relevant docs:**
[docs/SECURITY.md](../../SECURITY.md) (current unsigned-release caveats + placeholder verify section),
[.github/workflows/publish.yml](../../../.github/workflows/publish.yml) (current build → create-release → PyPI publish flow)

## Goal

Add Sigstore keyless signing for clickwork's release artifacts (wheel + sdist), publish the resulting `.sigstore` bundles alongside the artifacts on the GitHub Release, move git-tag signing from the maintainer's local GPG key into the release workflow, and document verification commands in `docs/SECURITY.md` so downstream consumers can prove provenance of every 1.0.x release onward.

## Non-goals

- SLSA provenance attestation (separate issue, post-1.0.x).
- Reproducible-build work (out of scope for #61).
- Retroactively signing 0.2.0 or 1.0.0. The first signed release is 1.0.1.

## Current state

Release flow on `main` (from `.github/workflows/publish.yml`):

1. Push tag `v*` → workflow fires.
2. `build` job: `uv build` → `dist/*.whl` + `dist/*.tar.gz` → upload artifact.
3. `create-release` job: download artifact → `softprops/action-gh-release` (pinned to a specific commit SHA that resolves to the v2 line, per supply-chain discipline) creates the Release, attaches the dist files, uses `.github/release.yml` for auto-generated notes.
4. `publish` job: download artifact → `pypa/gh-action-pypi-publish` uploads to PyPI via Trusted Publishing (OIDC).

Tag signing today: maintainer runs `git tag -s vX.Y.Z` locally, which requires `GPG_TTY=$(tty)` export and a GPG key in the maintainer's keyring. See the "Cutting a release" runbook in `CONTRIBUTING.md`. The tag is cryptographically signed but the workflow itself is not involved — future maintainers would need their own GPG key + the same runbook.

`docs/SECURITY.md` has a placeholder "Verifying release artifacts" section with a pip hash-check example and a note that Sigstore signing is planned for 1.0.1 with `cosign verify-blob` / `sigstore-python` as the recommended verify path.

## Scope of this plan

Three pieces, each with one or more open design questions the maintainer needs to answer before implementation:

1. **Sigstore signing of wheel + sdist** (build-job or create-release-job?)
2. **Workflow-driven tag signing** (cosign keyless vs workflow GPG key?)
3. **Verification docs** (inline expansion of SECURITY.md vs new dedicated file?)

## Design questions

### Q1. Which Sigstore action?

- **A) `sigstore/gh-action-sigstore-python`** — official Sigstore action, wraps the `sigstore` Python CLI, produces `.sigstore` bundle files. Maintained by Sigstore Foundation. Works with any artifact type.
- **B) `pypa/gh-action-pypi-publish` with `attestations: true`** — pip-installable packages only; publishes attestations to PyPI's attestation endpoint rather than sideloading bundles. Simpler surface, coupled to PyPI.
- **C) `slsa-framework/slsa-github-generator`** — different tool focused on SLSA provenance rather than Sigstore-bundle sign-everything. Out of scope per non-goals above but worth naming for rejection.

**Recommendation:** A. Gives us standalone `.sigstore` bundles that verify without a PyPI round-trip and works the same for wheel, sdist, or any future artifact (e.g., Docker images if we ever ship one). B is narrower and couples us to PyPI's attestation story.

**Open question for maintainer:** confirm A, or if B's simplicity wins.

### Q2. Where in the workflow does Sigstore run?

- **A) Inside the `build` job** — run `sigstore sign dist/*` right after `uv build`, upload the `.sigstore` bundles as part of the `dist` artifact alongside the wheel and sdist. `create-release` then attaches bundles to the Release as files.
- **B) Inside a new dedicated `sign` job** — between `build` and `create-release`. `build` produces dist, `sign` downloads+signs+uploads a new artifact including bundles, `create-release` and `publish` both download the new artifact.
- **C) Inside `create-release`** — sign just before uploading to the Release.

**Recommendation:** A. One artifact, one OIDC exchange, fewer job boundaries. B adds a deployment stage for no security win (the OIDC token is scoped the same way). C couples signing to release creation which makes it harder to sign a sdist for PyPI that we don't surface on the Release (edge case but still).

**Open question for maintainer:** confirm A, or prefer B's job-level separation.

### Q3. How are `.sigstore` bundles published?

- **A) Release assets only** — bundles live on GitHub Release alongside wheel/sdist. Verification command: download bundle + artifact, `sigstore verify identity ...`.
- **B) PyPI attestations + Release assets** — same as A, PLUS publish to PyPI's attestation endpoint via `pypa/gh-action-pypi-publish` `attestations: true`. Verification command (PyPI side) is manual today via `pypi-attestations verify` or `sigstore-python`; pip/uv auto-verify of PyPI attestations is not GA yet (tracked upstream; once it ships, B upgrades to "automatic on install" for free).
- **C) Release assets + Sigstore transparency log only** — rely on Rekor transparency log + artifact hash for verification, skip bundle files on the Release.

**Recommendation:** B. PyPI attestations give every `pip install clickwork` consumer a future-proof path to automatic verification (the moment pip lands auto-verify, B starts working with zero action from us), while the Release-side bundles cover anyone installing from a tarball or a git tag today. A alone leaves PyPI consumers with no attestation story at all. C alone makes manual verification harder and depends entirely on Rekor uptime.

**Open question for maintainer:** confirm B (bundle + PyPI attestation both).

### Q4. Tag signing mechanism?

- **A) cosign keyless OIDC via `sigstore/cosign-installer` + `cosign sign-blob` on the tag ref** — workflow uses its own OIDC token against Sigstore's Fulcio CA to create an ephemeral signing identity tied to the workflow+repo. No long-lived key to manage. Signature is a separate `.sig` file (or a transparency-log entry on Rekor), NOT a git `-S` signature on the tag itself.
- **B) Workflow-managed GPG key stored as an encrypted secret in the `pypi` environment** — workflow imports the key, runs `git tag -s` inside the workflow. Produces a real GPG-signed git tag that `git verify-tag` checks natively. Long-lived key needs rotation policy, but the tag signature form is the one GitHub's "Verified" badge understands.
- **C) Status quo** — maintainer signs locally with their own GPG key; the workflow stays agnostic. Lose workflow-driven signing as a 1.0.x goal; defer indefinitely.

**Recommendation:** B. GitHub's "Verified" badge on the tag page is what most downstream consumers actually look at. Cosign keyless is a good complement (sign the release artifacts with it per Q1) but isn't recognised as a git tag signature. Managing one long-lived GPG key in the `pypi` environment secret is a known-acceptable operational cost.

**Open concern with B:** the private key material ends up in secrets, which is a step back from the "no long-lived keys" principle that Sigstore is built around. Rotating the key should be part of the runbook.

**Alt: hybrid A+B** — use cosign keyless to produce a Rekor entry referencing the tag SHA (proves the workflow signed something) AND the maintainer continues to `git tag -s` locally. Preserves the "Verified" badge on GitHub, adds a workflow-level artifact. Belt-and-suspenders.

**Open question for maintainer:** B, A, or hybrid A+B? This is the biggest design call in the plan.

### Q5. Verification docs placement?

- **A) Expand the existing "Verifying release artifacts" section in `docs/SECURITY.md`** — one file, cross-references stay simple. File gets longer.
- **B) New `docs/VERIFYING.md`** — dedicated page that SECURITY.md links to. Keeps SECURITY.md focused on threat model.
- **C) Both — brief 3-4-line summary in SECURITY.md, detailed commands in `docs/VERIFYING.md`** — progressive disclosure.

**Recommendation:** C. SECURITY.md readers want the high-level "yes there's a verify path and here's one command," while someone actively verifying wants a dedicated reference. Matches the skill's own progressive-disclosure convention (SKILL.md + references).

**Open question for maintainer:** A, B, or C?

### Q6. Backward compat / retroactive signing?

- **A) No retroactive signing. First signed release is 1.0.1.**
- **B) Retroactively sign 1.0.0 from the tag** — rerun the signing step against the existing `v1.0.0` tag, upload bundles as a second Release asset batch. Viable because `cosign sign-blob` + Sigstore bundles work against any artifact SHA, regardless of when it was built.
- **C) Retroactively sign 0.2.0 too** — same mechanism, older release.

**Recommendation:** A. Keeping a clean "from this version onward, signed" cutline is easier to document and reduces the change surface. B+C open us up to "does the sdist on PyPI for 1.0.0 need to be re-published or just gain a Release-side bundle?" ambiguity.

**Open question for maintainer:** confirm A, or push back for B.

## Proposed implementation waves

Assuming maintainer picks **Q1=A, Q2=A, Q3=B, Q4=B (or hybrid A+B), Q5=C, Q6=A**, the implementation breaks into:

### Wave 1 (PR #a): Sigstore bundle signing on release artifacts

- Add `sigstore/gh-action-sigstore-python@v3` step in the `build` job after `uv build`. Signs `dist/*.whl` and `dist/*.tar.gz`. Produces `.sigstore` bundles in `dist/` alongside the artifacts.
- The current `upload-artifact` step already uploads the entire `dist/` directory, so `.sigstore` files emitted into `dist/` come along automatically — no change to that step needed.
- Extend `softprops/action-gh-release` `files:` glob to include `dist/*.sigstore` so the bundles appear as Release assets.
- In the `publish` job, add `attestations: true` to `pypa/gh-action-pypi-publish`. This handles Q3=B's PyPI attestation side.
- New permissions on the build job: `id-token: write` (for OIDC → Fulcio). Already present on the publish job for Trusted Publishing.

**Target diff size:** ~30 lines of `publish.yml` + workflow smoke-test verification.

### Wave 2 (PR #b): Workflow-driven tag signing

If Q4=B: generate a GPG key, store public half on GitHub user account (for the Verified badge), store private half as secret in the `pypi` environment. Add a pre-build step that imports the key and re-tags the current ref with `git tag -s`. Subtle issue: the workflow sees the tag that fired it, not a mutable HEAD — retagging during the same workflow run is awkward. Alternative: a separate `tag-signing` workflow that runs on `workflow_dispatch`, signs a future tag in advance.

If Q4=A: add `cosign-installer` action, `cosign sign-blob` against the tag SHA, upload result as a Rekor entry. Simpler, no secrets to manage, but no Verified badge.

If Q4=hybrid: just do A for the workflow (cosign blob signing) and keep the CONTRIBUTING runbook for the maintainer's local-GPG tag signing. Simplest landing for 1.0.1 while leaving the B path open for later.

**Target diff size:** depends on Q4 — hybrid is ~15 lines, full B is ~60 lines + secrets-setup documentation.

### Wave 3 (PR #c): Verification docs

Assuming Q5=C:

- `docs/VERIFYING.md`: concrete commands for (1) verifying PyPI attestations for a downloaded wheel/sdist — as of early 2026 this is a manual step via the `pypi-attestations` CLI or `sigstore-python`; pip's own auto-verification of PyPI attestations is not yet GA, so we document the manual flow until it is, (2) verifying a downloaded wheel/sdist against its Release-attached `.sigstore` bundle via `sigstore verify identity --cert-identity <workflow-identity> --cert-oidc-issuer https://token.actions.githubusercontent.com`, (3) verifying the tag via `git verify-tag` (if Q4=B) or the Rekor transparency-log entry (if Q4=A or hybrid).
- Update `docs/SECURITY.md` "Verifying release artifacts" section to a short summary + link to `VERIFYING.md`.
- Cross-link from `CONTRIBUTING.md`'s "Cutting a release" runbook.

**Target diff size:** ~100 lines new docs, ~10 lines updates.

### Wave 4 (PR #d): Cut 1.0.1 to exercise the flow

Release-cut PR (version bump + CHANGELOG 1.0.1 entry). Maintainer tags+pushes, the new `publish.yml` fires end-to-end, Sigstore bundles land on the Release, attestations show on PyPI, verification commands in `VERIFYING.md` work against the real 1.0.1 artifacts. Any bugs that surface get fixed in a follow-up PR before the pattern is documented as "this is how we ship."

**Target diff size:** minimal — version + changelog + follow-up fixes as needed.

## Merge-order constraints

- Wave 1 must land before Wave 4 (can't cut 1.0.1 unsigned with the unsigned publish flow).
- Wave 2 can land in parallel with Wave 1 if hybrid-A.
- Wave 3 can land in parallel with Waves 1+2 (docs). Arguably easier to write the verify docs AFTER we've seen the actual bundle shapes, so schedule Wave 3 last-or-parallel but not first.

## Success criteria

- `publish.yml` on main ends with: PyPI upload attested, Release has `.sigstore` bundles as assets, tag verified (by whichever of A/B/hybrid we pick in Q4).
- `docs/VERIFYING.md` documents three concrete verification paths: PyPI attestation, bundle-side, tag-side.
- `pypi-attestations verify` (or equivalent client) against a PyPI-hosted clickwork 1.0.1 wheel returns success, using the workflow identity on Fulcio. `pip install` itself does not yet auto-verify PyPI attestations (pending upstream work); the success criterion here is "attestations are published and manually verifiable," not "pip blocks unverified installs."
- A fresh consumer running `sigstore verify identity dist/clickwork-1.0.1-py3-none-any.whl --bundle dist/clickwork-1.0.1-py3-none-any.whl.sigstore --cert-identity <workflow-identity> --cert-oidc-issuer https://token.actions.githubusercontent.com` against the Release-attached bundle gets a pass.

## Out of scope for this plan

- SLSA provenance attestation (separate follow-up after 1.0.x stabilises).
- Signing orbit-admin's clickwork dependency. Downstream concern for orbit-admin's own release workflow.
- Mirroring signed bundles to conda-forge (conda-forge has its own signing story via #62).

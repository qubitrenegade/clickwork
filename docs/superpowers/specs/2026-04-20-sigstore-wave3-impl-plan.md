# Implementation plan — Sigstore Wave 3 (verification docs)

**Date:** 2026-04-20
**Milestone:** 1.0.x
**Parent plan:** [2026-04-19-sigstore-signing-plan.md](2026-04-19-sigstore-signing-plan.md) (locked Q5=C: short summary in `docs/SECURITY.md` + detailed dedicated verification doc)
**Parent issue:** [#61](https://github.com/qubitrenegade/clickwork/issues/61)
**Scope:** Wave 3 only — user-facing verification documentation. Builds on Wave 1 (#108 Sigstore + PEP 740 attestations) and Wave 2 (#110 workflow-driven signed tags). Wave 4 (cut 1.0.1) is a separate plan.
**Relevant files:** [docs/reference/security.md](../../reference/security.md) (existing 1-line "Verifying release artifacts" section, last ~25 lines), [docs/SECURITY.md](../../SECURITY.md) (redirect to reference/security.md), [.github/workflows/publish.yml](../../../.github/workflows/publish.yml) (Wave 1 output: Sigstore + attestations), [.github/workflows/sign-release-tag.yml](../../../.github/workflows/sign-release-tag.yml) (Wave 2 output: signed tags)

## Goal

Ship concrete, copy-pasteable verification commands for every provenance path clickwork now supports:

1. **PyPI attestation** (PEP 740) — `pypi-attestations verify` against an installed wheel or sdist.
2. **Sigstore bundle on the GitHub Release** — `sigstore verify identity` against a downloaded `.sigstore` bundle + artifact.
3. **Signed git tag** — `git verify-tag vX.Y.Z` against the workflow-signed tag.

The current `security.md` "Verifying release artifacts" section is a placeholder that predates Waves 1+2; it documents a `pip --require-hashes` flow only and notes that Sigstore is "planned for 1.0.1." After this wave the docs catch up to reality.

## Non-goals (Wave 3)

- Cutting 1.0.1 (Wave 4).
- Automating verification in CI — the verify flow is consumer-side, we document the commands they run.
- Building a verify-my-install helper script shipped inside `clickwork` itself (separate follow-up issue if we ever want one).
- Explaining Sigstore's transparency-log (Rekor) internals — the `sigstore verify identity` command does the right thing; deep Rekor mechanics are upstream doc surface.

## Current state

- `docs/reference/security.md` lines 215–238 document only the hash-pinning verify path (pre-Sigstore). It still describes Sigstore as "planned," which is now stale even though the issue reference points to active tracker #61.
- `docs/SECURITY.md` is a 1-line redirect to `reference/security.md`.
- No dedicated verify doc exists.
- Wave 1 produces `.sigstore` bundles on the Release + PEP 740 attestations on PyPI.
- Wave 2 produces signed annotated tags `vX.Y.Z` verifiable via `git verify-tag`.
- No CONTRIBUTING.md cross-link to consumer-side verify (CONTRIBUTING.md covers the maintainer side — how to *cut* a release, not how to *verify* one).

## Scope of this plan

Five deliverables:

1. **New file** `docs/reference/verifying.md` — the canonical "how to verify a clickwork release" page. Three concrete command blocks (one per verify path), plus a short "troubleshooting" subsection.
2. **New file** `docs/VERIFYING.md` (top-level) — 1-line redirect stub pointing at `reference/verifying.md`. This matches the existing `docs/SECURITY.md` → `reference/security.md` pattern and honors parent plan #97's Q5=C wording which named the detailed doc as "`docs/VERIFYING.md`".
3. **`mkdocs.yml` updates** — add `VERIFYING.md` to `exclude_docs`, add `VERIFYING.md: reference/verifying.md` to `plugins.redirects.redirect_maps`, add `Verifying: reference/verifying.md` to `nav.Reference`. Without these, RTD's `fail_on_warning` build breaks on (a) the new orphan page in `docs/` and (b) the `reference/verifying.md` page not being in nav.
4. **Rewrite** `docs/reference/security.md` lines 215–238 from hash-pinning-focused to a short "Verifying release artifacts" summary that names the three paths and links to `verifying.md`.
5. **Cross-link** from `README.md` (install section mentions the verify doc) and `CONTRIBUTING.md` (release-cutting runbook references the verify doc for consumer-side expectations).

## Design questions

### Q1. File path for the verify doc?

clickwork's `docs/` tree has `reference/`, `how-to/`, `tutorials/`, `explanation/` (Diátaxis-style structure). Parent plan #97's locked Q5=C specifically named `docs/VERIFYING.md` (top-level), but `docs/SECURITY.md` is itself a redirect-stub to `docs/reference/security.md`, so "top-level path" has precedent for being a redirect rather than the real content.

- **A) `docs/reference/verifying.md` for real content + top-level `docs/VERIFYING.md` redirect stub** — matches the existing `SECURITY.md` → `reference/security.md` pattern. Honors parent plan's "docs/VERIFYING.md" wording (the top-level path exists) while keeping the real content beside `security.md` in `reference/`.
- **B) `docs/how-to/verify-a-release.md`** — Diátaxis says "how-to" is for goal-oriented recipes, which fits verification perfectly. Diverges from parent plan.
- **C) Top-level `docs/VERIFYING.md` with no redirect** — literal reading of parent plan. Inconsistent with how `security.md` is placed (real content lives in `reference/`).

**Recommendation:** A. The `SECURITY.md` → `reference/security.md` redirect pattern is already the project's established convention; applying it to VERIFYING.md keeps docs/reference/ as the home for detailed reference pages while the top-level path still resolves for anyone following the parent plan's wording or wanting a short URL.

**Open question for maintainer:** confirm A (redirect + reference/ content), or push back toward B (how-to) or C (top-level only)?

### Q2. Depth of worked examples — templates vs specific version?

- **A) Specific-version worked examples using `1.0.1` as the placeholder** — e.g., `sigstore verify identity dist/clickwork-1.0.1-py3-none-any.whl ...`. Concrete, copy-paste-friendly.
- **B) Template with `<version>` substitution markers** — e.g., `sigstore verify identity dist/clickwork-<version>-py3-none-any.whl ...`. Less visual noise, reader has to substitute.
- **C) Both — worked example first, template underneath** — verbose but maximally clear.

**Recommendation:** A with a single-sentence "substitute your target version" note above. Worked examples read faster; the reader sees the real shape of the command. The "1.0.1" placeholder is the first signed release anyway, so it's real-ish.

**Open question for maintainer:** confirm A (worked examples), or B (templates)?

### Q3. How explicit about the "pip/uv auto-verify not yet GA" caveat?

PyPI's PEP 740 attestations are published by our Wave 1 changes, but `pip install --verify-attestations` (or equivalent) is not yet GA in pip or uv at time of writing. Consumers today run `pypi-attestations verify` manually.

- **A) One-paragraph "note" box at the top of the PyPI-attestation section** — explicit about the "manual verify today, auto verify when installers ship it" transition.
- **B) One sentence inline in the PyPI-attestation section** — less prominent, still honest.
- **C) No explicit note — just document the `pypi-attestations verify` command as THE way** — cleaner doc, but readers who assume `pip install` already verifies will be surprised later.

**Recommendation:** A. The "manual vs auto-verify" distinction is a real gotcha that will catch readers who assume the attestation does auto-magic. Being explicit up front saves a confused-user-opens-issue cycle.

**Open question for maintainer:** A (note box), or B (single sentence)?

### Q4. Cross-references from README + CONTRIBUTING?

- **A) README.md install section adds a one-line "Verify your install: see docs/reference/verifying.md"** — visible to first-time installers.
- **B) CONTRIBUTING.md "Cutting a release" section adds a closing note pointing at the verify doc for consumer expectations** — visible to release-cutters thinking about what consumers will do.
- **C) Both A + B** — redundant but each catches a different audience.

**Recommendation:** C. Low-cost, high-coverage. README reaches consumers. CONTRIBUTING reaches maintainers. They're different audiences with different needs for the same doc.

**Open question for maintainer:** confirm C (both), or just A (README only — consumers are the primary audience)?

## Proposed implementation

### Step 1. `docs/reference/verifying.md` (new file, ~120 lines)

Structure:

```markdown
# Verifying a clickwork release

Every release from 1.0.1 onward is provenance-protected three ways:
the PyPI package carries PEP 740 attestations, the GitHub Release
carries Sigstore `.sigstore` bundles, and the git tag is GPG-signed
(workflow key by default; maintainer key in fallback). Pick whichever
verify path matches how you installed.

(Examples below use `1.0.1` as the target version — substitute the
version you installed.)

## Verifying the PyPI package

> **Note:** pip's built-in auto-verify of PEP 740 attestations is
> not yet GA. Today, verification is a manual step via the
> `pypi-attestations` CLI. When installers ship auto-verify, this
> section will update to reference the flag.

Install `pypi-attestations` in a scratch venv:

    pip install pypi-attestations

Verify the attestations for an installed clickwork:

    pypi-attestations verify pypi clickwork==1.0.1

Expected output: "OK" per artifact, with the workflow identity
(`https://github.com/qubitrenegade/clickwork/...@refs/tags/v1.0.1`)
named.

## Verifying a GitHub Release asset

Download the wheel (or sdist) + its `.sigstore` bundle from the
Release page. Install the `sigstore-python` CLI from PyPI
(`sigstore`):

    pip install sigstore

Verify:

    sigstore verify identity \
      dist/clickwork-1.0.1-py3-none-any.whl \
      --bundle dist/clickwork-1.0.1-py3-none-any.whl.sigstore \
      --cert-identity https://github.com/qubitrenegade/clickwork/.github/workflows/publish.yml@refs/tags/v1.0.1 \
      --cert-oidc-issuer https://token.actions.githubusercontent.com

(Repeat with the sdist if you pulled the sdist.)

Expected output: "OK: dist/clickwork-1.0.1-py3-none-any.whl"

## Verifying the git tag

    git verify-tag v1.0.1

Expected output: "Good signature from clickwork-release-bot
<release@clickwork.invalid>" (or whatever the dedicated
release-signing key's UID shows).

The public half of the release-signing key is published on the
maintainer's GitHub account (Settings → SSH and GPG keys), which is
what gives signed tags a green "Verified" badge on the tag detail
page.

## Troubleshooting

### "no such file or directory" for the `.sigstore` bundle

Releases before 1.0.1 were not signed. If the Release page has no
`.sigstore` files, the verify path is unavailable and you should
either upgrade to 1.0.1+ or fall back to the hash-pinning verify
path documented in `security.md`.

### `pypi-attestations` reports no attestations

Check you're on 1.0.1 or later: `pip show clickwork`. Attestations
start with 1.0.1.

### `git verify-tag` says "Can't check signature: No public key"

The local-GPG fallback path (documented in `CONTRIBUTING.md`) was
used for this release. The tag is still signed, but with the
maintainer's personal key rather than the workflow key. Either tag
signature verifies via `git verify-tag`; this message means your
local GPG keyring doesn't have the signer public key yet. Fetch it:

    gpg --keyserver keys.openpgp.org --recv-keys <fingerprint-from-tag-page>

If `git verify-tag` instead reports "no signature", the tag is
unsigned and this fallback flow does not apply.

## See also

- [security.md](security.md) — threat model + hash-pinning fallback
  verify path for pre-1.0.1 releases.
- [CONTRIBUTING.md — Cutting a release (recommended: workflow-driven)](https://github.com/qubitrenegade/clickwork/blob/main/CONTRIBUTING.md#cutting-a-release-recommended-workflow-driven) — how the release-signing machinery works from the maintainer side. (Absolute GitHub URL — `CONTRIBUTING.md` isn't published under `docs/` and a relative link would break RTD's `fail_on_warning` build.)
- Parent issue: [#61](https://github.com/qubitrenegade/clickwork/issues/61).
```

### Step 2. `docs/VERIFYING.md` (top-level redirect stub, ~1 line)

Mirrors `docs/SECURITY.md`:

```markdown
> This page has moved to [reference/verifying.md](reference/verifying.md).
```

Exists so the parent-plan-named path (`docs/VERIFYING.md`) resolves for anyone who reads the parent plan directly, and so future cross-links can use either the short top-level path or the full `reference/` path.

### Step 3. `mkdocs.yml` updates (~6 lines)

Three changes to keep the RTD `fail_on_warning` build green:

1. Add `VERIFYING.md` to `exclude_docs` (top-level stub is a redirect, not a published page):

       exclude_docs: |
         # ... existing entries ...
         VERIFYING.md

2. Add a redirect entry in `plugins.redirects.redirect_maps`:

       redirect_maps:
         # ... existing entries ...
         VERIFYING.md: reference/verifying.md

3. Add `Verifying: reference/verifying.md` to `nav.Reference` (otherwise the new page is an orphan and `strict`/RTD builds warn):

       nav:
         # ...
         - Reference:
             # ... existing entries ...
             - Verifying: reference/verifying.md

### Step 4. Rewrite `docs/reference/security.md` "Verifying release artifacts" section (~25 lines → ~15 lines)

Replace the existing hash-pinning-focused paragraph with:

```markdown
## Verifying release artifacts

Every release from 1.0.1 onward can be verified three ways:

1. **PyPI attestation** (PEP 740): `pypi-attestations verify pypi clickwork==<version>`
2. **Sigstore bundle** (GitHub Release asset): `sigstore verify identity <wheel> --bundle <wheel>.sigstore --cert-identity <workflow-url> --cert-oidc-issuer https://token.actions.githubusercontent.com`
3. **Signed git tag**: `git verify-tag v<version>`

See [verifying.md](verifying.md) for full worked examples + troubleshooting.

For pre-1.0.1 releases (no signing) or if the verify tooling is
unavailable, pin by hash:

<retain the existing requirements.txt + pip --require-hashes + uv.lock paragraphs, about 15 lines>
```

### Step 5. README cross-link

Append a short note to the existing `## Installation` section (the actual heading — not "Install"). The existing section already contains the `uv pip install "clickwork>=1.0,<2"` block and surrounding prose; we add a new trailing paragraph.

`README.md` is PyPI's long-description per `pyproject.toml` (`readme = "README.md"`), so relative `docs/...` links render broken on the PyPI project page. The existing README already uses absolute `clickwork.readthedocs.io` URLs for docs references — match that pattern:

    **Verifying your install:** see
    <https://clickwork.readthedocs.io/en/latest/reference/verifying/>
    for the three verify paths — PyPI attestation, Sigstore bundle,
    signed tag.

~3 lines added.

### Step 6. CONTRIBUTING.md cross-link

At the end of the "Cutting a release (recommended: workflow-driven)" subsection, add:

```markdown
Consumers verify the release using the commands in
[`docs/reference/verifying.md`](docs/reference/verifying.md).
Running those commands against the RC tag during smoke-test
(see Wave 2 plan) catches most pipeline bugs before 1.0.1 ships.
```

~4 lines added.

## Smoke-test plan

- After this PR merges: render the new `verifying.md` in the published docs site (`clickwork.readthedocs.io` per `mkdocs.yml`), confirm links resolve.
- Dry-run the three verify commands against an existing test RC if Wave 2 produced one. Document any command-string glitches.
- Do NOT ship 1.0.1 yet — Wave 4 handles that, and its smoke-test explicitly runs these verify commands against the first real signed release.

## Target diff size

- `docs/reference/verifying.md`: ~120 new lines (real content).
- `docs/VERIFYING.md`: ~1 new line (redirect stub, mirrors `SECURITY.md`).
- `mkdocs.yml`: ~6 lines added (exclude_docs entry, redirect_maps entry, nav entry).
- `docs/reference/security.md`: ~25 lines removed, ~15 added (net −10).
- `README.md`: ~3 lines added.
- `CONTRIBUTING.md`: ~4 lines added.

Total: ~135 lines net, 2 new files.

## Merge-order constraints

- Wave 3 depends on Wave 1 (#108) + Wave 2 (#110) having merged — otherwise we'd document commands that verify things that don't exist yet. Both merged 2026-04-20. ✓
- Wave 4 (cut 1.0.1) is gated on Wave 3 merging + the verify docs being live, so a 1.0.1-release blog post or tweet can link to `verifying.md` without circular dependency.
- No dependency on #62 (conda-forge).

## Success criteria

- `docs/reference/verifying.md` exists and renders on the docs site.
- `docs/reference/security.md` "Verifying release artifacts" section no longer references Sigstore as "planned" — it points at the three live verify paths with a link to the detailed doc.
- README + CONTRIBUTING cross-links resolve to `verifying.md`.
- A fresh reader can run all three verify commands against the next signed release (Wave 4) without ambiguity.

## Risks / open

- **Identity-string drift between spec + reality.** The `--cert-identity` string in the Sigstore verify command is workflow-path-sensitive — if we ever rename `publish.yml` the string breaks. Mitigation: `verifying.md` mentions the identity-string is "the workflow URL" and shows the current shape, not a regex match — users who copy-paste are fine, but a future `publish.yml` rename will drift the doc. Flagged as a maintenance note in `verifying.md`.
- **`pypi-attestations verify` command syntax changes.** The CLI is early-stage (0.0.x at time of writing). Version-pinning the install recommendation (`pip install pypi-attestations==X.Y.Z`) could help future-proof, but we'd need to bump with its releases. Open to maintainer preference.
- **Stale "planned" language elsewhere.** Besides `security.md`, the phrase "Sigstore planned" may appear in other docs. Sweep as part of the implementation PR's general doc cleanup.

## Out of scope for this plan

- Shipping `clickwork verify` subcommand (separate feature, not signing-workflow).
- Deep Rekor transparency-log walkthrough (upstream Sigstore docs do this).
- Automating verify in CI on the consumer side (tool choice lives in the consumer's hands).
- Retroactively signing older releases (locked Q6=A: no retroactive).

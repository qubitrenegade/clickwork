# Plan — conda-forge recipe submission (issue #62)

**Date:** 2026-04-19
**Milestone:** post-1.0
**Parent issue:** [#62](https://github.com/qubitrenegade/clickwork/issues/62)
**Relevant docs:** [README.md](../../../README.md) install section, [pyproject.toml](../../../pyproject.toml) (input to `grayskull pypi clickwork`, which produces a first-pass `meta.yaml` we hand-edit; conda-forge itself doesn't auto-generate from pyproject.toml)

## Decisions (locked 2026-04-19)

After review on this PR the maintainer confirmed:

| # | Question | Decision |
|---|---|---|
| Q1 | When do we submit? | **C** — wait ~2 weeks after 1.0.0 release (starting ~2026-05-03) to confirm no PyPI-side bugs in the wild before entering staged-recipes review |
| Q2 | Who maintains the feedstock? | **B** — clickwork maintainer + request a community co-maintainer from conda-forge in the staged-recipes PR (standard for first-timers, zero cost, rotates off after a few cycles) |
| Q3 | Python version range in recipe? | **A** — mirror PyPI pin exactly: `python >=3.11` (no invented upper bound; per-version test confidence stays with GitHub Actions CI) |
| Q4 | Build system declaration? | **A + B** — `noarch: python` with `{{ PYTHON }} -m pip install .` build script, plus explicit `host:` requirements naming `python`, `pip`, `hatchling` |
| Q5 | conda install command wording in README? | **A** — one line alongside existing pip/uv commands, no separate subsection |
| Q6 | Staged-recipes PR shape? | **A + C** — one focused PR, one recipe, use `grayskull pypi clickwork` to generate the initial draft then hand-edit for maintainers list, home URL, etc |

Implementation waves below assume these decisions are final.

## Goal

Submit a `meta.yaml` recipe for clickwork to the [conda-forge/staged-recipes](https://github.com/conda-forge/staged-recipes) repo, get it merged, and confirm the resulting `clickwork-feedstock` publishes to conda-forge so users can `conda install -c conda-forge clickwork`. Document the install path in the README alongside `pip` / `uv`.

## Non-goals

- Signing conda-forge artifacts — conda-forge has its own signing story (feedstock bot + channel signing) which we inherit rather than run ourselves.
- Publishing to a conda channel other than conda-forge (e.g., bioconda, our own channel). Not a goal unless the audience materialises.
- Cross-distro binary builds — clickwork is pure Python, noarch; conda-forge handles this trivially.

## Current state

- clickwork 1.0.0 live on PyPI (`pip install clickwork`).
- Pure-Python package (no C extensions, no binary deps). Python >=3.11, `click>=8.2`.
- Not currently on any conda channel.
- PyPI metadata (`pyproject.toml`) is comprehensive: name, version, description, authors, license file, classifiers, dependencies, URLs. This is the input conda-forge's grayskull tool turns into a `meta.yaml`.

## Scope of this plan

1. Generate an initial `meta.yaml` for clickwork.
2. Open a PR against `conda-forge/staged-recipes`.
3. Respond to review from conda-forge maintainers until merged.
4. Verify the bot-generated `clickwork-feedstock` repo exists and publishes successfully.
5. Update clickwork's README with the conda install command.
6. First-month follow-up: respond to the first auto-generated feedstock maintenance PR (dep updates, etc) to learn the maintainer workflow.

## Design questions (resolved — kept for historical context)

The A/B/C alternatives below were the options considered; each has a **Decision:** line pointing at the locked choice from the table above. Left in the doc so future readers can see what was weighed and why.

### Q1. When do we submit?

- **A) Submit immediately** — 1.0.0 went live on 2026-04-19 (same day as this plan). No reported bugs yet, but correspondingly no real in-the-wild usage yet either.
- **B) Wait for 1.0.1** — give Sigstore work (#61) time to land first, so the conda-forge recipe references an already-verified PyPI release. conda-forge bot pulls the sdist from PyPI and verifies against its hash; Sigstore bundles are a separate layer conda-forge doesn't use directly, so there's no hard dependency.
- **C) Wait 1-2 weeks of real PyPI usage first** — the #62 issue itself says "wait until 1.0 is stable on PyPI (a week or two) before submitting to staged-recipes so we aren't iterating on a moving target inside the conda-forge review process." This matches the issue author's original intent.

**Decision: C.** Per the issue's own guidance. staged-recipes review can take 1-4 weeks; starting too early means the recipe might need republishing to 1.0.1 mid-review if a PyPI-side bug surfaces.

### Q2. Who maintains the feedstock?

Once staged-recipes merges, conda-forge bot creates `conda-forge/clickwork-feedstock`. That repo has `recipe-maintainers:` listed in its `meta.yaml` — they get auto-mentioned on every dep update PR, new-version PR, etc.

- **A) Just the clickwork maintainer (qubitrenegade)** — minimal list. Reliable but bus-factor = 1.
- **B) clickwork maintainer + a conda-forge "community maintainer"** — conda-forge often assigns one of their own to help with the first few PRs until the project-side maintainer is familiar. Ask for this in the staged-recipes PR.
- **C) clickwork maintainer + a named backup from elsewhere in our org / collaborators** — requires finding a willing second.

**Decision: B.** Standard conda-forge pattern for first-time submitters. The community maintainer is a volunteer from the conda-forge organisation — no hiring, no cost, and no commitment burden on anyone in our circle. They rotate off after a few cycles once the clickwork maintainer is familiar with the feedstock maintenance workflow. Request is made in the staged-recipes PR body (e.g., "Requesting a community co-maintainer per the first-time-submitter convention; `@conda-forge/help-python` tag when appropriate").

### Q3. Python version range in the recipe?

clickwork requires Python >=3.11 per `pyproject.toml`. conda-forge recipes specify a pin like `python >=3.11`.

- **A) Mirror the PyPI pin exactly: `python >=3.11`**
- **B) Also cap at a known-good upper bound: `python >=3.11,<3.14`** — conda-forge sometimes asks for an upper bound to prevent unexpected breakage on new Python releases.
- **C) `python >=3.11` + an explicit test matrix for 3.11, 3.12, 3.13** — mostly symbolic for a `noarch: python` recipe, since noarch builds once on a single Python and relies on import-time compatibility rather than per-version CI.

**Decision: A.** Matches PyPI, matches our documented policy, doesn't invent a cap we don't actually have evidence for. For `noarch: python` recipes the feedstock runs the test section once on a single migrator-selected Python — we don't get a free per-version test matrix from conda-forge here. If we want per-version test confidence, that's already our GitHub Actions CI's job on the PyPI side, not the feedstock's.

### Q4. Build system declaration?

conda-forge needs to know how to build clickwork. We use `hatchling` via `pyproject.toml`.

- **A) `noarch: python` + `{{ PYTHON }} -m pip install .` in the build script** — standard pure-Python recipe shape. Works for hatchling projects out of the box.
- **B) Explicit `host:` requirement naming the concrete conda packages: `python`, `pip`, `hatchling`** (these are the actual conda-forge package names for the build toolchain; `python-build-backend` is not a conda package). More explicit; staged-recipes reviewers typically ask for it.
- **C) `pyproject.toml`-native build via conda-forge's `python-build` helper** — newer pattern, cleaner recipe, but support matrix is narrower.

**Decision: A + B.** `noarch: python` with `{{ PYTHON }} -m pip install .` plus explicit `host:` requirements naming `python`, `pip`, `hatchling`. grayskull generates this shape by default. Matches what other modern pure-Python packages in staged-recipes look like and survives staged-recipes reviewers' typical requests.

### Q5. conda install command wording in README?

Once the feedstock publishes:

- **A) Add a `conda install -c conda-forge clickwork` line alongside the existing pip/uv commands, same install section, no prose change.**
- **B) Separate subsection for conda** — "## Install via conda" — lets us explain channel pinning and caveats briefly.
- **C) One-liner in the existing install block + a brief footnote** — progressive disclosure.

**Decision: A.** The existing install section is already short and channel-hopping is not something clickwork users need to think about. Less prose = less rot.

### Q6. Staged-recipes PR shape?

staged-recipes expects one recipe per PR, with `recipes/<package>/meta.yaml` and usually a `LICENSE.txt` copy inside the recipe folder.

- **A) One PR, one recipe, no bells and whistles.**
- **B) Bundle with related-ecosystem packages if we plan to submit more soon.** — not applicable; no other packages in flight.
- **C) Use `grayskull` to auto-generate first draft, hand-edit minimally.** — grayskull is the conda-forge-recommended tool for turning PyPI metadata into a starting-point recipe. Worth using.

**Decision: A + C.** Use `grayskull pypi clickwork` for the initial draft, hand-edit for maintainers list + home URL + any version-pinning tweaks, open one focused PR against `conda-forge/staged-recipes`.

## Proposed implementation waves

Based on the locked decisions above — **Q1=C, Q2=B, Q3=A, Q4=A+B, Q5=A, Q6=A+C**:

### Wave 0 (local prep, no PR on clickwork)

- Wait ~2 weeks after the 1.0.0 release (so starting roughly 2026-05-03 given 1.0.0 shipped 2026-04-19) to confirm no PyPI-side bugs in the wild.
- In the meantime: install grayskull locally, run `grayskull pypi clickwork`, review the generated `meta.yaml`, hand-edit as needed for our specific needs (maintainers list, home URL, etc).

### Wave 1 (PR on conda-forge/staged-recipes, not clickwork)

- Fork `conda-forge/staged-recipes`, add `recipes/clickwork/meta.yaml` + `recipes/clickwork/LICENSE.txt`, open PR against staged-recipes.
- In the PR body, request a community co-maintainer per Q2.
- Respond to conda-forge reviewer feedback. Typical turnaround: 1-4 weeks for first-time submitters.
- When merged, the conda-forge bot creates `conda-forge/clickwork-feedstock` and builds the first release.

### Wave 2 (PR on clickwork/main)

- Update `README.md` install section with `conda install -c conda-forge clickwork` per Q5.
- Update `docs/GUIDE.md` install section similarly.
- Small PR, docs-only.
- Gated on Wave 1 being fully merged + the feedstock actually publishing (check `https://anaconda.org/conda-forge/clickwork` for a non-404 response).

### Wave 3 (follow-up, no scheduled PR)

- First time the feedstock bot opens a maintenance PR on `clickwork-feedstock` (dep bump, rebuild, etc), the clickwork maintainer should walk through the merge flow once to learn the shape.
- Document the feedstock maintenance pattern in `docs/superpowers/specs/` or `CONTRIBUTING.md` if it's worth preserving for future maintainers.

## Merge-order constraints

- Wave 2 (README update) CANNOT land until Wave 1's staged-recipes PR merges AND `clickwork-feedstock` actually publishes. Otherwise we document an install command that doesn't work yet.
- Wave 0/1 have no constraint on clickwork's repo state beyond "1.0.0 is released to PyPI" (satisfied today) AND "has 1-2 weeks of in-the-wild usage without regressions" (Q1's "stable" bar — NOT yet satisfied; that's what Wave 0's waiting period is for).
- No dependency on #61 Sigstore work — conda-forge has its own signing chain.

## Success criteria

- `conda install -c conda-forge clickwork` installs 1.0.x successfully.
- README + GUIDE install sections list conda as a valid install path.
- Feedstock auto-generates a maintenance PR for our next release (1.0.1 or 1.1.0) and the clickwork maintainer successfully shepherds it through.

## Risks / open

- **staged-recipes review takes a month+ and asks for substantive changes.** Budget for it; don't block #62 closure on a narrow-window goal.
- **A PyPI-side bug forces a 1.0.1 mid-review.** Fine — we update the `url:` and `sha256:` in the recipe PR, conda-forge reviewers are used to this.
- **The recipe diverges from pyproject.toml over time** (e.g., we add a new runtime dep but forget to bump the feedstock). conda-forge has bots for this but the first few cycles are manual.

## Out of scope for this plan

- Sigstore artifact verification on the conda-forge side (out of our control; #61 adjacent).
- Publishing to other conda channels (bioconda etc) — not a current audience.
- Maintaining our own conda channel — conda-forge is the community standard and we should use it.

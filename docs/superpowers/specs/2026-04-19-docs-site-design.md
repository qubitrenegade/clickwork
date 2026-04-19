# Documentation site — design spec

**Date:** 2026-04-19
**Issue:** [#94](https://github.com/qubitrenegade/clickwork/issues/94)
**Milestone:** post-1.0

## Goal

Publish the existing `docs/` folder as a browsable documentation site at `https://qubitrenegade.github.io/clickwork/`, restructured around a task-oriented information architecture and expanded with tutorial content that serves three audiences: beginners with out-of-control script directories, power users wanting to go deeper, and LLMs implementing against the library.

This is a scope expansion of #94 as originally filed. The issue proposed "turn `docs/` into a browsable site"; this spec turns it into "a documentation site that teaches."

## Context

As of 1.0.0, the existing docs skew reference-y: `API_POLICY.md`, `ARCHITECTURE.md`, `PLUGINS.md`, `SECURITY.md`, `MIGRATING.md`, `GUIDE.md`, `LLM_REFERENCE.md`. They are thorough but assume the reader already knows why they're reading them. There is no hand-holding onboarding path and no cookbook of common tasks. The site rebuild is the right moment to close that gap rather than deferring it to a later initiative that may never happen.

## Audiences

1. **Beginner user** — has an out-of-control directory of utility scripts and wants to convert them into a coherent CLI. Needs to be led from "install" to "my first command works" to "my plugin works" without reading reference material.
2. **Power user** — already using clickwork, wants to do less-obvious things (custom config precedence, plugin authoring patterns, signal forwarding edge cases). Needs recipe-shaped content and architectural rationale.
3. **LLM** — implementing against the library or helping a developer use it. Needs a structured, stable entry point (`llms.txt`) and the existing `LLM_REFERENCE.md`.

## Out of scope

Each deferral below is listed with the reason it does not trap us if added later.

- **`mike` versioned docs.** 1.x is stable and single-version docs are sufficient. `mike` can layer on later without reshaping existing pages — it operates by copying the built site into version-prefixed directories on the `gh-pages` branch.
- **`mkdocstrings` beyond the Reference appendix.** The auto-generated API page is in scope; rewriting hand-authored docs around embedded docstring directives is not. The auto-generated page is additive — pulling docstrings into prose sections can happen page-by-page later if it ever becomes valuable.
- **Custom domain.** Landing on `github.io/clickwork/` is fine. A `CNAME` file plus a DNS change lights up a custom domain without any content edits.
- **External link checker as blocking CI.** External sites go down; a per-PR blocking check would cause flakes unrelated to the PR. Scheduled weekly instead.
- **Vale as blocking CI.** Prose linters produce too much subjective noise to block merges on. Annotations-only mode gives authors the signal without the friction.
- **Splitting `GUIDE.md` into the new Diátaxis sections.** Kept intact under Reference for v1. Harvesting pieces into How-To can happen once real usage shows which parts readers reach for; splitting speculatively now risks breaking inbound links without a clear target shape.
- **Doctest-style verified examples.** `pytest --doctest-glob='*.md'` can run fenced code blocks as tests. Skipped for v1: fragile under whitespace and nondeterministic output, and we have no evidence yet of example drift being a real problem.

## Information architecture

The site follows [Diátaxis](https://diataxis.fr/). Top-level nav:

```
Home
Tutorials        (learning-oriented)
How-To           (task-oriented)
Explanation      (understanding-oriented)
Reference        (information-oriented)
```

### Full section map

**Home** — `docs/index.md`. Landing page mirroring the README: one-paragraph "what is it," install snippet, a "New here? → Tutorials" / "Looking for a specific task? → How-To" / "Need to look something up? → Reference" triage block, feature highlights, links to GitHub.

**Tutorials** — learning-oriented, linear, opinionated.
- `tutorials/quickstart.md` — install to first working command, target ~5 minute read. One happy path, no branching.
- `tutorials/walkthrough/index.md` — multi-page practical walkthrough (30–60 min). Builds a realistic small project. Pages:
  - `01-your-first-command.md` — set up project layout, register a command, run it.
  - `02-adding-a-plugin.md` — split a command out into an entry-point plugin, install it, verify discovery.
  - `03-packaging.md` — `pyproject.toml` metadata, `uv build`, installing the wheel in a fresh venv, shipping to a teammate.

**How-To** — task-oriented recipes. Short (~1 page each), self-contained, assume the reader knows what they want.
- `how-to/index.md` — categorized landing page.
- `how-to/tame-a-script-directory.md` — seed recipe targeting the beginner persona directly: "I have a pile of bash + Python utility scripts, how do I turn them into a single CLI?"
- `how-to/add-a-command.md` — add one more command to an existing clickwork project.
- `how-to/write-a-plugin.md` — minimal plugin walkthrough, cross-referenced from `reference/plugins.md`.
- `how-to/migrate-from-argparse.md` — pattern-by-pattern migration from vanilla `argparse` (and a note on vanilla `click`).

**Explanation** — understanding-oriented, rationale and model.
- `explanation/architecture.md` — existing `ARCHITECTURE.md`, moved.
- `explanation/api-policy.md` — existing `API_POLICY.md`, moved.
- `explanation/plugin-model.md` — new short page: why clickwork's plugin model is entry-point based, how discovery works conceptually, how the local-wins rule plays out. Cross-links into `reference/plugins.md` for the spec.

**Reference** — information-oriented, lookup surface.
- `reference/guide.md` — existing `GUIDE.md`, moved. V1 keeps it intact; splitting into How-To deferred.
- `reference/plugins.md` — existing `PLUGINS.md`, moved.
- `reference/security.md` — existing `SECURITY.md`, moved.
- `reference/migrating.md` — existing `MIGRATING.md`, moved.
- `reference/api.md` — new, auto-generated via `mkdocstrings`. The "appendix" — every public symbol and its docstring, rendered from the live codebase. Populated by a single `::: clickwork` directive plus narrower directives for `clickwork.http`, `clickwork.platform`, `clickwork.testing`, `clickwork.config`.
- `reference/llm-reference.md` — existing `LLM_REFERENCE.md`, moved.

**`llms.txt`** — served at site root, follows the [llmstxt.org](https://llmstxt.org/) format. Short markdown file: `# clickwork`, one-paragraph summary, H2 sections for Tutorials / How-To / Reference, each listing the canonical pages under it. Generated by hand (not automated) — the list is small and the curation matters.

### File moves

Moved files risk breaking two surfaces: the deployed site (for users landing on the old URL) and the GitHub-native markdown rendering (for users who have bookmarked a file path in the repo). These are handled separately.

- **Deployed site.** The [`mkdocs-redirects`](https://github.com/mkdocs/mkdocs-redirects) plugin (a third-party plugin, not bundled with mkdocs-material) configures client-side redirects via its `redirect_maps` key in `mkdocs.yml`. Landing on `/GUIDE/` will auto-redirect to `/reference/guide/`.
- **GitHub-native view.** At each old path we leave a one-line markdown stub — `> This page has moved to [reference/guide.md](reference/guide.md).` — so anyone hitting the file on github.com still gets a working link. These stubs live at the repo root-relative `docs/` paths (e.g. `docs/GUIDE.md`). They must be excluded from the mkdocs build via [`exclude_docs`](https://www.mkdocs.org/user-guide/configuration/#exclude_docs) in `mkdocs.yml`, because omitting a file from `nav` alone still publishes it as an orphan page — which would collide with the `mkdocs-redirects` entry pointing at the same URL.

The pair of surfaces costs one plugin entry, one stub file per moved doc, and a single `exclude_docs` block listing every stub.

## Technical stack

- **[mkdocs-material](https://squidfunk.github.io/mkdocs-material/)** — theme. Covers search, dark mode, syntax highlighting, instant-loading nav, content tabs, admonitions, code-copy buttons.
- **[mkdocstrings](https://mkdocstrings.github.io/)** with the `python` handler (default `griffe` backend) — powers `reference/api.md`.
- **[pymdown-extensions](https://facelessuser.github.io/pymdown-extensions/)** — shipped by material; enables fenced tabs, task lists, admonition syntax.
- **Material-native nav features**: `navigation.instant`, `navigation.sections`, `navigation.expand`, `content.code.copy`, `search.suggest`, `search.highlight`.

### Packaging

Docs dependencies live in `pyproject.toml` under a `docs` dependency group (the project already uses `uv`):

```toml
[dependency-groups]
docs = [
    "mkdocs-material",
    "mkdocstrings[python]",
    "mkdocs-redirects",
]
```

Local authoring: `uv sync --group docs && uv run mkdocs serve`.
CI: `uv sync --frozen --group docs`, then `uv run mkdocs build --strict` (see the CI section for why `--frozen` specifically).

No parallel `requirements-docs.txt` — the dependency group is the single source of truth. CI pins via the committed `uv.lock` and `--frozen` makes stale-lock a hard failure.

### `mkdocs.yml` structure (outline)

```yaml
site_name: clickwork
site_url: https://qubitrenegade.github.io/clickwork/
repo_url: https://github.com/qubitrenegade/clickwork
repo_name: qubitrenegade/clickwork
edit_uri: edit/main/docs/

exclude_docs: |
  # GitHub-native stubs for moved files — not published on the site.
  GUIDE.md
  PLUGINS.md
  SECURITY.md
  MIGRATING.md
  ARCHITECTURE.md
  API_POLICY.md
  LLM_REFERENCE.md
  # Internal planning + spec artifacts — live under docs/ for proximity to
  # the work they describe, not for publication. Without these excludes,
  # `mkdocs build --strict` fails on them as orphan pages.
  plans/**
  superpowers/**

theme:
  name: material
  features:
    - navigation.instant
    - navigation.sections
    - navigation.expand
    - content.code.copy
    - search.suggest
    - search.highlight
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
    - media: "(prefers-color-scheme: dark)"
      scheme: slate

markdown_extensions:
  - admonition
  - attr_list
  - md_in_html
  - pymdownx.details
  - pymdownx.highlight
  - pymdownx.inlinehilite
  - pymdownx.snippets
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
  - pymdownx.tasklist:
      custom_checkbox: true

plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          paths: [src]
  - redirects:
      redirect_maps:
        GUIDE.md: reference/guide.md
        PLUGINS.md: reference/plugins.md
        # ...one entry per moved file

nav:
  - Home: index.md
  - Tutorials:
      - Quickstart: tutorials/quickstart.md
      - Walkthrough:
          - tutorials/walkthrough/index.md
          - Your first command: tutorials/walkthrough/01-your-first-command.md
          - Adding a plugin: tutorials/walkthrough/02-adding-a-plugin.md
          - Packaging: tutorials/walkthrough/03-packaging.md
  - How-To:
      - how-to/index.md
      - Tame a script directory: how-to/tame-a-script-directory.md
      - Add a command: how-to/add-a-command.md
      - Write a plugin: how-to/write-a-plugin.md
      - Migrate from argparse: how-to/migrate-from-argparse.md
  - Explanation:
      - Architecture: explanation/architecture.md
      - API Policy: explanation/api-policy.md
      - Plugin Model: explanation/plugin-model.md
  - Reference:
      - User Guide: reference/guide.md
      - Plugins: reference/plugins.md
      - Security: reference/security.md
      - Migrating: reference/migrating.md
      - API Reference: reference/api.md
      - LLM Reference: reference/llm-reference.md
```

## CI & deploy

Two workflows.

### `.github/workflows/docs.yml`

Triggered by changes to docs-relevant paths only:

```yaml
on:
  push:
    branches: [main]
    paths:
      - 'docs/**'
      - 'mkdocs.yml'
      - '.github/workflows/docs.yml'
      - '.markdownlint.yaml'
      - '.vale.ini'
      - '.vale/**'
      - 'pyproject.toml'
      - 'uv.lock'
      - 'src/**'
  pull_request:
    paths:
      - 'docs/**'
      - 'mkdocs.yml'
      - '.github/workflows/docs.yml'
      - '.markdownlint.yaml'
      - '.vale.ini'
      - '.vale/**'
      - 'pyproject.toml'
      - 'uv.lock'
```

- `pyproject.toml` is included because changing the `docs` dependency group affects the build.
- `uv.lock` is included so a lockfile change re-runs CI.
- `.github/workflows/docs.yml` is included so edits to CI config itself re-trigger the job.
- `.markdownlint.yaml`, `.vale.ini`, and `.vale/**` are included so rule changes re-run lint output on the existing corpus.
- `src/**` is included **only in the `push` trigger (main-branch merges), not `pull_request`**. `reference/api.md` is auto-generated from `src/` via `mkdocstrings`, so a code-only merge to main must re-deploy the site to keep the API reference in sync. Leaving `src/**` out of the `pull_request` trigger keeps the PR-event behavior we want — a code-only PR does not run docs CI. Code-only *merges to main* do run the docs workflow by design, because that is the moment the deployed API reference would otherwise go stale; any docstring-rendering breakage surfaces on main immediately because the push trigger will rebuild.

Jobs:

1. **`build`** (runs on PR and main):
   - `uv sync --frozen --group docs` (`--frozen` matches `.github/workflows/bench.yml` and ensures CI fails on a stale lockfile rather than silently re-resolving).
   - `uv run mkdocs build --strict` (broken internal links, missing nav entries, orphaned pages → fail).
   - `markdownlint-cli2 'docs/**/*.md'` with a checked-in `.markdownlint.yaml` config. Blocking.
   - Vale via `errata-ai/vale-action@v2` with `fail_on_error: false`. Advisory annotations only.
2. **`deploy`** (runs on main only, `needs: build`):
   - Declare `permissions: contents: write` at job scope. The default `GITHUB_TOKEN` is read-only in many repo configurations, and `mkdocs gh-deploy` pushes to the `gh-pages` branch — without explicit write permission the deploy will fail with a 403 on first run. Job scope (not workflow scope) so the build job retains its minimal default permissions.
   - `uv run mkdocs gh-deploy --force` using the default `GITHUB_TOKEN`. Publishes to the `gh-pages` branch.

`--force` is appropriate here because `gh-pages` is a deploy artifact rather than a source branch — no human edits it by hand, and each build replaces the prior contents wholesale.

### `.github/workflows/docs-linkcheck.yml`

Independent scheduled workflow:

```yaml
on:
  schedule:
    - cron: '0 12 * * 1'  # Mondays 12:00 UTC
  workflow_dispatch: {}
```

Runs `lycheeverse/lychee-action` against `docs/**/*.md`. On failure, opens a GitHub issue (via lychee's built-in issue-opening mode or `peter-evans/create-issue-from-file`). Never blocks a merge because it's never attached to a PR.

### GitHub Pages setup

One-time manual step in repo Settings → Pages → Source: `gh-pages` branch. This gets documented in `CONTRIBUTING.md` under a "Maintainers" heading so the setting is reproducible if it is ever cleared.

## New content budget

This spec requires the following new prose to be written in the implementation PRs:

- `docs/index.md` — landing page.
- `docs/tutorials/quickstart.md`.
- `docs/tutorials/walkthrough/index.md`, `docs/tutorials/walkthrough/01-your-first-command.md`, `docs/tutorials/walkthrough/02-adding-a-plugin.md`, `docs/tutorials/walkthrough/03-packaging.md`.
- `docs/how-to/index.md`, `docs/how-to/tame-a-script-directory.md`, `docs/how-to/add-a-command.md`, `docs/how-to/write-a-plugin.md`, `docs/how-to/migrate-from-argparse.md`.
- `docs/explanation/plugin-model.md`.
- `docs/llms.txt`.
- A short "this page has moved" stub at each old path (`docs/GUIDE.md`, `docs/PLUGINS.md`, etc.), excluded from the build via `exclude_docs`.

Every other file in the nav is an existing doc moved into its Diátaxis slot.

## Success criteria

- `uv run mkdocs serve` builds locally with no warnings.
- `uv run mkdocs build --strict` passes in CI on every docs PR.
- Site deploys to `https://qubitrenegade.github.io/clickwork/` on merge to main.
- All internal links in `docs/` resolve both on GitHub (the raw-markdown rendering) and on the deployed site.
- Redirect stubs exist for every moved file so inbound links from outside the repo still resolve.
- `llms.txt` is reachable at `https://qubitrenegade.github.io/clickwork/llms.txt`.
- Nav surfaces Home plus the four Diátaxis sections in the order Home → Tutorials → How-To → Explanation → Reference.
- No code-only pull request event triggers the docs workflow. (Code-only merges *to main* do trigger the workflow by design, to redeploy the `mkdocstrings`-generated API reference.) Filtering the other direction — preventing docs-only PRs from triggering the existing `test.yml` / `lint.yml` / `types.yml` — is out of scope for this initiative; it would require editing those workflows and is a separate follow-up.

## Open questions

- **Vale style package choice.** `google`, `microsoft`, `write-good`, and `proselint` each have a different tone. Defer the pick to the implementation PR for the CI workflow; if the chosen pack is too noisy, reduce the ruleset rather than re-picking, since the annotations-only mode makes switching cheap later.
- **Granularity of the redirect map.** Every file in `docs/*.md` that is not staying put needs an entry. Enumerate in the implementation plan, not here.

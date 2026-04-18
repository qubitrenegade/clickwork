# Benchmarks

This directory holds recorded benchmark baselines for `clickwork`. The
only file here today is `baseline.json`, produced by
`scripts/bench_coldstart.py`.

## What `baseline.json` measures

`import_ms` is the **median wall-clock time** (in milliseconds) of

```
python -c "import clickwork"
```

across N fresh subprocess invocations, with the first run discarded as
a warm-up. `min_ms` and `p95_ms` are the min and ~95th-percentile
samples from the same batch; they're recorded for diagnostic context
but the regression check only uses `import_ms` (the median).

## Output channels (stdout vs stderr)

`scripts/bench_coldstart.py` splits its output so machine-readable and
human-readable consumers don't step on each other:

- **stdout** is always **pure JSON** — the full result dict, exactly
  what gets written to `baseline.json`. Pipe it to `jq`, `tee` it into
  a file, feed it to another script; no prose will get mixed in.
- **stderr** carries the human-readable context: the
  `baseline: … / current: … / delta: …` diff when `--baseline` is
  used, the `wrote baseline to …` confirmation when
  `--update-baseline` is used, and the `REGRESSION: …` message when
  the gate fires.

So when running the script by hand you'll see the prose on your
terminal and the JSON on its own line(s). If you want just the JSON,
redirect stderr away:

```bash
python scripts/bench_coldstart.py --baseline benchmarks/baseline.json 2>/dev/null
```

If you want just the human-readable summary, swallow stdout instead:

```bash
python scripts/bench_coldstart.py --baseline benchmarks/baseline.json >/dev/null
```

The CI workflow relies on this split: it wraps stdout in a ` ```json `
fence in the step summary (valid JSON inside the fence) while stderr
shows up inline in the raw Actions log for at-a-glance context.

The number represents **cold-start cost**: how long a user waits
between hitting Enter on their `click`-based CLI and seeing the first
byte of output. A regression here is immediately user-visible.

## Why a committed baseline

CI checks the current PR's measurement against this file. We commit
the baseline — instead of, say, comparing the PR branch against `main`
in CI — because:

1. Re-measuring `main` on every PR doubles CI time for no added signal.
2. A committed baseline gives an explicit, reviewable record of when
   cold-start cost moved. Each bump of this file is a deliberate act
   visible in the commit log.

## Updating the baseline (intentional regressions)

If a PR's slowdown is deliberate (a new feature genuinely requires a
heavy import, for example), update the baseline in the same PR:

```bash
python scripts/bench_coldstart.py --runs 7 --update-baseline benchmarks/baseline.json
git add benchmarks/baseline.json
git commit -m "bench: refresh cold-start baseline after <why>"
```

The commit message should say **why** the baseline moved — that's
what future debuggers of "why is clickwork startup slow?" will grep
for.

## Why the 20% threshold

The regression gate in `scripts/bench_coldstart.py` fires when the
current median exceeds the baseline median by more than 20%.

- **Smaller** (e.g. 5% or 10%) would trip constantly on shared-runner
  noise. GitHub Actions runners are shared VMs; cold-start timings
  move around with neighbour activity, and even with the median +
  warm-up discard, ±10% swings are normal.
- **Larger** (e.g. 50%) would let real regressions sneak through. A
  new module-level import of, say, `pandas` would double cold-start
  cost; we want to catch that in review, not discover it after
  release.

20% is the loosest threshold that still catches regressions of the
size we care about. If CI starts flapping on 20%, investigate
variance first before widening the threshold — a noisy benchmark is
a broken benchmark.

## Why we don't run this as a pytest test

- pytest collects and imports everything in one interpreter, which
  defeats the entire point of measuring import cost per subprocess.
- Benchmark results are noisy in a way test assertions shouldn't be;
  mixing them into the unit-test job would make that job flaky.

It runs as its own workflow (`.github/workflows/bench.yml`) so the
failure signal stays separate and the job can be retried
independently.

## Heads-up: initial baseline vs CI runner

The committed `baseline.json` was captured on the original author's
dev machine (Linux / glibc 2.35 / Python 3.13.6). CI runs on
`ubuntu-24.04` (glibc 2.39). glibc and kernel differences can shift
absolute `import_ms` by more than the 20% gate, so the very first CI
run on the PR that introduced this workflow may fail the regression
check against the dev-captured baseline.

The fix is straightforward: after the workflow exists on `main`, use
the **Run workflow** button on the GitHub Actions UI
(`bench.yml` has a `workflow_dispatch` trigger) to capture a fresh
baseline directly on the CI runner, grab the stdout JSON from the
logs, and commit it as `benchmarks/baseline.json` in a follow-up PR.
Every subsequent PR is then compared against a baseline captured on
the same hardware it's running on.

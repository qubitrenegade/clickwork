#!/usr/bin/env python3
"""Cold-start benchmark for `import clickwork`.

Measures the wall-clock cost of `python -c "import clickwork"` across N
subprocess invocations and emits a JSON summary (median / min / p95).

Why a subprocess per iteration?  Python caches imports inside a single
interpreter, so timing `import clickwork` repeatedly in-process only
measures the first call.  Spawning a fresh subprocess each time is the
only way to capture actual interpreter-startup + import cost on every
sample.

Why the median?  CI runners are noisy shared machines.  One-off stalls
(GC pause, IO hiccup, neighbouring container spike) skew the mean but
not the median.  We also discard the very first run — the OS file
cache is cold on that one and it tends to be an outlier.

Compared against a committed baseline JSON, this script exits non-zero
when the current median exceeds the baseline median by more than 20%.
20% is deliberately loose: tight enough to catch a real regression
(e.g. a new heavy module-level import), loose enough to survive
normal CI-runner variance.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

# The import statement we time.  Matches the decision pinned in the
# clickwork 1.0 roadmap (Wave 2b, #59).
IMPORT_CMD = [sys.executable, "-c", "import clickwork"]

# Regression threshold.  Tuned in the roadmap: smaller is noise on
# shared CI runners; larger lets real slowdowns sneak through.
REGRESSION_THRESHOLD = 1.20  # 20% over baseline median


def measure_once() -> float:
    """Run `python -c "import clickwork"` and return wall-clock ms.

    We use ``time.perf_counter_ns`` (monotonic, high-resolution) rather
    than ``time.time`` so results aren't corrupted by wall-clock
    adjustments mid-benchmark.
    """
    start = time.perf_counter_ns()
    # ``check=True`` so an import failure (e.g. the package isn't
    # installed in the active env) fails the benchmark loudly instead
    # of silently reporting interpreter-only startup time.
    #
    # We deliberately do NOT capture stderr here.  If ``import clickwork``
    # explodes (SyntaxError in a module, missing transitive dep, etc.)
    # the traceback is the single most useful thing a CI reader can see.
    # Swallowing it into a ``CalledProcessError`` with no output makes
    # "bench failed" look like a flaky benchmark when it's really a real
    # import bug.  Letting stderr pass through to our stderr (which CI
    # shows inline) surfaces the actual failure.
    subprocess.run(IMPORT_CMD, check=True, stdout=subprocess.DEVNULL)
    end = time.perf_counter_ns()
    return (end - start) / 1_000_000.0  # ns -> ms


def run_benchmark(runs: int) -> dict[str, object]:
    """Run the cold-start benchmark ``runs`` times plus one warm-up.

    Returns a result dict ready to serialize as the benchmark output
    or be written as a new baseline.
    """
    # Discard the first run: OS caches (page cache, inode cache) are
    # cold and the first interpreter bootstrap is usually a big outlier.
    measure_once()

    samples = [measure_once() for _ in range(runs)]
    samples_sorted = sorted(samples)

    return {
        "import_ms": statistics.median(samples_sorted),
        "min_ms": min(samples_sorted),
        # ``statistics.quantiles(..., n=20)[18]`` gives us p95 from 20
        # quantile buckets.  For small N this is approximate but that's
        # fine — we only use it to spot heavy-tail regressions in the
        # step summary, not as the pass/fail criterion.
        #
        # ``method='inclusive'`` is required here: the default
        # ``method='exclusive'`` needs at least ``n + 1`` samples (21
        # for n=20) and raises StatisticsError on our default 7-run
        # batch.  Inclusive mode interpolates endpoints from the data
        # itself, so it works with any N >= 2.
        "p95_ms": (
            statistics.quantiles(samples_sorted, n=20, method="inclusive")[18]
            if len(samples_sorted) >= 2
            else samples_sorted[0]
        ),
        "runs": runs,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def compare_to_baseline(current: dict[str, object], baseline_path: Path) -> int:
    """Return 0 if ``current`` is within threshold of baseline, else 1.

    Prints a short diff so CI logs + reviewers can see both numbers at
    a glance without having to crack open the JSON.

    All human-readable output goes to **stderr**, not stdout.  Stdout is
    reserved for the machine-readable JSON emitted by ``main()`` so the
    CI step summary can wrap stdout in a ``json`` fence and get valid
    JSON rather than a mix of JSON and prose.
    """
    # Explicit utf-8 so a developer running the script under a non-utf-8
    # locale (e.g. cp1252 on Windows) decodes the JSON identically to
    # CI, which always runs in utf-8. Without this the default decoder
    # would use locale.getencoding() and deltas could depend on where
    # the script was last run, not what changed in the code.
    #
    # Wrap the load in try/except so CI gets an actionable error
    # instead of a raw traceback when the baseline is missing or
    # malformed. Each error path below prints a short message to
    # stderr and returns non-zero so the CI step fails cleanly.
    try:
        baseline_text = baseline_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"ERROR: baseline file not found at {baseline_path}. "
            "Either commit one (capture via --update-baseline) or "
            "remove the --baseline flag to skip the regression check.",
            file=sys.stderr,
        )
        return 1
    try:
        baseline = json.loads(baseline_text)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: baseline at {baseline_path} is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 1
    if "import_ms" not in baseline:
        print(
            f"ERROR: baseline at {baseline_path} is missing the "
            "'import_ms' key. Re-capture via "
            "`--update-baseline <path>`.",
            file=sys.stderr,
        )
        return 1

    # Warn -- but don't fail -- if the environment that captured the
    # baseline materially differs from the current environment.
    # Python patch and platform string are cheap to compare and both
    # affect cold-start timing enough to show up in the delta. A
    # mismatch doesn't fail because the real reconciliation is
    # "re-capture on the CI runner" (see benchmarks/README.md); the
    # warning lets a reviewer spot that a delta is environment-driven
    # rather than code-driven.
    for field in ("python", "platform"):
        baseline_val = baseline.get(field)
        current_val = current.get(field)
        if baseline_val and current_val and baseline_val != current_val:
            print(
                f"WARNING: baseline was captured under "
                f"{field}={baseline_val!r} but this run is on "
                f"{field}={current_val!r}. Delta below may reflect "
                "environment differences rather than code changes. "
                "Consider re-capturing baseline via the bench "
                "workflow's manual dispatch.",
                file=sys.stderr,
            )

    current_ms = float(current["import_ms"])  # type: ignore[arg-type]
    baseline_ms = float(baseline["import_ms"])
    # ``max(1e-9, ...)`` guards against a zero baseline (shouldn't
    # happen in practice but cheap insurance against ZeroDivisionError).
    delta_pct = ((current_ms - baseline_ms) / max(1e-9, baseline_ms)) * 100.0

    print(f"baseline: {baseline_ms:.2f} ms  (from {baseline_path})", file=sys.stderr)
    print(f"current:  {current_ms:.2f} ms", file=sys.stderr)
    print(
        f"delta:    {delta_pct:+.1f}%  (threshold: +{(REGRESSION_THRESHOLD - 1) * 100:.0f}%)",
        file=sys.stderr,
    )

    if current_ms > baseline_ms * REGRESSION_THRESHOLD:
        print(
            "REGRESSION: cold-start import time exceeds baseline by more than "
            f"{(REGRESSION_THRESHOLD - 1) * 100:.0f}%.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark `import clickwork` cold-start time.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=7,
        help="Number of timed runs after the warm-up (default: 7).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Compare current median against this baseline JSON. "
        "Exit 1 if current is >20%% slower.",
    )
    parser.add_argument(
        "--update-baseline",
        type=Path,
        default=None,
        help="Write the current result to this path as a new baseline.",
    )
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be >= 1")

    result = run_benchmark(args.runs)

    # Emit JSON to **stdout** first so the number is always visible,
    # even when we go on to write a baseline or fail a comparison.
    # Stdout is kept pure-JSON so ``bench.yml``'s step summary can wrap
    # it in a ```json`` fence and downstream tooling (jq, etc.) can
    # parse it without stripping prose.  Anything human-readable goes
    # to stderr.
    print(json.dumps(result, indent=2))

    if args.update_baseline is not None:
        args.update_baseline.parent.mkdir(parents=True, exist_ok=True)
        # Trailing newline is a common git-friendly convention and
        # keeps ``cat`` / diff output tidy.
        args.update_baseline.write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        # Human-readable confirmation goes to stderr so stdout stays
        # pure JSON for ``--update-baseline`` runs too.
        print(f"wrote baseline to {args.update_baseline}", file=sys.stderr)

    if args.baseline is not None:
        return compare_to_baseline(result, args.baseline)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

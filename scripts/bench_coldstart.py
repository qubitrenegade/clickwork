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
    subprocess.run(IMPORT_CMD, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
        "p95_ms": (
            statistics.quantiles(samples_sorted, n=20)[18]
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
    """
    baseline = json.loads(baseline_path.read_text())
    current_ms = float(current["import_ms"])  # type: ignore[arg-type]
    baseline_ms = float(baseline["import_ms"])
    # ``max(1e-9, ...)`` guards against a zero baseline (shouldn't
    # happen in practice but cheap insurance against ZeroDivisionError).
    delta_pct = ((current_ms - baseline_ms) / max(1e-9, baseline_ms)) * 100.0

    print(f"baseline: {baseline_ms:.2f} ms  (from {baseline_path})")
    print(f"current:  {current_ms:.2f} ms")
    print(f"delta:    {delta_pct:+.1f}%  (threshold: +{(REGRESSION_THRESHOLD - 1) * 100:.0f}%)")

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

    # Emit JSON first so the number is always visible, even when we go
    # on to write a baseline or fail a comparison.
    print(json.dumps(result, indent=2))

    if args.update_baseline is not None:
        args.update_baseline.parent.mkdir(parents=True, exist_ok=True)
        # Trailing newline is a common git-friendly convention and
        # keeps ``cat`` / diff output tidy.
        args.update_baseline.write_text(json.dumps(result, indent=2) + "\n")
        print(f"wrote baseline to {args.update_baseline}")

    if args.baseline is not None:
        return compare_to_baseline(result, args.baseline)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

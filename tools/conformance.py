"""Conformance harness: lineage's schedule interpreter against an external oracle.

    pip install croniter==6.0.0            # CI and this script only, never runtime
    python tools/conformance.py            # prints the score, fails on regression
    python tools/conformance.py --update-baseline

D1's whole claim is "this job declares it runs at most N days apart", and every
coverage statement and scream-test tier leans on that number. A wrong max gap is
a wrong answer to "has this job had a chance to run in the window we watched?"
So the interpreter is scored, not just unit-tested: every expression in
tests/conformance/cron.txt is evaluated by lineage and by croniter over the same
four years, and the two max gaps must agree.

croniter is MIT and is run as a black box -- its output is compared, its code is
not read or copied (REPO_RULES section 3). Without it installed the harness
reports SKIP and exits 0, so the stdlib-only promise of the package holds.

The baseline (tests/conformance/baseline.json) only goes up. CI fails when the
pass count drops below it, not only when everything fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lineage.jobs import CronError, cron_max_gap_days  # noqa: E402

CORPUS = ROOT / "tests" / "conformance" / "cron.txt"
BASELINE = ROOT / "tests" / "conformance" / "baseline.json"
START, END = dt.datetime(2025, 1, 1), dt.datetime(2029, 1, 1)    # lineage's window


def corpus():
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def oracle(expr, croniter):
    """Max gap in days between consecutive firings in [START, END), or None
    when it fires fewer than twice -- the same contract as cron_max_gap_days."""
    try:
        it = croniter(expr, START - dt.timedelta(minutes=1))
    except Exception:
        return "error"
    prev, gap, n = None, 0.0, 0
    while True:
        t = it.get_next(dt.datetime)
        if t >= END:
            break
        n += 1
        if prev is not None:
            gap = max(gap, (t - prev).total_seconds() / 86400)
        prev = t
    return gap if n >= 2 else None


def ours(expr):
    try:
        return cron_max_gap_days(expr)
    except CronError:
        return "error"


def agree(a, b):
    if isinstance(a, float) or isinstance(b, float) or isinstance(a, int) or isinstance(b, int):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 1e-6
    return a == b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)
    try:
        from croniter import croniter
    except ImportError:
        print("SKIP: croniter not installed (pip install croniter==6.0.0); cron conformance not scored")
        return 0

    exprs = list(corpus())
    failures = []
    for e in exprs:
        a, b = ours(e), oracle(e, croniter)
        if not agree(a, b):
            failures.append((e, a, b))
    passed = len(exprs) - len(failures)

    for e, a, b in failures:
        fmt = lambda v: f"{v:.4f}" if isinstance(v, float) else str(v)  # noqa: E731
        print(f"  FAIL  {e:<22} lineage={fmt(a):<10} oracle={fmt(b)}")
    print(f"cron conformance: {passed}/{len(exprs)} pass")

    baseline = json.loads(BASELINE.read_text())["cron"] if BASELINE.exists() else 0
    if args.update_baseline:
        if passed < baseline:
            print(f"refusing to lower the baseline from {baseline} to {passed}")
            return 1
        BASELINE.write_text(json.dumps({"cron": passed, "of": len(exprs)}, indent=2) + "\n")
        print(f"baseline set to {passed}")
        return 0
    if passed < baseline:
        print(f"REGRESSION: {passed} < baseline {baseline}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""smoke_graded_threshold — WS-163 graded eval regression gate

Runs the 50-case chat smoke suite and enforces graded eval thresholds.
If overall average score drops below threshold, CI fails.

This test REQUIRES a running server (uvicorn on :8765).
It is designed to run alongside make test-chat, not as part of make test.

Usage:
  python3 tests/smoke_graded_threshold.py [--url http://localhost:8765]

If server is not available, this test safely skips (returns 0).

Thresholds (customizable via env):
- HIPOP_GRADED_OVERALL_MIN: Minimum overall average (default 0.80)
- HIPOP_GRADED_SOURCE_MIN: Minimum correct_source average (default 0.80)
- HIPOP_GRADED_REAL_TASK_MIN: Minimum real_task average (default 0.85)
- HIPOP_GRADED_FAIL_CLOSED_MIN: Minimum fail_closed average (default 0.80)

WS-163: When chat server is available, this gate ensures graded scores don't regress.
"""
from __future__ import annotations

import os
import sys
import argparse
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Import smoke_chat functions and cases
from tests import smoke_chat


def get_thresholds():
    """Load threshold requirements from env (or defaults)."""
    return {
        "overall": float(os.environ.get("HIPOP_GRADED_OVERALL_MIN", "0.80")),
        "correct_source": float(os.environ.get("HIPOP_GRADED_SOURCE_MIN", "0.80")),
        "correct_time_window": float(os.environ.get("HIPOP_GRADED_TIME_MIN", "0.75")),
        "real_task": float(os.environ.get("HIPOP_GRADED_REAL_TASK_MIN", "0.85")),
        "fail_closed": float(os.environ.get("HIPOP_GRADED_FAIL_CLOSED_MIN", "0.80")),
    }


def main():
    ap = argparse.ArgumentParser(description="WS-163 graded eval threshold gate")
    ap.add_argument("--url", default=os.environ.get("HIPOP_URL", "http://localhost:8765"))
    ap.add_argument("--filter", help="Only run cases matching this keyword")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    print(f"=== WS-163 Graded Eval Threshold Gate ===")
    print(f"URL: {args.url}")

    thresholds = get_thresholds()
    print(f"Thresholds: {thresholds}")

    # Set up smoke test authentication
    try:
        smoke_chat.ensure_smoke_user_tenant1()
        opener = smoke_chat.build_authenticated_opener(args.url)
    except Exception as e:
        print(f"\n⚠️  Server not available: {type(e).__name__}")
        print("   Graded eval threshold gate requires running uvicorn server.")
        print("   Skipping (run alongside 'make test-chat' when server is ready)")
        sys.exit(0)  # Safe skip — not a failure
    global _AUTH_OPENER
    smoke_chat._AUTH_OPENER = opener

    # Prepare expectations and cases
    cases = [c for c in smoke_chat.CASES if (not args.filter) or args.filter in c.name]
    try:
        smoke_chat._bind_runtime_expectations(cases)
        smoke_chat._prepare_dynamic_expectations(args.url, opener)
    except Exception as e:
        print(f"\n✗ Fixture prep failed: {e}")
        sys.exit(1)

    print(f"Running {len(cases)} cases...")
    graded_results = []
    t0 = time.time()

    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c.name[:50]} ", end="", flush=True)
        t = time.time()
        resp = smoke_chat.post_chat(opener, args.url, c.question, c.store, c.timeout)
        ok, reasons = smoke_chat.check(c, resp)
        grades = smoke_chat.grade_case(c, resp)
        elapsed = time.time() - t

        graded_results.append({"name": c.name, "grades": grades})
        status = "✓" if ok else "✗"
        print(f"{status} {grades['overall']:.2f} ({elapsed:.1f}s)")

    total = time.time() - t0

    # Compute averages
    avg_by_dim = {}
    for dim in ["correct_source", "correct_time_window", "real_task", "fail_closed", "overall"]:
        scores = [r["grades"][dim] for r in graded_results]
        avg_by_dim[dim] = sum(scores) / len(scores) if scores else 0

    print(f"\n=== Results ===")
    print(f"Cases: {len(graded_results)}, Time: {total:.1f}s")
    print(f"\nAverage Scores:")
    print(f"  correct_source:     {avg_by_dim['correct_source']:.3f} (threshold: {thresholds['correct_source']:.2f})")
    print(f"  correct_time_window: {avg_by_dim['correct_time_window']:.3f} (threshold: {thresholds['correct_time_window']:.2f})")
    print(f"  real_task:          {avg_by_dim['real_task']:.3f} (threshold: {thresholds['real_task']:.2f})")
    print(f"  fail_closed:        {avg_by_dim['fail_closed']:.3f} (threshold: {thresholds['fail_closed']:.2f})")
    print(f"  overall:            {avg_by_dim['overall']:.3f} (threshold: {thresholds['overall']:.2f})")

    # Check against thresholds
    failures = []
    for dim in thresholds.keys():
        if avg_by_dim[dim] < thresholds[dim]:
            failures.append(
                f"{dim}: {avg_by_dim[dim]:.3f} < {thresholds[dim]:.2f}"
            )

    if failures:
        print(f"\n✗ Threshold violations (CI FAIL):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"\n✓ All thresholds met (CI PASS)")
        sys.exit(0)


if __name__ == "__main__":
    main()

"""smoke_graded_threshold — WS-163 LIVE graded-eval regression gate (server lane).

Runs the current chat smoke suite against a LIVE server and fails if the live graded
averages regress below the committed DeepSeek baseline (baseline_deepseek.json) by more
than a tolerance. This is the live-lane half of acceptance #4 ("graded 分数接进回归网当阈值"):

  * Offline half  → tests/smoke_graded_decision.py  (runs in `make test` / gate.yml,
                    no server: coverage + decision① + committed-baseline floors).
  * Live half     → THIS file (runs in the chat-e2e live lane via ci_chat_e2e_gate.sh,
                    has a real server: live responses must not regress vs baseline).

Because it needs uvicorn it is EXCLUDED from `make test` autodiscovery (see Makefile and
smoke_makefile_autodiscover.py) and is invoked explicitly by tests/ci_chat_e2e_gate.sh.

fail-closed: the live lane is supposed to have a server. If HIPOP_GRADED_REQUIRE_SERVER=1
(set by ci_chat_e2e_gate.sh) and the server is unreachable, this EXITS 1 (RED) — it must
never report a silent green when it could not actually measure anything. Only when the
gate is invoked OUTSIDE the live lane with no server and no REQUIRE flag does it skip(0).

Thresholds: live_avg[dim] must be ≥ baseline_avg[dim] − HIPOP_GRADED_REGRESS_TOL (0.07).
The floor is DERIVED from the committed baseline, not a hand-tuned constant — so it tracks
the real measured level and cannot be quietly relaxed to "make today pass".

Usage:
  python3 tests/smoke_graded_threshold.py [--url http://127.0.0.1:8765]
  python3 tests/smoke_graded_threshold.py --from-json /tmp/chat-smoke.json
  python3 tests/smoke_graded_threshold.py --check-baseline-decision  # delegates offline gate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import smoke_chat

DEEPSEEK_BASELINE = os.path.join(HERE, "baseline_deepseek.json")
REGRESS_TOL = float(os.environ.get("HIPOP_GRADED_REGRESS_TOL", "0.07"))
DIMS = ["correct_source", "correct_time_window", "real_task", "fail_closed", "overall"]


def baseline_floors() -> dict:
    """Regression floors = committed DeepSeek baseline averages − tolerance."""
    with open(DEEPSEEK_BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    avgs = base.get("averages", {})
    return {d: round(avgs.get(d, 0.0) - REGRESS_TOL, 3) for d in DIMS}


def check_baseline_decision() -> int:
    """Backward-compatible alias — the decision①/coverage logic now lives in the offline gate."""
    from tests import smoke_graded_decision
    return smoke_graded_decision.main()


def averages_from_json(path: str) -> Tuple[dict, int]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    avgs = data.get("averages") or {}
    if all(d in avgs for d in DIMS):
        count = len(data.get("cases") or [])
        return {d: float(avgs[d]) for d in DIMS}, count
    cases = data.get("cases") or []
    if not cases:
        raise ValueError("graded JSON has no averages or cases")
    grades = [c.get("grades") or {} for c in cases]
    return {d: sum(float(g.get(d, 0.0)) for g in grades) / len(grades) for d in DIMS}, len(grades)


def report_averages(avg: dict, floors: dict, *, count: int, elapsed: Optional[float] = None) -> int:
    suffix = f", {elapsed:.1f}s" if elapsed is not None else ""
    print(f"\n--- live averages ({count} cases{suffix}) ---")
    failures = []
    for d in DIMS:
        mark = "✓" if avg[d] >= floors[d] else "✗"
        print(f"  {d:<20} live {avg[d]:.3f}  floor {floors[d]:.3f}  {mark}")
        if avg[d] < floors[d]:
            failures.append(f"{d}: live {avg[d]:.3f} < floor {floors[d]:.3f} (regression vs baseline)")

    if failures:
        print("\n✗ LIVE GRADED REGRESSION (CI RED):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✓ live graded averages within tolerance of baseline (no regression).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="WS-163 live graded eval regression gate")
    ap.add_argument("--url", default=os.environ.get("HIPOP_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--from-json", help="grade the JSON matrix emitted by smoke_chat.py --json-output")
    ap.add_argument("--filter", help="only run cases matching this keyword")
    ap.add_argument("--check-baseline-decision", action="store_true",
                    help="run the OFFLINE decision①/coverage gate (no server) and exit")
    args = ap.parse_args()

    if args.check_baseline_decision:
        return check_baseline_decision()

    floors = baseline_floors()
    print("=== WS-163 live graded-eval regression gate ===")
    print(f"regression floors (baseline − {REGRESS_TOL}): {floors}")

    if args.from_json:
        print(f"source JSON: {args.from_json}")
        try:
            avg, count = averages_from_json(args.from_json)
        except Exception as e:
            print(f"\n✗ graded JSON unreadable: {type(e).__name__}: {e}")
            return 1
        return report_averages(avg, floors, count=count)

    print(f"URL: {args.url}")

    require_server = os.environ.get("HIPOP_GRADED_REQUIRE_SERVER") == "1"
    try:
        smoke_chat.ensure_smoke_user_tenant1()
        opener = smoke_chat.build_authenticated_opener(args.url)
    except Exception as e:
        if require_server:
            print(f"\n✗ server unreachable but HIPOP_GRADED_REQUIRE_SERVER=1 → FAIL-CLOSED (RED): "
                  f"{type(e).__name__}: {e}")
            return 1
        print(f"\n⚠️  server not available ({type(e).__name__}); this gate needs a live server.")
        print("   Not in live lane (HIPOP_GRADED_REQUIRE_SERVER unset) → skip(0).")
        print("   In CI the live lane sets HIPOP_GRADED_REQUIRE_SERVER=1 so a missing server is RED.")
        return 0
    smoke_chat._AUTH_OPENER = opener

    cases = [c for c in smoke_chat.CASES if (not args.filter) or args.filter in c.name]
    try:
        smoke_chat._bind_runtime_expectations(cases)
        smoke_chat._prepare_dynamic_expectations(args.url, opener)
    except Exception as e:
        print(f"\n✗ fixture prep failed: {e}")
        return 1

    print(f"Running {len(cases)} cases...\n")
    graded = []
    t0 = time.time()
    for i, c in enumerate(cases, 1):
        resp = smoke_chat.post_chat(opener, args.url, c.question, c.store, c.timeout)
        g = smoke_chat.grade_case(c, resp)
        graded.append(g)
        print(f"[{i}/{len(cases)}] {c.name[:48]:<48} {g['overall']:.2f}")

    avg = {d: sum(x[d] for x in graded) / len(graded) for d in DIMS}
    return report_averages(avg, floors, count=len(graded), elapsed=time.time() - t0)


if __name__ == "__main__":
    sys.exit(main())

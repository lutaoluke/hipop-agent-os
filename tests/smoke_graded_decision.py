"""smoke_graded_decision — WS-163 OFFLINE graded-eval regression + decision① gate.

This is the load-bearing answer to acceptance #4 ("把 graded 分数接进 Phase4 回归网当阈值,
不只 pass/fail"). It is named `smoke_*.py` so the Makefile auto-discovers it and it runs
inside the REQUIRED `make test` lane (gate.yml) — NO server, NO LLM, fully deterministic,
and it genuinely goes RED on regression. Unlike smoke_graded_threshold.py (which needs a
live server and therefore only runs in the live chat-e2e lane), this gate reads the
COMMITTED baseline matrices and enforces three invariants on every PR:

  1. COVERAGE (fail-closed) — every current smoke_chat.CASES case must be present in the
     committed DeepSeek baseline. If someone adds a case to smoke_chat without re-running
     the baseline, this gate goes RED. This closes the verifier's "missing cases silently
     pass" hole: a case absent from the baseline can no longer be silently ignored.

  2. DECISION① (KEEP DeepSeek) — over the DeepSeek×strong-model overlap, the average graded
     gap must stay ≤ GAP_MAX and the keep-rate ≥ KEEP_PCT_MIN. If the strong model ever
     pulls decisively ahead (gap blows past tolerance / keep-rate collapses), the gate goes
     RED — that is the real "switch model" signal, not a test bug. The strong-model arm must
     also cover the full case set (symmetry), else RED.

  3. BASELINE FLOOR (regression net) — the committed DeepSeek averages must stay at/above
     documented floors. Floors are set BELOW the measured baseline by a regression margin
     (current − ~0.05), so a future baseline regenerated against a weaker/regressed model
     trips the gate. Floors are NOT tuned down to "make today pass" — they sit under today's
     numbers on purpose so a drop is caught.

Run:  python3 tests/smoke_graded_decision.py        # exits non-zero (RED) on any violation
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import smoke_chat

DEEPSEEK_BASELINE = os.path.join(HERE, "baseline_deepseek.json")
STRONG_BASELINE = os.path.join(HERE, "baseline_opus.json")

# ── Decision① thresholds (gap between production model and strong-model baseline) ──
GAP_MAX = float(os.environ.get("HIPOP_GRADED_GAP_MAX", "0.05"))
KEEP_PCT_MIN = float(os.environ.get("HIPOP_GRADED_KEEP_PCT_MIN", "90"))

# ── DeepSeek regression floors (documented as: measured baseline − ~0.05 margin) ──
# Measured 2026-06-11 DeepSeek 31-case: overall 0.928 / source 0.858 / time 0.935 /
# real_task 0.935 / fail_closed 0.984. Floors sit below those so a real regression trips.
BASELINE_FLOORS = {
    "overall": float(os.environ.get("HIPOP_GRADED_OVERALL_MIN", "0.88")),
    "correct_source": float(os.environ.get("HIPOP_GRADED_SOURCE_MIN", "0.80")),
    "correct_time_window": float(os.environ.get("HIPOP_GRADED_TIME_MIN", "0.85")),
    "real_task": float(os.environ.get("HIPOP_GRADED_REAL_TASK_MIN", "0.85")),
    "fail_closed": float(os.environ.get("HIPOP_GRADED_FAIL_CLOSED_MIN", "0.90")),
}


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _by_question(matrix: dict) -> dict:
    """Index a baseline matrix by the STABLE `question` key.

    smoke_chat mutates Case.name at runtime (dynamic fixtures), so name is not a stable
    join key. question is never mutated — use it for both coverage and gap joins."""
    return {c["question"]: c for c in matrix.get("cases", []) if c.get("question")}


def evaluate(arm_a: dict, arm_b: dict, current_questions: list,
             gap_max: float = GAP_MAX, keep_pct_min: float = KEEP_PCT_MIN,
             floors: dict = None) -> tuple:
    """Pure, deterministic gate logic — no IO. Returns (failures, report).

    arm_a = production model (DeepSeek) matrix, arm_b = strong-model (Opus/GPT5.5) matrix,
    current_questions = stable `question` keys of the live smoke suite. `failures` empty
    ⇒ gate green. Kept side-effect-free so test_graded_eval.py can fail-then-pass it on
    synthetic matrices without touching the committed files."""
    floors = BASELINE_FLOORS if floors is None else floors
    failures: list = []
    report: dict = {}
    a_by_q = _by_question(arm_a)
    b_by_q = _by_question(arm_b)

    # ── 1) COVERAGE: every current smoke case must be in the DeepSeek (production) baseline ──
    missing_a = [q for q in current_questions if q not in a_by_q]
    if missing_a:
        failures.append(
            f"COVERAGE: {len(missing_a)} smoke case(s) absent from DeepSeek baseline "
            f"(re-run tests/run_baseline_arm.py): " + "; ".join(q[:40] for q in missing_a))

    # ── 2) SYMMETRY: strong-model arm must also cover the full set (decision① needs both) ──
    missing_b = [q for q in current_questions if q not in b_by_q]
    if missing_b:
        failures.append(
            f"COVERAGE: {len(missing_b)} smoke case(s) absent from strong-model baseline "
            f"(re-run Opus/GPT5.5 arm): " + "; ".join(q[:40] for q in missing_b))

    # Gap computed over the cases present in BOTH arms (the decision① comparison).
    overlap = [q for q in current_questions if q in a_by_q and q in b_by_q]
    gaps = {q: abs(b_by_q[q]["grades"]["overall"] - a_by_q[q]["grades"]["overall"]) for q in overlap}
    if not gaps:
        failures.append("DECISION①: no overlapping cases between the two baselines")
    else:
        avg_gap = sum(gaps.values()) / len(gaps)
        keep = sum(1 for g in gaps.values() if g <= gap_max)
        keep_pct = keep / len(gaps) * 100
        report.update(overlap=len(overlap), avg_gap=round(avg_gap, 3),
                      keep=keep, keep_pct=round(keep_pct, 1),
                      upgrade_cases=[(q, round(g, 3)) for q, g in gaps.items() if g > 0.15])
        if avg_gap > gap_max:
            failures.append(
                f"DECISION①: avg gap {avg_gap:.3f} > {gap_max} — strong model now decisively "
                f"ahead; re-evaluate KEEP-DeepSeek (this is a real signal, not a test bug)")
        if keep_pct < keep_pct_min:
            failures.append(
                f"DECISION①: keep-rate {keep_pct:.0f}% < {keep_pct_min:.0f}% — too many cases "
                f"depend on model strength; re-evaluate KEEP-DeepSeek")

    # ── 3) BASELINE FLOOR: committed DeepSeek averages must stay ≥ documented floors ──
    avgs = arm_a.get("averages", {})
    report["floors"] = {}
    for dim, floor in floors.items():
        got = avgs.get(dim)
        report["floors"][dim] = got
        if got is None:
            failures.append(f"FLOOR: DeepSeek baseline missing dim '{dim}'")
        elif got < floor:
            failures.append(f"FLOOR: DeepSeek {dim} {got:.3f} < {floor:.2f} (regression)")
    return failures, report


def main() -> int:
    if not os.path.exists(DEEPSEEK_BASELINE) or not os.path.exists(STRONG_BASELINE):
        print("✗ baseline matrices missing (baseline_deepseek.json / baseline_opus.json)")
        return 1

    arm_a = _load(DEEPSEEK_BASELINE)
    arm_b = _load(STRONG_BASELINE)
    current_questions = [c.question for c in smoke_chat.CASES]

    print("=== WS-163 graded-eval regression + decision① gate (offline) ===")
    print(f"smoke_chat.CASES        : {len(current_questions)}")
    print(f"DeepSeek baseline cases : {arm_a.get('case_count', len(arm_a.get('cases', [])))} ({arm_a.get('model')})")
    print(f"Strong-model baseline   : {arm_b.get('case_count', len(arm_b.get('cases', [])))} ({arm_b.get('model')})")

    failures, report = evaluate(arm_a, arm_b, current_questions)

    if "avg_gap" in report:
        print(f"\n--- decision① (overlap {report['overlap']} cases) ---")
        print(f"avg gap         : {report['avg_gap']:.3f}  (≤ {GAP_MAX})")
        print(f"keep DeepSeek   : {report['keep']}/{report['overlap']} "
              f"({report['keep_pct']:.0f}%)  (≥ {KEEP_PCT_MIN:.0f}%)")
        if report["upgrade_cases"]:
            print(f"upgrade signals : {report['upgrade_cases']}")
    print(f"\n--- DeepSeek baseline floors ---")
    for dim, floor in BASELINE_FLOORS.items():
        got = report.get("floors", {}).get(dim)
        mark = "✓" if (got is not None and got >= floor) else "✗"
        print(f"  {dim:<20} {got if got is None else f'{got:.3f}'} ≥ {floor:.2f} {mark}")

    if failures:
        print("\n✗ GRADED GATE FAILED (CI RED):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n✓ DECISION① VERIFIED: KEEP DeepSeek — coverage complete, gap within tolerance, "
          "no baseline regression.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

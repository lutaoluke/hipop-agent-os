"""run_baseline_arm — WS-163 deterministic baseline runner for ONE model arm.

Runs the full current smoke_chat.CASES suite against a live server, grades each
case with the deterministic 4-dim rubric (smoke_chat.grade_case), and exports a
JSON matrix in the format consumed by smoke_graded_decision / smoke_graded_threshold.

Why this exists (replaced an earlier shell orchestrator that killed whatever ran on :8765):
  * Reuses smoke_chat's own opener / fixture binding / grader — no parallel harness.
  * Absorbs transient infra jitter (HTTP 429 rate_limit, 5xx, network) by RETRYING the
    same case with backoff, so a rate-limited reply never gets graded as a model-quality
    failure. This is the same "retry to absorb LLM jitter, deterministic-real failures
    still fail" invariant the live chat gate uses — a 429 is infra, not a wrong answer.
  * Paces requests (--pace) so a subscription-rate-limited backend (e.g. Opus via OAuth)
    does not get throttled in the first place.

fail-closed: if a case still returns a transient/error reply after all retries, the run
ABORTS (exit 2) rather than writing a contaminated baseline. We never fabricate or grade
a rate-limited reply as if it were the model's real answer.

Usage:
  python3 tests/run_baseline_arm.py --url http://127.0.0.1:8799 \
      --output tests/baseline_deepseek.json --model-name deepseek-chat
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests import smoke_chat

# Markers that mean "infra hiccup, not a real model answer" — retry these.
_TRANSIENT_RE = re.compile(
    r"rate[_ ]?limit|Error code:\s*(?:429|5\d\d)|⚠️\s*chat error|timed? ?out|temporarily unavailable",
    re.IGNORECASE,
)


def _is_transient(resp: dict) -> bool:
    if resp.get("_http_error") in (429, 500, 502, 503, 504):
        return True
    if resp.get("_error"):
        return True
    reply = (resp.get("reply") or "") + (resp.get("clean_reply") or "")
    return bool(_TRANSIENT_RE.search(reply))


def post_with_retry(opener, url, c, retries: int, pace: float) -> dict:
    """Post one case, retrying transient infra failures with exponential backoff.

    Returns the first non-transient response. Raises RuntimeError if every attempt
    is transient (caller treats that as fail-closed abort, not a graded 0)."""
    last = None
    for attempt in range(1, retries + 2):  # retries extra attempts after the first
        resp = smoke_chat.post_chat(opener, url, c.question, c.store, c.timeout)
        if not _is_transient(resp):
            return resp
        last = resp
        wait = min(2 ** attempt, int(os.environ.get("HIPOP_BASELINE_MAX_BACKOFF", "30"))) + pace
        marker = resp.get("_error") or resp.get("_http_error") or (resp.get("reply") or "")[:80]
        print(f"    ↻ transient ({marker!r}); backoff {wait:.0f}s, attempt {attempt}/{retries+1}",
              flush=True)
        time.sleep(wait)
    raise RuntimeError(
        f"case still transient after {retries+1} attempts: {c.name} :: "
        f"{(last or {}).get('reply', last)!r}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="WS-163 single-arm baseline runner")
    ap.add_argument("--url", default=os.environ.get("HIPOP_URL", "http://127.0.0.1:8799"))
    ap.add_argument("--output", required=True, help="JSON matrix output path")
    ap.add_argument("--model-name", default=os.environ.get("MODEL_NAME", "unknown"))
    ap.add_argument("--retries", type=int, default=5, help="extra retries per case on transient failures")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds to sleep between cases")
    ap.add_argument("--filter", help="only run cases whose name contains this substring")
    ap.add_argument("--merge-into", help="load this existing matrix and add/replace measured "
                    "cases by question key (use to extend a clean baseline with new "
                    "DETERMINISTIC cases without re-running rate-limited LLM cases)")
    args = ap.parse_args()

    print(f"=== WS-163 baseline arm: {args.model_name} ===")
    print(f"URL: {args.url}  retries={args.retries}  pace={args.pace}s")

    try:
        smoke_chat.ensure_smoke_user_tenant1()
        opener = smoke_chat.build_authenticated_opener(args.url)
    except Exception as e:
        print(f"✗ smoke login failed: {type(e).__name__}: {e}")
        return 2
    smoke_chat._AUTH_OPENER = opener

    cases = [c for c in smoke_chat.CASES if (not args.filter) or args.filter in c.name]
    try:
        smoke_chat._bind_runtime_expectations(cases)
        smoke_chat._prepare_dynamic_expectations(args.url, opener)
    except Exception as e:
        print(f"✗ fixture prep failed: {type(e).__name__}: {e}")
        return 2

    print(f"Cases: {len(cases)}" + (f" (filter={args.filter!r})" if args.filter else "") + "\n")
    graded_results = []
    t0 = time.time()
    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c.name[:50]} ", end="", flush=True)
        t = time.time()
        try:
            resp = post_with_retry(opener, args.url, c, args.retries, args.pace)
        except RuntimeError as e:
            print(f"\n✗ FAIL-CLOSED ABORT: {e}")
            print("   Not writing a contaminated baseline (rate-limited reply ≠ model answer).")
            return 2
        ok, _ = smoke_chat.check(c, resp, opener, args.url)
        grades = smoke_chat.grade_case(c, resp)
        graded_results.append({
            "name": c.name,
            "question": c.question,   # STABLE key (name mutates at runtime; question does not)
            "pass": ok,
            "grades": grades,
        })
        print(f"{'✓' if ok else '✗'} {grades['overall']:.2f} ({time.time()-t:.1f}s)")
        if args.pace:
            time.sleep(args.pace)

    total = time.time() - t0

    # Optionally merge the freshly-measured cases into an existing baseline (by question key).
    if args.merge_into:
        with open(args.merge_into, encoding="utf-8") as f:
            base = json.load(f)
        by_q = {c["question"]: c for c in base.get("cases", []) if c.get("question")}
        for r in graded_results:
            by_q[r["question"]] = r          # add or replace
        cases_out = list(by_q.values())
        print(f"\nmerged {len(graded_results)} freshly-measured case(s) into "
              f"{args.merge_into} (was {len(base.get('cases', []))} → {len(cases_out)})")
    else:
        cases_out = graded_results

    avg = {}
    for dim in ["correct_source", "correct_time_window", "real_task", "fail_closed", "overall"]:
        avg[dim] = round(sum(r["grades"][dim] for r in cases_out) / len(cases_out), 3)

    export = {
        "model": args.model_name,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "case_count": len(cases_out),
        "cases": cases_out,
        "averages": avg,
    }
    with open(args.output, "w") as f:
        json.dump(export, f, indent=2, ensure_ascii=False)
    print(f"\n✓ {len(cases_out)} cases total ({len(graded_results)} measured this run, "
          f"{total:.1f}s) → {args.output}")
    print(f"  averages: {avg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

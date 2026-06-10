# WS-163: Graded Evaluation Methodology (50-case Matrix)

## Executive Summary

Replaces pure pass/fail testing with **4-dimensional graded rubric (0-1 per dimension)**
to quantify:
1. Which test cases are solved by **deterministic routing + data gates** (→ keep DeepSeek)
2. Which still depend on **model capability upgrades** (→ flag for stronger model or mechanism design)

## 4-Dimensional Rubric

Each case produces 4 scores [0-1] aggregated into a weighted overall score:

### 1. **Correct Source** (correct_source)
Data came from real tool/API, not fabricated.

- **1.0**: All `must_use_tools` called + zero hallucination_warnings
- **0.5**: Some tools called but hallucination_warnings present (data risky)
- **0.0**: No tools called OR HTTP/network error

**Rationale**: Ensures Agent is anchored in real data. High warn count → low source score.

### 2. **Correct Time Window** (correct_time_window)
Query scoped to correct time frame (30d vs historical vs today).

- **1.0**: All time-specific `must_contain` patterns matched (e.g., "30天", "近30天")
- **0.5 - 0.99**: Partial match (e.g., 2/3 time patterns matched)
- **0.0**: Time window mismatch or fabricated dates

**Rationale**: Prevents "mixing baselines" (reporting all-time stats as 30d). Time constraints
separate query paths and discovery (if DeepSeek respects 30d-only routing, time score stays high
even without explicit "最近30天" phrasing — the tool enforces it).

### 3. **Real Task** (real_task)
No fake task_id, hallucinated entities, or non-existent SKUs.

- **1.0**: Zero hallucination_warnings + zero blacklist violations
- **0.5**: Hallucination_warnings present but no blacklist hits (moderate risk)
- **0.0**: Blacklist violations detected (e.g., "agent.diangou", "已为你导出" without tool)

**Rationale**: Audit trail. Blacklist = known hallucinations (vendor names, domains, false tool claims).

### 4. **Fail-Closed** (fail_closed)
Report missing data honestly; don't fabricate when unavailable.

- **1.0**: check() passes (all validations succeed)
- **0.5**: 1-2 failures but not systematic fabrication (partial correctness)
- **0.0**: Multiple failures suggesting made-up values across dimensions

**Rationale**: "Fail-closed" = stops early rather than invents. Gradual penalties vs binary fail.

### Overall Score
Weighted average (default 0.25 per dimension):
```
overall = correct_source*0.25 + correct_time_window*0.25 + real_task*0.25 + fail_closed*0.25
```

Weights customizable per case (for future refinement).

---

## Deterministic Routing Benefit Matrix

After collecting scores for DeepSeek **and** a stronger baseline model on the same 50 cases:

| Dimension | Case Example | DeepSeek Score | Baseline Score | Interpretation |
|-----------|--------------|---|---|---|
| Correct Source | T04 TBB0116A 30d | 1.0 | 1.0 | ✓ Routing prevents hallucination equally for both |
| Time Window | "不用刷新...最畅销" | 1.0 | 1.0 | ✓ Data gate enforces freshness equally |
| Real Task | "导出表格" | 0.8 | 1.0 | ⚠ Stronger model more honest; DeepSeek hallucinates sometimes |
| Fail-Closed | "STALE_TST001 缺失" | 0.7 | 0.95 | ⚠ Baseline more systematic in null-handling |

**Conclusion Matrix**:
- High scores for both → routing/data gates already solved → **keep DeepSeek**
- Low DeepSeek, High baseline → model capability gap → **flag for upgrade or pattern-based workaround**
- Low both → harness issue (tool routing?) → **fix deterministic logic**

---

## Validation & CI Integration

### 1. fail-then-pass Smoke Tests
- **tests/test_graded_eval.py**: 11 unit tests validating each dimension's scoring logic
  - Tests input patterns (tools called, patterns matched, blacklist hits)
  - Tests edge cases (partial matches, mixed results)
  - Tests weighted aggregation

### 2. Chat Smoke Test Output
- **smoke_chat.py main()** now outputs:
  - Full grading matrix (50 cases × 4 dims + overall)
  - Per-dimension averages (shows where DeepSeek excels/struggles)
  - Individual case scores inline (for triage)

### 3. Phase4 Regression Integration
- Current: smoke_chat.py runs standalone (offline, no browser automation)
- Future (E9.2): Browser-driven E2E tests + graded scoring → feed graded matrix into Phase4 threshold logic
- Hook point: `_prepare_dynamic_expectations()` and `check()` remain stable; grading layer is orthogonal

---

## Implementation Notes

### Deterministic = No Prompting
All rubric logic resides in `grade_case()` (Python, not LLM):
- Regex matching on reply (must_contain/must_not_contain)
- Tool presence check (must_use_tools in response)
- Hallucination_warnings signal detection (from runtime _safety)
- Check() validation (existing pass/fail logic)

**No SYSTEM_PROMPT rules, no learned thresholds.** Fully auditable, reproducible.

### Case Configuration
Cases can override `rubric_weights`:
```python
Case(
    name="...",
    rubric_weights={
        "correct_source": 0.4,      # Source more critical for this case
        "correct_time_window": 0.1,
        "real_task": 0.3,
        "fail_closed": 0.2,
    }
)
```
Default is [0.25, 0.25, 0.25, 0.25].

### Output Format
Matrix printed to stdout after smoke run:
```
Case                                            Pass  Source Time     Real   Closed  Overall
---------------------------------------------  ----  ------  ----   ----   -----   -------
数据更新时间问答（不能假说今天）                  PASS   1.00   1.00   1.00   1.00    1.00
T04 TBB0116A 30d 口径（...）                    PASS   1.00   1.00   0.95   1.00    0.99
...
AVERAGE                                                1.00   0.95   0.98   0.97    0.97
```

---

## Decision① Verification (WS-163 Implementation)

**Decision①**: "KEEP DeepSeek + invest in deterministic routing" is now **codified as executable verifier**:

### Evidence Files
- `tests/baseline_deepseek.json` — Arm A run (25 cases, avg 0.95)
- `tests/baseline_opus.json` — Arm B run (25 cases, avg 0.97)
- `tests/baseline_comparison.md` — Difference analysis & boundary conclusion

### Verifier
Run: `python3 tests/smoke_graded_threshold.py --check-baseline-decision`

Asserts:
- avg_gap ≤ 0.05 (threshold for "routing solved") ✓ 0.018
- keep_cases ≥ 90% (threshold for "sufficient coverage") ✓ 92% (23/25)
- conclusion = KEEP_DEEPSEEK (not UPGRADE_MODEL) ✓

**If this test fails**, the empirical basis for decision① no longer holds, and
the decision must be revisited (e.g., if newer baseline model shows large gaps,
or if DeepSeek regresses).

### Integration with CI
When make test runs (after WS-170 clears the gate):
```bash
smoke_graded_threshold.py --check-baseline-decision  # Verifies decision① holds
smoke_graded_threshold.py                             # Regression gate on current DeepSeek performance
```

---

## Next Steps (Phase4 / E9.2 Handoff)

1. **E9.2 Integration** (when browser harness ready):
   - Automate real-user-like walkthrough of 50 cases
   - Attach graded scores to workflow_task results
   - Feed scores into Phase4 E8.2 regression network threshold tuning
   - Use baseline_comparison.md gap analysis to set per-dimension thresholds

2. **Continuous Monitoring**:
   - Decision① verifier runs on every commit (baseline matrices as ground truth)
   - If avg_gap or keep_pct drift, escalate to human review
   - Basis for future model upgrade decisions if routing improvements plateau

---

## References

- **Anthropic "Building Agents"** (p.8): Baseline model → smaller model evaluation pattern
- **WS-163 Issue**: 50-case graded eval + model baseline comparison
- **E9.2 (WS-156)**: Browser replay harness (pending)
- **Phase4 E8.2**: Regression network + graded thresholds (future integration)

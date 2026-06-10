"""baseline_comparison_template.py — WS-163 DeepSeek vs Baseline Model Comparison

Template for running 50-case graded eval with two models and comparing results.

This is NOT a standalone smoke test — it's a framework to:
1. Run 50-case suite against DeepSeek (current model)
2. Run same 50-case suite against baseline model (GPT-4 / Claude 3.5)
3. Produce diff matrix + "decision①" conclusion (which cases stay on DeepSeek vs need upgrade)

Usage:
  # First, collect DeepSeek results (current state)
  python3 tests/smoke_chat.py --url http://localhost:8765 > deepseek_run.txt 2>&1

  # Then, swap model config to baseline (e.g., switch SYSTEM_PROMPT to use Claude 3.5)
  # and run again
  python3 tests/smoke_chat.py --url http://localhost:8765 > baseline_run.txt 2>&1

  # Then use this script to compare:
  python3 tests/baseline_comparison_template.py deepseek_run.txt baseline_run.txt

Expected Output:
  - Grading matrix for each model
  - Diff table (case name, DeepSeek score, Baseline score, gap, categorization)
  - Summary: "Deterministic Routing Benefit Matrix"
    - Green (gap ≤ 0.05): Routing/data gates already solved → keep DeepSeek
    - Yellow (gap 0.05-0.15): Minor model diff, maybe architecture fix
    - Red (gap > 0.15): Model capability gap → candidate for upgrade
  - Decision①: Edge case (score threshold) where DeepSeek is sufficient vs needs upgrade
"""

import json
import re
from typing import Dict, List, Tuple
from dataclasses import dataclass


@dataclass
class ModelRunResult:
    """One model's run results on 50-case suite."""
    model_name: str
    case_scores: Dict[str, dict]  # {case_name: {"overall": X, "source": Y, ...}}
    avg_overall: float
    avg_source: float
    avg_time_window: float
    avg_real_task: float
    avg_fail_closed: float


def parse_grading_matrix_from_output(text: str, model_name: str) -> ModelRunResult:
    """Parse grading matrix output from smoke_chat.py main().

    Looks for output like:
      Case                                            Pass  Source Time     Real   Closed  Overall
      数据更新时间问答（不能假说今天）                  PASS   1.00   1.00   1.00   1.00    1.00
      ...
      AVERAGE                                                1.00   0.95   0.98   0.97    0.97
    """
    # This is a template — actual parsing would extract the matrix table.
    # For now, we document the structure.
    raise NotImplementedError(
        "parse_grading_matrix_from_output requires parsing smoke_chat.py output. "
        "Implement by reading the table format or exporting to JSON in smoke_chat.py"
    )


def compute_benefit_matrix(deepseek: ModelRunResult, baseline: ModelRunResult) -> List[dict]:
    """Compare two model runs and categorize cases.

    Returns:
      [
        {
          "case_name": "...",
          "deepseek_overall": 0.95,
          "baseline_overall": 0.99,
          "gap": 0.04,
          "category": "green" | "yellow" | "red",
          "recommendation": "keep_deepseek" | "investigate" | "upgrade_model",
        },
        ...
      ]

    Categories:
      - Green (gap ≤ 0.05): Routing/data gates already mitigate model diff → keep DeepSeek
      - Yellow (gap 0.05-0.15): Minor model diff, maybe mechanism fix helps
      - Red (gap > 0.15): Clear model capability gap → candidate for upgrade
    """
    matrix = []

    for case_name in deepseek.case_scores.keys():
        if case_name not in baseline.case_scores:
            continue

        ds = deepseek.case_scores[case_name]["overall"]
        bl = baseline.case_scores[case_name]["overall"]
        gap = abs(bl - ds)

        if gap <= 0.05:
            category = "green"
            recommendation = "keep_deepseek"
        elif gap <= 0.15:
            category = "yellow"
            recommendation = "investigate"
        else:
            category = "red"
            recommendation = "upgrade_model"

        matrix.append({
            "case_name": case_name,
            "deepseek_overall": round(ds, 3),
            "baseline_overall": round(bl, 3),
            "gap": round(gap, 3),
            "category": category,
            "recommendation": recommendation,
        })

    return sorted(matrix, key=lambda x: x["gap"], reverse=True)


def compute_decision_boundary(deepseek: ModelRunResult, baseline: ModelRunResult) -> dict:
    """Determine decision① boundary: at what score is DeepSeek sufficient?

    Decision①: "What score threshold separates 'keep DeepSeek' from 'upgrade model'?"

    Logic:
      - Collect all cases where gap > 0.15 (clear model weakness)
      - Find median score across those cases
      - That's the "upgrade threshold": scores below this suggest model limits
      - Scores above this suggest deterministic routing already solved the case
    """
    # This is a template for the actual logic.
    red_cases = [
        c for c in compute_benefit_matrix(deepseek, baseline)
        if c["category"] == "red"
    ]
    green_cases = [
        c for c in compute_benefit_matrix(deepseek, baseline)
        if c["category"] == "green"
    ]

    if not red_cases:
        return {
            "decision": "keep_deepseek",
            "reason": "No significant model capability gaps found (all gaps ≤ 0.15)",
            "threshold": 0.85,  # Maintain baseline threshold
        }

    red_scores = [c["baseline_overall"] for c in red_cases]
    red_median = sorted(red_scores)[len(red_scores) // 2] if red_scores else 0.8

    return {
        "decision": "investigate_or_upgrade" if len(red_cases) > 5 else "keep_deepseek",
        "reason": (
            f"{len(red_cases)}/{len(compute_benefit_matrix(deepseek, baseline))} cases show "
            f"model gaps > 0.15 (baseline scores: {[round(s, 2) for s in red_scores]})"
        ),
        "upgrade_candidate_cases": [c["case_name"] for c in red_cases[:5]],
        "threshold": round(red_median, 2),
    }


if __name__ == "__main__":
    print(__doc__)
    print("\n⚠️  This is a template/framework, not a runnable smoke test.")
    print("   Implement the parsing and run this against captured smoke outputs.")

#!/bin/bash
# WS-163: Run baseline comparison (DeepSeek vs Opus) on 50-case suite
# Usage: bash tests/run_baseline_comparison.sh [--skip-arm-a] [--skip-arm-b]
#
# This script:
# 1. Starts uvicorn with Arm A (DeepSeek) → runs 50-case → exports JSON
# 2. Stops uvicorn, restarts with Arm B (Opus) → runs 50-case → exports JSON
# 3. Compares matrices, produces difference table + decision① conclusion
#
# Output: tests/baseline_*.json, tests/baseline_comparison.md

set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
ARM_A_OUTPUT="$REPO/tests/baseline_deepseek.json"
ARM_B_OUTPUT="$REPO/tests/baseline_opus.json"
COMPARISON_OUTPUT="$REPO/tests/baseline_comparison.md"

SKIP_ARM_A=0
SKIP_ARM_B=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-arm-a) SKIP_ARM_A=1; shift ;;
    --skip-arm-b) SKIP_ARM_B=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Utility: Find and kill any existing uvicorn process on :8765
kill_existing_server() {
  if lsof -i :8765 >/dev/null 2>&1; then
    echo "▶ Killing existing uvicorn on :8765..."
    lsof -ti :8765 | xargs kill -9 2>/dev/null || true
    sleep 2
  fi
}

# Utility: Wait for server to start
wait_for_server() {
  echo "▶ Waiting for server at $1..."
  for i in {1..30}; do
    if curl -s "$1/api/chat-history/ksa?limit=1" >/dev/null 2>&1; then
      echo "  Server ready!"
      return 0
    fi
    echo "  Attempt $i/30..."
    sleep 2
  done
  echo "✗ Server failed to start"
  return 1
}

###############################################################################
# ARM A: DeepSeek (current state)
###############################################################################

if [ $SKIP_ARM_A -eq 0 ]; then
  echo ""
  echo "=== ARM A: DeepSeek (current state) ==="
  echo ""

  kill_existing_server

  # Start with default env (DeepSeek)
  echo "▶ Starting uvicorn with DeepSeek..."
  cd "$REPO"
  timeout 60 bash start_server.sh >/dev/null 2>&1 &
  SERVER_PID=$!

  if ! wait_for_server "http://localhost:8765"; then
    kill $SERVER_PID 2>/dev/null || true
    exit 1
  fi

  echo "▶ Running 50-case smoke with DeepSeek..."
  /usr/bin/python3 tests/smoke_chat.py \
    --url "http://localhost:8765" \
    --json-output "$ARM_A_OUTPUT" \
    || {
      echo "✗ Arm A smoke failed"
      kill $SERVER_PID 2>/dev/null || true
      exit 1
    }

  echo "✓ Arm A complete. Output: $ARM_A_OUTPUT"
  kill $SERVER_PID 2>/dev/null || true
  sleep 3
else
  echo "⊘ Skipping Arm A (--skip-arm-a)"
fi

###############################################################################
# ARM B: Opus
###############################################################################

if [ $SKIP_ARM_B -eq 0 ]; then
  echo ""
  echo "=== ARM B: Opus (baseline model) ==="
  echo ""

  kill_existing_server

  # Start with Opus env vars
  echo "▶ Starting uvicorn with Opus..."
  cd "$REPO"
  export LLM_PROVIDER=anthropic
  export ANTHROPIC_CHAT_MODEL=claude-opus-4-8
  timeout 60 bash start_server.sh >/dev/null 2>&1 &
  SERVER_PID=$!

  if ! wait_for_server "http://localhost:8765"; then
    kill $SERVER_PID 2>/dev/null || true
    unset LLM_PROVIDER ANTHROPIC_CHAT_MODEL
    exit 1
  fi

  echo "▶ Running 50-case smoke with Opus..."
  /usr/bin/python3 tests/smoke_chat.py \
    --url "http://localhost:8765" \
    --json-output "$ARM_B_OUTPUT" \
    || {
      echo "✗ Arm B smoke failed"
      kill $SERVER_PID 2>/dev/null || true
      unset LLM_PROVIDER ANTHROPIC_CHAT_MODEL
      exit 1
    }

  echo "✓ Arm B complete. Output: $ARM_B_OUTPUT"
  kill $SERVER_PID 2>/dev/null || true
  unset LLM_PROVIDER ANTHROPIC_CHAT_MODEL
  sleep 3
else
  echo "⊘ Skipping Arm B (--skip-arm-b)"
fi

###############################################################################
# Compare and produce decision①
###############################################################################

echo ""
echo "=== Comparing Arm A vs Arm B ==="
echo ""

if [ ! -f "$ARM_A_OUTPUT" ] || [ ! -f "$ARM_B_OUTPUT" ]; then
  echo "✗ Missing JSON outputs. Arm A: $ARM_A_OUTPUT, Arm B: $ARM_B_OUTPUT"
  exit 1
fi

/usr/bin/python3 << 'PYTHON_COMPARISON'
import json
import sys
from pathlib import Path

ARM_A_FILE = "tests/baseline_deepseek.json"
ARM_B_FILE = "tests/baseline_opus.json"
COMPARISON_FILE = "tests/baseline_comparison.md"

# Load both arms
with open(ARM_A_FILE) as f:
    arm_a = json.load(f)
with open(ARM_B_FILE) as f:
    arm_b = json.load(f)

# Build comparison table
comparison = []
total_gap = 0
case_count = 0

for case_a in arm_a.get("cases", []):
    name_a = case_a["name"]
    case_b = next((c for c in arm_b.get("cases", []) if c["name"] == name_a), None)
    if not case_b:
        continue

    score_a = case_a["grades"]["overall"]
    score_b = case_b["grades"]["overall"]
    gap = abs(score_b - score_a)
    total_gap += gap
    case_count += 1

    # Categorize: gap ≈ 0 → routing solved → keep DeepSeek
    #            gap small (0.05-0.15) → minor diff
    #            gap significant (>0.15) → model capability needed
    if gap <= 0.05:
        category = "✓ keep_deepseek"
    elif gap <= 0.15:
        category = "⚠ investigate"
    else:
        category = "✗ upgrade_model"

    comparison.append({
        "case": name_a[:50],
        "deepseek": round(score_a, 3),
        "opus": round(score_b, 3),
        "gap": round(gap, 3),
        "category": category,
    })

# Decision① boundary: cases with gap > 0.15 suggest model capability is critical
upgrade_cases = [c for c in comparison if "upgrade_model" in c["category"]]
investigate_cases = [c for c in comparison if "investigate" in c["category"]]
keep_cases = [c for c in comparison if "keep_deepseek" in c["category"]]

avg_gap = total_gap / case_count if case_count else 0

# Write comparison markdown
with open(COMPARISON_FILE, "w") as f:
    f.write("# WS-163: DeepSeek vs Opus Baseline Comparison\n\n")
    f.write(f"## Summary\n\n")
    f.write(f"- Cases analyzed: {case_count}\n")
    f.write(f"- Average gap: {avg_gap:.3f}\n")
    f.write(f"- Keep DeepSeek: {len(keep_cases)} cases (gap ≤ 0.05)\n")
    f.write(f"- Investigate: {len(investigate_cases)} cases (gap 0.05-0.15)\n")
    f.write(f"- Upgrade model: {len(upgrade_cases)} cases (gap > 0.15)\n\n")

    f.write("## Difference Matrix\n\n")
    f.write("| Case | DeepSeek | Opus | Gap | Category |\n")
    f.write("|------|----------|------|-----|----------|\n")
    for c in sorted(comparison, key=lambda x: x["gap"], reverse=True):
        f.write(f"| {c['case']} | {c['deepseek']:.3f} | {c['opus']:.3f} | {c['gap']:.3f} | {c['category']} |\n")

    f.write("\n## Decision① Boundary Conclusion\n\n")
    if avg_gap < 0.05:
        f.write("**Recommendation: KEEP DeepSeek + invest in deterministic routing**\n\n")
        f.write(f"Average gap {avg_gap:.3f} indicates routing/data gates already mitigate model differences. ")
        f.write("Focus on extending mechanism design (e.g., better case-specific guardrails) rather than model upgrade.\n")
    elif avg_gap < 0.15:
        f.write("**Recommendation: HYBRID — Keep DeepSeek for solved cases, Opus for critical paths**\n\n")
        f.write(f"Average gap {avg_gap:.3f} shows medium model influence. ")
        f.write(f"Promote {len(upgrade_cases)} high-gap cases to Opus, keep remainder on DeepSeek.\n")
    else:
        f.write("**Recommendation: UPGRADE to Opus (or equivalent) + redesign deterministic gates**\n\n")
        f.write(f"Average gap {avg_gap:.3f} indicates model capability is critical. ")
        f.write(f"{len(upgrade_cases)} cases show significant performance differences. ")
        f.write("Consider moving to Opus as primary model with DeepSeek as fallback.\n")

    if upgrade_cases:
        f.write(f"\n### High-gap cases (upgrade candidates):\n")
        for c in sorted(upgrade_cases, key=lambda x: x["gap"], reverse=True)[:10]:
            f.write(f"- {c['case']}: DeepSeek {c['deepseek']:.2f} vs Opus {c['opus']:.2f} (gap {c['gap']:.2f})\n")

print(f"✓ Comparison written to {COMPARISON_FILE}")
PYTHON_COMPARISON

echo "✓ Baseline comparison complete!"
echo ""
echo "=== Outputs ==="
echo "- Arm A (DeepSeek): $ARM_A_OUTPUT"
echo "- Arm B (Opus): $ARM_B_OUTPUT"
echo "- Comparison (Decision①): $COMPARISON_OUTPUT"

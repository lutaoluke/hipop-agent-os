"""WS-68 smoke: 1688 supply fallback and evidence states."""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_phase1 import (
    test_selection_phase1_1688_login_unavailable_is_evidence_insufficient,
    test_selection_phase1_1688_low_similarity_not_high_confidence_same_match,
    test_selection_phase1_1688_text_fallback_keeps_supply_candidate,
)


if __name__ == "__main__":
    test_selection_phase1_1688_text_fallback_keeps_supply_candidate()
    print("  ✓ test_selection_phase1_1688_text_fallback_keeps_supply_candidate")
    test_selection_phase1_1688_login_unavailable_is_evidence_insufficient()
    print("  ✓ test_selection_phase1_1688_login_unavailable_is_evidence_insufficient")
    test_selection_phase1_1688_low_similarity_not_high_confidence_same_match()
    print("  ✓ test_selection_phase1_1688_low_similarity_not_high_confidence_same_match")

"""WS-66 smoke: frozen offline fixtures for selection deterministic rules."""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_phase1 import (
    test_selection_phase1_deterministic_rules_run_in_production_path,
    test_selection_phase1_evidence_insufficient_is_explicit_offline,
    test_selection_inventory_malformed_rows_returns_evidence_insufficient,
    test_selection_inventory_type_a_stock_no_size_returns_evidence_insufficient,
    test_selection_inventory_type_b_size_no_stock_returns_evidence_insufficient,
)


if __name__ == "__main__":
    test_selection_phase1_deterministic_rules_run_in_production_path()
    print("  ✓ test_selection_phase1_deterministic_rules_run_in_production_path")
    test_selection_phase1_evidence_insufficient_is_explicit_offline()
    print("  ✓ test_selection_phase1_evidence_insufficient_is_explicit_offline")
    test_selection_inventory_malformed_rows_returns_evidence_insufficient()
    print("  ✓ test_selection_inventory_malformed_rows_returns_evidence_insufficient")
    test_selection_inventory_type_a_stock_no_size_returns_evidence_insufficient()
    print("  ✓ test_selection_inventory_type_a_stock_no_size_returns_evidence_insufficient")
    test_selection_inventory_type_b_size_no_stock_returns_evidence_insufficient()
    print("  ✓ test_selection_inventory_type_b_size_no_stock_returns_evidence_insufficient")

"""WS-121 smoke: chat ops facts need deterministic evidence.

Runs the focused phase tests through the Makefile's smoke_*.py auto-discovery.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tests import test_phase1  # noqa: E402


TESTS = [
    test_phase1.test_ws121_blocks_window_sales_topn_without_top_sales_tool,
    test_phase1.test_ws121_allows_window_sales_topn_with_top_sales_tool,
    test_phase1.test_ws121_today_sales_topn_is_window_route_not_bare_bucket,
    test_phase1.test_ws121_blocks_stock_ranking_without_total_stock_tool,
    test_phase1.test_ws121_allows_stock_ranking_with_total_stock_tool,
    test_phase1.test_ws121_blocks_logistics_status_without_live_tool,
    test_phase1.test_ws121_allows_logistics_status_with_live_tool,
    test_phase1.test_ws121_allows_workflow_or_unavailable_without_fake_facts,
]


if __name__ == "__main__":
    print("=== WS-121 ops evidence safety smoke ===")
    for test in TESTS:
        test()
        print(f"  [PASS] {test.__name__}")
    print("WS-121 ops evidence safety smoke PASS")

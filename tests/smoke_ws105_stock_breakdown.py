"""WS-105 smoke: query_stock_breakdown tool 单 SKU 库存拆分（接线 + 真实值验证）。"""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_phase1 import (
    test_ws105_t12_tbb0116a_stock_breakdown,
    test_ws105_t11_tbp0169a_stock_breakdown,
    test_ws105_query_stock_breakdown_in_tool_funcs,
)


if __name__ == "__main__":
    tests = [
        test_ws105_query_stock_breakdown_in_tool_funcs,
        test_ws105_t12_tbb0116a_stock_breakdown,
        test_ws105_t11_tbp0169a_stock_breakdown,
    ]
    passed, failed = 0, []
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed.append(t.__name__)
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        import sys as _sys
        _sys.exit(1)

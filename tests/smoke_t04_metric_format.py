"""T04 deterministic SKU metric reply formatting.

Round-18 regression: chat must never expose Python None for a missing metric.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server.agent import _format_sku_metric_reply


def test_missing_history_total_is_user_facing_unavailable():
    reply = _format_sku_metric_reply(
        "TBB0116A",
        {
            "items": [
                {
                    "sku": "TBB0116A",
                    "found": True,
                    "data_stale": False,
                    "as_of_date": "2026-06-09",
                    "sales_30d": 54,
                    "total_orders_30d": 43,
                    "history_total": None,
                    "return_rate_30d": 0,
                    "cancel_rate_30d": 0,
                }
            ]
        },
    )
    assert "历史总销量" in reply
    assert "暂无数据" in reply
    assert "None" not in reply


def test_missing_primary_metrics_are_not_rendered_as_none():
    reply = _format_sku_metric_reply(
        "TBB0116A",
        {
            "items": [
                {
                    "sku": "TBB0116A",
                    "found": True,
                    "data_stale": False,
                    "as_of_date": "2026-06-09",
                    "sales_30d": None,
                    "total_orders_30d": None,
                    "history_total": None,
                    "return_rate_30d": 0,
                    "cancel_rate_30d": 0,
                }
            ]
        },
    )
    assert reply.count("暂无数据") >= 3
    assert "None" not in reply


if __name__ == "__main__":
    tests = [
        test_missing_history_total_is_user_facing_unavailable,
        test_missing_primary_metrics_are_not_rendered_as_none,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"✗ {test.__name__}: {exc}")
    if failed:
        print(f"{failed}/{len(tests)} failed")
        sys.exit(1)
    print(f"{len(tests)}/{len(tests)} passed")

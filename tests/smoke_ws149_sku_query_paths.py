"""WS-149 smoke: query_sku_live must be live-only and source-labelled.

FAIL before WS-149:
  ERP login failure silently falls back to wf3 cache and returns ok=True.

PASS after WS-149:
  query_sku_live has a clear boundary: realtime ERP logistics only. If realtime
  is unavailable it fails closed, does not read wf3 cache, and the response
  carries source/time metadata for callers.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _restore(module, originals):
    for name, value in originals.items():
        setattr(module, name, value)


def test_query_sku_live_login_failure_fails_closed_without_wf3_cache():
    from hipop.server import agent

    cache_calls = []
    originals = {
        "_erp_token_or_error": agent._erp_token_or_error,
    }
    if hasattr(agent, "_query_sku_from_cache"):
        originals["_query_sku_from_cache"] = agent._query_sku_from_cache

    def fake_token_or_error(tid):
        return None, {
            "ok": False,
            "error": "erp_login_failed",
            "message": "simulated ERP login failure",
        }

    def fake_cache(sku, tid):
        cache_calls.append((sku, tid))
        return {
            "ok": True,
            "sku": sku,
            "fetched_from": "wf3_logistics_hub_v2 cache",
            "stale_warn": "old cache data",
            "in_transit_total_qty": 99,
        }

    try:
        agent._erp_token_or_error = fake_token_or_error
        if hasattr(agent, "_query_sku_from_cache"):
            agent._query_sku_from_cache = fake_cache
        result = agent.tool_query_sku_live("WS149-SKU")
    finally:
        _restore(agent, originals)

    assert cache_calls == [], f"query_sku_live must not read wf3 cache on live failure: {cache_calls}"
    assert result.get("ok") is False, f"live failure must fail closed, got: {result}"
    assert result.get("error") == "erp_login_failed_no_cache", result
    assert result.get("cache_fallback") is False, result
    assert result.get("source") == "ERP /delivery (realtime)", result
    assert result.get("fetched_at"), result
    assert "stale_warn" not in result, result


def test_query_sku_live_success_labels_live_source_and_time():
    from hipop.server import agent

    class FakeWf0:
        get_erp_token = None

    class FakeWls:
        get_erp_token = None

        @staticmethod
        def collect_sku_orders(sku, token):
            assert sku == "WS149-SKU"
            assert token == "token-1"
            return (
                [
                    {
                        "order_no": "PDZ-WS149",
                        "qty": 7,
                        "logistics_name": "安时达",
                        "tracking_no": "AST149",
                        "delivery_at": "2026-06-09 10:00:00",
                    }
                ],
                [
                    {
                        "order_no": "PDZ-OLD",
                        "logistics_name": "安时达",
                        "delivery_at": "2026-06-01 10:00:00",
                    }
                ],
            )

    originals = {
        "_erp_token_or_error": agent._erp_token_or_error,
        "_patch_wls_token": agent._patch_wls_token,
        "_physical_tracking_url": agent._physical_tracking_url,
    }

    try:
        agent._erp_token_or_error = lambda tid: ("token-1", None)
        agent._patch_wls_token = lambda token: (FakeWf0, FakeWls, "orig-token-fn")
        agent._physical_tracking_url = lambda forwarder, tracking_no: f"https://track.test/{tracking_no}"
        result = agent.tool_query_sku_live("WS149-SKU")
    finally:
        _restore(agent, originals)

    assert result.get("ok") is True, result
    assert result.get("source") == "ERP /delivery (realtime)", result
    assert result.get("fetched_from") == "ERP realtime", result
    assert result.get("cache_fallback") is False, result
    assert result.get("fetched_at"), result
    assert result.get("in_transit_total_qty") == 7, result
    assert result.get("references") == [
        {"table": "ERP /delivery (realtime)", "where": "keyword=WS149-SKU", "as_of_date": "now"}
    ], result


def run():
    failures = []
    tests = [
        test_query_sku_live_login_failure_fails_closed_without_wf3_cache,
        test_query_sku_live_success_labels_live_source_and_time,
    ]
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"  [FAIL] {fn.__name__}\n         -> {type(e).__name__}: {e}")
            traceback.print_exc()
    if failures:
        print(f"x {len(failures)} failures")
        return 1
    print("OK ws149 sku query path smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())

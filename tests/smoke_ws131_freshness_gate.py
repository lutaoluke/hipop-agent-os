"""Smoke: WS-131 freshness gate.

Acceptance:
  1. Live success may answer numbers, with source/update time.
  2. Live failure + <=3 day cache asks for operator consent before any number.
  3. After explicit consent, <=3 day cache may answer and must be marked as cache.
  4. Live failure + >3 day cache / missing timestamp / missing cache fails closed.
  5. TopN/list consumers reuse the same freshness decision and show update time.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["HIPOP_DB"] = _TMP_DB
os.environ.pop("DB_URL", None)

TENANT = 1
ALIAS = "hipop_ksa"
SKU = "WS131SKU1"
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"missing CREATE TABLE for {table}"
    return m.group(0)


def _setup_db() -> None:
    c = sqlite3.connect(_TMP_DB)
    for table in (
        "sales_entities",
        "wf2_sku",
        "wf2_orders",
        "wf1_stock",
        "wf5_sales_cycle",
        "wf3_logistics_hub_v2",
    ):
        c.executescript(_extract_create(table))
    c.execute(
        "INSERT OR IGNORE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
    )
    c.commit()
    c.close()


def _reset_rows() -> None:
    c = sqlite3.connect(_TMP_DB)
    for table in ("wf2_orders", "wf2_sku", "wf1_stock"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    c.close()


def _seed_sku_cache(days_old: int = 2, imported_at: str | None = None) -> None:
    as_of = (_dt.date.today() - _dt.timedelta(days=days_old)).isoformat()
    imported = imported_at if imported_at is not None else as_of + "T08:00:00"
    c = sqlite3.connect(_TMP_DB)
    c.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, as_of_date, imported_at, "
        " sales_30d, sales_10d, latest_profit_rate, is_listed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TENANT, ALIAS, SKU, "WS131 Test SKU", as_of, imported, 42, 12, 0.2, 1),
    )
    c.execute(
        "INSERT OR REPLACE INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, order_date) "
        "VALUES (?,?,?,?,?)",
        (TENANT, ALIAS, SKU, "ws131-order", as_of),
    )
    c.commit()
    c.close()


def _seed_topn_row(days_old: int = 0, updated_at: str | None = None) -> None:
    ts = updated_at if updated_at is not None else (
        _dt.date.today() - _dt.timedelta(days=days_old)
    ).isoformat()
    c = sqlite3.connect(_TMP_DB)
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "noon_total_qty, noon_saleable_qty, overseas_total_qty, yiwu_qty, dongguan_qty, "
        "pending_inbound_qty, total_stock, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, ALIAS, "TOP-FRESH", 100, 80, 20, 5, 5, 7, 137, ts),
    )
    c.commit()
    c.close()


def test_pure_freshness_gate_matrix() -> None:
    from hipop.scripts.freshness_gate import decide_freshness, render_freshness_suffix

    now = _dt.datetime(2026, 6, 9, 12, 0, 0)
    live = decide_freshness(
        live_ok=True,
        live_source="noon",
        live_fetched_at="2026-06-09T11:59:00Z",
        cache_available=True,
        cache_fetched_at="2026-06-07T09:00:00",
        now=now,
        subject="SKU 销量",
    )
    assert live["status"] == "live" and live["can_output_number"] is True, live
    assert "来源" in render_freshness_suffix(live) and "更新时间" in render_freshness_suffix(live)

    ask = decide_freshness(
        live_ok=False,
        live_error="ERP timeout",
        cache_available=True,
        cache_fetched_at="2026-06-07T09:00:00",
        operator_cache_consent=False,
        now=now,
        subject="SKU 销量",
    )
    assert ask["status"] == "ask_cache_consent" and ask["can_output_number"] is False, ask
    assert "是否使用缓存" in ask["message"], ask

    allowed = decide_freshness(
        live_ok=False,
        live_error="ERP timeout",
        cache_available=True,
        cache_fetched_at="2026-06-07T09:00:00",
        operator_cache_consent=True,
        now=now,
        subject="SKU 销量",
    )
    assert allowed["status"] == "cache_allowed" and allowed["can_output_number"] is True, allowed
    assert "缓存" in render_freshness_suffix(allowed), allowed

    rejected = decide_freshness(
        live_ok=False,
        live_error="ERP timeout",
        cache_available=True,
        cache_fetched_at="2026-06-07T09:00:00",
        operator_cache_rejected=True,
        now=now,
        subject="SKU 销量",
    )
    assert rejected["status"] == "blocked" and rejected["reason"] == "cache_rejected", rejected
    assert rejected["can_output_number"] is False and "不同意" in rejected["message"], rejected

    for label, kwargs in [
        ("too_old", {"cache_available": True, "cache_fetched_at": "2026-06-05T09:00:00"}),
        ("missing_time", {"cache_available": True, "cache_fetched_at": None}),
        ("missing_cache", {"cache_available": False, "cache_fetched_at": None}),
    ]:
        blocked = decide_freshness(
            live_ok=False,
            live_error="ERP timeout",
            operator_cache_consent=True,
            now=now,
            subject=f"SKU 销量 {label}",
            **kwargs,
        )
        assert blocked["status"] == "blocked" and blocked["can_output_number"] is False, blocked


def test_sku_live_failure_asks_then_uses_consented_cache() -> None:
    _reset_rows()
    _seed_sku_cache(days_old=2)

    import server.data as _data
    from server import agent as _agent

    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)
    _agent._chat_scope.set({"tenant_id": TENANT, "store": "KSA", "user": "ws131"})
    orig_live = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = lambda sku, nation_id, token: {
        "ok": False, "error": "erp_timeout", "message": "ERP 实时取数超时"
    }
    try:
        without_consent = _agent.tool_query_sku(
            [SKU], store="KSA", allow_cache_on_live_failure=False,
        )
        reply = _agent._format_sku_metric_reply(SKU, without_consent)
        assert "是否使用缓存" in reply, reply
        assert "42" not in reply, reply

        with_consent = _agent.tool_query_sku(
            [SKU], store="KSA", allow_cache_on_live_failure=True,
        )
        reply2 = _agent._format_sku_metric_reply(SKU, with_consent)
        assert "42" in reply2, reply2
        assert "缓存" in reply2 and "更新时间" in reply2, reply2

        rejected = _agent.tool_query_sku(
            [SKU],
            store="KSA",
            allow_cache_on_live_failure=False,
            reject_cache_on_live_failure=True,
        )
        reply3 = _agent._format_sku_metric_reply(SKU, rejected)
        assert "42" not in reply3, reply3
        assert "不同意" in reply3 and "不能使用缓存" in reply3, reply3
    finally:
        _agent._sku_sales_live_fn = orig_live


def test_sku_live_success_answers_with_source_time() -> None:
    _reset_rows()
    _seed_sku_cache(days_old=0)

    import server.data as _data
    from server import agent as _agent

    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)
    _agent._chat_scope.set({"tenant_id": TENANT, "store": "KSA", "user": "ws131"})
    orig_live = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = lambda sku, nation_id, token: {
        "ok": True,
        "sales_30d": 51,
        "history_total": 88,
        "fetched_at": "2026-06-09T10:30:00Z",
        "source": "ERP /product-order-statistics (realtime)",
    }
    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
        reply = _agent._format_sku_metric_reply(SKU, result)
        assert "51" in reply, reply
        assert "来源" in reply and "更新时间" in reply, reply
        assert "ERP /product-order-statistics" in reply, reply
    finally:
        _agent._sku_sales_live_fn = orig_live


def test_sku_blocks_old_or_timestampless_cache() -> None:
    import server.data as _data
    from server import agent as _agent

    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)
    _agent._chat_scope.set({"tenant_id": TENANT, "store": "KSA", "user": "ws131"})
    orig_live = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = lambda sku, nation_id, token: {
        "ok": False, "error": "erp_timeout", "message": "ERP 实时取数超时"
    }
    try:
        _reset_rows()
        _seed_sku_cache(days_old=4)
        old_reply = _agent._format_sku_metric_reply(
            SKU,
            _agent.tool_query_sku([SKU], store="KSA", allow_cache_on_live_failure=True),
        )
        assert "42" not in old_reply, old_reply
        assert "超过 3 天" in old_reply or "不能使用缓存" in old_reply, old_reply

        _reset_rows()
        _seed_sku_cache(days_old=0, imported_at="")
        no_time_reply = _agent._format_sku_metric_reply(
            SKU,
            _agent.tool_query_sku([SKU], store="KSA", allow_cache_on_live_failure=True),
        )
        assert "42" not in no_time_reply, no_time_reply
        assert "没有缓存时间" in no_time_reply or "缺少缓存时间" in no_time_reply, no_time_reply
    finally:
        _agent._sku_sales_live_fn = orig_live


def test_topn_uses_same_gate_and_displays_update_time() -> None:
    _reset_rows()
    _seed_topn_row(days_old=0)

    import server.data as _data
    from server import agent as _agent

    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)
    result = _agent.tool_total_stock_topn(store="KSA", n=1)
    assert result.get("freshness_decision", {}).get("status") == "cache_allowed", result
    reply = _agent._format_total_stock_topn_reply("KSA", result)
    assert "137" in reply, reply
    assert "来源" in reply and "取数时间" in reply, reply

    _reset_rows()
    _seed_topn_row(days_old=4)
    stale = _agent.tool_total_stock_topn(store="KSA", n=1)
    assert stale.get("fail_closed") is True, stale
    assert stale.get("freshness_decision", {}).get("status") == "blocked", stale

    _reset_rows()
    _seed_topn_row(updated_at="")
    no_time = _agent.tool_total_stock_topn(store="KSA", n=1)
    assert no_time.get("fail_closed") is True, no_time
    assert no_time.get("freshness_decision", {}).get("reason") == "cache_missing_timestamp", no_time


def main() -> None:
    _setup_db()
    tests = [
        test_pure_freshness_gate_matrix,
        test_sku_live_failure_asks_then_uses_consented_cache,
        test_sku_live_success_answers_with_source_time,
        test_sku_blocks_old_or_timestampless_cache,
        test_topn_uses_same_gate_and_displays_update_time,
    ]
    for fn in tests:
        print(f"▶ {fn.__name__}")
        fn()
        print(f"✓ {fn.__name__}")
    print("\n5/5 passed (WS-131 freshness gate)")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

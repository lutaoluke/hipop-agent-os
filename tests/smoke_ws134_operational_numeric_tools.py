"""WS-134 smoke: operational numeric answers require deterministic tools + evidence.

This covers the S6 aggregate contract across the existing T03/T04/T07/T11/T12/
T15/T27/T29 slices:

- sales TopN（『近30天』含时间窗）must route to top_sales_by_window（WS-120 choice A：
  逐单现算 + 按最新订单业务日倒推）but fail closed when the latest order date is stale
  (>3 days), instead of leaking old ranked numbers. （WS-120 前是 list_products/sales_30d
  固定桶；choice A 改走窗口工具，时效门 >3 天 fail-closed 这道承重墙保持不变。）
- general replenishment advice must route deterministically to
  compute_replenishment and must not use stale wf5 recommendations as facts.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.pop("DB_URL", None)

TENANT = 1
ALIAS = "hipop_ksa"
SCOPE = {"tenant_id": TENANT, "current_user": "smoke", "current_role": "admin", "store": "KSA"}
SCHEMA_V2 = REPO / "db" / "schema_v2.sql"


def _extract_create(table: str) -> str:
    sql = SCHEMA_V2.read_text(encoding="utf-8")
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert match, f"missing CREATE TABLE for {table}"
    return match.group(0)


def _fresh_db(*tables: str) -> tuple[str, sqlite3.Connection]:
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.environ["HIPOP_DB"] = path

    from hipop.server import data as _data

    _data.DB_PATH = path
    _data.set_current_tenant(TENANT)
    conn = sqlite3.connect(path)
    for table in tables:
        conn.executescript(_extract_create(table))
    conn.execute(
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
    )
    conn.commit()
    return path, conn


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def test_sales_topn_stale_snapshot_fails_closed() -> None:
    """近30天 TopN must not expose stale ranks. WS-120 choice A：改走 top_sales_by_window，
    最新订单业务日 >3 天即 fail-closed（时效承重墙不变），不泄陈旧名次。"""
    path, conn = _fresh_db("sales_entities", "wf2_sku", "wf2_orders")
    stale = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()   # 最新订单日 5 天前 > 3 天
    try:
        rows = [
            ("SKU-A", "PROD-A", "old low", 7, 90),
            ("SKU-B", "PROD-B", "old high", 30, 120),
            ("SKU-C", "PROD-C", "old mid", 18, 180),
        ]
        conn.executemany(
            "INSERT INTO wf2_sku "
            "(tenant_id, entity_alias, partner_sku, product_id, title, is_listed, "
            " sales_30d, sales_180d, latest_price, as_of_date, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(TENANT, ALIAS, r[0], r[1], r[2], 1, r[3], r[4], 10.0, stale, stale + "T09:00:00")
             for r in rows],
        )
        # wf2_orders：每个 SKU 一笔陈旧订单（最新订单日 = stale = today-5），制造「>3 天」陈旧态。
        conn.executemany(
            "INSERT INTO wf2_orders "
            "(tenant_id, entity_alias, partner_sku, item_nr, order_date, is_cancelled) "
            "VALUES (?,?,?,?,?,?)",
            [(TENANT, ALIAS, r[0], f"{r[0]}-1", stale, 0) for r in rows],
        )
        conn.commit()

        from hipop.server import agent as _agent
        from hipop.server import _provider

        _agent._chat_tenant.set(TENANT)
        with patch.object(_provider, "get_provider", return_value="smoke"), \
             patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
            result = _agent.chat(
                [{"role": "user", "content": "KSA 近30天销量最高的3个商品"}],
                SCOPE,
            )

        assert result.get("tools_used") == ["top_sales_by_window"], result
        assert result.get("judge_method") == "deterministic_window_sales_topn_router", result
        reply = result.get("reply") or ""
        assert "不能出数" in reply or "刷新" in reply or "超过 3 天" in reply, reply
        assert "SKU-B" not in reply, "stale ranked SKU leaked into reply: {}".format(reply)
    finally:
        conn.close()
        _cleanup(path)


def _seed_ready_stock_and_skus(conn: sqlite3.Connection, when: str, n: int = 20) -> None:
    sku_rows = []
    stock_rows = []
    for i in range(n):
        sku = "RSKU{:03d}".format(i)
        sku_rows.append((TENANT, ALIAS, sku, "Ready SKU {}".format(i), 1, 1, 1, when))
        stock_rows.append((TENANT, ALIAS, sku, 10, 8, 2, 5, 3, 2, 20, when, when))
    conn.executemany(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, is_listed, "
        " sales_30d, sales_180d, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        sku_rows,
    )
    conn.executemany(
        "INSERT INTO wf1_stock "
        "(tenant_id, entity_alias, partner_sku, noon_total_qty, noon_saleable_qty, "
        " noon_unsaleable_qty, overseas_total_qty, yiwu_qty, dongguan_qty, "
        " total_stock, imported_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        stock_rows,
    )


def _seed_wf5(conn: sqlite3.Connection, when: str) -> None:
    rows = [
        ("RSKU005", 30, "high", 2.5, "建议本周补货", "urgent"),
        ("RSKU003", 20, "mid", 1.5, "建议补货", "medium"),
    ]
    conn.executemany(
        "INSERT INTO wf5_sales_cycle "
        "(tenant_id, entity_alias, partner_sku, weekly_total_replenish, "
        " urgency, daily_rate, ops_advice, trend, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, sku, qty, urgency, daily, advice, "stable", when)
         for sku, qty, urgency, daily, advice, _priority in rows],
    )
    conn.commit()


def test_replenishment_list_routes_deterministically_and_blocks_stale_wf5() -> None:
    """General replenishment advice must be deterministic and stale wf5 rows cannot leak."""
    path, conn = _fresh_db("sales_entities", "wf2_sku", "wf1_stock", "wf5_sales_cycle")
    today = _dt.date.today().isoformat()
    stale = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()
    try:
        _seed_ready_stock_and_skus(conn, today)
        _seed_wf5(conn, stale + "T09:00:00")

        from hipop.server import agent as _agent
        from hipop.server import _provider

        _agent._chat_tenant.set(TENANT)
        direct = _agent.tool_compute_replenishment("KSA", limit=2)
        assert direct.get("fail_closed") is True, direct
        assert not direct.get("items"), direct

        with patch.object(_provider, "get_provider", return_value="smoke"), \
             patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
            result = _agent.chat(
                [{"role": "user", "content": "KSA 本周补货建议前2"}],
                SCOPE,
            )

        assert result.get("tools_used") == ["compute_replenishment"], result
        assert result.get("judge_method") == "deterministic_replenishment_list_router", result
        reply = result.get("reply") or ""
        assert "不能出数" in reply or "刷新" in reply or "超过 3 天" in reply, reply
        assert "RSKU005" not in reply, "stale replenishment SKU leaked into reply: {}".format(reply)
    finally:
        conn.close()
        _cleanup(path)


def run() -> int:
    tests = [
        test_sales_topn_stale_snapshot_fails_closed,
        test_replenishment_list_routes_deterministically_and_blocks_stale_wf5,
    ]
    failures = []
    for test in tests:
        try:
            test()
            print("  [PASS] {}".format(test.__name__))
        except Exception as exc:
            failures.append("{}: {}: {}".format(test.__name__, type(exc).__name__, exc))
            print("  [FAIL] {} -> {}: {}".format(test.__name__, type(exc).__name__, exc))
    if failures:
        print("\n".join(failures))
        return 1
    print("OK ws134 operational numeric tools smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

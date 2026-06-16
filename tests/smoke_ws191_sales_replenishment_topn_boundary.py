"""WS-191 smoke: sales-window TopN and replenishment TopN stay on separate routes.

The regression this binds:
  - Sales questions with an explicit time window (for example "KSA 近30天销量最高的3个商品")
    must use top_sales_by_window evidence, never the sales_30d/list_products snapshot.
  - Replenishment TopN phrasing (for example "KSA 补货 Top3 建议") must use
    compute_replenishment, not fall through to the provider and not get eaten by sales TopN.

Fail-before-fix on current main: "KSA 补货 Top3 建议" is not recognized by
_deterministic_replenishment_list_request, so the chat path reaches the provider.
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


def _d(days_ago: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).isoformat()


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


def _seed_sales_window_fixture(conn: sqlite3.Connection) -> None:
    # sales_30d snapshot intentionally disagrees with the order-window result.
    sku_rows = [
        ("SKU-WIN", "窗口冠军", 1, 1),
        ("SKU-BUCKET", "快照冠军", 1, 99),
        ("SKU-SECOND", "窗口第二", 1, 20),
        ("SKU-THIRD", "窗口第三", 1, 50),
    ]
    order_rows = [
        ("SKU-WIN", "W0", 29, 0),
        ("SKU-WIN", "W1", 0, 0),
        ("SKU-WIN", "W2", 1, 0),
        ("SKU-WIN", "W3", 5, 0),
        ("SKU-SECOND", "S1", 2, 0),
        ("SKU-SECOND", "S2", 4, 0),
        ("SKU-THIRD", "T1", 3, 0),
    ]
    conn.executemany(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, is_listed, sales_30d, as_of_date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, sku, title, listed, sales_30d, _d(0), f"{_d(0)}T09:00:00")
         for sku, title, listed, sales_30d in sku_rows],
    )
    conn.executemany(
        "INSERT INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, order_date, is_cancelled) "
        "VALUES (?,?,?,?,?,?)",
        [(TENANT, ALIAS, sku, item, _d(days), cancelled)
         for sku, item, days, cancelled in order_rows],
    )
    conn.commit()


def _seed_replenishment_fixture(conn: sqlite3.Connection) -> None:
    when = f"{_d(0)}T09:00:00"
    sku_rows = []
    stock_rows = []
    wf5_rows = [
        ("RSKU-A", "补货冠军", 30, "high", 3.0),
        ("RSKU-B", "补货第二", 20, "mid", 2.0),
        ("RSKU-C", "补货第三", 10, "low", 1.0),
    ]
    for sku, title, _qty, _urgency, _daily in wf5_rows:
        sku_rows.append((TENANT, ALIAS, sku, title, 1, 5, 10, _d(0), when))
        stock_rows.append((TENANT, ALIAS, sku, 10, 8, 2, 5, 3, 2, 20, when, when))
    for i in range(17):
        sku = f"RSKU-FILL-{i:02d}"
        sku_rows.append((TENANT, ALIAS, sku, f"库存就绪填充 {i}", 1, 1, 2, _d(0), when))
        stock_rows.append((TENANT, ALIAS, sku, 10, 8, 2, 5, 3, 2, 20, when, when))
    conn.executemany(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, is_listed, sales_30d, sales_180d, as_of_date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        sku_rows,
    )
    conn.executemany(
        "INSERT INTO wf1_stock "
        "(tenant_id, entity_alias, partner_sku, noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, "
        "overseas_total_qty, yiwu_qty, dongguan_qty, total_stock, imported_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        stock_rows,
    )
    conn.executemany(
        "INSERT INTO wf5_sales_cycle "
        "(tenant_id, entity_alias, partner_sku, weekly_total_replenish, urgency, daily_rate, "
        "ops_advice, trend, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, sku, qty, urgency, daily, "建议补货", "stable", when)
         for sku, _title, qty, urgency, daily in wf5_rows],
    )
    conn.commit()


def test_route_classifier_separates_window_sales_and_replenishment_topn() -> None:
    from hipop.server._deterministic_routes import (
        _deterministic_product_sales_topn_request,
        _deterministic_replenishment_sku_request,
        _deterministic_replenishment_list_request,
        _deterministic_window_sales_topn_request,
    )

    sales_window = "KSA 近30天销量最高的3个商品"
    assert _deterministic_window_sales_topn_request(sales_window) == {"relative_days": 30, "limit": 3}
    assert _deterministic_replenishment_list_request(sales_window) is None

    replenishment = "KSA 补货 Top3 建议"
    assert _deterministic_replenishment_list_request(replenishment) == 3
    assert _deterministic_window_sales_topn_request(replenishment) is None
    assert _deterministic_product_sales_topn_request(replenishment) is None

    assert _deterministic_window_sales_topn_request("KSA 销量最高的3个商品") is None
    assert _deterministic_product_sales_topn_request("KSA 销量最高的3个商品") == 3
    assert _deterministic_window_sales_topn_request("列出 KSA 商品") is None

    sku_replenishment = "TBS0228A 补货 pipeline"
    assert _deterministic_replenishment_sku_request(sku_replenishment) == "TBS0228A"
    assert _deterministic_replenishment_list_request(sku_replenishment) is None
    print("    route classifier: window sales / replenishment TopN / windowless sales stay separated")


def test_chat_sales_window_topn_uses_window_evidence_not_snapshot() -> None:
    path, conn = _fresh_db("sales_entities", "wf2_sku", "wf2_orders")
    try:
        _seed_sales_window_fixture(conn)

        from hipop.server import _provider
        from hipop.server import agent as _agent

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
        assert "SKU-WIN" in reply and "窗口销量" in reply, reply
        assert "SKU-BUCKET" not in reply, f"sales_30d snapshot leaked into window TopN: {reply!r}"
        assert "wf2_orders" in reply and "wf2_sku.sales_30d" not in reply, reply
        print("    chat sales-window TopN -> top_sales_by_window with wf2_orders evidence")
    finally:
        conn.close()
        _cleanup(path)


def test_chat_replenishment_topn_uses_replenishment_tool_not_sales() -> None:
    path, conn = _fresh_db("sales_entities", "wf2_sku", "wf1_stock", "wf5_sales_cycle")
    try:
        _seed_replenishment_fixture(conn)

        from hipop.server import _provider
        from hipop.server import agent as _agent

        _agent._chat_tenant.set(TENANT)
        with patch.object(_provider, "get_provider", return_value="smoke"), \
             patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
            result = _agent.chat(
                [{"role": "user", "content": "KSA 补货 Top3 建议"}],
                SCOPE,
            )

        assert result.get("tools_used") == ["compute_replenishment"], result
        assert result.get("judge_method") == "deterministic_replenishment_list_router", result
        reply = result.get("reply") or ""
        assert "RSKU-A" in reply and "建议补货 30" in reply, reply
        assert "wf5_sales_cycle" in reply and "窗口销量" not in reply, reply
        print("    chat replenishment TopN -> compute_replenishment, not sales/list_products")
    finally:
        conn.close()
        _cleanup(path)


def run() -> int:
    tests = [
        test_route_classifier_separates_window_sales_and_replenishment_topn,
        test_chat_sales_window_topn_uses_window_evidence_not_snapshot,
        test_chat_replenishment_topn_uses_replenishment_tool_not_sales,
    ]
    failures = []
    for test in tests:
        try:
            test()
            print(f"  [PASS] {test.__name__}")
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
            print(f"  [FAIL] {test.__name__} -> {type(exc).__name__}: {exc}")
    if failures:
        print("\n".join(failures))
        return 1
    print("\n3/3 passed (WS-191 sales/replenishment TopN boundary)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

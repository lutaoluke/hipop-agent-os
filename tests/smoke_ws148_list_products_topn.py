"""WS-148 smoke: list_products is the deterministic 30d sales TopN path.

FAIL before fix:
  - chat("近30天销量最高的3个商品") falls through to the LLM/provider path.
  - list_products returns sorted rows but does not declare the sales_30d TopN
    sort/evidence contract.

PASS after fix:
  - chat routes directly to list_products with limit=N.
  - limit=N means TopN by sales_30d DESC.
  - rendered reply carries source/time/coverage evidence before showing numbers.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from unittest.mock import patch


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = TMP_DB

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server import agent as _agent  # noqa: E402
from hipop.server import data as _data  # noqa: E402
from hipop.server import _provider as _provider  # noqa: E402


TENANT = 1
ALIAS = "hipop_ksa"
SCOPE = {"tenant_id": TENANT, "current_user": "test", "current_role": "admin", "store": "KSA"}
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert match, f"missing CREATE TABLE for {table}"
    return match.group(0)


def _setup_db() -> None:
    conn = sqlite3.connect(TMP_DB)
    conn.executescript(_extract_create("sales_entities"))
    conn.executescript(_extract_create("wf2_sku"))
    conn.execute(
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
    )
    rows = [
        ("SKU-A", "PROD-A", "低销量商品", 1, 7, 90, 19.0, "2026-06-08"),
        ("SKU-B", "PROD-B", "最高销量商品", 1, 30, 120, 29.0, "2026-06-08"),
        ("SKU-C", "PROD-C", "第二销量商品", 1, 18, 180, 39.0, "2026-06-08"),
        ("SKU-D", "PROD-D", "无销量商品", 0, None, 300, 49.0, "2026-06-08"),
    ]
    conn.executemany(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, product_id, title, is_listed, "
        "sales_30d, sales_180d, latest_price, as_of_date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, *r, "2026-06-08T09:00:00") for r in rows],
    )
    conn.commit()
    conn.close()
    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)


def test_tool_limit_means_sales_30d_topn() -> None:
    result = _agent.tool_list_products("KSA", listing="all", limit=3)
    sort = result.get("sort") or {}
    assert sort.get("field") == "sales_30d", f"sort field must be sales_30d, got {sort!r}"
    assert sort.get("direction") == "desc", f"sort direction must be desc, got {sort!r}"
    assert result.get("n_requested") == 3, f"limit=3 should be recorded as n_requested, got {result!r}"
    assert [item["sku"] for item in result.get("items", [])] == ["SKU-B", "SKU-C", "SKU-A"], (
        f"limit=3 should return sales_30d Top3, got {result.get('items')!r}"
    )
    evidence = result.get("evidence") or {}
    assert evidence.get("fetched_at") == "2026-06-08", f"missing TopN evidence time: {evidence!r}"
    assert "sales_30d" in evidence.get("coverage", ""), f"coverage must name sales_30d: {evidence!r}"
    print("    list_products limit=3 => sales_30d DESC Top3 with evidence")


def test_chat_sales_topn_routes_to_list_products() -> None:
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("WS-148 must not call provider")):
        result = _agent.chat(
            [{"role": "user", "content": "KSA 近30天销量最高的3个商品"}],
            SCOPE,
        )

    tools = result.get("tools_used") or []
    reply = result.get("reply") or ""
    assert tools == ["list_products"], f"TopN sales query must use list_products only, got {tools}"
    assert result.get("judge_method") == "deterministic_product_sales_topn_router", (
        f"wrong judge_method: {result.get('judge_method')!r}"
    )
    assert "SKU-B" in reply and "30" in reply, f"reply should include Top1 SKU-B sales_30d=30: {reply!r}"
    assert "SKU-D" not in reply, f"null-sales SKU-D must not appear in Top3: {reply!r}"
    assert "来源" in reply and "2026-06-08" in reply and "sales_30d" in reply, (
        f"reply must carry source/time/coverage evidence: {reply!r}"
    )
    print("    chat TopN sales query routes deterministically to list_products with evidence")


def main() -> None:
    _setup_db()
    test_tool_limit_means_sales_30d_topn()
    test_chat_sales_topn_routes_to_list_products()
    print("\n2/2 passed (WS-148 list_products TopN)")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(TMP_DB)
        except OSError:
            pass

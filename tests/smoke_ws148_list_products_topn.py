"""WS-148 smoke: list_products is the deterministic **windowless** sales TopN path.

WS-120 choice A 后边界：带时间窗的 TopN（『近30天/近N天/指定日期窗口』）改走
top_sales_by_window 逐单现算；list_products/sales_30d 固定桶只服务**无时间窗的裸
TopN**（如『销量最高的3个商品』）。故本用例用裸 TopN 验证 list_products 路由仍在。

PASS:
  - chat("销量最高的3个商品") routes directly to list_products with limit=N.
  - chat("不用刷新...哪些 SKU 最畅销") also routes directly and gets a deterministic stale hint.
  - limit=N means TopN by sales_30d DESC.
  - rendered reply carries source/time/coverage evidence before showing numbers.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import datetime as _dt
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
FIXTURE_AS_OF = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


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
        ("SKU-A", "PROD-A", "低销量商品", 1, 7, 90, 19.0, FIXTURE_AS_OF),
        ("SKU-B", "PROD-B", "最高销量商品", 1, 30, 120, 29.0, FIXTURE_AS_OF),
        ("SKU-C", "PROD-C", "第二销量商品", 1, 18, 180, 39.0, FIXTURE_AS_OF),
        ("SKU-D", "PROD-D", "无销量商品", 0, None, 300, 49.0, FIXTURE_AS_OF),
    ]
    conn.executemany(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, product_id, title, is_listed, "
        "sales_30d, sales_180d, latest_price, as_of_date, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, *r, f"{FIXTURE_AS_OF}T09:00:00") for r in rows],
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
    assert evidence.get("fetched_at") == FIXTURE_AS_OF, f"missing TopN evidence time: {evidence!r}"
    assert "sales_30d" in evidence.get("coverage", ""), f"coverage must name sales_30d: {evidence!r}"
    print("    list_products limit=3 => sales_30d DESC Top3 with evidence")


def test_chat_sales_topn_routes_to_list_products() -> None:
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("WS-148 must not call provider")):
        result = _agent.chat(
            [{"role": "user", "content": "KSA 销量最高的3个商品"}],
            SCOPE,
        )

    tools = result.get("tools_used") or []
    reply = result.get("reply") or ""
    assert tools == ["list_products"], f"裸 TopN（无时间窗）must use list_products only, got {tools}"
    assert result.get("judge_method") == "deterministic_product_sales_topn_router", (
        f"wrong judge_method: {result.get('judge_method')!r}"
    )
    assert "SKU-B" in reply and "30" in reply, f"reply should include Top1 SKU-B sales_30d=30: {reply!r}"
    assert "SKU-D" not in reply, f"null-sales SKU-D must not appear in Top3: {reply!r}"
    assert "来源" in reply and FIXTURE_AS_OF in reply and "sales_30d" in reply, (
        f"reply must carry source/time/coverage evidence: {reply!r}"
    )
    print("    chat TopN sales query routes deterministically to list_products with evidence")


def test_chat_sales_skip_topn_is_deterministic_with_stale_hint() -> None:
    """T07-2 live flake guard: skip-refresh + 最畅销 must not wait on LLM wording/timeouts."""
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("T07-2 must not call provider")):
        result = _agent.chat(
            [{"role": "user", "content": "不用刷新，就用现在的告诉我哪些 SKU 最畅销"}],
            SCOPE,
        )

    tools = result.get("tools_used") or []
    reply = result.get("reply") or ""
    assert tools == ["list_products"], f"skip-refresh 最畅销 must use list_products, got {tools}"
    assert result.get("judge_method") == "deterministic_product_sales_topn_router", (
        f"wrong judge_method: {result.get('judge_method')!r}"
    )
    assert "SKU-B" in reply and "来源" in reply and "sales_30d" in reply, reply
    assert ("未更新到今天" in reply or "偏保守" in reply or "不新鲜" in reply), (
        f"reply must carry deterministic T07 stale hint, got {reply!r}"
    )
    print("    skip-refresh 最畅销 → deterministic list_products + stale hint, no provider")


def main() -> None:
    _setup_db()
    test_tool_limit_means_sales_30d_topn()
    test_chat_sales_topn_routes_to_list_products()
    test_chat_sales_skip_topn_is_deterministic_with_stale_hint()
    print("\n3/3 passed (WS-148 list_products TopN)")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(TMP_DB)
        except OSError:
            pass

"""WS-120 [T07] smoke: 指定日期窗口销量 TopN 确定性工具（top_sales_by_window）。

死法 / 为什么存在：
  WS-148 让「销量 TopN」走 list_products，按 wf2_sku.sales_30d 固定桶排序（且 WS-134
  给它加了 >3 天陈旧 fail-closed）。固定桶只适合「无时间窗的裸 TopN」；对**任意指定日期
  窗口**（如 2026-05-07~2026-06-05）或**近N天按最新业务日倒推**，用 30d 固定桶冒充就是
  占位假数据。本工具从 wf2_orders 按 order_date 逐单现算；窗口任一端缺数 fail-closed、
  绝不返回排名；「近N天」按最新订单业务日倒推且最新订单 >3 天即 fail-closed（保留 WS-134
  承重墙）。

choice A（Luke 2026-06-14 拍板）：近30天统一走 top_sales_by_window 逐单现算，不再走
  list_products/sales_30d 固定桶。

FAIL before fix: 没有 top_sales_by_window 工具/路由；近30天/指定窗口落到 list_products
  固定桶或 LLM。
PASS after fix: 见各用例。
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
ALIAS_UAE = "hipop_uae"
SCOPE = {"tenant_id": TENANT, "current_user": "test", "current_role": "admin", "store": "KSA"}
SCOPE_UAE = {"tenant_id": TENANT, "current_user": "test", "current_role": "admin", "store": "UAE"}
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")

TODAY = _dt.date.today()


def _d(n: int) -> str:
    """今天往前 n 天的 YYYY-MM-DD（fixture 锚定到 today，让相对/时效门确定性可测）。"""
    return (TODAY - _dt.timedelta(days=n)).isoformat()


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert match, f"missing CREATE TABLE for {table}"
    return match.group(0)


# KSA fresh orders: (sku, item_nr, days_ago, is_cancelled)
_KSA_ORDERS = [
    ("SKU-W1", "W1-1", 0, 0), ("SKU-W1", "W1-2", 2, 0), ("SKU-W1", "W1-3", 5, 0),
    ("SKU-W1", "W1-4", 10, 0), ("SKU-W1", "W1-5", 20, 0),
    ("SKU-W1", "W1-C", 3, 1),     # 取消 → 不计
    ("SKU-W1", "W1-OLD", 40, 0),  # 30天外、窗口[40,0]内
    ("SKU-W2", "W2-1", 1, 0), ("SKU-W2", "W2-2", 8, 0), ("SKU-W2", "W2-3", 15, 0),
    ("SKU-W3", "W3-1", 12, 0),
    ("SKU-NOISE", "N-OLD", 45, 0),  # 设最早覆盖
]
# wf2_sku.sales_30d 故意与窗口排序相反：用固定桶则 Top1=SKU-W3
_KSA_SKUS = [
    ("SKU-W1", "窗口冠军", 1, 1), ("SKU-W2", "窗口第二", 1, 50),
    ("SKU-W3", "窗口第三", 1, 99), ("SKU-NOISE", "噪声", 1, 80),
]
# UAE stale orders: latest = today-5（>3 天）→ 近N天必须 fail-closed
_UAE_ORDERS = [
    ("SKU-U1", "U1-1", 5, 0), ("SKU-U1", "U1-2", 9, 0), ("SKU-U1", "U1-3", 15, 0),
    ("SKU-U2", "U2-1", 7, 0),
    ("SKU-U-OLD", "UO-1", 50, 0),
]
_UAE_SKUS = [("SKU-U1", "U1", 1, 30), ("SKU-U2", "U2", 1, 10)]


def _setup_db() -> None:
    conn = sqlite3.connect(TMP_DB)
    conn.executescript(_extract_create("sales_entities"))
    conn.executescript(_extract_create("wf2_sku"))
    conn.executescript(_extract_create("wf2_orders"))
    conn.executemany(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        [(TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
         (TENANT, ALIAS_UAE, "AE", "Noon", "HIPOP-NOON-UAE", 1)],
    )
    conn.executemany(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, title, is_listed, "
        "sales_30d, as_of_date, imported_at) VALUES (?,?,?,?,?,?,?,?)",
        [(TENANT, ALIAS, s, t, l, s30, _d(0), f"{_d(0)}T09:00:00") for (s, t, l, s30) in _KSA_SKUS]
        + [(TENANT, ALIAS_UAE, s, t, l, s30, _d(5), f"{_d(5)}T09:00:00") for (s, t, l, s30) in _UAE_SKUS],
    )
    conn.executemany(
        "INSERT INTO wf2_orders (tenant_id, entity_alias, partner_sku, item_nr, order_date, is_cancelled) "
        "VALUES (?,?,?,?,?,?)",
        [(TENANT, ALIAS, s, it, _d(n), cx) for (s, it, n, cx) in _KSA_ORDERS]
        + [(TENANT, ALIAS_UAE, s, it, _d(n), cx) for (s, it, n, cx) in _UAE_ORDERS],
    )
    conn.commit()
    conn.close()
    _data.set_current_tenant(TENANT)
    _agent._chat_tenant.set(TENANT)


def _recompute(sku: str, start: str, end: str, alias: str = ALIAS) -> int:
    conn = sqlite3.connect(TMP_DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM wf2_orders WHERE tenant_id=? AND entity_alias=? AND partner_sku=? "
        "AND is_cancelled=0 AND order_date >= ? AND order_date <= ?",
        (TENANT, alias, sku, start, end),
    ).fetchone()[0]
    conn.close()
    return n


def test_tool_window_topn_recomputable() -> None:
    start, end = _d(40), _d(0)
    result = _agent.tool_top_sales_by_window("KSA", start, end, limit=3)
    assert result.get("available") is True, f"window should be covered: {result!r}"
    items = result.get("items") or []
    skus = [it["partner_sku"] for it in items]
    assert skus == ["SKU-W1", "SKU-W2", "SKU-W3"], f"window Top3 wrong: {skus!r}"
    for it in items:  # acceptance #2：每个 SKU 销量由同一 SQL 复算一致
        assert it["window_sales"] == _recompute(it["partner_sku"], start, end), it
    assert [it["window_sales"] for it in items] == [6, 3, 1], f"counts wrong: {items!r}"
    ev = result.get("evidence") or {}
    assert ev.get("fetched_at") and "wf2_orders" in (ev.get("coverage") or ""), f"evidence: {ev!r}"
    print("    tool window Top3 recomputable + evidence")


def test_window_differs_from_sales_30d() -> None:
    win = _agent.tool_top_sales_by_window("KSA", _d(40), _d(0), limit=3)
    bucket = _agent.tool_list_products("KSA", listing="all", limit=3)
    win_top1 = (win.get("items") or [{}])[0].get("partner_sku")
    bucket_top1 = (bucket.get("items") or [{}])[0].get("sku")
    assert win_top1 == "SKU-W1", f"window Top1 must be SKU-W1, got {win_top1!r}"
    assert bucket_top1 == "SKU-W3", f"sales_30d Top1 should be SKU-W3, got {bucket_top1!r}"
    assert win_top1 != bucket_top1, "window TopN must differ from sales_30d bucket"
    print("    window Top1 (W1) != sales_30d Top1 (W3) — no fixed-bucket cheat")


def test_window_end_not_covered_fail_closed() -> None:
    result = _agent.tool_top_sales_by_window("KSA", "2099-01-01", "2099-01-31", limit=3)
    assert result.get("available") is False and not result.get("items"), result
    assert result.get("reason") == "window_end_not_covered", f"wrong reason: {result!r}"
    print("    end-uncovered window → fail-closed, no ranking")


def test_window_start_not_covered_fail_closed() -> None:
    # 起点 2000-01-01 早于已有订单 → 前半段缺数，必须 fail-closed（红队 gap#1）。
    result = _agent.tool_top_sales_by_window("KSA", "2000-01-01", _d(0), limit=3)
    assert result.get("available") is False and not result.get("items"), result
    assert result.get("reason") == "window_start_not_covered", f"wrong reason: {result!r}"
    print("    start-uncovered window → fail-closed, no ranking")


def test_chat_explicit_window_routes_to_tool() -> None:
    start, end = _d(40), _d(0)
    q = f"{start} 到 {end} KSA 销量最高 3 个 SKU"
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
        result = _agent.chat([{"role": "user", "content": q}], SCOPE)
    assert result.get("tools_used") == ["top_sales_by_window"], result.get("tools_used")
    assert result.get("judge_method") == "deterministic_window_sales_topn_router", result.get("judge_method")
    reply = result.get("reply") or ""
    assert "SKU-W1" in reply and "窗口销量" in reply, reply
    assert "300" not in reply and "1800" not in reply, f"fabricated sample numbers: {reply!r}"
    assert "来源" in reply and start in reply and end in reply, reply
    print("    chat 指定窗口 → top_sales_by_window, no provider, evidence")


def test_chat_relative_30d_routes_to_window_tool_fresh() -> None:
    # choice A：近30天走 top_sales_by_window，按最新订单业务日(today)倒推现算，不走 sales_30d。
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
        result = _agent.chat([{"role": "user", "content": "最近30天 KSA 销量最高 3 个 SKU"}], SCOPE)
    assert result.get("tools_used") == ["top_sales_by_window"], result.get("tools_used")
    assert result.get("judge_method") == "deterministic_window_sales_topn_router", result.get("judge_method")
    reply = result.get("reply") or ""
    # 倒推锚点 = 最新订单业务日 today；窗口 [today-29, today]
    assert _d(0) in reply and _d(29) in reply, f"30d window must anchor to latest order date today: {reply!r}"
    assert "SKU-W1" in reply and "窗口销量" in reply, reply
    print("    最近30天 → top_sales_by_window anchored to latest order date (fresh)")


def test_chat_relative_30d_stale_fails_closed() -> None:
    # 保留 WS-134 承重墙：最新订单 >3 天（UAE latest=today-5）→ fail-closed，不泄陈旧名次。
    with patch.object(_provider, "get_provider", return_value="smoke"), \
         patch.object(_provider, "chat_with_tools", side_effect=AssertionError("must not call provider")):
        result = _agent.chat([{"role": "user", "content": "最近30天 UAE 销量最高 3 个 SKU"}], SCOPE_UAE)
    assert result.get("tools_used") == ["top_sales_by_window"], result.get("tools_used")
    reply = result.get("reply") or ""
    assert ("不能出数" in reply or "超过 3 天" in reply or "刷新" in reply), f"stale must fail-closed: {reply!r}"
    assert "SKU-U1" not in reply and "SKU-U2" not in reply, f"stale ranked SKU leaked: {reply!r}"
    print("    最近30天(陈旧 >3天) → fail-closed, no stale ranking (WS-134 wall kept)")


def test_router_parses_paraphrased_windows() -> None:
    """验门人 round-2 红队：『近30天』的自然同义写法都必须被结构化识别为窗口（relative_days），
    不能只钉住『最近30天』一种写法（否则换说法就落回 sales_30d 固定桶）。"""
    from hipop.server._deterministic_routes import _deterministic_window_sales_topn_request as R
    cases = {
        "过去30天 KSA 销量最高 3 个 SKU": (30, 3),
        "近三十天 KSA 销量最高 3 个 SKU": (30, 3),
        "近30日 KSA 销量最高 3 个 SKU": (30, 3),
        "这30天 KSA 销量最高 5 个 SKU": (30, 5),
        "过往7天 KSA 销量最高的商品": (7, 10),
        "前30天 KSA 销量最高 3 个 SKU": (30, 3),   # 「前30天」=窗口；limit 来自「3 个」非 30
        "最近九十天 KSA 卖得最好的 SKU": (90, 10),
    }
    for q, (days, lim) in cases.items():
        r = R(q)
        assert r and r.get("relative_days") == days and r.get("limit") == lim, \
            f"{q!r} → {r!r}, 期望 relative_days={days}, limit={lim}"
    # 反例：无时间窗的裸 TopN 不能被窗口路由抢走（仍归 list_products）
    assert R("KSA 销量最高的3个商品") is None, "无时间窗裸 TopN 不应进窗口路由"
    assert R("KSA 销量最高的3个商品") is None
    print("    paraphrased windows (过去/近三十/30日/这/过往/前N天/九十天) all → relative_days")


def test_chat_paraphrased_30d_routes_to_window_tool() -> None:
    """端到端复现红队两条打回 case：换同义写法仍必须走 top_sales_by_window，不落 list_products。"""
    for q in ("过去30天 KSA 销量最高 3 个 SKU", "近三十天 KSA 销量最高 3 个 SKU"):
        with patch.object(_provider, "get_provider", return_value="smoke"), \
             patch.object(_provider, "chat_with_tools",
                          side_effect=AssertionError("must not call provider")):
            result = _agent.chat([{"role": "user", "content": q}], SCOPE)
        assert result.get("tools_used") == ["top_sales_by_window"], f"{q!r} → {result.get('tools_used')}"
        assert result.get("judge_method") == "deterministic_window_sales_topn_router", f"{q!r}"
        reply = result.get("reply") or ""
        assert _d(0) in reply and _d(29) in reply and "窗口销量" in reply, f"{q!r} → {reply!r}"
        assert "wf2_sku.sales_30d" not in reply, f"{q!r} 仍回退到固定桶口径: {reply!r}"
    print("    过去30天 / 近三十天 → top_sales_by_window（不落 sales_30d 固定桶）")


def main() -> None:
    _setup_db()
    test_tool_window_topn_recomputable()
    test_window_differs_from_sales_30d()
    test_window_end_not_covered_fail_closed()
    test_window_start_not_covered_fail_closed()
    test_chat_explicit_window_routes_to_tool()
    test_chat_relative_30d_routes_to_window_tool_fresh()
    test_chat_relative_30d_stale_fails_closed()
    test_router_parses_paraphrased_windows()
    test_chat_paraphrased_30d_routes_to_window_tool()
    print("\n9/9 passed (WS-120 window sales TopN)")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(TMP_DB)
        except OSError:
            pass

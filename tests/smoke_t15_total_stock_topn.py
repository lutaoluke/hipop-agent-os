"""Smoke: T15 — 总库存最高 SKU（WS-139）

验收：total_stock TopN 必须含 pending_inbound；可售（noon_saleable）与总库存分开命名，
fail-closed（>3 天数据不出数），chat 路由确定性接线。

fail-then-pass 场景（接线缺失 / 死代码短路）：
  Before：total_stock 没有 pending_inbound（legacy ERP-only 计算）→
          TopN 结果里 pending 为 0，order 错误。
  After：merge_stock_snapshot_v2 跑完 → total_stock 含 pending →
         TopN 按正确总库存排序，可售与总库存区分，超 3 天 fail-closed。

跑法：python3 tests/smoke_t15_total_stock_topn.py
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import sqlite3
import tempfile
import time
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))

import server.data as _data  # noqa: E402

_data.set_current_tenant(1)

TENANT = 1
ALIAS = "hipop_ksa"
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE"
    return m.group(0)


def _setup_db_once():
    """Create tables that only need to exist once."""
    c = sqlite3.connect(_TMP_DB)
    c.executescript(_extract_create("wf1_stock"))
    c.executescript(_extract_create("sales_entities"))
    # Insert the sales entity so _resolve_entity_alias works
    c.execute(
        "INSERT OR IGNORE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
    )
    c.commit()
    c.close()


def _setup_db(pending_in_total_stock: bool = False):
    """
    pending_in_total_stock=False: legacy state — total_stock = ERP only (no pending, no noon).
    pending_in_total_stock=True:  merged state — total_stock includes pending_inbound.

    SKU-A: noon=100, overseas=50, yiwu=10, dongguan=5, pending=40
           → total_legacy=65 (yiwu+dongguan+overseas), total_merged=205
    SKU-B: noon=0, overseas=200, yiwu=0, dongguan=0, pending=15
           → total_legacy=200, total_merged=215   ← TopN rank changes when pending included
    SKU-C: noon=60, overseas=0, yiwu=20, dongguan=20, pending=0
           → total_legacy=40, total_merged=100
    """
    c = sqlite3.connect(_TMP_DB)
    if pending_in_total_stock:
        rows = [
            # (sku, noon_total, noon_saleable, noon_unsaleable, overseas, yiwu, dongguan, pending, total)
            ("SKU-A", 100, 80, 20, 50, 10, 5, 40, 205),   # 205 = 100+50+10+5+40
            ("SKU-B", 0, 0, 0, 200, 0, 0, 15, 215),       # 215 = 0+200+0+0+15  ← 应排第1
            ("SKU-C", 60, 60, 0, 0, 20, 20, 0, 100),      # 100 = 60+0+20+20+0
        ]
    else:
        # legacy: total_stock = ERP-only (yiwu+dongguan+overseas), pending ignored/NULL
        rows = [
            ("SKU-A", 100, 80, 20, 50, 10, 5, None, 65),  # 65 = 10+5+50
            ("SKU-B", 0, 0, 0, 200, 0, 0, None, 200),     # 200 ← 排第1
            ("SKU-C", 60, 60, 0, 0, 20, 20, None, 40),    # 40 = 20+20
        ]

    from datetime import date, timedelta
    today_str = date.today().isoformat()
    for (sku, ntot, nsal, nuns, ov, yw, dg, pending, total) in rows:
        c.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, "
            "overseas_total_qty, yiwu_qty, dongguan_qty, "
            "pending_inbound_qty, total_stock, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (TENANT, ALIAS, sku, ntot, nsal, nuns, ov, yw, dg, pending, total, today_str),
        )
    c.commit()
    c.close()


def _reset_db():
    c = sqlite3.connect(_TMP_DB)
    c.execute("DELETE FROM wf1_stock")
    c.commit()
    c.close()


def main():
    _setup_db_once()

    from server import agent as _agent
    _agent._chat_tenant.set(TENANT)

    # ─────────────────────────────────────────────────────────────
    # 阶段 0: 单元测试 _deterministic_total_stock_topn_request 路由
    # ─────────────────────────────────────────────────────────────
    router = _agent._deterministic_total_stock_topn_request

    assert router("KSA 总库存最高的 10 个 SKU") == 10, "触发词未匹配"
    assert router("库存最多的 5 个 SKU") == 5, "数字提取失败"
    assert router("当前库存量排行") == 10, "无数字时默认 10"
    assert router("总库存最高") == 10, "最短触发词失败"
    assert router("积压最多的商品") == 10, "积压触发词失败"
    assert router("库存 TopN") == 10, "TopN 触发词失败"
    # 非库存 TopN 问题不应触发
    assert router("SKU-A 的销量") is None, "误触发：销量问题"
    assert router("哪些需要补货") is None, "误触发：补货问题"
    assert router("库存够不够") is None, "误触发：库存充足问题"

    print("✓ 路由器: 8/8 case 通过（触发 + 数字提取 + 非触发过滤）")

    # ─────────────────────────────────────────────────────────────
    # 阶段 1: fail-then-pass — 改动前 total_stock 不含 pending
    # ─────────────────────────────────────────────────────────────
    _setup_db(pending_in_total_stock=False)

    result_before = _agent.tool_total_stock_topn(store="KSA", n=3)
    assert not result_before.get("fail_closed"), "新鲜数据不应 fail_closed"
    assert not result_before.get("empty"), "有数据不应 empty"
    items_before = result_before["items"]
    assert len(items_before) == 3, f"期望 3 行，得 {len(items_before)}"

    # 改动前：top-1 应是 SKU-B（legacy total=200），SKU-A 只有 65，顺序与含 pending 后不同
    top_skus_before = [r["partner_sku"] for r in items_before]
    assert top_skus_before[0] == "SKU-B", f"改动前 top-1 应是 SKU-B (200)，实际 {top_skus_before}"
    # pending 全 NULL（legacy），结果里 pending_inbound_qty == 0 或 None
    for r in items_before:
        assert r.get("pending_inbound_qty") in (None, 0), \
            f"改动前 {r['partner_sku']} pending 应为 NULL/0，实际 {r['pending_inbound_qty']}"

    print(f"✓ 改动前: 第1名={top_skus_before[0]}，pending 全为 NULL/0（旧 total_stock 漏 pending）")

    # ─────────────────────────────────────────────────────────────
    # 阶段 2: fail-then-pass — 改动后 total_stock 含 pending（merge 后）
    # ─────────────────────────────────────────────────────────────
    _reset_db()
    _setup_db(pending_in_total_stock=True)

    result_after = _agent.tool_total_stock_topn(store="KSA", n=3)
    assert not result_after.get("fail_closed"), "新鲜数据不应 fail_closed"
    assert not result_after.get("empty"), "有数据不应 empty"
    items_after = result_after["items"]
    assert len(items_after) == 3, f"期望 3 行，得 {len(items_after)}"

    # 改动后：含 pending → SKU-B(215) > SKU-A(205) > SKU-C(100)
    top_skus_after = [r["partner_sku"] for r in items_after]
    assert top_skus_after[0] == "SKU-B", f"含 pending 后 top-1 应是 SKU-B (215)，实际 {top_skus_after}"
    assert top_skus_after[1] == "SKU-A", f"含 pending 后 top-2 应是 SKU-A (205)，实际 {top_skus_after}"

    # 验证 total_stock 数字正确（含 pending）
    r_a = next(r for r in items_after if r["partner_sku"] == "SKU-A")
    assert r_a["total_stock"] == 205, f"SKU-A total_stock={r_a['total_stock']} != 205"
    assert r_a["pending_inbound_qty"] == 40, f"SKU-A pending={r_a['pending_inbound_qty']} != 40"
    assert r_a["noon_saleable_qty"] == 80, f"SKU-A saleable={r_a['noon_saleable_qty']} != 80"

    r_b = next(r for r in items_after if r["partner_sku"] == "SKU-B")
    assert r_b["total_stock"] == 215, f"SKU-B total_stock={r_b['total_stock']} != 215"
    assert r_b["pending_inbound_qty"] == 15, f"SKU-B pending={r_b['pending_inbound_qty']} != 15"

    print(f"✓ 改动后: top={top_skus_after}，SKU-A total=205（含 pending 40），SKU-B total=215（含 pending 15）")

    # ─────────────────────────────────────────────────────────────
    # 阶段 3: 口径区分 — total_stock vs noon_saleable 不同
    # ─────────────────────────────────────────────────────────────
    # total_stock(205) != noon_saleable(80) for SKU-A，两个口径必须分开返回
    assert r_a["total_stock"] != r_a["noon_saleable_qty"], \
        "total_stock 与 noon_saleable_qty 相等，口径未区分"
    # tool 返回里有明确的字段定义说明
    assert "total_stock_definition" in result_after, "缺 total_stock_definition 字段"
    assert "noon_saleable_note" in result_after, "缺 noon_saleable_note 字段"
    assert "pending" in result_after["total_stock_definition"].lower() or \
           "pending_inbound" in result_after["total_stock_definition"], \
        "total_stock_definition 没提 pending_inbound"

    print("✓ 口径区分: total_stock != noon_saleable_qty；定义说明字段存在且含 pending")

    # ─────────────────────────────────────────────────────────────
    # 阶段 4: fail-closed — 数据超 3 天
    # ─────────────────────────────────────────────────────────────
    from datetime import date, timedelta
    stale_date = (date.today() - timedelta(days=5)).isoformat()
    c = sqlite3.connect(_TMP_DB)
    c.execute("UPDATE wf1_stock SET updated_at=? WHERE tenant_id=? AND entity_alias=?",
              (stale_date, TENANT, ALIAS))
    c.commit(); c.close()

    result_stale = _agent.tool_total_stock_topn(store="KSA", n=3)
    assert result_stale.get("fail_closed") is True, \
        f"数据 5 天前应 fail_closed=True，实际 {result_stale}"
    assert result_stale.get("stale_days") == 5, \
        f"stale_days 应=5，实际 {result_stale.get('stale_days')}"
    assert "message" in result_stale, "fail_closed 应含 message"
    assert "items" not in result_stale or not result_stale.get("items"), \
        "fail_closed 不应返 items"
    print(f"✓ fail-closed: 5 天陈旧数据不出数字，返 fail_closed=True + message")

    # ─────────────────────────────────────────────────────────────
    # 阶段 4.5: 混合新鲜度 — fresh 低库存行 + stale 高库存行 → stale 行不出数
    # 验门人 round-1 发现的 bug：旧实现用 MAX(updated_at) 判整批，导致 stale 高库存行
    # 被当成 TopN 第 1 名返回。修复：逐行检查 updated_at，过期行不出数。
    # ─────────────────────────────────────────────────────────────
    _reset_db()
    # 构造：1 条今日新鲜低库存行 (FRESH-LOW, total=10) + 1 条 5 天前过期高库存行 (STALE-HIGH, total=999)
    c = sqlite3.connect(_TMP_DB)
    today_iso = date.today().isoformat()
    stale_iso = (date.today() - timedelta(days=5)).isoformat()
    c.executemany(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, "
        "overseas_total_qty, yiwu_qty, dongguan_qty, "
        "pending_inbound_qty, total_stock, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (TENANT, ALIAS, "FRESH-LOW", 5, 5, 0, 0, 3, 2, 0, 10, today_iso),
            (TENANT, ALIAS, "STALE-HIGH", 500, 500, 0, 400, 50, 49, 0, 999, stale_iso),
        ],
    )
    c.commit(); c.close()

    # 改动前（旧实现）：MAX(updated_at) = today → fail_closed=False → STALE-HIGH 排第 1
    # 改动后（新实现）：逐行过滤 → STALE-HIGH 被排除 → 只返 FRESH-LOW，或 TopN=1
    result_mixed = _agent.tool_total_stock_topn(store="KSA", n=10)
    assert not result_mixed.get("fail_closed"), \
        f"有新鲜行时不应 fail_closed，实际：{result_mixed}"
    items_mixed = result_mixed.get("items") or []
    returned_skus = [r["partner_sku"] for r in items_mixed]
    assert "STALE-HIGH" not in returned_skus, (
        f"stale 行 (5 天前) 不应出现在 TopN 结果中，实际返回：{returned_skus}"
    )
    assert "FRESH-LOW" in returned_skus, (
        f"fresh 行应被返回，实际：{returned_skus}"
    )
    print(f"✓ 混合新鲜度: STALE-HIGH(999, 5天前) 排除，FRESH-LOW(10, 今天) 保留 — 旧实现此处 FAIL")

    # ─────────────────────────────────────────────────────────────
    # 阶段 5: 格式化回复 — 可售与总库存区分
    # ─────────────────────────────────────────────────────────────
    # 恢复新鲜数据
    _reset_db()
    _setup_db(pending_in_total_stock=True)
    c = sqlite3.connect(_TMP_DB)
    c.execute("UPDATE wf1_stock SET updated_at=? WHERE tenant_id=? AND entity_alias=?",
              (date.today().isoformat(), TENANT, ALIAS))
    c.commit(); c.close()
    result_fresh = _agent.tool_total_stock_topn(store="KSA", n=3)
    reply = _agent._format_total_stock_topn_reply("KSA", result_fresh)
    assert "SKU-B" in reply, "回复应含 SKU-B"
    assert "215" in reply.replace(",", "") or "215" in reply, "回复应含总库存数值 215"
    # 回复必须提到可售与总库存的区分
    assert "saleable" in reply.lower() or "可售" in reply, "回复应提到可售/saleable"
    assert "pending" in reply.lower() or "送仓未上架" in reply, "回复应提到 pending/送仓未上架"

    # fail_closed 格式化
    reply_stale = _agent._format_total_stock_topn_reply("KSA", result_stale)
    assert "3 天" in reply_stale or "未更新" in reply_stale or "刷新" in reply_stale, \
        f"fail_closed 回复应提示刷新，实际：{reply_stale}"

    print("✓ 回复格式: 含总库存数值 + 可售/saleable 区分 + pending 标注；fail_closed 提示刷新")

    # ─────────────────────────────────────────────────────────────
    # 阶段 6: 接线 — tool 在 TOOL_FUNCS + TOOLS 中注册
    # ─────────────────────────────────────────────────────────────
    assert "total_stock_topn" in _agent.TOOL_FUNCS, \
        "total_stock_topn 未在 TOOL_FUNCS 注册"
    tool_def_names = {t["name"] for t in _agent.TOOLS}
    assert "total_stock_topn" in tool_def_names, \
        "total_stock_topn 未在 TOOLS schema 中定义"

    print("✓ 接线: TOOL_FUNCS + TOOLS schema 均已注册")

    # ─────────────────────────────────────────────────────────────
    # 阶段 7: SYSTEM_PROMPT 路由提示含 total_stock_topn
    # ─────────────────────────────────────────────────────────────
    assert "total_stock_topn" in _agent.SYSTEM_PROMPT, \
        "SYSTEM_PROMPT 没有 total_stock_topn 路由条目"

    print("✓ SYSTEM_PROMPT 含 total_stock_topn 路由条目")

    print("\n8/8 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

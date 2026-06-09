"""Smoke: T15 chat 路由单元测试（WS-139，round-2）

验证 chat() 确定性路由在不依赖 live server 的条件下能正确处理 T15 问题。

覆盖点：
1. T15 触发词通过 chat() 路由到 total_stock_topn（不走 LLM）
2. mixed-freshness：stale 高库存行不出现在 chat 回复中
3. fail-closed：超 3 天数据 → chat 回复提示刷新，不返数字
4. tools_used 含 total_stock_topn + judge_method = deterministic

跑法：python3 tests/smoke_t15_chat_unit.py
（纯 SQLite 临时库，不依赖 uvicorn / live server / LLM 调用。）
"""
import os
import sys
import sqlite3
import tempfile
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
    c = sqlite3.connect(_TMP_DB)
    c.executescript(_extract_create("wf1_stock"))
    c.executescript(_extract_create("sales_entities"))
    c.execute(
        "INSERT OR IGNORE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, active) "
        "VALUES (?,?,?,?,?,?)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 1),
    )
    c.commit()
    c.close()


def _reset_db():
    c = sqlite3.connect(_TMP_DB)
    c.execute("DELETE FROM wf1_stock")
    c.commit()
    c.close()


def _insert_rows(rows):
    c = sqlite3.connect(_TMP_DB)
    c.executemany(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, "
        "overseas_total_qty, yiwu_qty, dongguan_qty, "
        "pending_inbound_qty, total_stock, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    c.commit()
    c.close()


def main():
    _setup_db_once()

    from server import agent as _agent
    _agent._chat_tenant.set(TENANT)

    from datetime import date, timedelta
    today = date.today().isoformat()
    stale = (date.today() - timedelta(days=5)).isoformat()

    SCOPE = {"store": "KSA", "tenant_id": TENANT}

    # ─────────────────────────────────────────────────────────────
    # Case 1: T15 触发词路由到 total_stock_topn，结果含 pending
    # ─────────────────────────────────────────────────────────────
    _reset_db()
    _insert_rows([
        (TENANT, ALIAS, "SKU-X", 100, 80, 20, 50, 10, 5, 40, 205, today),
        (TENANT, ALIAS, "SKU-Y", 0,   0,  0, 200, 0, 0, 15, 215, today),
        (TENANT, ALIAS, "SKU-Z", 60, 60,  0,   0, 20, 20, 0, 100, today),
    ])

    result = _agent.chat(
        [{"role": "user", "content": "KSA 总库存最高的 3 个 SKU"}],
        SCOPE,
    )
    assert "total_stock_topn" in result.get("tools_used", []), (
        f"T15 问题应路由到 total_stock_topn，实际 tools_used={result.get('tools_used')}"
    )
    assert result.get("judge_method") == "deterministic_total_stock_topn_router", (
        f"judge_method 应为 deterministic_total_stock_topn_router，实际={result.get('judge_method')}"
    )
    reply = result.get("reply") or ""
    assert "SKU-Y" in reply, f"回复应含总库存最高的 SKU-Y(215)，实际：{reply[:300]}"
    assert "215" in reply.replace(",", ""), f"回复应含总库存数值 215，实际：{reply[:300]}"

    print("✓ Case 1: T15 触发词路由 total_stock_topn，回复含正确排名和数值")

    # ─────────────────────────────────────────────────────────────
    # Case 2: mixed-freshness — stale 高库存行不出现在 chat 回复中
    # ─────────────────────────────────────────────────────────────
    _reset_db()
    _insert_rows([
        (TENANT, ALIAS, "FRESH-LOW",  5,  5, 0,  0,  3, 2, 0,  10, today),
        (TENANT, ALIAS, "STALE-HIGH", 500, 500, 0, 400, 50, 49, 0, 999, stale),
    ])

    result2 = _agent.chat(
        [{"role": "user", "content": "库存最多的 SKU"}],
        SCOPE,
    )
    reply2 = result2.get("reply") or ""
    assert "STALE-HIGH" not in reply2, (
        f"stale 行(5天前)不应出现在 chat 回复中，实际：{reply2[:300]}"
    )
    assert "total_stock_topn" in result2.get("tools_used", []), (
        f"mixed-freshness 场景应路由到 total_stock_topn，实际：{result2.get('tools_used')}"
    )

    print("✓ Case 2: mixed-freshness — STALE-HIGH(999,5天前) 不出现在 chat 回复")

    # ─────────────────────────────────────────────────────────────
    # Case 3: fail-closed — 所有行超 3 天 → chat 回复提示刷新不出数字
    # ─────────────────────────────────────────────────────────────
    _reset_db()
    _insert_rows([
        (TENANT, ALIAS, "SKU-OLD", 100, 80, 20, 50, 10, 5, 40, 205, stale),
    ])

    result3 = _agent.chat(
        [{"role": "user", "content": "总库存最高的 SKU 是哪个"}],
        SCOPE,
    )
    reply3 = result3.get("reply") or ""
    assert "total_stock_topn" in result3.get("tools_used", []), (
        f"fail-closed 场景应路由到 total_stock_topn，实际：{result3.get('tools_used')}"
    )
    # fail-closed 回复必须提示刷新，不出数字
    has_refresh_hint = any(w in reply3 for w in ["刷新", "3 天", "未更新", "过期", "陈旧", "fail"])
    assert has_refresh_hint, (
        f"fail-closed 回复应提示刷新，实际：{reply3[:300]}"
    )
    assert "205" not in reply3.replace(",", ""), (
        f"fail-closed 不应返回旧库存数字 205，实际：{reply3[:300]}"
    )

    print("✓ Case 3: fail-closed — 超 3 天数据 chat 回复提示刷新，不出旧数字")

    print("\n3/3 passed (T15 chat 单元测试，不依赖 live server)")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

"""Smoke: stock TopN production tool + deterministic chat route (WS-102 T15).

fail-then-pass coverage:
  1. chat route: stock TopN intent must bypass the LLM and execute query_stock_top
  2. production tool: tool_query_stock_top returns true total_stock ordering
  3. ok=False: empty snapshot returns no ranking data and chat says it cannot confirm
  4. unknown store returns ok=False instead of guessed data
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB = _tmp.name
_tmp.close()

os.environ["HIPOP_DB"] = _DB
os.environ.pop("DB_URL", None)

from hipop.server import agent, data  # noqa: E402


def _setup_db(wf1_rows=None):
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS sales_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id BIGINT NOT NULL,
            alias TEXT NOT NULL,
            country TEXT NOT NULL,
            platform TEXT NOT NULL,
            store_name TEXT NOT NULL,
            active INT NOT NULL DEFAULT 1,
            UNIQUE (tenant_id, alias)
        );
        CREATE TABLE IF NOT EXISTS wf1_stock (
            tenant_id BIGINT NOT NULL,
            entity_alias TEXT NOT NULL,
            partner_sku TEXT NOT NULL,
            noon_total_qty INT,
            pending_inbound_qty INT,
            overseas_total_qty INT,
            total_stock INT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        );
        CREATE TABLE IF NOT EXISTS wf2_sku (
            tenant_id BIGINT NOT NULL,
            entity_alias TEXT NOT NULL,
            partner_sku TEXT NOT NULL,
            title TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        );
        INSERT OR IGNORE INTO sales_entities
            (tenant_id, alias, country, platform, store_name, active)
        VALUES
            (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-NOON-KSA', 1),
            (1, 'hipop_uae', 'AE', 'Noon', 'HIPOP-NOON-UAE', 1);
    """)
    c.execute("DELETE FROM wf1_stock")
    c.execute("DELETE FROM wf2_sku")
    if wf1_rows:
        c.executemany(
            "INSERT OR REPLACE INTO wf1_stock "
            "(tenant_id, entity_alias, partner_sku, total_stock, imported_at) "
            "VALUES (?,?,?,?,?)",
            wf1_rows,
        )
    c.commit()
    c.close()
    data.set_current_tenant(1)
    agent._chat_tenant.set(1)


def test_tool_returns_real_topn_order():
    print("== test_tool_returns_real_topn_order ==")
    _setup_db([
        (1, "hipop_ksa", "P51000", 300, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "TBP0169A", 999, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51001", 200, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "NULL_SKU", None, "2026-06-05T10:00:00"),
    ])
    res = agent.tool_query_stock_top("KSA", 3)
    skus = [r["sku"] for r in res["items"]]
    assert res["ok"] is True, res
    assert skus == ["TBP0169A", "P51000", "P51001"], skus
    assert res["items"][0]["total_stock"] == 999, res["items"][0]
    assert "NULL_SKU" not in skus, skus
    print(f"  ✓ production tool Top3: {[(r['sku'], r['total_stock']) for r in res['items']]}")


def test_chat_stock_topn_uses_deterministic_tool_route():
    print("== test_chat_stock_topn_uses_deterministic_tool_route ==")
    _setup_db([
        (1, "hipop_ksa", "TBP0169A", 999, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51000", 300, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51001", 200, "2026-06-05T10:00:00"),
    ])
    res = agent.chat(
        [{"role": "user", "content": "请列出 KSA 当前总库存最高的 3 个 SKU 和库存数量"}],
        {"store": "KSA", "tenant_id": 1},
    )
    reply = res["reply"]
    assert res["tools_used"] == ["query_stock_top"], res
    assert res["judge_method"] == "deterministic_stock_top_router", res
    assert "TBP0169A" in reply and "999" in reply, reply
    assert "已生成" not in reply and "已排名" not in reply and "没有直接" not in reply, reply
    print(f"  ✓ chat route tools={res['tools_used']} reply={reply.splitlines()[0]}")


def test_okfalse_empty_snapshot_no_guessing():
    print("== test_okfalse_empty_snapshot_no_guessing ==")
    _setup_db([])
    tool_res = agent.tool_query_stock_top("KSA", 3)
    assert tool_res["ok"] is False, tool_res
    chat_res = agent.chat(
        [{"role": "user", "content": "KSA 库存排名前三的 SKU 是哪些"}],
        {"store": "KSA", "tenant_id": 1},
    )
    reply = chat_res["reply"]
    assert chat_res["tools_used"] == ["query_stock_top"], chat_res
    assert "当前无法确认库存排名" in reply, reply
    assert "TBP0169A" not in reply and "P51000" not in reply, reply
    assert "已生成" not in reply and "已排名" not in reply, reply
    print("  ✓ empty snapshot says cannot confirm and does not guess SKU ranking")


def test_unknown_store_returns_okfalse():
    print("== test_unknown_store_returns_okfalse ==")
    _setup_db([])
    res = agent.tool_query_stock_top("EU", 3)
    assert res["ok"] is False, res
    assert "未知店铺" in res["error"], res
    print("  ✓ unknown store returns ok=False")


def main():
    print("\n▶ smoke_stock_topn_unit — production TopN route")
    tests = [
        test_tool_returns_real_topn_order,
        test_chat_stock_topn_uses_deterministic_tool_route,
        test_okfalse_empty_snapshot_no_guessing,
        test_unknown_store_returns_okfalse,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__} ERROR: {type(e).__name__}: {e}")
            failed += 1
    try:
        os.unlink(_DB)
    except Exception:
        pass
    print(f"\n{'✓' if failed == 0 else '✗'} smoke_stock_topn_unit: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

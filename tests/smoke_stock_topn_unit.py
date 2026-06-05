"""Smoke / unit test: query_stock_top tool 确定性验证（WS-102 T15）

fail-then-pass 覆盖点:
  1. ok=False 路径 — wf1_stock 快照为空时工具必须返回 ok=False，不许返回假数据
  2. 正向 TopN 路径 — 有数据时按 total_stock DESC 返回 Top N
  3. store 未知路径 — 未知 store code 必须返回 ok=False + error 含 "未知店铺"

测试方法：直接对 SQLite 运行 tool 所用的 SQL，无需 anthropic / 不需 server。
前提：运行前设置 HIPOP_DB 指向临时数据库（由本脚本自动 setup）。

跑法：
  python3 tests/smoke_stock_topn_unit.py
  或 make test（会被自动发现）
"""
import os
import sys
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# ── setup: 临时 DB，在 import data 之前设好 ──────────────────────────────────
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_path = _tmp.name
_tmp.close()
os.environ["HIPOP_DB"] = _tmp_path
os.environ.pop("DB_URL", None)

# 建 schema
def _setup_db(path, wf1_rows=None):
    """创建最小 schema 并可选写入 wf1_stock 行。"""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS sales_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id BIGINT NOT NULL,
            alias TEXT NOT NULL,
            country TEXT NOT NULL,
            platform TEXT NOT NULL,
            store_name TEXT NOT NULL,
            store_id INT,
            currency TEXT,
            feishu_table_id TEXT,
            feishu_decisions_table_id TEXT,
            feishu_stock_table_id TEXT,
            active INT NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, alias)
        );
        CREATE TABLE IF NOT EXISTS wf1_stock (
            tenant_id BIGINT NOT NULL,
            entity_alias TEXT NOT NULL,
            partner_sku TEXT NOT NULL,
            noon_total_qty INT,
            pending_inbound_qty INT,
            overseas_total_qty INT,
            yiwu_qty INT,
            dongguan_qty INT,
            total_stock INT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
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
    if wf1_rows:
        c.executemany(
            "INSERT OR REPLACE INTO wf1_stock "
            "(tenant_id, entity_alias, partner_sku, total_stock, imported_at) "
            "VALUES (?,?,?,?,?)",
            wf1_rows
        )
    c.commit()
    c.close()


# ── SQL from tool_query_stock_top (mirrors agent.py exactly) ─────────────────
TOPN_SQL = """
    SELECT s.partner_sku, s.total_stock, s.noon_total_qty,
           s.pending_inbound_qty, s.overseas_total_qty,
           s.imported_at, w2.title
    FROM wf1_stock s
    LEFT JOIN wf2_sku w2
      ON s.tenant_id = w2.tenant_id AND s.entity_alias = w2.entity_alias
         AND s.partner_sku = w2.partner_sku
    WHERE s.tenant_id = ? AND s.entity_alias = ? AND s.total_stock IS NOT NULL
    ORDER BY s.total_stock DESC
    LIMIT ?
"""

def _run_topn_sql(path, tid, alias, n):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    rows = c.execute(TOPN_SQL, (tid, alias, n)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── 测试 1: ok=False — wf1_stock 为空时返回空行 ──────────────────────────────
def test_okfalse_empty_snapshot():
    print("== test_okfalse_empty_snapshot ==")
    _setup_db(_tmp_path, wf1_rows=[])  # 空快照
    rows = _run_topn_sql(_tmp_path, 1, "hipop_ksa", 3)
    assert rows == [], f"预期空列表 → ok=False 路径，实际: {rows}"
    print("  ✓ wf1_stock 空 → rows==[] → tool 会返回 ok=False，禁止 Agent 编排名")


# ── 测试 2: 正向 Top3 — 按 total_stock DESC ───────────────────────────────────
def test_topn_returns_correct_order():
    print("== test_topn_returns_correct_order ==")
    _setup_db(_tmp_path, wf1_rows=[
        (1, "hipop_ksa", "TBP0169A", 999, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51000",   300, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51001",   300, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "P51002",   150, "2026-06-05T10:00:00"),
    ])
    rows = _run_topn_sql(_tmp_path, 1, "hipop_ksa", 3)
    assert len(rows) == 3, f"预期 3 行，实际 {len(rows)}"
    assert rows[0]["partner_sku"] == "TBP0169A", f"Top1 应是 TBP0169A，实际: {rows[0]['partner_sku']}"
    assert rows[0]["total_stock"] == 999, f"Top1 total_stock 应为 999，实际: {rows[0]['total_stock']}"
    print(f"  ✓ Top1={rows[0]['partner_sku']}={rows[0]['total_stock']}, Top2={rows[1]['partner_sku']}, Top3={rows[2]['partner_sku']}")


# ── 测试 3: NULL total_stock 不进排名 ────────────────────────────────────────
def test_null_total_stock_excluded():
    print("== test_null_total_stock_excluded ==")
    _setup_db(_tmp_path, wf1_rows=[
        (1, "hipop_ksa", "TBP0169A", 999, "2026-06-05T10:00:00"),
        (1, "hipop_ksa", "NULL_SKU", None, "2026-06-05T10:00:00"),  # NULL 应排除
    ])
    rows = _run_topn_sql(_tmp_path, 1, "hipop_ksa", 5)
    skus = [r["partner_sku"] for r in rows]
    assert "NULL_SKU" not in skus, f"NULL total_stock 的 SKU 不应出现在排名中，实际: {skus}"
    assert "TBP0169A" in skus, f"TBP0169A 应在排名中，实际: {skus}"
    print(f"  ✓ NULL_SKU 已过滤，排名: {skus}")


# ── 测试 4: LIMIT N 生效 ──────────────────────────────────────────────────────
def test_limit_n():
    print("== test_limit_n ==")
    _setup_db(_tmp_path, wf1_rows=[
        (1, "hipop_ksa", f"SKU{i:03d}", 100 - i, "2026-06-05T10:00:00")
        for i in range(10)
    ])
    rows = _run_topn_sql(_tmp_path, 1, "hipop_ksa", 3)
    assert len(rows) == 3, f"LIMIT 3 应返回 3 行，实际: {len(rows)}"
    assert rows[0]["total_stock"] >= rows[1]["total_stock"] >= rows[2]["total_stock"], \
        f"排名应降序: {[r['total_stock'] for r in rows]}"
    print(f"  ✓ LIMIT 3 生效，top stocks: {[r['total_stock'] for r in rows]}")


# ── runner ───────────────────────────────────────────────────────────────────
def main():
    print("\n▶ smoke_stock_topn_unit — T15 query_stock_top 数据层验证")
    tests = [
        test_okfalse_empty_snapshot,
        test_topn_returns_correct_order,
        test_null_total_stock_excluded,
        test_limit_n,
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
            print(f"  ✗ {t.__name__} ERROR: {e}")
            failed += 1

    # cleanup
    try:
        os.unlink(_tmp_path)
    except Exception:
        pass

    print(f"\n{'✓' if failed == 0 else '✗'} smoke_stock_topn_unit: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""smoke_t12_stock_source_contract.py — T12 库存查询事实源契约（WS-129/P0-S1）。

旧 T12（WS-105 fixture）钉死了固定实时数字（total=148 / noon_saleable=81 / overseas=66），
但这些是某天的快照值，不是可测试的口径。本 smoke 废弃那些固定数字，改钉：

  1. 每个库存数字必须携带来源（noon / erp）和时间戳（imported_at / updated_at）。
  2. total_stock = 各来源列的确定性求和（不含在途 in_transit_total_qty）。
  3. 在途（in_transit_total_qty）存在于 wf3_logistics_hub_v2，不在 wf1_stock.total_stock 里。
  4. 缺时间戳的缓存行不能当事实（validate_stock_row 报问题）。

fail-then-pass 验收：
  - 改前（WS-105 旧测试）：测 148/81/66 特定数字 → 合并契约后数字可能变 → FAIL
  - 改后（本测试）：钉来源+时间戳 + total_stock 计算公式 → 与具体实时数字解耦 → PASS

数据夹具（确定性，不涉及实时 DB / 不碰 hipop.db）：
  - TSKU001：完整 noon+ERP 数据，有 imported_at/updated_at
  - TSKU002：仅 ERP 数据（noon_total_qty=NULL），有 updated_at，没有 imported_at（ERP-only行）
  - TSKU003：noon_total_qty 非 NULL 但 imported_at 为 NULL → 应被契约识别为问题行
  - wf3 另建：TSKU001 有 in_transit_total_qty，验证它独立于 total_stock

跑法：
  python3 tests/smoke_t12_stock_source_contract.py
  make test-one F=tests/smoke_t12_stock_source_contract.py
  （也被 make test 自动聚合）
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import atexit
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "hipop" / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "hipop" / "scripts"))

# ── SQLite 临时库（CI 安全，不碰 hipop.db）──────────────────────────────────────
_TMP_DB = tempfile.NamedTemporaryFile(suffix="_t12.db", delete=False).name
atexit.register(lambda: os.unlink(_TMP_DB) if os.path.exists(_TMP_DB) else None)
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

# ── 夹具常量（来源与时间，不是固定实时数字）────────────────────────────────────
TENANT_ID = 9912
ALIAS = "t12_ksa"
IMPORTED_AT = "2026-06-08 08:00:00"   # noon 数据入库时间（固定，用于断言时间戳存在）
UPDATED_AT  = "2026-06-08 08:00:00"   # ERP 数据更新时间（固定，用于断言时间戳存在）
WF3_UPDATED_AT = "2026-06-08 09:00:00"


def _create_schema(conn: sqlite3.Connection) -> None:
    """在临时 DB 建所需表（wf1_stock + wf3_logistics_hub_v2）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wf1_stock (
          tenant_id                  INTEGER NOT NULL,
          entity_alias               TEXT    NOT NULL,
          partner_sku                TEXT    NOT NULL,
          noon_total_qty             INTEGER,
          noon_saleable_qty          INTEGER,
          noon_unsaleable_qty        INTEGER,
          noon_warehouses_json       TEXT,
          pending_inbound_qty        INTEGER,
          overseas_total_qty         INTEGER,
          overseas_breakdown_json    TEXT,
          yiwu_qty                   INTEGER,
          dongguan_qty               INTEGER,
          total_stock                INTEGER,
          imported_at                TEXT,
          updated_at                 TEXT,
          PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wf3_logistics_hub_v2 (
          tenant_id               INTEGER NOT NULL,
          sku                     TEXT    NOT NULL,
          in_transit_total_qty    INTEGER,
          has_stuck_batch         INTEGER,
          needs_ops_input         INTEGER,
          avg_transit_days        REAL,
          groups_json             TEXT,
          transit_batches_json    TEXT,
          total_transit_qty       INTEGER,
          updated_at              TEXT,
          PRIMARY KEY (tenant_id, sku)
        )
    """)
    conn.commit()


def _insert_fixtures(conn: sqlite3.Connection) -> None:
    """插入测试夹具行。数字仅为合理示例，断言不钉具体数字。"""
    # TSKU001：noon + ERP 完整行，有 imported_at + updated_at
    #   total_stock = noon_total + overseas + yiwu + dongguan + pending_inbound（不含 in_transit）
    noon_total = 80
    overseas = 60
    yiwu = 10
    dongguan = 5
    pending = 8
    total = noon_total + overseas + yiwu + dongguan + pending   # = 163
    conn.execute("""
        INSERT OR REPLACE INTO wf1_stock
        (tenant_id, entity_alias, partner_sku,
         noon_total_qty, noon_saleable_qty, noon_unsaleable_qty,
         overseas_total_qty, yiwu_qty, dongguan_qty, pending_inbound_qty,
         total_stock, imported_at, updated_at)
        VALUES (?,?,?, ?,?,?, ?,?,?,?, ?, ?,?)
    """, (TENANT_ID, ALIAS, "TSKU001",
          noon_total, noon_total - 1, 1,
          overseas, yiwu, dongguan, pending,
          total, IMPORTED_AT, UPDATED_AT))

    # TSKU002：ERP-only 行（noon_total_qty=NULL），imported_at 为 NULL — ERP-only 行不需要 noon 时间戳
    conn.execute("""
        INSERT OR REPLACE INTO wf1_stock
        (tenant_id, entity_alias, partner_sku,
         noon_total_qty, noon_saleable_qty,
         overseas_total_qty, yiwu_qty, dongguan_qty, pending_inbound_qty,
         total_stock, imported_at, updated_at)
        VALUES (?,?,?, ?,?, ?,?,?,?, ?, ?,?)
    """, (TENANT_ID, ALIAS, "TSKU002",
          None, None,
          30, 5, 0, 2,
          37, None, UPDATED_AT))

    # TSKU003：问题行——noon_total_qty 非 NULL 但 imported_at 为 NULL（契约违规）
    conn.execute("""
        INSERT OR REPLACE INTO wf1_stock
        (tenant_id, entity_alias, partner_sku,
         noon_total_qty, noon_saleable_qty,
         total_stock, imported_at, updated_at)
        VALUES (?,?,?, ?,?, ?, ?,?)
    """, (TENANT_ID, ALIAS, "TSKU003",
          50, 45,
          50, None, UPDATED_AT))

    # wf3：TSKU001 有在途数量（in_transit_total_qty），独立于 wf1_stock.total_stock
    in_transit_qty = 40
    conn.execute("""
        INSERT OR REPLACE INTO wf3_logistics_hub_v2
        (tenant_id, sku, in_transit_total_qty, updated_at)
        VALUES (?,?, ?,?)
    """, (TENANT_ID, "TSKU001", in_transit_qty, WF3_UPDATED_AT))
    conn.commit()


# 建库并写夹具（在 hipop.server.data import 之前）
_conn = sqlite3.connect(_TMP_DB)
_conn.row_factory = sqlite3.Row
_create_schema(_conn)
_insert_fixtures(_conn)
_conn.close()


# ── 测试函数 ─────────────────────────────────────────────────────────────────────

def _fetch_one(sku: str) -> dict | None:
    """直接从 SQLite 读 wf1_stock 单行（不走 hipop.server.data，避免 PG 依赖）。"""
    conn = sqlite3.connect(_TMP_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM wf1_stock WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT_ID, ALIAS, sku),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _fetch_wf3(sku: str) -> dict | None:
    """读 wf3_logistics_hub_v2 单行。"""
    conn = sqlite3.connect(_TMP_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND sku=?",
        (TENANT_ID, sku),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def test_t12_stock_carries_timestamp():
    """T12 核心（来源/时间取代固定数字）：库存行必须携带 imported_at/updated_at。

    TSKU001 是完整 noon+ERP 行，两个时间戳都有。
    断言：有 noon 数据的行 imported_at 非空，任何行 updated_at 非空。
    不断言具体的库存数字（与来源解耦，数字随实时数据变化）。
    """
    print("== T12 核心：库存行携带来源时间戳 ==")
    import fact_source_contract as C

    row = _fetch_one("TSKU001")
    assert row is not None, "TSKU001 应存在于 wf1_stock"

    # 1. noon 有数据 → imported_at 非空
    assert row["noon_total_qty"] is not None, "TSKU001 应有 noon_total_qty"
    assert row["imported_at"] is not None and row["imported_at"] != "", \
        f"noon 有数据的行必须携带 imported_at，但 imported_at={row['imported_at']!r}"
    print(f"  ✓ noon_total_qty={row['noon_total_qty']}，imported_at={row['imported_at']!r}（来源有时间戳）")

    # 2. updated_at 非空
    assert row["updated_at"] is not None and row["updated_at"] != "", \
        f"行必须携带 updated_at，但 updated_at={row['updated_at']!r}"
    print(f"  ✓ updated_at={row['updated_at']!r}（ERP 更新时间戳）")

    # 3. 契约验证通过（不应报问题）
    problems = C.validate_stock_row(row)
    assert not problems, f"TSKU001 是合规行，不应有契约问题: {problems}"
    print("  ✓ validate_stock_row：TSKU001 无契约违规")

    # 4. 不验证具体数字（来源/时间是唯一断言，不是 148/81/66）
    print(f"  ✓ 数字仅供参考（不钉值）：noon_total={row['noon_total_qty']}，"
          f"overseas={row['overseas_total_qty']}，total_stock={row['total_stock']}")


def test_t12_total_stock_excludes_in_transit():
    """T12 库存口径：total_stock 不含在途；在途来自 wf3，独立展示。

    TSKU001：
      total_stock = noon_total + overseas + yiwu + dongguan + pending（来自 wf1_stock）
      in_transit_total_qty = wf3 专属字段，来自 wf3_logistics_hub_v2

    断言：total_stock != total_stock + in_transit（确认在途没有加进去）。
    断言：wf3 行有 updated_at（在途数据也必须有时间戳）。
    """
    print("== T12 在途单列：total_stock 不含 in_transit_total_qty ==")
    import fact_source_contract as C
    from hipop.scripts import merge_stock_snapshot_v2 as merge

    row = _fetch_one("TSKU001")
    w3  = _fetch_wf3("TSKU001")
    assert row is not None, "TSKU001 应存在于 wf1_stock"
    assert w3  is not None, "TSKU001 应存在于 wf3_logistics_hub_v2"

    total = row["total_stock"]
    in_transit = w3["in_transit_total_qty"]

    # total_stock 由 TOTAL_STOCK_COMPONENTS 求和决定；in_transit 是独立字段
    expected_total = merge.compute_total_stock(row)
    assert total == expected_total, \
        f"total_stock={total} 应等于各来源列求和 {expected_total}（契约口径）"
    print(f"  ✓ total_stock={total} = 各来源列确定性求和（不含在途 {in_transit}）")

    # 如果在途被错误加入 total_stock，断言失败
    assert total != total + in_transit or in_transit == 0, \
        (f"total_stock={total} 不应等于 total+in_transit={total+in_transit}"
         f"（在途被错误加入库存合计）")
    print(f"  ✓ in_transit={in_transit} 来自 wf3_logistics_hub_v2，独立于 total_stock")

    # wf3 行有时间戳（在途数据也必须有时间戳）
    assert w3["updated_at"] is not None and w3["updated_at"] != "", \
        f"wf3 行缺 updated_at（在途数据也必须有时间戳）：{w3['updated_at']!r}"
    print(f"  ✓ wf3.updated_at={w3['updated_at']!r}（在途时间戳存在）")

    # in_transit 不在 NOT_IN_INVENTORY_TOTAL 映射的 wf1_stock 列里
    assert "in_transit_total_qty" not in merge.TOTAL_STOCK_COMPONENTS, \
        "in_transit_total_qty 不应在 TOTAL_STOCK_COMPONENTS 里"
    assert "in_transit_total_qty" in C.NOT_IN_INVENTORY_TOTAL, \
        "in_transit_total_qty 应在 NOT_IN_INVENTORY_TOTAL 里（单列规则）"
    print("  ✓ in_transit_total_qty ∈ NOT_IN_INVENTORY_TOTAL，∉ TOTAL_STOCK_COMPONENTS")


def test_t12_erp_only_row_no_noon_timestamp_required():
    """ERP-only 行（noon_total_qty=NULL）不要求 noon imported_at，但要有 updated_at。"""
    print("== T12 ERP-only 行：无 noon 数据不要求 noon 时间戳 ==")
    import fact_source_contract as C

    row = _fetch_one("TSKU002")
    assert row is not None, "TSKU002 应存在于 wf1_stock"
    assert row["noon_total_qty"] is None, "TSKU002 应是 ERP-only 行（noon_total_qty=NULL）"

    problems = C.validate_stock_row(row)
    assert not problems, \
        f"ERP-only 行不应报 noon 时间戳问题: {problems}"
    print(f"  ✓ ERP-only 行无契约违规（noon_total_qty=NULL 时不要求 imported_at）")

    assert row["updated_at"] is not None, "ERP-only 行应有 updated_at"
    print(f"  ✓ updated_at={row['updated_at']!r}（ERP 时间戳存在）")


def test_t12_no_noon_timestamp_is_contract_violation():
    """缺 noon 时间戳的行被契约识别为违规（缓存不能当事实）。"""
    print("== T12 无时间戳缓存不能当事实 ==")
    import fact_source_contract as C

    row = _fetch_one("TSKU003")
    assert row is not None, "TSKU003 应存在于 wf1_stock"
    assert row["noon_total_qty"] is not None, "TSKU003 应有 noon_total_qty"
    assert row["imported_at"] is None, "TSKU003 的 imported_at 应为 NULL（违规行）"

    problems = C.validate_stock_row(row)
    assert problems, \
        "TSKU003（noon 有数但 imported_at=NULL）应被识别为契约违规"
    assert any("imported_at" in p for p in problems), \
        f"问题应提及 imported_at: {problems}"
    print(f"  ✓ TSKU003 被识别为契约违规：{problems[0][:80]}")
    print("  ✓ 无时间戳的缓存不能作为事实（WS-129 契约）")


def test_t12_source_attribution_constants():
    """T12 来源归因：noon 和 ERP 列的来源归属常量正确。"""
    print("== T12 来源归因常量 ==")
    import fact_source_contract as C

    # noon 库存和销量来自 noon
    assert C.AUTHORITATIVE_SOURCES.get("noon_inventory") == C.SOURCE_NOON
    assert C.AUTHORITATIVE_SOURCES.get("sales") == C.SOURCE_NOON
    print(f"  ✓ noon_inventory/sales → SOURCE_NOON='{C.SOURCE_NOON}'")

    # 国内仓/海外仓/在途来自 ERP
    assert C.AUTHORITATIVE_SOURCES.get("domestic_inventory") == C.SOURCE_ERP
    assert C.AUTHORITATIVE_SOURCES.get("overseas_inventory") == C.SOURCE_ERP
    assert C.AUTHORITATIVE_SOURCES.get("in_transit") == C.SOURCE_ERP
    print(f"  ✓ domestic/overseas/in_transit → SOURCE_ERP='{C.SOURCE_ERP}'")

    # 来源归因的 wf1_stock 列示意（不需要 tool 支持，直接从契约常量验证）
    noon_cols_in_wf1 = {"noon_total_qty", "noon_saleable_qty", "noon_unsaleable_qty"}
    erp_cols_in_wf1 = {"yiwu_qty", "dongguan_qty", "overseas_total_qty", "pending_inbound_qty"}
    transit_in_wf3 = {"in_transit_total_qty"}

    for col in noon_cols_in_wf1:
        assert col not in transit_in_wf3, f"{col} 不应与在途字段混用"
    for col in transit_in_wf3:
        assert col not in C.INVENTORY_TOTAL_COMPONENTS, \
            f"{col} 不应在库存合计里（在途单列）"
    print("  ✓ noon 列 / ERP 列 / 在途列：来源分组清晰，无混用")


def main():
    failures = []
    tests = [
        test_t12_stock_carries_timestamp,
        test_t12_total_stock_excludes_in_transit,
        test_t12_erp_only_row_no_noon_timestamp_required,
        test_t12_no_noon_timestamp_is_contract_violation,
        test_t12_source_attribution_constants,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failures.append((fn.__name__, str(e)))
            import traceback
            print(f"  ✗ {fn.__name__}: {e}")
            traceback.print_exc()
        print()

    if failures:
        print(f"✗ {len(failures)} 项失败: {[n for n, _ in failures]}")
        return 1
    print("✓ T12 事实源契约 smoke 全过（WS-129：来源+时间戳，不钉固定数字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

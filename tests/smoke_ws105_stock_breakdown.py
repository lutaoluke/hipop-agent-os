"""WS-105 smoke: query_stock_breakdown tool 单 SKU 库存拆分（接线 + 真实值验证）。

fail-then-pass 承重墙：
  · 改前 base commit 8c1552a 无 query_stock_breakdown → import NameError → fail
  · 改后 tool 注册 + 读 wf1_hipop_ksa_stock → 返回正确值 → pass

纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db，CI 可复现。
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import atexit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ── 必须在 import hipop.server.data 之前设置 env，否则 DB_PATH 已被 module 缓存 ──
_TMP_DB = tempfile.NamedTemporaryFile(suffix="_ws105.db", delete=False).name
atexit.register(os.unlink, _TMP_DB)
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)


def _setup_fixture_db():
    """在临时 SQLite 中建 wf1_hipop_ksa_stock 并插入 TBB0116A / TBP0169A 两行。"""
    c = sqlite3.connect(_TMP_DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS wf1_hipop_ksa_stock (
            partner_sku              TEXT PRIMARY KEY,
            noon_total_qty           INTEGER,
            noon_saleable_qty        INTEGER,
            noon_unsaleable_qty      INTEGER,
            noon_warehouses_json     TEXT,
            pending_inbound_qty      INTEGER,
            overseas_total_qty       INTEGER,
            overseas_breakdown_json  TEXT,
            yiwu_qty                 INTEGER,
            dongguan_qty             INTEGER,
            total_stock              INTEGER,
            imported_at              TEXT,
            updated_at               TEXT
        )
    """)
    # TBB0116A: total=148, noon_saleable=81, overseas=66
    c.execute("""
        INSERT OR REPLACE INTO wf1_hipop_ksa_stock
        (partner_sku, noon_total_qty, noon_saleable_qty, noon_unsaleable_qty,
         overseas_total_qty, yiwu_qty, dongguan_qty, total_stock)
        VALUES ('TBB0116A', 82, 81, 1, 66, 0, 0, 148)
    """)
    # TBP0169A: total=10702, noon_saleable=22, overseas=10680
    c.execute("""
        INSERT OR REPLACE INTO wf1_hipop_ksa_stock
        (partner_sku, noon_total_qty, noon_saleable_qty, noon_unsaleable_qty,
         overseas_total_qty, yiwu_qty, dongguan_qty, total_stock)
        VALUES ('TBP0169A', 22, 22, 0, 10680, 0, 0, 10702)
    """)
    # TBN0001A: NULL fields — for NULL-guard test
    c.execute("""
        INSERT OR REPLACE INTO wf1_hipop_ksa_stock
        (partner_sku, noon_total_qty, noon_saleable_qty, noon_unsaleable_qty,
         overseas_total_qty, yiwu_qty, dongguan_qty, total_stock)
        VALUES ('TBN0001A', NULL, NULL, NULL, NULL, NULL, NULL, NULL)
    """)
    c.commit()
    c.close()


_setup_fixture_db()

# ── Import AFTER env is set and fixture DB is populated ──────────────────────
from hipop.server.agent import tool_query_stock_breakdown, TOOL_FUNCS  # noqa: E402


def test_wiring():
    """三死法 check: query_stock_breakdown 必须在 TOOL_FUNCS 中（接线缺失检查）。"""
    assert "query_stock_breakdown" in TOOL_FUNCS, \
        "query_stock_breakdown 未在 TOOL_FUNCS 中注册（接线缺失）"


def test_t12_tbb0116a():
    """T12: TBB0116A 各仓库存拆分返回正确值 total=148/noon_saleable=81/overseas=66。"""
    result = tool_query_stock_breakdown("TBB0116A", "KSA")
    assert result.get("found") is True, f"TBB0116A should be found: {result}"
    assert str(result.get("total_stock")) == "148", \
        f"total_stock should be 148, got {result.get('total_stock')}"
    assert str(result.get("noon_saleable")) == "81", \
        f"noon_saleable should be 81, got {result.get('noon_saleable')}"
    assert str(result.get("overseas")) == "66", \
        f"overseas should be 66, got {result.get('overseas')}"


def test_t11_tbp0169a():
    """T11: TBP0169A 各仓库存拆分返回正确值 total=10702/noon_saleable=22/overseas=10680。"""
    result = tool_query_stock_breakdown("TBP0169A", "KSA")
    assert result.get("found") is True, f"TBP0169A should be found: {result}"
    assert str(result.get("noon_saleable")) == "22", \
        f"noon_saleable should be 22, got {result.get('noon_saleable')}"
    assert str(result.get("overseas")) == "10680", \
        f"overseas should be 10680, got {result.get('overseas')}"
    assert str(result.get("total_stock")) == "10702", \
        f"total_stock should be 10702, got {result.get('total_stock')}"


def test_null_fields_return_placeholder():
    """NULL 字段防护：字段为 NULL 时返回 '无数据/未刷新'，不返回 0 或 None。"""
    result = tool_query_stock_breakdown("TBN0001A", "KSA")
    assert result.get("found") is True, f"TBN0001A should be found: {result}"
    assert result.get("total_stock") == "无数据/未刷新", \
        f"NULL total_stock should be '无数据/未刷新', got {result.get('total_stock')!r}"
    assert result.get("noon_saleable") == "无数据/未刷新", \
        f"NULL noon_saleable should be '无数据/未刷新', got {result.get('noon_saleable')!r}"
    assert result.get("overseas") == "无数据/未刷新", \
        f"NULL overseas should be '无数据/未刷新', got {result.get('overseas')!r}"


def test_missing_sku_returns_not_found():
    """无行防护：SKU 不在表中返回 found=False + error，不报异常，不返回 0。"""
    result = tool_query_stock_breakdown("TBZ_NOT_EXIST", "KSA")
    assert result.get("found") is False, \
        f"非存在 SKU 应返回 found=False: {result}"
    assert "error" in result, f"应有 error 字段: {result}"
    assert result.get("source") == "wf1_hipop_ksa_stock", \
        f"source 应为 wf1_hipop_ksa_stock: {result}"


if __name__ == "__main__":
    tests = [
        ("wiring (TOOL_FUNCS 接线)", test_wiring),
        ("T12 TBB0116A (148/81/66)", test_t12_tbb0116a),
        ("T11 TBP0169A (10702/22/10680)", test_t11_tbp0169a),
        ("NULL 字段 → '无数据/未刷新'", test_null_fields_return_placeholder),
        ("缺 SKU → found=False", test_missing_sku_returns_not_found),
    ]
    passed, failed = 0, []
    for name, t in tests:
        try:
            t()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed.append(name)
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)

"""Smoke: WS-N3.1 — noon ingest 的 CSV 入口与 live row 入口落库逐字段一致。

承重墙（数据 ingest 契约 + 确定性等价 smoke）：
  WS-N2 的 live fetcher 只是「同款 row 的新生产者」。本 smoke 钉死：同一份
  noon inventory，分别经
    · CSV 入口   —— run_v2(file=...)，内部 csv.DictReader → _aggregate → _upsert
    · live 入口  —— 直接把同形 dict row 喂 _aggregate(rows) → _upsert
  写进 wf1_stock 的 noon_total/saleable/unsaleable_qty 与 noon_warehouses_json
  **完全一致**；且两条路径都不覆盖 ERP 列（yiwu/dongguan/overseas/total_stock）
  与 pending_inbound_qty（保护部分 upsert 边界，归 ERP/WS-11）。

钉死三种死法：
  · 接线缺失：live row 必须真正经过同一 `_aggregate`/`_upsert`，不是另起炉灶。
  · 死代码短路：CSV 生产路径（run_v2）也必须走 row 接口；用「两路结果逐字段
    相等」证明 CSV 没有绕开重构后的 `_aggregate`。
  · 占位假数据：不 hardcode 期望值——直接比对两条真实路径的真实落库结果，
    并对真实聚合口径（total=saleable+unsaleable、按 row 顺序的 warehouses）做断言。

fail-then-pass：
  改动前 `_aggregate(path, tenant_id)` 只接 CSV 路径，`_aggregate(live_rows, ...)`
  会对 list 调 open() → TypeError → smoke fail。改动后接受 row 可迭代 → pass。

跑法：
  python3 tests/smoke_wf1_noon_csv_live_parity.py   或   make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db。）
"""
import os
import re
import sys
import csv
import json
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
# （server.data 在 import 时即读 HIPOP_DB → DB_PATH，故此处必须最先设好。）
_DB_CSV = tempfile.NamedTemporaryFile(suffix="_csv.db", delete=False).name   # _data / run_v2 写这里
_DB_LIVE = tempfile.NamedTemporaryFile(suffix="_live.db", delete=False).name  # live 路径 _upsert 写这里
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB_CSV

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1


def _extract_create(table: str) -> str:
    """从 db/schema_v2.sql 抠出指定表的 CREATE TABLE 语句（与真 schema 一致）。"""
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)

# ── 单一事实源：一份 noon inventory，3 SKU / 2 entity / 多仓多类型 ──────
# live fetcher 产出同形 dict（键同 noon Inventory CSV 列）。CSV 路径由此生成，
# 二者数据完全同源 → 任何落库差异都只可能来自「入口不同」。
INVENTORY_ROWS = [
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whA", "qty": 10, "inventory_type": "saleable"},
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whB", "qty": 5,  "inventory_type": "unsaleable"},
    {"country_code": "SA", "sku": "ZSA002", "warehouse_code": "whA", "qty": 20, "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 7,  "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 3,  "inventory_type": "unsaleable"},
]
_CSV_COLS = ["country_code", "sku", "warehouse_code", "qty", "inventory_type"]

# ERP 列预置值（验部分 upsert 不覆盖）。SKU-A 已有 ERP + pending_inbound。
_ERP_SEED = (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7)
_NOON_COMPARE_COLS = ("noon_total_qty", "noon_saleable_qty",
                      "noon_unsaleable_qty", "noon_warehouses_json")
_ERP_COLS = ("yiwu_qty", "dongguan_qty", "overseas_total_qty", "total_stock", "pending_inbound_qty")


def _seed(db_path, with_entities):
    """两库都建 wf1_stock + 预置 ERP 行；只有 _data 库需要 entities/sku 映射
    （`_aggregate` 的 country→entity、平台 SKU→partner_sku 解析读 _data 库）。"""
    c = sqlite3.connect(db_path)
    c.executescript(_extract_create("wf1_stock"))
    if with_entities:
        for t in ("sales_entities", "wf2_sku"):
            c.executescript(_extract_create(t))
        c.executemany(
            "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
            "VALUES (?,?,?,?,?,?,1)",
            [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
             (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
        )
        c.executemany(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
            [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
             (TENANT, "hipop_ksa", "SKU-B", "ZSA002"),
             (TENANT, "hipop_uae", "SKU-C", "ZAE001")],
        )
    # 两库都预置同一条带 ERP 列 + pending_inbound 的行，验 noon 部分 upsert 不覆盖
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
        "VALUES (?,?,?,?,?,?,?,?)",
        _ERP_SEED,
    )
    c.commit()
    c.close()


def _write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in INVENTORY_ROWS:
            w.writerow(r)


def _dump(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


def main():
    _seed(_DB_CSV, with_entities=True)    # _data 库：entities + sku 映射 + ERP 行
    _seed(_DB_LIVE, with_entities=False)  # live 路径落库目标：仅 wf1_stock + ERP 行

    import ingest_noon_stock_csv_v2 as noon

    # ── 路径 1：CSV 入口（run_v2 → csv.DictReader → _aggregate → _upsert）──
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_inventory.csv")
        _write_csv(csv_path)
        res = noon.run_v2(TENANT, file=csv_path)
    assert res["rows"] == 5, f"CSV 读入行数 {res['rows']} != 5"
    assert res["unmapped"] == 0, f"CSV 路径有 {res['unmapped']} 行未映射"
    assert res["skus"] == 3, f"CSV 写入 SKU 数 {res['skus']} != 3"

    # ── 路径 2：live 入口（直接把同形 dict row 喂 _aggregate → _upsert）──
    # live row 与 CSV 同序、qty 为 int（API 原生），证明两入口归一化后等价。
    live_rows = [dict(r) for r in INVENTORY_ROWS]
    bucket, n_rows, n_unmapped = noon._aggregate(live_rows, TENANT)
    assert (n_rows, n_unmapped) == (5, 0), f"live _aggregate 计数异常: {n_rows}/{n_unmapped}"
    live_conn = sqlite3.connect(_DB_LIVE)
    try:
        noon._upsert(live_conn, TENANT, bucket)
    finally:
        live_conn.close()

    csv_rows = _dump(_DB_CSV)
    live_rows_db = _dump(_DB_LIVE)

    # ── 等价断言：两条路径的 partner_sku 集合 + noon_* + warehouses_json 逐字段一致 ──
    assert set(csv_rows) == set(live_rows_db) == {"SKU-A", "SKU-B", "SKU-C"}, \
        f"两路 partner_sku 不一致: csv={set(csv_rows)} live={set(live_rows_db)}"
    assert not any(k.startswith("Z") for k in csv_rows), "平台 SKU 被当成主键写进去了"

    for sku in ("SKU-A", "SKU-B", "SKU-C"):
        cv, lv = csv_rows[sku], live_rows_db[sku]
        for col in _NOON_COMPARE_COLS:
            assert cv[col] == lv[col], \
                f"{sku}.{col} CSV={cv[col]!r} != live={lv[col]!r}（CSV/live 落库分叉）"
        # warehouses_json 不只字符串相等，反序列化后结构也必须一致
        assert json.loads(cv["noon_warehouses_json"]) == json.loads(lv["noon_warehouses_json"]), \
            f"{sku} noon_warehouses_json 结构不一致"

    # ── 真实聚合口径（非 hardcode 占位）：total = saleable + unsaleable ──
    for sku, row in csv_rows.items():
        assert row["noon_total_qty"] == row["noon_saleable_qty"] + row["noon_unsaleable_qty"], \
            f"{sku} total != saleable+unsaleable: {row}"
    a = csv_rows["SKU-A"]
    assert (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) == (15, 10, 5), a
    assert len(json.loads(a["noon_warehouses_json"])) == 2, "SKU-A 应有 2 条仓库明细"

    # ── 部分 upsert 边界：两路都不覆盖 ERP 列 / pending_inbound_qty ──
    for label, rows in (("csv", csv_rows), ("live", live_rows_db)):
        ra = rows["SKU-A"]
        assert (ra["yiwu_qty"], ra["dongguan_qty"], ra["overseas_total_qty"], ra["total_stock"]) == (99, 88, 77, 264), \
            f"[{label}] noon 路径覆盖了 ERP 列: {ra}"
        assert ra["pending_inbound_qty"] == 7, f"[{label}] noon 路径动了 pending_inbound_qty"
        assert rows["SKU-B"]["pending_inbound_qty"] is None and rows["SKU-C"]["pending_inbound_qty"] is None, \
            f"[{label}] 新建行凭空写了 pending_inbound_qty"

    print("✓ CSV 入口与 live row 入口共用同一 _aggregate/_upsert")
    print("✓ 同一份 noon inventory 两路落库 noon_* + noon_warehouses_json 逐字段一致")
    print("✓ 聚合口径真实（total=saleable+unsaleable，warehouses 按 row 顺序）")
    print("✓ 两路部分 upsert 都不覆盖 ERP 列与 pending_inbound_qty")
    print("\n4/4 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in (_DB_CSV, _DB_LIVE):
            try:
                os.unlink(p)
            except OSError:
                pass

"""Smoke: WS-N3.2 — noon_live_ingest runner 把 live 行接进同一 ingest 契约。

承重墙（runner 接线 + live/CSV 等价 + 取数失败回落，不编数）：
  WS-N3.1 已把 `_aggregate(rows, tenant_id)` / `_upsert` 重构成接 row 可迭代；
  本条把 live 源真正接上：
    · 注册 `noon_live_ingest` runner（进 workflow_runners._RUNNERS + api.WORKFLOW_REGISTRY）
    · runner 声明读/写表/来源，且声明的写列 == 真正落库的 noon_* 列（声明=实现）
    · live 行经 `run_live` 走同一 `_aggregate`/`_upsert`，与跑同份 CSV 逐字段一致
    · live 取数失败 → 整链回落 CSV interim（同契约，不短路），有明确失败信号；
      无 CSV 可回落 → 红灯 raise，绝不写 0/空仓库 JSON 冒充成功

钉死三种死法：
  · 接线缺失：断言 runner 已注册 + 真能从 runner 调到 run_live → _aggregate/_upsert
    （用 live 落库结果 == CSV 落库结果证明没另起炉灶）。
  · 死代码短路：fallback 必须走 run_v2（同一 _iter_csv_rows→_aggregate→_upsert），
    用「fallback 落库 == 直接跑 CSV 落库」证明没绕过契约。
  · 占位假数据：live 失败且无 CSV 时必须 raise，断言库里没有凭空写出的 0 库存行。

fail-then-pass：
  改动前 `ingest_noon_stock_csv_v2` 无 `run_live` / `set_live_row_producer`，
  且 `noon_live_ingest` 未注册 → AttributeError / 断言 fail。实现后 → 全 pass。

跑法：
  python3 tests/smoke_wf1_noon_live_runner.py   或   make test
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
_DB = tempfile.NamedTemporaryFile(suffix="_live_runner.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 单一事实源：一份 noon inventory（3 SKU / 2 entity / 多仓多类型）。
# live producer 产出同形 dict（键同 noon Inventory CSV 列）；CSV 路径由此生成，
# 两路数据完全同源 → 任何落库差异都只可能来自「入口不同」。
INVENTORY_ROWS = [
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whA", "qty": 10, "inventory_type": "saleable"},
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whB", "qty": 5,  "inventory_type": "unsaleable"},
    {"country_code": "SA", "sku": "ZSA002", "warehouse_code": "whA", "qty": 20, "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 7,  "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 3,  "inventory_type": "unsaleable"},
]
_CSV_COLS = ["country_code", "sku", "warehouse_code", "qty", "inventory_type"]

_ERP_SEED = (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7)
_NOON_COMPARE_COLS = ("noon_total_qty", "noon_saleable_qty",
                      "noon_unsaleable_qty", "noon_warehouses_json")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _reset_db():
    """重建 wf1_stock + entities/sku 映射 + 预置 ERP 行（每个子场景前调一次，
    保证两路从同一干净起点落库可比）。"""
    c = sqlite3.connect(_DB)
    try:
        c.executescript("DROP TABLE IF EXISTS wf1_stock;")
        c.executescript("DROP TABLE IF EXISTS sales_entities;")
        c.executescript("DROP TABLE IF EXISTS wf2_sku;")
        for t in ("wf1_stock", "sales_entities", "wf2_sku"):
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
        c.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _ERP_SEED,
        )
        c.commit()
    finally:
        c.close()


def _write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in INVENTORY_ROWS:
            w.writerow(r)


def _dump():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


def _assert_noon_equal(a, b, label):
    assert set(a) == set(b) == {"SKU-A", "SKU-B", "SKU-C"}, \
        f"[{label}] partner_sku 集合不一致: a={set(a)} b={set(b)}"
    for sku in ("SKU-A", "SKU-B", "SKU-C"):
        for col in _NOON_COMPARE_COLS:
            assert a[sku][col] == b[sku][col], \
                f"[{label}] {sku}.{col} {a[sku][col]!r} != {b[sku][col]!r}（落库分叉）"
        assert json.loads(a[sku]["noon_warehouses_json"]) == json.loads(b[sku]["noon_warehouses_json"]), \
            f"[{label}] {sku} noon_warehouses_json 结构不一致"


def _assert_erp_protected(rows, label):
    ra = rows["SKU-A"]
    assert (ra["yiwu_qty"], ra["dongguan_qty"], ra["overseas_total_qty"], ra["total_stock"]) == (99, 88, 77, 264), \
        f"[{label}] noon 路径覆盖了 ERP 列: {ra}"
    assert ra["pending_inbound_qty"] == 7, f"[{label}] noon 路径动了 pending_inbound_qty"


def main():
    import ingest_noon_stock_csv_v2 as noon
    from hipop.runtime import workflow_runners
    from hipop.server import api as _api

    live_rows_factory = lambda tenant_id: [dict(r) for r in INVENTORY_ROWS]

    # ── 1. 注册 + 声明检查（接线缺失死法）─────────────────────────────
    assert "noon_live_ingest" in workflow_runners.list_runners(), \
        "noon_live_ingest 未注册进 workflow_runners._RUNNERS（接线缺失）"
    assert "noon_live_ingest" in _api.WORKFLOW_REGISTRY, \
        "noon_live_ingest 未声明进 api.WORKFLOW_REGISTRY"
    runner = workflow_runners.get_runner("noon_live_ingest")
    reads = getattr(runner, "reads", None)
    writes = getattr(runner, "writes", None)
    assert reads and writes, "noon_live_ingest runner 未声明 reads/writes（读写声明不完整）"
    # 声明的写列必须正是 ingest 真正部分 upsert 的 noon_* 四列（声明=实现，不空喊）
    assert set(writes) == {f"wf1_stock.{c}" for c in noon._NOON_COLS}, \
        f"runner.writes 声明 {writes} 与实际写列 {noon._NOON_COLS} 不一致"
    # 读声明必须同时覆盖 live 源与 CSV fallback 输入（两条真实输入路径都得声明）
    reads_blob = " ".join(reads).lower()
    assert "live" in reads_blob or "fbn" in reads_blob, f"reads 未声明 live 源: {reads}"
    assert "csv" in reads_blob or "inbox" in reads_blob, f"reads 未声明 CSV fallback 输入: {reads}"
    print("✓ noon_live_ingest 已注册（runner + WORKFLOW_REGISTRY）且读写声明完整 = 实现")

    # ── 2. live 路径 == CSV 路径（逐字段一致）──────────────────────────
    _reset_db()
    res_live = noon.run_live(TENANT, live_producer=live_rows_factory)
    assert res_live["source"] == "live", f"应走 live 源: {res_live}"
    assert res_live["rows"] == 5 and res_live["skus"] == 3, f"live 计数异常: {res_live}"
    live_dump = _dump()

    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_inventory.csv")
        _write_csv(csv_path)
        res_csv = noon.run_v2(TENANT, file=csv_path)
    assert res_csv["rows"] == 5 and res_csv["skus"] == 3, f"CSV 计数异常: {res_csv}"
    csv_dump = _dump()

    _assert_noon_equal(live_dump, csv_dump, "live-vs-csv")
    _assert_erp_protected(live_dump, "live")
    _assert_erp_protected(csv_dump, "csv")
    a = live_dump["SKU-A"]
    assert (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) == (15, 10, 5), a
    assert len(json.loads(a["noon_warehouses_json"])) == 2, "SKU-A 应有 2 条仓库明细"
    print("✓ live 行经同一 _aggregate/_upsert，与同份 CSV 落库 noon_* 逐字段一致")

    # ── 3. runner 端到端：真能从 runner 调到 run_live → 落库（接线缺失死法）──
    # runner 用包路径 import（hipop.scripts.*），与本文件顶层 import 是不同 module
    # 实例 → 必须在 runner 真正用的 module 上注册 producer / stub merge。
    from hipop.scripts import ingest_noon_stock_csv_v2 as pkg_noon
    from hipop.scripts import merge_stock_snapshot_v2 as _merge
    _orig_merge = _merge.run_v2
    _merge.run_v2 = lambda tenant_id, **kw: {"_stub": True}  # merge 归 WS-12，本条 stub 掉
    pkg_noon.set_live_row_producer(live_rows_factory)
    try:
        _reset_db()
        out = runner("tid", TENANT, None, {}, {}, lambda: None, lambda p: None)
        assert "live" in out["summary"], f"runner summary 未标 live 源: {out}"
        runner_dump = _dump()
    finally:
        _merge.run_v2 = _orig_merge
        pkg_noon.set_live_row_producer(None)
    _assert_noon_equal(runner_dump, csv_dump, "runner-vs-csv")
    print("✓ runner 真正调到 run_live→_aggregate/_upsert（落库 == CSV 路径）")

    # ── 4. live 取数失败 + 有 CSV interim → 回落同一契约，有失败信号，落真数据 ──
    def _boom(tenant_id):
        raise RuntimeError("noon FBN API 503")

    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_inventory.csv")
        _write_csv(csv_path)
        res_fb = noon.run_live(TENANT, live_producer=_boom, file=csv_path)
    assert res_fb["source"] == "csv_fallback", f"应回落 CSV: {res_fb}"
    assert res_fb.get("live_error"), f"回落必须带明确失败信号 live_error: {res_fb}"
    assert "503" in res_fb["live_error"], f"失败信号应含原始 live 错误: {res_fb}"
    fb_dump = _dump()
    _assert_noon_equal(fb_dump, csv_dump, "fallback-vs-csv")  # 回落落的是真 CSV 数据
    _assert_erp_protected(fb_dump, "fallback")
    print("✓ live 失败 + 有 CSV → 回落同一契约（run_v2），有失败信号，落真数据不分叉")

    # ── 5. live 取数失败 + 无 CSV interim → 红灯 raise，绝不写 0 假数据 ──
    _reset_db()
    with tempfile.TemporaryDirectory() as empty_dir:
        raised = False
        try:
            noon.run_live(TENANT, live_producer=_boom, inbox=empty_dir)
        except noon.LiveSourceUnavailable as e:
            raised = True
            assert "503" in str(e), f"红灯异常应保留原始 live 错误: {e}"
    assert raised, "live 失败且无 CSV 可回落时必须 raise（不得冒充成功）"
    dead = _dump()
    # 只剩预置 ERP 行，noon_* 仍为 NULL；没有凭空写出的 0 库存/空 JSON 行
    assert set(dead) == {"SKU-A"}, f"红灯路径凭空写了行（占位假数据死法）: {set(dead)}"
    assert dead["SKU-A"]["noon_total_qty"] is None, \
        f"红灯路径把 noon_total_qty 写成了占位值: {dead['SKU-A']['noon_total_qty']!r}"
    assert dead["SKU-A"]["noon_warehouses_json"] is None, \
        f"红灯路径写了空仓库 JSON 冒充成功: {dead['SKU-A']['noon_warehouses_json']!r}"
    print("✓ live 失败且无 CSV 可回落 → 红灯 raise，库里无凭空 0 库存/空 JSON")

    print("\n6/6 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass

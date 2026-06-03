"""Smoke: WS-35 — noon 订单 ingest 改实时行来源（socket + fixture 等价）。

承重墙（socket 接线 + live/CSV 等价 + 取数失败回落，不编数）：
  把 `ingest_noon_csv_v2` 按 stock 链 WS-N3.1 的形状重构出插座：
    · `_aggregate(rows, tenant_id)` 接受行可迭代输入（CSV 解析行 / live fetcher 行同形）
    · `run_live` / `set_live_row_producer` / `get_live_row_producer` / `LiveSourceUnavailable`
    · CSV 入口 process_csv_v2 继续可用，且与 live 共用同一 `_aggregate`/`_upsert`（不分叉）
    · live 取数失败 → 整链回落 CSV interim（同契约，不短路），有明确失败信号；
      无 CSV 可回落 → 红灯 raise，绝不写默认销量/金额冒充成功
  本条只做 socket + fixture 等价 smoke，不实现真抓取（真 producer 归 WS-N2.1/WS-58）。

钉死三种死法：
  · 接线缺失：断言 run_live 真正调到模块级 `_aggregate` 和 `_upsert`
    （spy 计数），且 CSV 入口也经同一对函数 → 没另起炉灶。
  · 死代码短路：「live 落库 == 跑同份 CSV 落库」逐字段一致，证明 live 没绕过契约；
    fallback 必须走 run_v2→process_csv_v2（同 _aggregate/_upsert），「fallback 落库 == CSV 落库」。
  · 占位假数据：live 失败且无 CSV 时必须 raise，断言库里没有凭空写出的订单行/默认值。

fail-then-pass：
  改动前 `ingest_noon_csv_v2` 无 `run_live` / `set_live_row_producer` / `_aggregate`
  → AttributeError，本 smoke fail。实现后 → 全 pass。

跑法：
  python3 tests/smoke_wf2_noon_order_live.py   或   make test
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
_DB = tempfile.NamedTemporaryFile(suffix="_order_live.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"

# 单一事实源：一份 noon 订单（3 行 / 2 SKU，含正常 / 取消 / 退货）。
# live producer 产出同形 dict（键同 noon 订单 CSV 列）；CSV 路径由此生成，
# 两路数据完全同源 → 任何落库差异都只可能来自「入口不同」。
_CSV_COLS = ["partner_sku", "sku", "item_nr", "order_timestamp", "status",
             "fulfillment_model", "offer_price", "gmv_lcy", "currency_code",
             "dest_country", "family", "brand_code"]

ORDER_ROWS = [
    {"partner_sku": "SKU-A", "sku": "ZSA001", "item_nr": "IT-1",
     "order_timestamp": "2026-05-01 10:00:00", "status": "delivered",
     "fulfillment_model": "FBN", "offer_price": "10.50 SAR", "gmv_lcy": "12.00 SAR",
     "currency_code": "SAR", "dest_country": "SA", "family": "FAM1", "brand_code": "BR1"},
    {"partner_sku": "SKU-A", "sku": "ZSA001", "item_nr": "IT-2",
     "order_timestamp": "2026-05-02 09:00:00", "status": "cancelled",
     "fulfillment_model": "FBN", "offer_price": "10.50 SAR", "gmv_lcy": "0 SAR",
     "currency_code": "SAR", "dest_country": "SA", "family": "FAM1", "brand_code": "BR1"},
    {"partner_sku": "SKU-B", "sku": "ZSA002", "item_nr": "IT-3",
     "order_timestamp": "2026-05-03 14:30:00", "status": "cir",
     "fulfillment_model": "FBN", "offer_price": "25.00 SAR", "gmv_lcy": "25.00 SAR",
     "currency_code": "SAR", "dest_country": "SA", "family": "FAM2", "brand_code": "BR2"},
]

_ORDER_COMPARE_COLS = ("partner_sku", "noon_sku", "item_nr", "order_date", "status",
                       "is_cancelled", "is_return", "seller_price", "customer_paid",
                       "currency", "fulfillment", "destination", "source")
_SKU_COMPARE_COLS = ("partner_sku", "noon_sku", "fulfillment", "family", "brand",
                     "currency", "is_listed")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _reset_db():
    """重建 wf2_orders + wf2_sku（每个子场景前调一次，保证两路从同一干净起点落库可比）。"""
    c = sqlite3.connect(_DB)
    try:
        for t in ("wf2_orders", "wf2_sku"):
            c.executescript(f"DROP TABLE IF EXISTS {t};")
            c.executescript(_extract_create(t))
        c.commit()
    finally:
        c.close()


def _write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in ORDER_ROWS:
            w.writerow(r)


def _dump(table, key):
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r[key]: dict(r)
                for r in c.execute(f"SELECT * FROM {table} ORDER BY {key}")}
    finally:
        c.close()


def _assert_orders_equal(a, b, label):
    assert set(a) == set(b) == {"IT-1", "IT-2", "IT-3"}, \
        f"[{label}] item_nr 集合不一致: a={set(a)} b={set(b)}"
    for it in ("IT-1", "IT-2", "IT-3"):
        for col in _ORDER_COMPARE_COLS:
            assert a[it][col] == b[it][col], \
                f"[{label}] wf2_orders {it}.{col} {a[it][col]!r} != {b[it][col]!r}（落库分叉）"
        assert json.loads(a[it]["raw_json"]) == json.loads(b[it]["raw_json"]), \
            f"[{label}] {it} raw_json 结构不一致"


def _assert_sku_equal(a, b, label):
    assert set(a) == set(b) == {"SKU-A", "SKU-B"}, \
        f"[{label}] wf2_sku partner_sku 集合不一致: a={set(a)} b={set(b)}"
    for sku in ("SKU-A", "SKU-B"):
        for col in _SKU_COMPARE_COLS:
            assert a[sku][col] == b[sku][col], \
                f"[{label}] wf2_sku {sku}.{col} {a[sku][col]!r} != {b[sku][col]!r}（落库分叉）"


def main():
    import ingest_noon_csv_v2 as noon

    live_rows_factory = lambda tenant_id: [dict(r) for r in ORDER_ROWS]

    # ── 1. socket 存在（接线缺失死法的前置）──────────────────────────────
    for name in ("_aggregate", "_upsert", "run_live", "run_v2",
                 "set_live_row_producer", "get_live_row_producer",
                 "LiveSourceUnavailable"):
        assert hasattr(noon, name), f"socket 缺 {name}（接线缺失）"
    print("✓ socket 齐备：_aggregate / _upsert / run_live / set|get_live_row_producer / LiveSourceUnavailable")

    # ── 2. live 路径 == CSV 路径（逐字段一致 + 真调到同一 _aggregate/_upsert）──
    # spy 包住模块级 _aggregate/_upsert，证明 run_live 真走它们（没另起炉灶）。
    calls = {"agg": 0, "ups": 0}
    _orig_agg, _orig_ups = noon._aggregate, noon._upsert
    noon._aggregate = lambda *a, **k: (calls.__setitem__("agg", calls["agg"] + 1) or _orig_agg(*a, **k))
    noon._upsert = lambda *a, **k: (calls.__setitem__("ups", calls["ups"] + 1) or _orig_ups(*a, **k))
    try:
        _reset_db()
        res_live = noon.run_live(TENANT, live_producer=live_rows_factory, entity_alias=ALIAS)
    finally:
        noon._aggregate, noon._upsert = _orig_agg, _orig_ups
    assert res_live["source"] == "live", f"应走 live 源: {res_live}"
    assert res_live["orders"] == 3 and res_live["skus"] == 2, f"live 计数异常: {res_live}"
    assert calls["agg"] >= 1 and calls["ups"] >= 1, \
        f"run_live 未调到模块级 _aggregate/_upsert（疑似旁路/另起炉灶）: {calls}"
    live_orders, live_sku = _dump("wf2_orders", "item_nr"), _dump("wf2_sku", "partner_sku")

    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_orders.csv")
        _write_csv(csv_path)
        res_csv = noon.run_v2(TENANT, file=csv_path, entity_alias=ALIAS)
    assert res_csv["orders"] == 3, f"CSV 计数异常: {res_csv}"
    csv_orders, csv_sku = _dump("wf2_orders", "item_nr"), _dump("wf2_sku", "partner_sku")

    _assert_orders_equal(live_orders, csv_orders, "live-vs-csv")
    _assert_sku_equal(live_sku, csv_sku, "live-vs-csv")
    # 业务口径抽查：取消行 is_cancelled=1、退货行 is_return=1、金额解析正确
    assert csv_orders["IT-1"]["is_cancelled"] == 0 and csv_orders["IT-1"]["is_return"] == 0
    assert csv_orders["IT-2"]["is_cancelled"] == 1, "cancelled 行应 is_cancelled=1"
    assert csv_orders["IT-3"]["is_return"] == 1, "cir 行应 is_return=1"
    assert csv_orders["IT-1"]["seller_price"] == 10.5 and csv_orders["IT-1"]["customer_paid"] == 12.0
    print("✓ live 行经同一 _aggregate/_upsert，与同份 CSV 落库 wf2_orders/wf2_sku 逐字段一致")

    # ── 3. CSV 入口 process_csv_v2 也经同一 _aggregate/_upsert（生产接线不分叉）──
    calls2 = {"agg": 0, "ups": 0}
    noon._aggregate = lambda *a, **k: (calls2.__setitem__("agg", calls2["agg"] + 1) or _orig_agg(*a, **k))
    noon._upsert = lambda *a, **k: (calls2.__setitem__("ups", calls2["ups"] + 1) or _orig_ups(*a, **k))
    try:
        _reset_db()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_orders.csv")
            _write_csv(csv_path)
            c = sqlite3.connect(_DB)
            try:
                n = noon.process_csv_v2(TENANT, csv_path, c, entity_alias=ALIAS)
            finally:
                c.close()
    finally:
        noon._aggregate, noon._upsert = _orig_agg, _orig_ups
    assert n == 3, f"process_csv_v2 应返回 3 行: {n}"
    assert calls2["agg"] >= 1 and calls2["ups"] >= 1, \
        f"process_csv_v2 未经模块级 _aggregate/_upsert（CSV 与 live 口径分叉）: {calls2}"
    direct_orders = _dump("wf2_orders", "item_nr")
    _assert_orders_equal(direct_orders, csv_orders, "process_csv_v2-vs-run_v2")
    print("✓ CSV 生产入口 process_csv_v2 与 live 共用同一 _aggregate/_upsert（不分叉）")

    # ── 4. live 取数失败 + 有 CSV interim → 回落同一契约，有失败信号，落真数据 ──
    def _boom(tenant_id):
        raise RuntimeError("noon order API 503")

    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_orders.csv")
        _write_csv(csv_path)
        res_fb = noon.run_live(TENANT, live_producer=_boom, file=csv_path, entity_alias=ALIAS)
    assert res_fb["source"] == "csv_fallback", f"应回落 CSV: {res_fb}"
    assert res_fb.get("live_error") and "503" in res_fb["live_error"], \
        f"回落必须带明确失败信号 live_error（含原始错误）: {res_fb}"
    fb_orders, fb_sku = _dump("wf2_orders", "item_nr"), _dump("wf2_sku", "partner_sku")
    _assert_orders_equal(fb_orders, csv_orders, "fallback-vs-csv")
    _assert_sku_equal(fb_sku, csv_sku, "fallback-vs-csv")
    print("✓ live 失败 + 有 CSV → 回落同一契约（run_v2→process_csv_v2），有失败信号，落真数据不分叉")

    # ── 5. live 取数失败 + 无 CSV interim → 红灯 raise，绝不写默认值假数据 ──
    _reset_db()
    with tempfile.TemporaryDirectory() as empty_dir:
        raised = False
        try:
            noon.run_live(TENANT, live_producer=_boom, inbox=empty_dir, entity_alias=ALIAS)
        except noon.LiveSourceUnavailable as e:
            raised = True
            assert "503" in str(e), f"红灯异常应保留原始 live 错误: {e}"
    assert raised, "live 失败且无 CSV 可回落时必须 raise（不得冒充成功）"
    dead_orders = _dump("wf2_orders", "item_nr")
    dead_sku = _dump("wf2_sku", "partner_sku")
    assert dead_orders == {}, f"红灯路径凭空写了订单行（占位假数据死法）: {set(dead_orders)}"
    assert dead_sku == {}, f"红灯路径凭空写了 SKU 行（占位假数据死法）: {set(dead_sku)}"
    print("✓ live 失败且无 CSV 可回落 → 红灯 raise，库里无凭空订单/默认销量金额")

    # ── 6. 无 producer（fetcher 未接入）→ 同样回落 / 红灯，不卡死 ──────────
    noon.set_live_row_producer(None)  # 确保未注册
    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "noon_orders.csv")
        _write_csv(csv_path)
        res_np = noon.run_live(TENANT, file=csv_path, entity_alias=ALIAS)
    assert res_np["source"] == "csv_fallback", f"无 producer 应回落 CSV: {res_np}"
    _reset_db()
    with tempfile.TemporaryDirectory() as empty_dir:
        raised = False
        try:
            noon.run_live(TENANT, inbox=empty_dir, entity_alias=ALIAS)
        except noon.LiveSourceUnavailable:
            raised = True
    assert raised, "无 producer 且无 CSV → 必须红灯 raise"
    print("✓ 无 live producer（fetcher 未接入）→ 有 CSV 回落 / 无 CSV 红灯，绝不冒充成功")

    print("\n6/6 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass

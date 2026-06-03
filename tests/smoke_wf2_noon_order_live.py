"""Smoke: WS-35 — noon 订单 ingest 改实时行来源（socket + fixture 等价）。

承重墙（socket 接线 + live/CSV 等价 + 取数失败回落，不编数）：
  把 `ingest_noon_csv_v2` 按 stock 链 WS-N3.1 的形状重构出插座：
    · `_aggregate(rows, tenant_id)` 接受行可迭代输入（CSV 解析行 / live fetcher 行同形）
    · `run_live` / `set_live_row_producer` / `get_live_row_producer` / `LiveSourceUnavailable`
    · CSV 入口 process_csv_v2 继续可用，且与 live 共用同一 `_aggregate`/`_upsert`（不分叉）
    · live 取数失败 → 整链回落 CSV interim（同契约，不短路），有明确失败信号；
      无 CSV 可回落 → 红灯 raise，绝不写默认销量/金额冒充成功
  本条只做 socket + fixture 等价 smoke，不实现真抓取（真 producer 归 WS-N2.1/WS-58）。

单一来源（WS-34 收口）：
  · 行字段 / fixture 以 noon_live_contract 的 ORDERS 契约为唯一来源——本 smoke
    直接 load_fixture_rows(ORDERS) 当数据源，不在脚本里另写一份订单行。
  · producer 注册表也是 contract 的统一注册表：经 ingest 的 set_live_row_producer
    注册，contract 必须读到同一 fn（否则 ingest 与 contract 两套真相）。

钉死三种死法：
  · 接线缺失：断言 run_live / process_csv_v2 真正调到模块级 `_aggregate`/`_upsert`
    （spy 计数），且 ingest.set_live_row_producer 写进 contract 的 ORDERS 注册表
    （双向单一来源）→ 没另起炉灶、没另一份注册表。
  · 死代码短路：「live 落库 == 跑同份 CSV 落库」逐字段一致，证明 live 没绕过契约；
    fallback 必须走 run_v2→process_csv_v2（同 _aggregate/_upsert），「fallback 落库 == CSV 落库」。
  · 占位假数据：live 失败且无 CSV 时必须 raise，断言库里没有凭空写出的订单行/默认值。

fail-then-pass：
  改动前 `ingest_noon_csv_v2` 无 `run_live` / `_aggregate`、且 set_live_row_producer
  不接 contract 注册表 → AttributeError / 单一来源断言 fail。实现后 → 全 pass。

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

import noon_live_contract as C  # noqa: E402  （行字段 + fixture 唯一来源）

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"

# 单一事实源：noon 订单 fixture = noon_live_contract 的 ORDERS fixture（WS-34）。
# live producer 产出同形 dict（即 fixture 行）；CSV 路径由同一批行写出，
# 两路数据完全同源 → 任何落库差异都只可能来自「入口不同」。本 smoke 不在脚本里
# 另写一份订单行，避免 fixture 与契约漂移。
ORDER_ROWS = C.load_fixture_rows(C.ORDERS)
_CSV_COLS = list(ORDER_ROWS[0].keys())
EXPECT_ITEMS = {r["item_nr"] for r in ORDER_ROWS}
EXPECT_SKUS = {r["partner_sku"] for r in ORDER_ROWS}

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
    assert set(a) == set(b) == EXPECT_ITEMS, \
        f"[{label}] item_nr 集合不一致: a={set(a)} b={set(b)} expect={EXPECT_ITEMS}"
    for it in EXPECT_ITEMS:
        for col in _ORDER_COMPARE_COLS:
            assert a[it][col] == b[it][col], \
                f"[{label}] wf2_orders {it}.{col} {a[it][col]!r} != {b[it][col]!r}（落库分叉）"
        assert json.loads(a[it]["raw_json"]) == json.loads(b[it]["raw_json"]), \
            f"[{label}] {it} raw_json 结构不一致"


def _assert_sku_equal(a, b, label):
    assert set(a) == set(b) == EXPECT_SKUS, \
        f"[{label}] wf2_sku partner_sku 集合不一致: a={set(a)} b={set(b)} expect={EXPECT_SKUS}"
    for sku in EXPECT_SKUS:
        for col in _SKU_COMPARE_COLS:
            assert a[sku][col] == b[sku][col], \
                f"[{label}] wf2_sku {sku}.{col} {a[sku][col]!r} != {b[sku][col]!r}（落库分叉）"


def main():
    import ingest_noon_csv_v2 as noon

    n_orders = len(ORDER_ROWS)
    n_skus = len(EXPECT_SKUS)
    live_rows_factory = lambda tenant_id: [dict(r) for r in ORDER_ROWS]

    # 本 smoke 全程自己掌控 orders producer 注册表状态，结束清空，不污染其它 smoke。
    C.set_live_row_producer(C.ORDERS, None)
    try:
        # ── 1. socket 存在（接线缺失死法的前置）──────────────────────────────
        for name in ("_aggregate", "_upsert", "run_live", "run_v2",
                     "set_live_row_producer", "get_live_row_producer",
                     "LiveSourceUnavailable"):
            assert hasattr(noon, name), f"socket 缺 {name}（接线缺失）"
        print("✓ socket 齐备：_aggregate / _upsert / run_live / set|get_live_row_producer / LiveSourceUnavailable")

        # ── 2. 单一来源：ingest 的 producer 注册表 == contract 的 ORDERS 注册表 ──
        # WS-34 收口红队点：ingest 真实读的 producer 必须就是 contract 注册表，否则
        # 「在 contract 注册」与「ingest 实际读到」两套真相。双向校验 + WS-38 收口可见。
        assert noon.LiveSourceUnavailable is C.LiveSourceUnavailable, \
            "ingest 的 LiveSourceUnavailable 必须就是 contract 的同一类（不另起一份）"
        noon.set_live_row_producer(None)
        assert C.get_live_row_producer(C.ORDERS) is None, "清空后 contract.orders 应为 None"
        assert noon.get_live_row_producer() is None, "清空后 ingest 视图应为 None"

        fn_ingest = lambda tenant_id: []
        noon.set_live_row_producer(fn_ingest)  # 经 ingest 入口注册
        assert C.get_live_row_producer(C.ORDERS) is fn_ingest, \
            "ingest 注册后 contract 必须读到同一 fn（单一来源）"
        assert C.ORDERS not in C.missing_live_producers(), \
            "ingest 注册后 contract.missing 不应再缺 orders（WS-38 收口可见）"

        fn_contract = lambda tenant_id: []
        C.set_live_row_producer(C.ORDERS, fn_contract)  # 经 contract 入口注册
        assert noon.get_live_row_producer() is fn_contract, \
            "contract 注册后 ingest 必须读到同一 fn（单一来源）"
        noon.set_live_row_producer(None)
        assert C.get_live_row_producer(C.ORDERS) is None, "ingest 清除后 contract 同步为 None"
        print("✓ ingest.set ↔ contract.get(ORDERS) 双向一致，producer 注册表单一来源（无两套真相）")

        # ── 3. live 路径 == CSV 路径（逐字段一致 + 真调到同一 _aggregate/_upsert）──
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
        assert res_live["orders"] == n_orders and res_live["skus"] == n_skus, \
            f"live 计数异常: {res_live}（期望 orders={n_orders} skus={n_skus}）"
        assert calls["agg"] >= 1 and calls["ups"] >= 1, \
            f"run_live 未调到模块级 _aggregate/_upsert（疑似旁路/另起炉灶）: {calls}"
        live_orders, live_sku = _dump("wf2_orders", "item_nr"), _dump("wf2_sku", "partner_sku")

        _reset_db()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_orders.csv")
            _write_csv(csv_path)
            res_csv = noon.run_v2(TENANT, file=csv_path, entity_alias=ALIAS)
        assert res_csv["orders"] == n_orders, f"CSV 计数异常: {res_csv}"
        csv_orders, csv_sku = _dump("wf2_orders", "item_nr"), _dump("wf2_sku", "partner_sku")

        _assert_orders_equal(live_orders, csv_orders, "live-vs-csv")
        _assert_sku_equal(live_sku, csv_sku, "live-vs-csv")
        # 业务口径抽查：取消/退货/正常行的标记 + 金额解析（从 fixture 行推期望，不写死）。
        for r in ORDER_ROWS:
            it = r["item_nr"]
            st = (r.get("status") or "").strip().lower()
            from ingest_noon_csv import STATUS_CANCELLED, STATUS_RETURN, parse_money
            assert csv_orders[it]["is_cancelled"] == (1 if st in STATUS_CANCELLED else 0), \
                f"{it} is_cancelled 口径错（status={st}）"
            assert csv_orders[it]["is_return"] == (1 if st in STATUS_RETURN else 0), \
                f"{it} is_return 口径错（status={st}）"
            exp_price, _ = parse_money(r.get("offer_price"))
            exp_paid, _ = parse_money(r.get("gmv_lcy"))
            assert csv_orders[it]["seller_price"] == exp_price, f"{it} seller_price 解析错"
            assert csv_orders[it]["customer_paid"] == exp_paid, f"{it} customer_paid 解析错"
        print(f"✓ live 行经同一 _aggregate/_upsert，与同份 CSV 落库 wf2_orders/wf2_sku 逐字段一致"
              f"（{n_orders} 行 / {n_skus} SKU，含取消/退货口径）")

        # ── 4. CSV 入口 process_csv_v2 也经同一 _aggregate/_upsert（生产接线不分叉）──
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
        assert n == n_orders, f"process_csv_v2 应返回 {n_orders} 行: {n}"
        assert calls2["agg"] >= 1 and calls2["ups"] >= 1, \
            f"process_csv_v2 未经模块级 _aggregate/_upsert（CSV 与 live 口径分叉）: {calls2}"
        direct_orders = _dump("wf2_orders", "item_nr")
        _assert_orders_equal(direct_orders, csv_orders, "process_csv_v2-vs-run_v2")
        print("✓ CSV 生产入口 process_csv_v2 与 live 共用同一 _aggregate/_upsert（不分叉）")

        # ── 5. live 取数失败 + 有 CSV interim → 回落同一契约，有失败信号，落真数据 ──
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

        # ── 6. live 取数失败 + 无 CSV interim → 红灯 raise，绝不写默认值假数据 ──
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

        # ── 7. 无 producer（fetcher 未接入）→ 同样回落 / 红灯，不卡死 ──────────
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

        # ── 8. 字段缺失/契约外字段的 live 坏行 → 红灯，绝不静默吞行（验收③）───────
        # 门2 红队补洞：改前 `_aggregate` 对缺 item_nr 的 live 行只是 continue，返回
        # ({},0,0) 不 raise；坏行被静默吞掉、库里凭空少单。这里钉死：
        #   (a) live 路径经 WS-34 validate_row 校验，缺必填 / 契约外字段 → raise；
        #   (b) CSV 路径仍宽松跳过（两路口径不分叉，仅 live 入口加严格门）；
        #   (c) run_live 喂坏行：无 CSV → 红灯 raise 且库里无凭空行；有 CSV → 回落同契约。
        good_row = dict(ORDER_ROWS[0])
        missing_required = {**good_row, "item_nr": ""}       # 缺必填 item_nr
        unknown_field    = {**good_row, "made_up_col": "x"}  # 带契约外字段

        # (a) live 严格门：_aggregate(validate_kind=ORDERS) 对坏行 raise（指出问题字段）
        for bad, why in ((missing_required, "item_nr"), (unknown_field, "made_up_col")):
            raised = False
            try:
                noon._aggregate([dict(bad)], TENANT, entity_alias=ALIAS, validate_kind=C.ORDERS)
            except noon.LiveSourceUnavailable as e:
                raised = True
                assert why in str(e), f"红灯异常应点名问题字段 {why}: {e}"
            assert raised, f"live 坏行（{why}）必须经 contract 校验红灯，不得静默吞（验收③）"

        # (b) CSV 口径不分叉：同一坏行无 validate_kind → 宽松跳过，返回空 bucket（不 raise）
        b, nr, _ = noon._aggregate([dict(missing_required)], TENANT, entity_alias=ALIAS)
        assert b == {} and nr == 0, f"CSV 路径应宽松跳过脏行（不红灯、不入账）: {b}, {nr}"

        # (c1) run_live 坏行 + 无 CSV → 红灯 raise，库里无凭空订单/SKU（占位假数据死法）
        bad_producer = lambda tenant_id: [dict(missing_required)]
        _reset_db()
        with tempfile.TemporaryDirectory() as empty_dir:
            raised = False
            try:
                noon.run_live(TENANT, live_producer=bad_producer, inbox=empty_dir, entity_alias=ALIAS)
            except noon.LiveSourceUnavailable as e:
                raised = True
                assert "contract" in str(e), f"红灯异常应标明 contract 校验失败: {e}"
        assert raised, "run_live 收到坏 live 行且无 CSV 可回落时必须红灯 raise"
        assert _dump("wf2_orders", "item_nr") == {}, "坏行红灯路径凭空写了订单行（占位假数据死法）"
        assert _dump("wf2_sku", "partner_sku") == {}, "坏行红灯路径凭空写了 SKU 行（占位假数据死法）"

        # (c2) run_live 坏行 + 有 CSV interim → 回落同一契约，落真实 CSV 数据（带失败信号）
        _reset_db()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_orders.csv")
            _write_csv(csv_path)
            res_bad_fb = noon.run_live(TENANT, live_producer=bad_producer, file=csv_path, entity_alias=ALIAS)
        assert res_bad_fb["source"] == "csv_fallback", f"坏 live 行应回落 CSV: {res_bad_fb}"
        assert res_bad_fb.get("live_error") and "contract" in res_bad_fb["live_error"], \
            f"回落必须带契约校验失败信号 live_error: {res_bad_fb}"
        _assert_orders_equal(_dump("wf2_orders", "item_nr"), csv_orders, "bad-live-fallback-vs-csv")
        print("✓ 字段缺失/契约外字段 live 坏行 → 红灯 raise，库里无凭空行；CSV 路径口径不分叉")

        print("\n8/8 passed")
    finally:
        C.set_live_row_producer(C.ORDERS, None)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass

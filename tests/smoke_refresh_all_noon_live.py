"""Smoke: WS-32.5 / WS-38 — refresh_all_v2 接入 noon 实时 ingest（订单 + 库存）并守住「迁移完成 ≠ 没报错」。

承重墙（钉死「假绿：接了 noon ingest 但静默回落 CSV / 空过，全链绿着脸读旧手工数据」死法）：
  WS-32.5 把 `refresh_all_v2` 从「纯 ERP」改成在对应 ERP ingest 之后插入两个 noon 实时
  ingest 步（订单 → wf2_orders/wf2_sku；可售库存 → wf1_stock.noon_*）。本 smoke 不验「没抛
  异常」，而是真驱动 `refresh_all_v2` runner、断言两类 ingest **真实跑出 source==live** 且数据
  真落库；并用「无 live、无 CSV」复刻 blocked 态，证明 refresh_all 会**跳过依赖 noon 的分析步**、
  绝不拿空/旧数据产虚假补货结论。

  唯一被替身的是 ERP/分析步（wf2_products/wf2_sales/wf1_stock/merge/wf5/wf3/wf6）——它们打真
  ERP / playwright，与本条「noon 实时接线」无关，替成记录调用的 stub（_RUNNERS 覆盖，refresh_all
  自身不动）。两个 noon 步是**真 runner + 真 ingest 链**，仅 live producer 喂 WS-34 fixture 行
  （真抓取器的等价验证另见 smoke_noon_order/stock_fetcher 与 smoke_wf1_noon_stock_live_e2e）。

钉死三种死法：
  · 接线缺失：改前 `refresh_all_v2` 无 noon 步 → live 态「两个 noon workflow 都被 refresh_all
    调到且 source==live」断言必 fail（fail-then-pass 的 fail 态）。
  · 死代码短路 / 假绿：blocked 态（无 live、无 CSV）下 noon 步 raise → refresh_all 记 noon_blocked
    并**跳过 wf5（销售周期/补货）**；断言 wf5 stub 未被调到、summary 标 BLOCKED，
    且不依赖 noon 的 wf3/wf6 照跑（不过度阻断）。
  · 占位假数据：live 态 noon_* / wf2_orders 是真聚合落库（逐 SKU 核），且部分 upsert 不覆盖
    ERP 列 / pending_inbound_qty（在途 ERP 链不退化）。

fail-then-pass（三个 fresh 子进程，各落临时 SQLite，不连紫鸟、不碰 PG / live hipop.db）：
  · live   → 两 noon 步 source==live + wf2_orders/wf1_stock.noon_* 真落库 + wf5 跑 + ERP/pending 不动。
  · block  → 两 noon 步 raise → noon_blocked → wf5 被跳过（「source==live」与「wf5 跑过」在该态必 fail）。
  · fallback → 无 producer 但有 CSV interim → source==csv_fallback（显式回落，非 live）+ summary 标记，wf5 仍跑。

跑法：
  python3 tests/smoke_refresh_all_noon_live.py    # 被 make test 自动聚合
"""
import os
import re
import sys
import csv
import json
import sqlite3
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 被替成 stub 的 ERP/分析步（与 noon 实时接线无关；打真 ERP/playwright，本条不真跑）。
# kind 见 refresh_all_v2：analysis_needs_noon 仅 wf5（消费 noon 数据），它必须在 blocked 态被跳过。
_ERP_OR_ANALYSIS = (
    "wf2_products_v2", "wf2_sales_v2", "wf1_stock_v2", "wf1_stock_merge_v2",
    "wf5_sales_cycle_v2", "wf3_logistics_v2", "wf6_alerts_v2",
)
_NOON_STEPS = ("noon_orders_live_ingest", "noon_live_ingest")
# 预置 ERP 行（SKU-A）：验 noon 部分 upsert 不覆盖 ERP 列 / pending_inbound_qty（在途链不退化）。
_ERP_SEED = (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7)  # yiwu/dongguan/overseas/total/pending
_ERP_COLS = ("yiwu_qty", "dongguan_qty", "overseas_total_qty", "total_stock")


def _extract_create(table):
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _seed_db(db):
    """建 wf1_stock / wf2_orders / wf2_sku / sales_entities + country→entity、noon_sku→partner_sku
    映射 + 一行 ERP 种子（每个子进程一份干净库）。"""
    c = sqlite3.connect(db)
    try:
        for t in ("wf1_stock", "wf2_orders", "wf2_sku", "sales_entities"):
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


def _dump(db, table, key):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return {r[key]: dict(r)
                for r in c.execute(f"SELECT * FROM {table} ORDER BY {key}")}
    finally:
        c.close()


def _write_fixture_csv(path, rows):
    cols = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ── 子进程：fresh 解释器，按 mode 驱动真 refresh_all_v2（noon 步真跑，ERP/分析步 stub）──────
def _child():
    mode = os.environ["HIPOP_REFRESH_MODE"]  # live / block / fallback
    db = tempfile.NamedTemporaryFile(suffix="_refresh_noon.db", delete=False).name
    os.environ.pop("DB_URL", None)
    os.environ["HIPOP_DB"] = db
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "hipop"))
    sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))
    _seed_db(db)

    from hipop.runtime import workflow_runners as R
    import noon_live_contract as C
    # 必须用 runner 同款的包路径（hipop.scripts.*）拿 ingest 模块 —— 与裸名 import 是
    # 两份不同的模块对象（globals 各自独立），否则 INBOX_DIR 覆盖打不到 runner 真用的那份。
    from hipop.scripts import ingest_noon_csv_v2 as noon_orders
    from hipop.scripts import ingest_noon_stock_csv_v2 as noon_stock
    from hipop.scripts import merge_stock_snapshot_v2 as _merge

    ORDER_ROWS = C.load_fixture_rows(C.ORDERS)
    INV_ROWS = C.load_fixture_rows(C.MY_INVENTORY)

    # merge 归 WS-12，本条 stub 掉（noon 库存 runner 跑完会调它重算合并快照，与本条 source==live 无关）。
    _merge.run_v2 = lambda tenant_id, **kw: {"_stub": True}

    # 控制两个 ingest 的默认 inbox（refresh_all 传空 spec → 用模块 INBOX_DIR）：
    # live/block 用空目录（无 CSV 可回落）；fallback 各放对应 CSV interim。
    orders_inbox = tempfile.mkdtemp(prefix="refresh_orders_inbox_")
    stock_inbox = tempfile.mkdtemp(prefix="refresh_stock_inbox_")
    noon_orders.INBOX_DIR = orders_inbox
    noon_stock.INBOX_DIR = stock_inbox
    if mode == "fallback":
        # 订单 CSV 入口按【文件名国别】路由（country_from_filename 认 `_SA_`/`_AE_`），
        # 故按 dest_country 拆成每国一文件（对齐运营按店导表）；库存 CSV 入口按行 country_code
        # 路由，单文件即可。
        for cc in sorted({r["dest_country"] for r in ORDER_ROWS}):
            rows_cc = [r for r in ORDER_ROWS if r["dest_country"] == cc]
            _write_fixture_csv(os.path.join(orders_inbox, f"noon_{cc}_orders.csv"), rows_cc)
        _write_fixture_csv(os.path.join(stock_inbox, "noon_inventory.csv"), INV_ROWS)

    if mode in ("live",):
        C.set_live_row_producer(C.ORDERS, lambda tid: [dict(r) for r in ORDER_ROWS])
        C.set_live_row_producer(C.MY_INVENTORY, lambda tid: [dict(r) for r in INV_ROWS])
    else:
        # block / fallback：无 producer（fetcher 未接入 / 取数失败的等价态）。
        C.set_live_row_producer(C.ORDERS, None)
        C.set_live_row_producer(C.MY_INVENTORY, None)

    # ── 覆盖 _RUNNERS：ERP/分析步换成记录调用的 stub；两个 noon 步用真 runner（含调用顺序记录）──
    # refresh_all_v2 自身不覆盖：它内部 get_runner(step) 读的就是这张被改的表 → 真接线被验证。
    call_order = []
    called = set()

    def _make_stub(wf):
        def _stub(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
            call_order.append(wf)
            called.add(wf)
            return {"summary": f"[stub] {wf}"}
        return _stub

    for wf in _ERP_OR_ANALYSIS:
        R._RUNNERS[wf] = _make_stub(wf)

    for wf in _NOON_STEPS:
        real = R.get_runner(wf)
        assert real is not None, f"FAIL: noon 步 {wf} 未注册（接线缺失死法）"

        def _wrap(real_fn, name):
            def _w(*a, **k):
                call_order.append(name)
                called.add(name)
                return real_fn(*a, **k)
            return _w
        R._RUNNERS[wf] = _wrap(real, wf)

    runner = R.get_runner("refresh_all_v2")
    saved = {}
    out = runner("tid", TENANT, None, {}, {}, lambda: None, lambda p: saved.update(p))

    orders = _dump(db, "wf2_orders", "item_nr")
    stock = _dump(db, "wf1_stock", "partner_sku")
    payload = {
        "mode": mode,
        "summary": out.get("summary", ""),
        "noon_sources": out.get("noon_sources", {}),
        "noon_blocked": out.get("noon_blocked", []),
        "skipped": [s["step"] for s in out.get("skipped", [])],
        "call_order": call_order,
        "n_orders": len(orders),
        "stock_skus": sorted(stock.keys()),
    }
    print("REFRESH_RESULT " + json.dumps(payload, ensure_ascii=False))

    # ── per-mode 断言 ───────────────────────────────────────────────────
    if mode == "live":
        # 1) 两个 noon 步都被 refresh_all 真调到，且 source==live（改前无 noon 步 → 此处必 fail）
        if payload["noon_sources"] != {"noon_orders_live_ingest": "live", "noon_live_ingest": "live"}:
            print(f"FAIL: 两 noon 步未都跑出 source==live（疑似漏接 / 静默回落 CSV 假绿）: {payload['noon_sources']}")
            return 1
        if not {"noon_orders_live_ingest", "noon_live_ingest"} <= called:
            print(f"FAIL: refresh_all 未调到 noon 步（接线缺失，改前态）: {call_order}")
            return 1
        # 2) 步序：订单实时在 wf2_sales 之后、wf1_stock 之前；库存实时在 wf1_stock 之后、merge 之前
        idx = {w: i for i, w in enumerate(call_order)}
        if not (idx["wf2_sales_v2"] < idx["noon_orders_live_ingest"] < idx["wf1_stock_v2"]):
            print(f"FAIL: noon 订单实时步序不对（应在 wf2_sales 后、wf1_stock 前）: {call_order}")
            return 1
        if not (idx["wf1_stock_v2"] < idx["noon_live_ingest"] < idx["wf1_stock_merge_v2"]):
            print(f"FAIL: noon 库存实时步序不对（应在 wf1_stock 后、merge 前）: {call_order}")
            return 1
        # 3) noon 订单真落库 wf2_orders（5 行 fixture）
        if payload["n_orders"] != len(ORDER_ROWS):
            print(f"FAIL: wf2_orders 未由 live 行写入（{payload['n_orders']} != {len(ORDER_ROWS)}）")
            return 1
        # 4) noon 可售库存真聚合写 wf1_stock.noon_*（SKU-A 应 total/saleable/unsaleable = 15/10/5）
        a = stock["SKU-A"]
        if (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) != (15, 10, 5):
            print(f"FAIL: SKU-A noon_* 非 live 真聚合值（应 15/10/5）: {a}")
            return 1
        # 5) 部分 upsert 不覆盖 ERP 列 / pending_inbound_qty（在途 ERP 链不退化）
        if tuple(a[c] for c in _ERP_COLS) != (99, 88, 77, 264):
            print(f"FAIL: noon 路径覆盖了 ERP 列: {a}")
            return 1
        if a["pending_inbound_qty"] != 7:
            print(f"FAIL: noon 路径动了 pending_inbound_qty（在途链退化）: {a}")
            return 1
        # 6) noon OK → 依赖 noon 的分析步 wf5 真跑、summary 标 noon live
        if "wf5_sales_cycle_v2" not in called:
            print(f"FAIL: noon 正常时 wf5（销售周期/补货）未跑: {call_order}")
            return 1
        if "noon live=2/2" not in payload["summary"]:
            print(f"FAIL: summary 未标 noon live=2/2: {payload['summary']!r}")
            return 1
        print("LIVE_OK")
        return 0

    if mode == "block":
        # 无 live、无 CSV → 两 noon 步 raise → noon_blocked 含两者，库里无凭空 noon 数据
        if set(payload["noon_blocked"]) != {"noon_orders_live_ingest", "noon_live_ingest"}:
            print(f"FAIL: 无 live/无 CSV 时两 noon 步应 blocked（不得静默成功）: {payload}")
            return 1
        if payload["noon_sources"]:
            print(f"FAIL: blocked 态不应有 source==live/csv（疑似假绿）: {payload['noon_sources']}")
            return 1
        # 依赖 noon 的分析步必须被跳过（不产虚假补货结论），且不依赖 noon 的 wf3/wf6 照跑
        if "wf5_sales_cycle_v2" in called:
            print(f"FAIL: noon blocked 仍跑了 wf5（销售周期/补货）→ 会产虚假结论: {call_order}")
            return 1
        if "wf5_sales_cycle_v2" not in payload["skipped"]:
            print(f"FAIL: wf5 未记入 skipped（blocked 不可见）: {payload['skipped']}")
            return 1
        if not {"wf3_logistics_v2", "wf6_alerts_v2"} <= called:
            print(f"FAIL: 不依赖 noon 的 wf3/wf6 被过度阻断（应照跑）: {call_order}")
            return 1
        if "BLOCKED" not in payload["summary"]:
            print(f"FAIL: summary 未标 BLOCKED: {payload['summary']!r}")
            return 1
        # 库里 noon_* 仍 NULL（不写假 0），wf2_orders 空（红灯路径无凭空订单）
        if stock["SKU-A"]["noon_total_qty"] is not None or payload["n_orders"] != 0:
            print(f"FAIL: blocked 路径凭空写了 noon 数据（占位假数据死法）: stock={stock.get('SKU-A')}, orders={payload['n_orders']}")
            return 1
        print("BLOCK_OK")
        return 0

    if mode == "fallback":
        # 无 producer 但有 CSV interim → 显式回落 csv_fallback（非 live）+ summary 标记，wf5 仍跑
        if payload["noon_sources"] != {"noon_orders_live_ingest": "csv_fallback",
                                       "noon_live_ingest": "csv_fallback"}:
            print(f"FAIL: 有 CSV interim 应显式回落 csv_fallback（且非 live）: {payload['noon_sources']}")
            return 1
        if payload["noon_blocked"]:
            print(f"FAIL: 有 CSV 可回落不应 blocked: {payload}")
            return 1
        if "csv_fallback=2" not in payload["summary"]:
            print(f"FAIL: summary 未标 csv_fallback（「未走 live」不可见）: {payload['summary']!r}")
            return 1
        if "wf5_sales_cycle_v2" not in called:
            print(f"FAIL: 显式 CSV 回落允许继续，wf5 应跑: {call_order}")
            return 1
        # 回落落的是真 CSV 数据（不丢运营手工数据）
        if payload["n_orders"] != len(ORDER_ROWS) or stock["SKU-A"]["noon_total_qty"] is None:
            print(f"FAIL: csv_fallback 未落真实 CSV 数据: orders={payload['n_orders']}, stock={stock.get('SKU-A')}")
            return 1
        print("FALLBACK_OK")
        return 0

    print(f"FAIL: 未知 mode {mode}")
    return 1


# ── 父进程：每个 mode 起一个 fresh 解释器跑子进程 ──────────────────────────
def _run_child(mode):
    env = dict(os.environ)
    env["HIPOP_REFRESH_CHILD"] = "1"
    env["HIPOP_REFRESH_MODE"] = mode
    # 不依赖生产自动接线（本条用 contract.set_live_row_producer 显式控制 producer 状态）。
    env["HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE"] = "1"
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    # 1) live：refresh_all 真调两个 noon 实时步并跑出 source==live，wf2_orders/wf1_stock.noon_*
    #    真落库，ERP 列/pending 不被覆盖，wf5 正常跑。（改前 refresh_all 无 noon 步 → 此处必 fail）
    rc, out = _run_child("live")
    assert rc == 0 and "LIVE_OK" in out, \
        f"refresh_all 应真跑出两个 noon 步 source==live 且数据落库（改前无 noon 步 → 此断言 fail）:\n{out}"
    print("✓ refresh_all_v2 真调 noon 订单 + 可售库存两个实时步，source==live，wf2_orders/"
          "wf1_stock.noon_* 由 live 行真落库（SKU-A 15/10/5），ERP 列/pending 不被覆盖，wf5 正常跑")

    # 2) block：无 live、无 CSV → 两 noon 步 raise → noon_blocked → 跳过 wf5（不产虚假补货结论），
    #    wf3/wf6 照跑。证明 source==live 与「wf5 跑过」取决于 noon 真有数据、非写死（fail 态成立）。
    rc2, out2 = _run_child("block")
    assert rc2 == 0 and "BLOCK_OK" in out2, \
        f"noon 取数失败且无 CSV 时应 blocked 并跳过依赖 noon 的分析步，不编数:\n{out2}"
    print("✓ noon 实时取数失败 + 无 CSV → refresh_all 记 noon_blocked、跳过 wf5（销售周期/补货）"
          "不产虚假结论，库里无凭空 noon 数据；不依赖 noon 的 wf3/wf6 照跑（不过度阻断）")

    # 3) fallback：无 producer 但有 CSV interim → 显式回落 csv_fallback（非 live）+ summary 标记，wf5 仍跑。
    rc3, out3 = _run_child("fallback")
    assert rc3 == 0 and "FALLBACK_OK" in out3, \
        f"无 producer 但有 CSV interim 应显式回落 csv_fallback（非 live）并在 summary 报告:\n{out3}"
    print("✓ 无 producer 但有 CSV interim → 显式回落 csv_fallback（非 live）、summary 标记，"
          "wf5 仍跑（允许显式回落），落真实 CSV 数据不丢运营手工数据")

    print("\n3/3 passed")
    return 0


if __name__ == "__main__":
    if os.environ.get("HIPOP_REFRESH_CHILD") == "1":
        sys.exit(_child())
    sys.exit(main())

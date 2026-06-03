"""Smoke: WS-12 — 合并 v2 当前库存快照 → wf1_stock.total_stock。

运营抽检场景：一个店铺/国家(hipop_ksa) + 3 个 SKU，覆盖官方仓(noon)/国内仓
(义乌+东莞)/海外仓/送仓未上架(ASN) 各来源；期望 total_stock 由确定性规则手算得出。

fail-then-pass 承重墙（钉死三种死法）：
  · 死代码短路（最终快照绕过 pending_inbound_qty 规则）：改动前 total_stock 是
    ERP-only（漏 noon + 漏 pending）→ 与手算期望不等、pending 仍 NULL；改动后跑真实
    生产 runner 路径（pending runner 内置 merge）→ total_stock == noon_total +
    overseas + yiwu + dongguan + pending，逐 SKU 完全一致，pending 不再 NULL。
  · 占位假数据：total_stock 由各来源列真求和得出（覆写 components 会改变结果，证明
    没写死）；pending 从 staging 真算、刷新到非 NULL。
  · 接线缺失：消费端 wf_sales_cycle.read_sales_v2 从系统 DB 读到合并后的快照行
    （不是旧 Excel / v1）；runner 进 WORKFLOW_REGISTRY + list_runners() + refresh_all_v2
    链；noon/pending runner 改完来源列都会触发 merge。
  · 越界写 / ghost row：merge 只 roll-up 已存在行，不新建行（行数不变）；来源列与
    追溯 json(overseas_breakdown_json / noon_warehouses_json)原样保留。

改动前（base commit）：scripts/merge_stock_snapshot_v2 不存在 → import 失败 → fail。
改动后 → pass。

跑法：python3 tests/smoke_wf1_stock_merge_v2.py
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import re
import json
import time
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"

# 真实生产 runner 用 _data.set_current_tenant() 选库；测试要保证它落到临时库。
import server.data as _data  # noqa: E402
_data.set_current_tenant(TENANT)


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


# 抽检 fixture：3 个 SKU 的来源切片（模拟 ERP + noon 已各自落列、但还没合并），
# total_stock 故意预置成 ERP-only 的陈旧值（漏 noon + 漏 pending），pending=NULL。
_NOON_WH_A = json.dumps([{"warehouse_code": "KSA1", "qty": 80, "inventory_type": "saleable"},
                         {"warehouse_code": "KSA1", "qty": 20, "inventory_type": "damaged"}],
                        ensure_ascii=False)
_OVERSEAS_A = json.dumps({"KSA-海外仓": 50}, ensure_ascii=False)

# (sku, noon_total, noon_saleable, noon_unsaleable, overseas, yiwu, dongguan,
#  stale_total_stock_ERP_only, noon_warehouses_json, overseas_breakdown_json)
PRESET_ROWS = [
    ("SKU-A", 100, 80, 20, 50, 10, 5, 50 + 10 + 5, _NOON_WH_A, _OVERSEAS_A),  # 全来源齐
    ("SKU-B", 0,   0,  0,  200, 0, 0, 200,          None,      None),         # 海外仓为主
    ("SKU-C", 60,  60, 0,  0,  20, 20, 0 + 20 + 20, None,      None),         # 国内+官方，无海外
]

# staging ASN（喂 compute_pending_inbound），按 WS-11 状态规则计入：
#   SKU-A: Scheduled 30 + Handover 10 (+GRN Completed 999 排除)        = 40
#   SKU-B: Put Away In Progress 15 (+Cancelled 100 排除)              = 15
#   SKU-C: 仅 Created 7（不计入）→ pending 真算成 0（证明刷新到非 NULL）  = 0
STAGING_ROWS = [
    (ALIAS, "noon_asn",    "ASN-A1", "SKU-A", 30,  "Scheduled"),
    (ALIAS, "noon_asn",    "ASN-A2", "SKU-A", 10,  "Handover"),
    (ALIAS, "noon_asn",    "ASN-A3", "SKU-A", 999, "GRN Completed"),
    (ALIAS, "noon_asn",    "ASN-B1", "SKU-B", 15,  "Put Away In Progress"),
    (ALIAS, "noon_asn",    "ASN-B2", "SKU-B", 100, "Cancelled"),
    (ALIAS, "noon_asn",    "ASN-C1", "SKU-C", 7,   "Created"),
]

EXPECTED_PENDING = {"SKU-A": 40, "SKU-B": 15, "SKU-C": 0}
# total_stock = noon_total + overseas + yiwu + dongguan + pending（确定性规则手算）
EXPECTED_TOTAL = {
    "SKU-A": 100 + 50 + 10 + 5 + 40,   # 205
    "SKU-B": 0 + 200 + 0 + 0 + 15,     # 215
    "SKU-C": 60 + 0 + 20 + 20 + 0,     # 100
}


def _setup_db():
    import ingest_inbound_staging_v2 as inbound  # noqa: E402
    c = sqlite3.connect(_TMP_DB)
    for t in ("wf1_stock", "wf2_sku"):
        c.executescript(_extract_create(t))
    inbound.ensure_staging_tables(c)
    c.executemany(
        f"INSERT INTO {inbound.STAGING_TABLE} "
        "(tenant_id, entity_alias, source, asn_number, partner_sku, qty, status) "
        "VALUES (?,?,?,?,?,?,?)",
        [(TENANT, e, src, asn, sku, qty, st) for (e, src, asn, sku, qty, st) in STAGING_ROWS],
    )
    for (sku, ntot, nsal, nuns, ov, yw, dg, stale_total, nwh, obd) in PRESET_ROWS:
        c.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, noon_warehouses_json, "
            "overseas_total_qty, overseas_breakdown_json, yiwu_qty, dongguan_qty, "
            "total_stock, pending_inbound_qty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TENANT, ALIAS, sku, ntot, nsal, nuns, nwh, ov, obd, yw, dg, stale_total, None),
        )
        # 消费端 read_sales_v2 先查 wf2_sku，才会返回库存。
        c.execute(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, "
            "sales_10d, sales_30d, sales_60d, sales_180d, latest_profit_rate) "
            "VALUES (?,?,?,?,?,?,?,?)", (TENANT, ALIAS, sku, 5, 15, 30, 90, 0.2),
        )
    c.commit()
    c.close()


def _rows():
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r) for r in c.execute(
            "SELECT * FROM wf1_stock WHERE tenant_id=? AND entity_alias=?", (TENANT, ALIAS))}
    finally:
        c.close()


def main():
    _setup_db()
    import merge_stock_snapshot_v2 as merge

    # ── 0. 规则是纯函数 + 参数化（不写死）──────────────────────────
    row = {"noon_total_qty": 100, "overseas_total_qty": 50, "yiwu_qty": 10,
           "dongguan_qty": 5, "pending_inbound_qty": 40}
    assert merge.compute_total_stock(row) == 205, "默认合并规则求和错"
    # 覆写 components：只算 overseas → 50，证明 components 真被用、没写死。
    assert merge.compute_total_stock(row, components=("overseas_total_qty",)) == 50, \
        "components 覆写无效 → 合并规则被写死了"
    # NULL/缺列当 0，不让缺一路输入把整行写成 NULL/崩。
    assert merge.compute_total_stock({"noon_total_qty": None, "yiwu_qty": 7}) == 7, \
        "NULL/缺列没按 0 处理"

    # ── 1. 改动前(merge 跑之前)：total_stock 陈旧 + pending 全 NULL（这正是要修的洞）──
    before = _rows()
    for sku, want in EXPECTED_TOTAL.items():
        assert before[sku]["pending_inbound_qty"] is None, f"{sku} 前置应为 NULL"
        assert before[sku]["total_stock"] != want, \
            f"{sku} 前置 total_stock 不该已等于期望（fixture 没体现「合并前不完整」）"

    # ── 2. 跑真实生产 runner 路径：pending runner 内置 merge（接线 + 反短路）──────
    from hipop.runtime import workflow_runners as wr
    noop = lambda *a, **k: None
    wr._RUNNERS["wf1_pending_inbound_v2"](
        "task-x", TENANT, "tester", {}, {}, noop, noop)

    after = _rows()

    # 行数不变：merge 只 roll-up，不新建 ghost row。
    assert len(after) == len(PRESET_ROWS), \
        f"merge 改了行数 {len(before)}→{len(after)}（凭空新建了行？）"

    for sku in EXPECTED_TOTAL:
        r = after[sku]
        # 占位假数据死法：pending 真算、刷新到非 NULL。
        assert r["pending_inbound_qty"] == EXPECTED_PENDING[sku], \
            f"{sku} pending_inbound={r['pending_inbound_qty']} != {EXPECTED_PENDING[sku]}"
        assert r["pending_inbound_qty"] is not None, f"{sku} pending 仍 NULL"
        # 死代码短路死法：合并后 total_stock 含 noon + pending，逐 SKU 与手算一致。
        assert r["total_stock"] == EXPECTED_TOTAL[sku], \
            f"{sku} total_stock={r['total_stock']} != {EXPECTED_TOTAL[sku]}（合并绕过 noon/pending？）"

    # ── 3. 来源列 + 追溯 json 原样保留（merge 只动 total_stock）────────────
    a = after["SKU-A"]
    assert (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"],
            a["overseas_total_qty"], a["yiwu_qty"], a["dongguan_qty"]) == (100, 80, 20, 50, 10, 5), \
        f"merge 覆盖了来源列: {a}"
    assert a["noon_warehouses_json"] == _NOON_WH_A, "noon_warehouses_json 追溯字段被改了"
    assert a["overseas_breakdown_json"] == _OVERSEAS_A, "overseas_breakdown_json 追溯字段被改了"

    # ── 4. 接线缺失死法：下游消费端从系统 DB 读到合并后的快照（非 Excel/v1）──────
    from hipop.workflows import wf_sales_cycle
    cc = sqlite3.connect(_TMP_DB)
    cc.row_factory = sqlite3.Row
    try:
        sale = wf_sales_cycle.read_sales_v2(TENANT, ALIAS, "SKU-A", cc)
    finally:
        cc.close()
    # immediate=noon_saleable(80)+pending(40)=120, transfer=overseas(50),
    # domestic=yiwu(10)+dongguan(5)=15 —— 全部来自系统 DB 合并行。
    assert sale and sale["immediate"] == 120 and sale["transfer"] == 50 and sale["domestic"] == 15, \
        f"下游读到的快照不对（接线缺失/读了旧源）: {sale}"

    # ── 5. 真实入口接线：runner + WORKFLOW_REGISTRY + refresh_all_v2 链 ──────
    assert "wf1_stock_merge_v2" in set(wr.list_runners()), "merge runner 没注册"
    from hipop.server import api
    assert "wf1_stock_merge_v2" in api.WORKFLOW_REGISTRY, \
        "wf1_stock_merge_v2 不在 WORKFLOW_REGISTRY → /run-workflow 会 400"
    _, steps, _ = api.WORKFLOW_REGISTRY["wf1_stock_merge_v2"]
    fn = api._resolve_callable(steps[0][2])
    assert callable(fn) and fn.__name__ == "run_v2", f"callable 解析失败: {steps}"
    # refresh_all_v2 全量链必须含 merge 步骤（否则 API 驱动路径 total_stock 仍漏 pending）。
    wr_src = open(os.path.join(REPO, "hipop", "runtime", "workflow_runners.py"),
                  encoding="utf-8").read()
    assert '("wf1_stock_merge_v2"' in wr_src, "refresh_all_v2 没接入 wf1_stock_merge_v2 步骤"

    # ── 5b. 确定性校验写成 verifier（交付门复跑，不靠自述）────────────────
    from hipop.runtime import verifiers as vr
    assert "wf1_stock_merge_v2" in vr._VERIFIERS, "verifier 注册表缺 → 交付门没确定性校验"
    # 合并后的快照口径一致 → verifier 过（total_stock 全 == 各来源求和、非 NULL）。
    res_ok = vr.run_verifier("wf1_stock_merge_v2", "smoke", TENANT, time.time() - 3600)
    assert res_ok and res_ok["ok"] is True, f"合并后 verifier 应过: {res_ok}"
    assert res_ok["evidence"]["mismatched_total_stock"] == 0, res_ok
    # 注入一行被绕过/写错的 total_stock（模拟最终快照绕过 pending）→ verifier 必须红灯。
    cbad = sqlite3.connect(_TMP_DB)
    cbad.execute(
        "UPDATE wf1_stock SET total_stock=999999 WHERE tenant_id=? AND entity_alias=? "
        "AND partner_sku='SKU-A'", (TENANT, ALIAS))
    cbad.commit(); cbad.close()
    res_bad = vr.run_verifier("wf1_stock_merge_v2", "smoke", TENANT, time.time() - 3600)
    assert res_bad and res_bad["ok"] is False, f"被绕过的 total_stock verifier 应红灯: {res_bad}"
    assert res_bad["evidence"]["mismatched_total_stock"] >= 1, res_bad

    # ── 6. 死代码短路死法：绝不创建/写 v1 per-alias 表 ──────────────────
    c = sqlite3.connect(_TMP_DB)
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    v1 = {t for t in tables if re.fullmatch(r"wf1_hipop_\w+_stock", t)}
    assert not v1, f"写到了 v1 per-alias 表（死法#2）: {v1}"

    print("✓ 合并规则纯函数 + components 参数化可覆写、NULL 当 0（未写死）")
    print("✓ 改动前 total_stock 漏 noon+pending、pending 全 NULL（洞已复现）")
    print("✓ 跑真实 pending runner 路径后：3 个 SKU total_stock = 官方仓+海外仓+国内+送仓未上架，逐项手算一致")
    print("✓ pending_inbound 真算并刷新到非 NULL（含计入和为 0 的 SKU-C）")
    print("✓ merge 只 roll-up 不新建行；来源列 + overseas/noon 追溯 json 原样保留")
    print("✓ 下游 read_sales_v2 从系统 DB 读到合并快照（immediate=noon_saleable+pending）")
    print("✓ runner + WORKFLOW_REGISTRY + refresh_all_v2 链均接入；未写 v1 wf1_<alias>_stock")
    print("✓ verifier 复跑确定性校验：合并后 ok=True，注入绕过/写错 total_stock 后 ok=False")
    print("\n8/8 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

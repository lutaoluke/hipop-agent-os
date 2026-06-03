"""Smoke: WS-N3.3 — noon_live_ingest verifier + 完整门测（数据链端到端验收）。

承重墙：WS-N3.2 已 land `noon_live_ingest` runner（live 行 → 同一 _aggregate/_upsert
落 wf1_stock.noon_*）。本条补**确定性 verifier + 完整门测**，证明整条数据链是通的，
不是孤立函数绿：

  live 行 → runner 走同一 ingest 落库 → WS-11 pending 真算 →
  verifier 真查 wf1_stock 拦三类坏数据 → 消费端 wf_sales_cycle.read_sales_v2 的
  即时可售 == noon_saleable + pending_inbound 真读到实时落库值。

钉死三种死法：
  · 接线缺失：不只测 verifier 函数 —— 真从 runner 调到 run_live 落库，再用消费端
    read_sales_v2 读出 immediate，证明 runner 写库后消费端真读到 live noon + 非 NULL
    pending（== noon可售 + 送仓未上架）。
  · 死代码短路：消费端口径 immediate 必须随 live noon_saleable + 真算 pending 变化，
    不读旧/空字段（用具体数值钉死：immediate == 落库 saleable + 落库 pending）。
  · 占位假数据：pending 由 WS-11 compute_pending_inbound_v2 **真算**（含 GRN→0 刷新），
    不硬编码/不测试预填；verifier 的 pending 非 NULL 断言查的是真实落库状态。

fail-then-pass（三类坏数据，改动前现有门不会失败 —— base commit 无 noon_live_ingest
verifier，run_verifier 返 None；本条加 verifier 后分别被拦下）：
  · noon_saleable_qty > noon_total_qty           → 断言 1 拦下
  · 过旧 updated_at（超新鲜度阈值 N 小时）        → 断言 2 拦下
  · 已知 SKU pending_inbound_qty IS NULL          → 断言 3 拦下
新鲜度阈值 N 是参数（配置/运行参数/测试可控默认），非 prompt 规则 —— 本测用
max_age_hours 覆写证明它是真参数。

跑法：python3 tests/smoke_wf_noon_live_verifier.py   或   make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db。）
"""
import os
import re
import sys
import time
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_DB = tempfile.NamedTemporaryFile(suffix="_noon_verifier.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB
# 别让外部 env 干扰新鲜度默认值（本测显式控制阈值）。
os.environ.pop("HIPOP_NOON_FRESHNESS_MAX_HOURS", None)

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 单一事实源：一份 noon inventory（live producer 产同形 dict，键同 noon Inventory CSV 列）。
# SKU-A: total15 saleable10 / SKU-B: total20 saleable20 / SKU-C: total10 saleable7
INVENTORY_ROWS = [
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whA", "qty": 10, "inventory_type": "saleable"},
    {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whB", "qty": 5,  "inventory_type": "unsaleable"},
    {"country_code": "SA", "sku": "ZSA002", "warehouse_code": "whA", "qty": 20, "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 7,  "inventory_type": "saleable"},
    {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": 3,  "inventory_type": "unsaleable"},
]

# ASN 送仓未上架明细（供 WS-11 真算 pending）：
#   SKU-A: Scheduled 50 + Handover 30 + ERP 发货 20 = 100；GRN Completed 999 排除。
#   SKU-B: Receiving 10 = 10。
#   SKU-C: 只有 GRN Completed → 真算 0（出现在 staging → 写 0，非 NULL，证明非占位）。
STAGING_ROWS = [
    ("hipop_ksa", "noon_asn",    "ASN001", "SKU-A", 50,  "Scheduled"),
    ("hipop_ksa", "noon_asn",    "ASN002", "SKU-A", 30,  "Handover"),
    ("hipop_ksa", "erp_inbound", "PO-9001", "SKU-A", 20, "发货"),
    ("hipop_ksa", "noon_asn",    "ASN003", "SKU-A", 999, "GRN Completed"),
    ("hipop_ksa", "noon_asn",    "ASN010", "SKU-B", 10,  "Receiving"),
    ("hipop_uae", "noon_asn",    "ASN020", "SKU-C", 40,  "GRN Completed"),
]
EXPECTED_PENDING = {"SKU-A": 100, "SKU-B": 10, "SKU-C": 0}
EXPECTED_SALEABLE = {"SKU-A": 10, "SKU-B": 20, "SKU-C": 7}


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _reset_db():
    """干净起点：建表 + entity/sku 映射 + 预置 ERP-only wf1_stock 行（含 SKU-C，
    保证 pending 快照范围覆盖 SKU-A/B/C）+ 灌 ASN staging。noon_* 与 pending 留 NULL，
    由后续 live ingest + WS-11 真算填。"""
    import ingest_inbound_staging_v2 as inbound
    c = sqlite3.connect(_DB)
    try:
        for t in ("wf1_stock", "sales_entities", "wf2_sku"):
            c.executescript(f"DROP TABLE IF EXISTS {t};")
            c.executescript(_extract_create(t))
        c.executescript(f"DROP TABLE IF EXISTS {inbound.STAGING_TABLE};")
        inbound.ensure_staging_tables(c)
        c.executemany(
            "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
            "VALUES (?,?,?,?,?,?,1)",
            [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
             (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
        )
        # 消费端 read_sales_v2 先查 wf2_sku，且 noon ingest 靠 noon_sku 映射回 partner_sku。
        c.executemany(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku, "
            "sales_10d, sales_30d, sales_60d, sales_180d, latest_profit_rate) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(TENANT, "hipop_ksa", "SKU-A", "ZSA001", 5, 15, 30, 90, 0.2),
             (TENANT, "hipop_ksa", "SKU-B", "ZSA002", 3, 9, 18, 54, 0.2),
             (TENANT, "hipop_uae", "SKU-C", "ZAE001", 2, 6, 12, 36, 0.2)],
        )
        # 预置 ERP-only 行（pending 快照范围内）。noon_*/pending 全 NULL → 由生产端填。
        c.executemany(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock) VALUES (?,?,?,?,?,?,?)",
            [(TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264),
             (TENANT, "hipop_ksa", "SKU-B", 11, 22, 33, 66),
             (TENANT, "hipop_uae", "SKU-C", 1, 2, 3, 6)],
        )
        c.executemany(
            f"INSERT INTO {inbound.STAGING_TABLE} "
            "(tenant_id, entity_alias, source, asn_number, partner_sku, qty, status) "
            "VALUES (?,?,?,?,?,?,?)",
            [(TENANT, e, src, asn, sku, qty, st) for (e, src, asn, sku, qty, st) in STAGING_ROWS],
        )
        c.commit()
    finally:
        c.close()


def _run_full_pipeline():
    """走真实生产端：runner(noon_live_ingest) 落 noon_* → WS-11 真算 pending。
    返回 started_at（verifier 的本-run 切点）。merge（WS-12）stub 掉（不在本条范围）。"""
    from hipop.scripts import ingest_noon_stock_csv_v2 as pkg_noon
    from hipop.scripts import merge_stock_snapshot_v2 as _merge
    from hipop.runtime import workflow_runners
    import compute_pending_inbound_v2 as pend

    started_at = time.time() - 5  # runner 写 updated_at=now → 落在 [started_at, now]
    live_rows = lambda tenant_id: [dict(r) for r in INVENTORY_ROWS]
    runner = workflow_runners.get_runner("noon_live_ingest")
    _orig = _merge.run_v2
    _merge.run_v2 = lambda tenant_id, **kw: {"_stub": True}
    pkg_noon.set_live_row_producer(live_rows)
    try:
        out = runner("tid", TENANT, None, {}, {}, lambda: None, lambda p: None)
        assert "live" in out["summary"], f"runner 未走 live 源: {out}"
        pend.run_v2(TENANT)  # WS-11 真算 pending（非预填/硬编码）
    finally:
        _merge.run_v2 = _orig
        pkg_noon.set_live_row_producer(None)
    return started_at


def _dump():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


def _exec(sql, params=()):
    c = sqlite3.connect(_DB)
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def main():
    from hipop.runtime import verifiers

    # 接线缺失死法 + fail-then-pass 锚点：base commit 无 noon_live_ingest verifier。
    assert "noon_live_ingest" in verifiers._VERIFIERS, \
        "noon_live_ingest verifier 未注册（base commit 状态 / 接线缺失）"
    vfn = verifiers._VERIFIERS["noon_live_ingest"]

    # ── 1. happy path：完整链路落库后 verifier 全绿 ──────────────────
    _reset_db()
    started_at = _run_full_pipeline()
    dump = _dump()
    # 生产端真落库口径（钉死「死代码短路 / 占位假数据」）：
    for sku in ("SKU-A", "SKU-B", "SKU-C"):
        assert dump[sku]["noon_saleable_qty"] == EXPECTED_SALEABLE[sku], \
            f"{sku} live noon_saleable 落库错: {dump[sku]['noon_saleable_qty']}"
        assert dump[sku]["pending_inbound_qty"] == EXPECTED_PENDING[sku], \
            f"{sku} pending 真算错: {dump[sku]['pending_inbound_qty']}"
    # SKU-C pending=0 必须是真算的 0、非 NULL（占位假数据死法）
    assert dump["SKU-C"]["pending_inbound_qty"] is not None, "SKU-C pending 仍 NULL（没真算）"
    res = verifiers.run_verifier("noon_live_ingest", "tid", TENANT, started_at)
    assert res is not None, "run_verifier 返 None（verifier 没接进 _VERIFIERS / 入口缺失）"
    assert res["ok"] is True, f"happy path 应全绿: {res}"
    assert res["evidence"]["rows_this_run"] == 3, f"本 run noon 行数不对: {res['evidence']}"
    assert res["evidence"]["saleable_gt_total"] == 0 and res["evidence"]["stale_rows"] == 0 \
        and res["evidence"]["pending_null_known_sku"] == 0, f"happy 证据不干净: {res['evidence']}"
    print("✓ 完整链路落库后 verifier 全绿（saleable<=total / 新鲜 / 已知 SKU pending 非 NULL）")

    # ── 2. 接线证明：消费端 read_sales_v2 即时可售 == live noon_saleable + 真算 pending ──
    from hipop.workflows import wf_sales_cycle
    cc = sqlite3.connect(_DB)
    cc.row_factory = sqlite3.Row
    try:
        sale_a = wf_sales_cycle.read_sales_v2(TENANT, "hipop_ksa", "SKU-A", cc)
        sale_c = wf_sales_cycle.read_sales_v2(TENANT, "hipop_uae", "SKU-C", cc)
    finally:
        cc.close()
    # SKU-A: immediate = noon_saleable(10) + pending(100) = 110
    assert sale_a and sale_a["immediate"] == 110, \
        f"消费端即时可售没读到 live noon + pending: {sale_a and sale_a.get('immediate')}（接线缺失/死代码短路）"
    # SKU-C: immediate = noon_saleable(7) + pending(0) = 7（pending=0 真算，非 NULL 被当 0 掩盖）
    assert sale_c and sale_c["immediate"] == 7, \
        f"SKU-C 即时可售={sale_c and sale_c.get('immediate')} != 7"
    print("✓ 消费端 wf_sales_cycle 即时可售 == live noon_saleable + 非 NULL pending（数据链通到消费端）")

    # ── 3. 新鲜度阈值是参数（非写死/非 prompt）：把一行 updated_at 设成 10h 前，
    #        阈值 8h 判过旧、12h 判新鲜 —— 同一行随阈值翻转，证明 N 是真参数。 ──
    ten_h_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 10 * 3600))
    _exec("UPDATE wf1_stock SET updated_at=? WHERE tenant_id=? AND partner_sku='SKU-B'",
          (ten_h_ago, TENANT))
    strict = vfn(task_id="t", tenant_id=TENANT, started_at=started_at, max_age_hours=8)
    assert strict["ok"] is False and strict["evidence"]["stale_rows"] == 1, \
        f"阈值 8h 应把 10h 前的行判过旧: {strict['evidence']}"
    loose = vfn(task_id="t", tenant_id=TENANT, started_at=started_at, max_age_hours=12)
    assert loose["evidence"]["stale_rows"] == 0, \
        f"阈值 12h 不该把 10h 前的行判过旧: {loose['evidence']}"
    print("✓ 新鲜度阈值 N 是可覆写参数（max_age_hours），同一行随阈值翻转 — 非写死 / 非 prompt 规则")

    # ── 4. fail-then-pass · 坏数据 1：noon_saleable_qty > noon_total_qty 被拦 ──
    _reset_db(); sa = _run_full_pipeline()
    _exec("UPDATE wf1_stock SET noon_saleable_qty = noon_total_qty + 5 "
          "WHERE tenant_id=? AND partner_sku='SKU-A'", (TENANT,))
    r1 = verifiers.run_verifier("noon_live_ingest", "tid", TENANT, sa)
    assert r1["ok"] is False and r1["evidence"]["saleable_gt_total"] >= 1, \
        f"saleable>total 没被拦: {r1}"
    assert r1["evidence"]["stale_rows"] == 0 and r1["evidence"]["pending_null_known_sku"] == 0, \
        f"应只命中 saleable>total 一项: {r1['evidence']}"
    print("✓ 坏数据①: noon_saleable_qty > noon_total_qty 被断言 1 拦下")

    # ── 5. 坏数据 2：过旧 updated_at（超新鲜度阈值）被拦 ──
    _reset_db(); sa = _run_full_pipeline()
    _exec("UPDATE wf1_stock SET updated_at='2020-01-01 00:00:00' "
          "WHERE tenant_id=? AND partner_sku='SKU-B'", (TENANT,))
    r2 = verifiers.run_verifier("noon_live_ingest", "tid", TENANT, sa)
    assert r2["ok"] is False and r2["evidence"]["stale_rows"] >= 1, f"过旧 updated_at 没被拦: {r2}"
    assert r2["evidence"]["saleable_gt_total"] == 0, f"不该误报 saleable: {r2['evidence']}"
    print("✓ 坏数据②: 过旧 updated_at（超 N 小时新鲜度）被断言 2 拦下")

    # ── 6. 坏数据 3：已知 SKU pending_inbound_qty IS NULL 被拦 ──
    _reset_db(); sa = _run_full_pipeline()
    _exec("UPDATE wf1_stock SET pending_inbound_qty=NULL "
          "WHERE tenant_id=? AND partner_sku='SKU-A'", (TENANT,))
    r3 = verifiers.run_verifier("noon_live_ingest", "tid", TENANT, sa)
    assert r3["ok"] is False and r3["evidence"]["pending_null_known_sku"] >= 1, \
        f"pending NULL 没被拦: {r3}"
    assert r3["evidence"]["saleable_gt_total"] == 0 and r3["evidence"]["stale_rows"] == 0, \
        f"应只命中 pending NULL 一项: {r3['evidence']}"
    print("✓ 坏数据③: 已知 SKU pending_inbound_qty IS NULL 被断言 3 拦下")

    # ── 7. 空过保护：本 run 没写任何 noon 行 → 不许冒充成功 ──
    _reset_db()  # 不跑 pipeline → 无 noon 数据
    r4 = verifiers.run_verifier("noon_live_ingest", "tid", TENANT, time.time())
    assert r4["ok"] is False and r4["evidence"]["rows_this_run"] == 0, \
        f"0 noon 行本 run 应判失败（空过冒充成功）: {r4}"
    print("✓ 空过保护: 本 run 无 noon 行写入 → verifier 判失败，不冒充成功")

    print("\n7/7 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass

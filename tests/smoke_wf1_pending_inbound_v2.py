"""Smoke: WS-11 — ASN 送仓未上架 → wf1_stock.pending_inbound_qty。

fail-then-pass 承重墙（钉死三种死法）：
  · 占位假数据：pending_inbound_qty 必须由 staging 真算出来 —— 全状态矩阵下只
    纳入 Scheduled/Handover/Receiving/Put Away In Progress（+ ERP 已发货），排除
    GRN Completed / Created / Pending / Cancelled / Expired / 拣货中；且预置的陈旧
    非零值会被刷回 0（不是写死、不是留 NULL）。
  · 接线缺失：消费端 wf_sales_cycle.read_sales_v2 读到的 immediate 必须体现
    pending（immediate == noon_saleable + pending_inbound）；runner 进
    WORKFLOW_REGISTRY + 可解析（/run-workflow 能触发）。
  · 死代码短路：只写 v2 wf1_stock，绝不创建/写 v1 wf1_<alias>_stock；部分 upsert
    不覆盖 ERP/Noon 列。

改动前（base commit）：scripts/compute_pending_inbound_v2 不存在 → import 失败 →
smoke fail。改动后 → pass。

跑法：python3 tests/smoke_wf1_pending_inbound_v2.py
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import re
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


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


# 全状态矩阵 staging 行：(entity, source, asn, sku, qty, status)
# 计入：Scheduled/Handover/Receiving/Put Away In Progress + ERP 发货
# 不计入：GRN Completed/Created/Pending/Cancelled/Expired/拣货中
STAGING_ROWS = [
    # SKU-A：两个 Noon ASN（Scheduled 50 + Handover 30）+ ERP 已发货 20 = 100；
    #        外加一个 GRN Completed 999 必须被排除。
    ("hipop_ksa", "noon_asn",    "ASN001", "SKU-A", 50,  "Scheduled"),
    ("hipop_ksa", "noon_asn",    "ASN002", "SKU-A", 30,  "Handover"),
    ("hipop_ksa", "erp_inbound", "PO-9001", "SKU-A", 20, "发货"),
    ("hipop_ksa", "noon_asn",    "ASN003", "SKU-A", 999, "GRN Completed"),
    # SKU-B：Receiving 10 + Put Away In Progress 5 = 15；其余四种作废/未离仓状态全排除。
    ("hipop_ksa", "noon_asn",    "ASN010", "SKU-B", 10,  "Receiving"),
    ("hipop_ksa", "noon_asn",    "ASN011", "SKU-B", 5,   "Put Away In Progress"),
    ("hipop_ksa", "noon_asn",    "ASN012", "SKU-B", 7,   "Created"),
    ("hipop_ksa", "noon_asn",    "ASN013", "SKU-B", 3,   "Pending"),
    ("hipop_ksa", "noon_asn",    "ASN014", "SKU-B", 100, "Cancelled"),
    ("hipop_ksa", "noon_asn",    "ASN015", "SKU-B", 100, "Expired"),
    # SKU-C：只有 GRN Completed + Cancelled → 出现在 staging 但计入和为 0（刷回 0，不留旧值）。
    ("hipop_ksa", "noon_asn",    "ASN020", "SKU-C", 40,  "GRN Completed"),
    ("hipop_ksa", "noon_asn",    "ASN021", "SKU-C", 60,  "Cancelled"),
    # SKU-D：ERP 拣货中（还没出库）→ 不计入 → 0。
    ("hipop_ksa", "erp_inbound", "PO-9002", "SKU-D", 40, "拣货中"),
    # 第二个 entity：SKU-E Scheduled 12 → 12（验聚合按 entity 隔离）。
    ("hipop_uae", "noon_asn",    "ASN100", "SKU-E", 12,  "scheduled"),  # 大小写不敏感
]

EXPECTED = {
    ("hipop_ksa", "SKU-A"): 100,
    ("hipop_ksa", "SKU-B"): 15,
    ("hipop_ksa", "SKU-C"): 0,
    ("hipop_ksa", "SKU-D"): 0,
    ("hipop_uae", "SKU-E"): 12,
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
    # 预置：SKU-A 已有 ERP/Noon 列 + 一个陈旧的 pending=999（验部分 upsert + 刷新）。
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "noon_saleable_qty, yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (TENANT, "hipop_ksa", "SKU-A", 10, 99, 88, 77, 264, 999),
    )
    # 预置：SKU-C 旧 pending=50（ASN 当时还是 Scheduled），现在全 GRN/Cancelled → 必须刷回 0。
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, pending_inbound_qty) "
        "VALUES (?,?,?,?)", (TENANT, "hipop_ksa", "SKU-C", 50),
    )
    # 消费端要 wf2_sku 行才会返回（read_sales_v2 先查 wf2_sku）。
    c.execute(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, "
        "sales_10d, sales_30d, sales_60d, sales_180d, latest_profit_rate) "
        "VALUES (?,?,?,?,?,?,?,?)", (TENANT, "hipop_ksa", "SKU-A", 5, 15, 30, 90, 0.2),
    )
    c.commit()
    c.close()


def _q(sql, params=()):
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def main():
    _setup_db()
    import compute_pending_inbound_v2 as pend

    # ── 0. 规则是参数化的（不写死）：覆写计入状态集合应改变聚合结果 ─────
    only_created = pend.aggregate_pending(
        [{"entity_alias": "x", "partner_sku": "S", "qty": 9, "status": "Created"}],
        counted={"created"},
    )
    assert only_created[("x", "S")] == 9, "counted_statuses 覆写无效 → 规则被写死了"
    default_excl = pend.aggregate_pending(
        [{"entity_alias": "x", "partner_sku": "S", "qty": 9, "status": "Created"}])
    assert default_excl[("x", "S")] == 0, "默认规则不该把 Created 计入"

    # ── 1. 跑确定性规则 → 写回 wf1_stock.pending_inbound_qty ─────────
    res = pend.run_v2(TENANT)
    assert res["skus"] == len(EXPECTED), f"写入 SKU 数 {res['skus']} != {len(EXPECTED)}"
    assert res["pending_total"] == 100 + 15 + 12, f"pending 总量 {res['pending_total']} 不对"

    rows = {(r["entity_alias"], r["partner_sku"]): r
            for r in _q("SELECT * FROM wf1_stock ORDER BY entity_alias, partner_sku")}
    for key, want in EXPECTED.items():
        got = rows[key]["pending_inbound_qty"]
        assert got == want, f"{key} pending_inbound_qty={got} != {want}（状态规则/聚合错）"

    # 占位假数据死法：SKU-C/SKU-D 必须是真算出的 0，不是 NULL；陈旧 50 已被刷回 0。
    assert rows[("hipop_ksa", "SKU-C")]["pending_inbound_qty"] == 0, "陈旧 pending 没被刷回 0"
    assert rows[("hipop_ksa", "SKU-D")]["pending_inbound_qty"] is not None, "拣货中 SKU 仍留 NULL"

    # 部分 upsert：SKU-A 的 ERP/Noon 列原样保留，只动 pending。
    a = rows[("hipop_ksa", "SKU-A")]
    assert (a["noon_saleable_qty"], a["yiwu_qty"], a["dongguan_qty"],
            a["overseas_total_qty"], a["total_stock"]) == (10, 99, 88, 77, 264), \
        f"pending producer 覆盖了 ERP/Noon 列: {a}"

    # ── 2. 接线缺失死法：消费端 read_sales_v2 真读到 pending（计入 immediate）──
    from hipop.workflows import wf_sales_cycle
    cc = sqlite3.connect(_TMP_DB)
    cc.row_factory = sqlite3.Row
    try:
        sale = wf_sales_cycle.read_sales_v2(TENANT, "hipop_ksa", "SKU-A", cc)
    finally:
        cc.close()
    # immediate = noon_saleable(10) + pending_inbound(100) = 110
    assert sale and sale["immediate"] == 110, \
        f"消费端 immediate={sale and sale.get('immediate')} 没体现 pending（接线缺失）"

    # ── 3. 真实入口接线：WORKFLOW_REGISTRY + runner 可解析 ───────────
    from hipop.server import api
    assert "wf1_pending_inbound_v2" in api.WORKFLOW_REGISTRY, \
        "wf1_pending_inbound_v2 不在 WORKFLOW_REGISTRY → /run-workflow 会 400"
    _, steps, _ = api.WORKFLOW_REGISTRY["wf1_pending_inbound_v2"]
    fn = api._resolve_callable(steps[0][2])
    assert callable(fn) and fn.__name__ == "run_v2", f"callable 解析失败: {steps}"
    from hipop.runtime import workflow_runners as wr
    assert "wf1_pending_inbound_v2" in set(wr.list_runners()), "后台 worker runner 没注册"

    # ── 4. 死代码短路死法：绝不创建/写 v1 per-alias 表 ──────────────
    tables = {r["name"] for r in _q("SELECT name FROM sqlite_master WHERE type='table'")}
    v1 = {t for t in tables if re.fullmatch(r"wf1_hipop_(ksa|uae)_stock", t)}
    assert not v1, f"写到了 v1 per-alias 表（死法#2）: {v1}"

    print("✓ 状态规则正确：只计入 Scheduled/Handover/Receiving/Put Away In Progress + ERP 发货")
    print("✓ 排除 GRN Completed/Created/Pending/Cancelled/Expired/拣货中；多 ASN×多 source 聚合求和")
    print("✓ 陈旧非零 pending 刷回 0、拣货中写真 0（非 NULL/非写死）；部分 upsert 不覆盖 ERP/Noon 列")
    print("✓ 消费端 read_sales_v2.immediate == noon_saleable + pending_inbound（接线通）")
    print("✓ wf1_pending_inbound_v2 进 WORKFLOW_REGISTRY + runner（真实入口可触发）")
    print("✓ 规则参数化可覆写、未创建/写入 v1 wf1_<alias>_stock 路径")
    print("\n6/6 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

"""Smoke: WS-11 — pending_inbound 只算「本次输入窗口」，历史残留 ASN 不污染。

验门人二次打回的真洞（机械门虽绿）：
  · `ingest_inbound_staging_v2` 只做 INSERT ... ON CONFLICT，从不清掉本轮文件里
    已消失的旧 ASN；`compute_pending_inbound_v2` 又对 tenant 下 staging 全历史求和。
    → 昨天 Scheduled 的旧单今天即使已 GRN Completed / 已从文件消失，仍被算进
    `pending_inbound_qty`，下游 `wf_sales_cycle.run_v2` 把已完成的在途当可售补充。

本 smoke 焊住的承重墙（走真 ingest → compute 全链路，不直插 staging 旁路）：
  1. 本次输入窗口：旧残留 `OLD-ASN / Scheduled / 50` + 本轮文件只含
     `NEW-ASN / GRN Completed / 5` → 本轮快照替换后 staging 里旧单被清掉，
     最终 `wf1_stock.pending_inbound_qty` 必须 = 0（不是 50）。
  2. 快照替换精确收敛：本轮没碰到的 (source, entity) —— hipop_uae 的 noon_asn
     残留 `Scheduled 12` —— 必须原样保留并照常计入（证明不是无脑全删）。

fail-then-pass：
  · 改动前（ingest 不做本轮替换，旧 ASN 残留）→ 断言 1 红（pending=50≠0）。
  · 改动后（_process_file 按 (source,alias) 本轮首次清一次）→ 全绿。

跑法：python3 tests/smoke_wf1_pending_inbound_scope_v2.py
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import re
import csv
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
_TMPFILES = [_TMP_DB]


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _write_csv(rows: list[dict]) -> str:
    """写一份本轮 Noon ASN 输入文件，返回路径。"""
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="", encoding="utf-8")
    _TMPFILES.append(f.name)
    cols = ["asn_number", "entity_alias", "partner_sku", "qty", "status"]
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    f.close()
    return f.name


def _setup_db(inbound):
    c = sqlite3.connect(_TMP_DB)
    for t in ("sales_entities", "wf2_sku", "wf1_stock"):
        c.executescript(_extract_create(t))
    inbound.ensure_staging_tables(c)
    # 两个销售主体
    c.executemany(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
        "VALUES (?,?,?,?,?,?,1)",
        [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
         (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
    )
    c.executemany(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
        [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
         (TENANT, "hipop_uae", "SKU-E", "ZAE005")],
    )
    # 本次库存快照：SKU-A / SKU-E 都已在 wf1_stock（在快照范围内，应被 UPDATE）。
    # 预置陈旧 pending 值，验最终被刷新成本轮真值。
    c.executemany(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, pending_inbound_qty) "
        "VALUES (?,?,?,?)",
        [(TENANT, "hipop_ksa", "SKU-A", 50),
         (TENANT, "hipop_uae", "SKU-E", 0)],
    )
    # ── 历史残留 ASN（昨天 run 落下的，今天文件里已消失）──────────────
    # hipop_ksa：旧 OLD-ASN/Scheduled/50（本轮 hipop_ksa noon_asn 会被快照替换清掉）。
    # hipop_uae：旧 UAE-OLD/Scheduled/12（本轮没碰 hipop_uae → 必须原样保留并计入）。
    c.executemany(
        f"INSERT INTO {inbound.STAGING_TABLE} "
        "(tenant_id, entity_alias, source, asn_number, partner_sku, qty, status) "
        "VALUES (?,?,?,?,?,?,?)",
        [(TENANT, "hipop_ksa", "noon_asn", "OLD-ASN", "SKU-A", 50, "Scheduled"),
         (TENANT, "hipop_uae", "noon_asn", "UAE-OLD", "SKU-E", 12, "Scheduled")],
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
    import ingest_inbound_staging_v2 as inbound
    import compute_pending_inbound_v2 as pend

    _setup_db(inbound)

    # 本轮 Noon ASN 输入：hipop_ksa 只有 NEW-ASN，且已 GRN Completed（不计入）。
    # 旧 OLD-ASN 没出现在本轮文件 → 本轮快照替换应把它从 staging 清掉。
    this_run = _write_csv([
        {"asn_number": "NEW-ASN", "entity_alias": "hipop_ksa",
         "partner_sku": "SKU-A", "qty": 5, "status": "GRN Completed"},
    ])
    inbound.run_v2(TENANT, noon_asn_file=this_run)

    # ── 1. 本轮快照替换：hipop_ksa 旧 OLD-ASN 被清掉，只剩本轮 NEW-ASN ──────
    ksa_stg = _q(
        f"SELECT asn_number, status, qty FROM {inbound.STAGING_TABLE} "
        "WHERE tenant_id=? AND entity_alias=? AND source='noon_asn' ORDER BY asn_number",
        (TENANT, "hipop_ksa"),
    )
    asns = {r["asn_number"] for r in ksa_stg}
    assert "OLD-ASN" not in asns, \
        f"历史残留 OLD-ASN 没被本轮快照替换清掉，staging 仍有: {ksa_stg}"
    assert asns == {"NEW-ASN"}, f"hipop_ksa staging 应只剩本轮 NEW-ASN, 实际 {asns}"

    # ── 2. 快照替换精确收敛：本轮没碰的 hipop_uae 残留必须保留 ────────────
    uae_stg = _q(
        f"SELECT asn_number FROM {inbound.STAGING_TABLE} "
        "WHERE tenant_id=? AND entity_alias=? AND source='noon_asn'",
        (TENANT, "hipop_uae"),
    )
    assert {r["asn_number"] for r in uae_stg} == {"UAE-OLD"}, \
        f"本轮没碰的 hipop_uae 残留被误删了（快照替换过度收敛）: {uae_stg}"

    # ── 3. compute：本次输入口径 → hipop_ksa pending=0（不是残留的 50）─────
    pend.run_v2(TENANT)
    rows = {(r["entity_alias"], r["partner_sku"]): r
            for r in _q("SELECT * FROM wf1_stock")}
    ksa = rows[("hipop_ksa", "SKU-A")]["pending_inbound_qty"]
    assert ksa == 0, \
        f"hipop_ksa/SKU-A pending_inbound_qty={ksa} != 0（历史残留 ASN 污染了本轮结果）"

    # hipop_uae 残留 Scheduled 12 在本轮窗口内 → 正常计入。
    uae = rows[("hipop_uae", "SKU-E")]["pending_inbound_qty"]
    assert uae == 12, \
        f"hipop_uae/SKU-E pending={uae} != 12（本轮未刷新的 entity 应保留并计入残留快照）"

    print("✓ 本轮快照替换：hipop_ksa 历史残留 OLD-ASN 被清，staging 只剩本轮 NEW-ASN")
    print("✓ 精确收敛：本轮未触及的 hipop_uae (source,entity) 残留原样保留，未被误删")
    print("✓ 本次输入口径：旧 Scheduled 50 + 本轮 GRN Completed 5 → pending=0（不是 50）")
    print("✓ 历史残留不污染本轮 pending_inbound_qty；下游销售周期不会把已完成在途当可售")
    print("\n4/4 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in _TMPFILES:
            try:
                os.unlink(p)
            except OSError:
                pass

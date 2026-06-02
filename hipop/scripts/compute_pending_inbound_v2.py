"""compute_pending_inbound v2 — ASN 送仓未上架 → wf1_stock.pending_inbound_qty（WS-11）

WS-10 已把两路在途/送仓原始数据落进 staging 表 `wf1_asn_lines_staging`
（source = 'noon_asn' | 'erp_inbound'）。本任务是 **确定性规则 / verifier**：
按 ASN 状态判定哪些 ASN 数量算作"即将可售但尚未上架"的库存，聚合后写回
v2 `wf1_stock.pending_inbound_qty`。消费端 `wf_sales_cycle.read_sales_v2`
已把该列计入 `immediate`（noon_saleable + pending_inbound）。

确定性规则（参数化、可测，**不写死在 prompt**，见 COUNTED_STATUSES）：
- 计入（货已离仓/在途、即将可售但还没上架）：
    Noon ASN: Scheduled / Handover / Receiving / Put Away In Progress
    ERP 送仓:  发货 / 已发货 / 已出库 / shipped（ERP 已确认出库）
- 不计入：
    GRN Completed（已收货上架 → 已计入 noon_saleable，不再算 pending）
    Created / Pending（还没真正离仓）
    Cancelled / Expired（作废）
    ERP 拣货中（还在拣货，没出库）

聚合主键 = v2 wf1_stock 主键 `(tenant_id, entity_alias, partner_sku)`；
同一 SKU 出现在多个 ASN（含多 source）时数量求和。

刷新语义（避免占位假数据 / 陈旧值）：
- 对**所有出现在 staging 里的** (alias, partner_sku) 都写一个算出来的值；
  没有任何计入状态的 SKU 写 0（而不是留 NULL / 沿用上次的旧值）。
  这样 ASN 从 Scheduled 流转到 GRN Completed 后，pending 会被刷回 0。
- staging 里完全没有的 SKU 不动（没有在途信息，保持原样）。
- 海外仓直送：ERP 库存清单已因"已发货"扣减海外仓时，pending 只作为即将
  可售补充（消费端 immediate 与 transfer/overseas 是分桶相加，不二次扣海外仓）。

写回用部分 upsert（只动 pending_inbound_qty + updated_at），不覆盖 ERP / Noon 列，
与 ingest_noon_stock_csv_v2._upsert 同范式。**不碰** v1 wf1_<alias>_stock / wf_stock_static。

CLI:
  python3 compute_pending_inbound_v2.py --tenant 1
"""
from __future__ import annotations

import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from server import data as _data

STAGING_TABLE = "wf1_asn_lines_staging"
STOCK_TABLE = "wf1_stock"

# 确定性规则的参数：算作 pending_inbound 的 ASN 状态（normalize 后比对）。
# 这是规则本体，写在受版本管理的代码里、可被测试覆盖与覆写，不进 SYSTEM_PROMPT。
COUNTED_STATUSES = frozenset({
    # Noon ASN 生命周期：已排程 → 交接 → 收货中 → 上架处理中（货在途/到仓未上架）
    "scheduled", "handover", "receiving", "put away in progress",
    # ERP 送仓：已确认出库/已发货（货已离国内仓，在途到海外仓）
    "发货", "已发货", "已出库", "shipped",
})


def _normalize_status(s) -> str:
    """统一状态：去空白、转小写、把内部连续空白压成单个空格。

    让 'Put Away In Progress' / 'put away in progress' / 'Put  Away In Progress'
    都能匹配同一个规则键。
    """
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())


def aggregate_pending(rows, counted=COUNTED_STATUSES) -> dict:
    """把 staging 行按 (entity_alias, partner_sku) 聚合成 pending_inbound 数量。

    rows: 可迭代 dict，含 entity_alias / partner_sku / qty / status。
    返回 {(alias, partner_sku): qty}，**包含** 全为非计入状态的 SKU（值 0），
    以便消费端把陈旧的非零值刷回 0。
    """
    counted_norm = {_normalize_status(c) for c in counted}
    agg: dict[tuple, int] = {}
    for r in rows:
        alias = (r.get("entity_alias") or "").strip()
        sku = (r.get("partner_sku") or "").strip()
        if not (alias and sku):
            continue
        key = (alias, sku)
        agg.setdefault(key, 0)  # 出现过就要写值（哪怕最终 0）
        if _normalize_status(r.get("status")) in counted_norm:
            try:
                agg[key] += int(r.get("qty") or 0)
            except (TypeError, ValueError):
                pass
    return agg


def _upsert_pending(conn, tenant_id, agg) -> int:
    """部分 upsert：只写 pending_inbound_qty（+ updated_at），不碰 ERP / Noon 列。"""
    ts = "datetime('now','localtime')"
    sql = (
        f"INSERT INTO {STOCK_TABLE} "
        f"(tenant_id, entity_alias, partner_sku, pending_inbound_qty, imported_at, updated_at) "
        f"VALUES (?,?,?,?, {ts}, {ts}) "
        f"ON CONFLICT (tenant_id, entity_alias, partner_sku) "
        f"DO UPDATE SET pending_inbound_qty=excluded.pending_inbound_qty, updated_at={ts}"
    )
    n = 0
    for (alias, sku), qty in agg.items():
        conn.execute(sql, (tenant_id, alias, sku, int(qty)))
        n += 1
    conn.commit()
    return n


def run_v2(tenant_id: int, counted_statuses=COUNTED_STATUSES,
           conn=None, dry_run: bool = False) -> dict:
    """读 wf1_asn_lines_staging → 按状态规则算 pending_inbound → 写回 wf1_stock。

    counted_statuses: 计入状态集合（运行参数，可测、可覆写，默认 COUNTED_STATUSES）。
    conn: 注入连接（测试用）；不给则按 tenant 取 _data.conn()。
    """
    print(f"\n=== compute_pending_inbound v2 tenant={tenant_id} ===", file=sys.stderr)
    _data.set_current_tenant(tenant_id)
    own_conn = conn is None
    if own_conn:
        conn = _data.conn()
    try:
        rows = conn.execute(
            f"SELECT entity_alias, partner_sku, qty, status "
            f"FROM {STAGING_TABLE} WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        rows = [dict(r) for r in rows]
        agg = aggregate_pending(rows, counted_statuses)
        counted_qty = sum(v for v in agg.values() if v)
        if dry_run:
            result = {"staging_rows": len(rows), "skus": len(agg),
                      "pending_total": counted_qty, "written": 0}
            print(f"[dry-run] {result}", file=sys.stderr)
            return result
        written = _upsert_pending(conn, tenant_id, agg)
    finally:
        if own_conn:
            conn.close()
    result = {"staging_rows": len(rows), "skus": len(agg),
              "pending_total": counted_qty, "written": written}
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_v2(args.tenant, dry_run=args.dry_run)

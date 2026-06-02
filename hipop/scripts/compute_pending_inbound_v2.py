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

快照范围（避免越界写 / ghost stock row —— 红队打回的洞）：
- **只更新本次 v2 库存快照范围内的 SKU**，即已经存在于 `wf1_stock`
  `(tenant_id, entity_alias, partner_sku)` 的行。生产端用 **UPDATE**（不是
  INSERT/upsert），所以结构上不可能给只在 staging 出现、却不在快照里的 SKU
  新建 `wf1_stock` 行。范围外的 staging SKU 被显式跳过并计数（`skipped_out_of_snapshot`、
  stderr 打印），不静默 INSERT。
  理由：staging 一旦混入错 SKU 或过期映射，不能让它凭空进正式库存表，
  否则下游 `wf_sales_cycle.run_v2` 会把不属于本次快照的 pending 算进销售周期。

刷新语义（避免占位假数据 / 陈旧值）：
- 对**快照内、且出现在 staging 里的** (alias, partner_sku) 都写一个算出来的值；
  没有任何计入状态的 SKU 写 0（而不是留 NULL / 沿用上次的旧值）。
  这样 ASN 从 Scheduled 流转到 GRN Completed 后，pending 会被刷回 0。
- staging 里完全没有的快照 SKU 不动（没有在途信息，保持原样）。
- 海外仓直送：ERP 库存清单已因"已发货"扣减海外仓时，pending 只作为即将
  可售补充（消费端 immediate 与 transfer/overseas 是分桶相加，不二次扣海外仓）。

写回用部分 UPDATE（只动 pending_inbound_qty + updated_at），不覆盖 ERP / Noon 列，
不新建行。**不碰** v1 wf1_<alias>_stock / wf_stock_static。

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


def _load_snapshot_keys(conn, tenant_id) -> set:
    """本次 v2 库存快照范围 = 当前 wf1_stock 里该 tenant 已有的 (alias, partner_sku)。"""
    rows = conn.execute(
        f"SELECT entity_alias, partner_sku FROM {STOCK_TABLE} WHERE tenant_id=?",
        (tenant_id,),
    ).fetchall()
    keys = set()
    for r in rows:
        d = dict(r)
        keys.add(((d.get("entity_alias") or "").strip(),
                  (d.get("partner_sku") or "").strip()))
    return keys


def _update_pending(conn, tenant_id, agg, snapshot_keys):
    """部分 UPDATE：只写**快照范围内**的 SKU 的 pending_inbound_qty（+ updated_at），
    不碰 ERP / Noon 列，也绝不为快照外的 staging SKU 新建行。

    返回 (written, skipped)，skipped = 不在快照范围、被显式跳过的 (alias, sku) 列表。
    """
    ts = "datetime('now','localtime')"
    sql = (
        f"UPDATE {STOCK_TABLE} "
        f"SET pending_inbound_qty=?, updated_at={ts} "
        f"WHERE tenant_id=? AND entity_alias=? AND partner_sku=?"
    )
    written = 0
    skipped = []
    for (alias, sku), qty in agg.items():
        if (alias, sku) not in snapshot_keys:
            skipped.append((alias, sku))  # 范围外：不新建行、显式归类
            continue
        conn.execute(sql, (int(qty), tenant_id, alias, sku))
        written += 1
    conn.commit()
    return written, skipped


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
        snapshot_keys = _load_snapshot_keys(conn, tenant_id)
        # 只统计快照范围内的量（这才是真正写进库存表、被下游消费的口径）。
        in_range = {k: v for k, v in agg.items() if k in snapshot_keys}
        out_of_snapshot = sorted(k for k in agg if k not in snapshot_keys)
        counted_qty = sum(v for v in in_range.values() if v)
        if out_of_snapshot:
            print(f"[skip] {len(out_of_snapshot)} 个 staging SKU 不在本次 v2 库存快照范围, "
                  f"不新建 wf1_stock 行: {out_of_snapshot[:20]}", file=sys.stderr)
        if dry_run:
            result = {"staging_rows": len(rows), "skus": len(in_range),
                      "skipped_out_of_snapshot": len(out_of_snapshot),
                      "pending_total": counted_qty, "written": 0}
            print(f"[dry-run] {result}", file=sys.stderr)
            return result
        written, skipped = _update_pending(conn, tenant_id, agg, snapshot_keys)
    finally:
        if own_conn:
            conn.close()
    result = {"staging_rows": len(rows), "skus": written,
              "skipped_out_of_snapshot": len(skipped),
              "pending_total": counted_qty, "written": written}
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_v2(args.tenant, dry_run=args.dry_run)

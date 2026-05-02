"""
飞书同步：hipop.db → 飞书 Bitable
- 主表「ERP-SKU」：写入"发货在途"等聚合字段
- 子表 1 物流告警：wf6_logistics_alerts
- 子表 2 在途批次明细：wf3_logistics_hub.groups_json 展开
- 子表 3 经营决策：wf6_replenishment_queue（合并 wf5 待重构后再加）
- 子表 4 约仓动作：wf6 蓝色告警

CLI:
  python3 -m scripts.feishu_sync                 # 全量同步
  python3 -m scripts.feishu_sync --sku TBJ0057A  # 指定 SKU
  python3 -m scripts.feishu_sync --tables hub alerts  # 只同步部分表
"""
import os
import sys
import json
import sqlite3
import argparse
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.feishu_bridge import bridge

DB = "/Users/luke/Downloads/点购工作流/hipop.db"


# ── 单 SKU 主表更新 ────────────────────────────────────
def sync_main_for_sku(sku: str, hub_record: dict) -> bool:
    """把 hub 的聚合字段写到主表 ERP-SKU=sku 那一行"""
    b = bridge()
    rec = b.find_record("main", "ERP-SKU", sku, field_names=["ERP-SKU", "发货在途"])
    if not rec:
        print(f"  ⚠️ 主表无 {sku}，跳过")
        return False
    in_transit = hub_record.get("in_transit_total_qty", 0)
    b.update_record("main", rec["record_id"], {"发货在途": str(in_transit)})
    return True


# ── 在途批次明细 ────────────────────────────────────────
def sync_in_transit(skus: Optional[List[str]] = None) -> int:
    b = bridge()
    conn = sqlite3.connect(DB)
    where = ""
    args = ()
    if skus:
        ph = ",".join("?" * len(skus))
        where = f"WHERE sku IN ({ph})"
        args = tuple(skus)
    rows = conn.execute(f"SELECT sku, groups_json FROM wf3_logistics_hub {where}", args).fetchall()
    conn.close()

    n = 0
    for sku, gj in rows:
        groups = json.loads(gj)
        main_rec = b.find_record("main", "ERP-SKU", sku, field_names=["ERP-SKU"])
        main_record_id = main_rec["record_id"] if main_rec else None
        # 主表"发货在途"同步
        if main_record_id:
            total = sum(g.get("in_transit_qty", 0) for g in groups)
            b.update_record("main", main_record_id, {"发货在途": str(total)})

        for g in groups:
            for batch in g.get("in_transit_batches", []):
                if batch.get("note") == "needs_ops_input":
                    continue
                key = f"{sku} · {batch['order_no']}"
                fields = {
                    "SKU": sku,
                    "货单号": batch["order_no"],
                    "件数": int(batch.get("qty") or 0),
                    "国家": g.get("country") or "",
                    "平台": g.get("platform") or "",
                    "物流公司": g.get("forwarder") or "",
                    "当前阶段": batch.get("current_stage") or "",
                    "当前状态原文": batch.get("current_status_text") or "",
                    "阶段停留天数": int(batch.get("stage_stay_days") or 0),
                    "历史阶段耗时": float(batch.get("history_stage_days") or 0),
                    "是否卡单": bool(batch.get("is_stuck")),
                }
                if main_record_id:
                    fields["关联主表-SKU"] = [main_record_id]
                b.upsert_by_field("in_transit", "SKU+货单", key, fields)
                n += 1
    return n


# ── 物流告警 ───────────────────────────────────────────
def sync_alerts(skus: Optional[List[str]] = None) -> int:
    b = bridge()
    conn = sqlite3.connect(DB)
    sql = """SELECT alert_id, order_no, forwarder, alert_reason, alert_level, stage,
                    threshold_days, actual_stay_days, history_stage_days, excess_over_threshold,
                    sku_list_json, action_owner, supervisor, required_action, feedback_fields,
                    ops_status, ops_contact_log
             FROM wf6_logistics_alerts"""
    args = ()
    if skus:
        # 过滤 sku_list_json 含任一 SKU
        like_clauses = " OR ".join(["sku_list_json LIKE ?"] * len(skus))
        sql += f" WHERE {like_clauses}"
        args = tuple(f'%"sku": "{s}"%' for s in skus)
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    n = 0
    for r in rows:
        sku_list = json.loads(r[10] or "[]")
        sku_str = ", ".join(f"{s['sku']}×{s['qty']}" for s in sku_list)
        log = json.loads(r[16] or "[]")
        latest_reply = log[-1]["content"] if log else ""
        is_lost = "未知"
        if r[15] == "已确认丢货": is_lost = "是"
        elif r[15] == "已确认推进": is_lost = "否"
        fields = {
            "告警ID":   str(r[0]),
            "货单号":   r[1] or "",
            "物流公司": r[2] or "",
            "告警原因": r[3] or "",
            "告警级别": r[4] or "",
            "阶段":     r[5] or "",
            "涉及SKU":  sku_str,
            "停留天数": int(r[7]) if r[7] is not None else 0,
            "历史均值": float(r[8]) if r[8] is not None else 0.0,
            "阈值":     int(r[6]) if r[6] is not None else 0,
            "超出":     float(r[9]) if r[9] is not None else 0.0,
            "主责":     r[11] or "",
            "协同":     r[12] or "",
            "需要的动作": r[13] or "",
            "需回填":   r[14] or "",
            "操作状态": r[15] or "待处理",
            "物流回复": latest_reply,
            "是否丢货": is_lost,
        }
        b.upsert_by_field("alerts", "告警ID", str(r[0]), fields)
        n += 1
    return n


# ── 经营决策（每个 sales_entity 一张飞书数据表，跟数据库 wf5/wf6 per-entity 对齐）────
PRESERVE_FIELDS = {"操作状态", "实际下单数"}  # update 时不覆盖运营填字段


def _sync_decisions_for_entity(b, ent, skus: Optional[List[str]] = None) -> int:
    """同步单个 sales_entity 的 wf5/wf6 数据到对应飞书数据表。"""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sales_entity import sales_cycle_table, replenish_queue_table

    alias = ent["alias"]
    table_id = ent.get("feishu_decisions_table_id")
    if not table_id:
        print(f"  [{alias}] 无 feishu_decisions_table_id，跳过（先跑 wf5_feishu_setup）")
        return 0

    conn = sqlite3.connect(DB)
    sc_tbl = sales_cycle_table(alias)
    rq_tbl = replenish_queue_table(alias)

    # 1) wf5_<alias>_sales_cycle 主数据
    wf5 = {}
    try:
        sql = f"""SELECT partner_sku, trend, daily_rate, forecast_10_days, forecast_30_days,
                         risk_label, current_pipeline, target_pipeline, wf5_replenish_qty,
                         lost_replenish_qty, weekly_total_replenish,
                         trigger_reasons, urgency, ops_advice, week_tag
                  FROM {sc_tbl}"""
        args: tuple = ()
        if skus:
            sql += " WHERE partner_sku IN (" + ",".join("?" * len(skus)) + ")"
            args = tuple(skus)
        for r in conn.execute(sql, args).fetchall():
            wf5[r[0]] = r
    except sqlite3.OperationalError:
        pass

    # 2) wf6_<alias>_replenishment_queue（丢货）
    wf6 = {}
    try:
        sql = f"""SELECT partner_sku, SUM(lost_qty), MAX(week_tag)
                  FROM {rq_tbl}
                  WHERE consumed_at IS NULL"""
        args = ()
        if skus:
            sql += " AND partner_sku IN (" + ",".join("?" * len(skus)) + ")"
            args = tuple(skus)
        sql += " GROUP BY partner_sku"
        for r in conn.execute(sql, args).fetchall():
            wf6[r[0]] = (r[1], r[2])
    except sqlite3.OperationalError:
        pass
    conn.close()

    all_skus = set(wf5.keys()) | set(wf6.keys())
    if skus:
        all_skus &= set(skus)

    n = 0
    for sku in all_skus:
        if sku in wf5:
            (_, trend, dr, fc10, fc30, risk, cp, tp, w5, lp, wt, reasons, urg, advice, week) = wf5[sku]
            fields = {
                "SKU":          sku,
                "趋势":         trend or "",
                "日均销量":     float(dr or 0),
                "预测10天":     int(fc10 or 0),
                "预测30天":     int(fc30 or 0),
                "断货风险":     risk or "无",
                "当前管道量":   int(cp or 0),
                "目标管道量":   int(tp or 0),
                "wf5建议补货": int(w5 or 0),
                "丢货必补":     int(lp or 0),
                "本周必补总量": int(wt or 0),
                "触发原因":     ", ".join(json.loads(reasons or "[]")),
                "紧急度":       urg or "正常",
                "运营建议":     advice or "",
                "周标签":       week or "",
            }
        else:
            lost, week = wf6[sku]
            fields = {
                "SKU":          sku,
                "丢货必补":     int(lost),
                "本周必补总量": int(lost),
                "触发原因":     "丢货补货",
                "周标签":       week,
            }

        existing = b.find_record(table_id, "SKU", sku, field_names=["SKU"])
        if existing:
            update_fields = {k: v for k, v in fields.items() if k not in PRESERVE_FIELDS}
            b.update_record(table_id, existing["record_id"], update_fields)
        else:
            full_fields = {**fields, "操作状态": "未处理"}
            b.insert_record(table_id, full_fields)
        n += 1
    return n


def sync_decisions(skus: Optional[List[str]] = None,
                   entity_aliases: Optional[List[str]] = None) -> int:
    """对所有（或指定）sales_entity 同步 wf5/wf6 数据到各自飞书表。"""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sales_entity import load_entities

    b = bridge()
    entities = [e for e in load_entities()
                if not entity_aliases or e["alias"] in entity_aliases]
    total = 0
    for ent in entities:
        n = _sync_decisions_for_entity(b, ent, skus=skus)
        total += n
        print(f"  [{ent['alias']}] synced {n} records")
    return total


# ── 约仓动作 ──────────────────────────────────────────
def sync_warehouse_appt(skus: Optional[List[str]] = None) -> int:
    b = bridge()
    conn = sqlite3.connect(DB)
    sql = """SELECT order_no, sku_list_json, ops_status
             FROM wf6_logistics_alerts
             WHERE alert_reason = '清关完成-需约仓'"""
    args = ()
    if skus:
        like_clauses = " OR ".join(["sku_list_json LIKE ?"] * len(skus))
        sql += f" AND ({like_clauses})"
        args = tuple(f'%"sku": "{s}"%' for s in skus)
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    n = 0
    for order_no, sku_list_json, ops_status in rows:
        sku_list = json.loads(sku_list_json or "[]")
        sku_str = ", ".join(f"{s['sku']}×{s['qty']}" for s in sku_list)
        wh_status = "已约仓" if ops_status == "已约仓" else "待约仓"
        fields = {"货单号": order_no, "SKU列表": sku_str, "状态": wh_status, "责任人": "运营"}
        b.upsert_by_field("warehouse_appt", "货单号", order_no, fields)
        n += 1
    return n


# ── 主入口 ────────────────────────────────────────────
ALL_TABLES = ["hub", "alerts", "decisions", "warehouse_appt"]

def sync_all(skus: Optional[List[str]] = None, tables: Optional[List[str]] = None, verbose: bool = True) -> dict:
    tables = tables or ALL_TABLES
    counts = {}
    if "hub" in tables:
        if verbose: print("→ 在途批次明细 + 主表「发货在途」")
        counts["in_transit"] = sync_in_transit(skus)
    if "alerts" in tables:
        if verbose: print("→ 物流告警")
        counts["alerts"] = sync_alerts(skus)
    if "decisions" in tables:
        if verbose: print("→ 经营决策")
        counts["decisions"] = sync_decisions(skus)
    if "warehouse_appt" in tables:
        if verbose: print("→ 约仓动作")
        counts["warehouse_appt"] = sync_warehouse_appt(skus)
    if verbose:
        print(f"\n汇总: {counts}")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", nargs="*", help="指定 SKU 列表（默认全部）")
    ap.add_argument("--tables", nargs="*", choices=ALL_TABLES, help=f"只同步指定表（{ALL_TABLES}）")
    args = ap.parse_args()
    sync_all(skus=args.sku, tables=args.tables)


if __name__ == "__main__":
    main()

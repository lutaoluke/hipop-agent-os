"""
一次性同步：hipop.db → 飞书子表
- wf3_logistics_hub → 子表 2 在途批次明细
- wf6_logistics_alerts → 子表 1 物流告警
- wf6_replenishment_queue → 子表 3 经营决策（丢货必补部分）
"""
import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.feishu_bridge import bridge

DB = "/Users/luke/Downloads/点购工作流/hipop.db"


def sync_hub():
    """wf3_logistics_hub → 子表 2 在途批次明细 + 主表双向关联"""
    b = bridge()
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT sku, groups_json FROM wf3_logistics_hub").fetchall()
    conn.close()

    n = 0
    for sku, gj in rows:
        groups = json.loads(gj)
        main_rec = b.find_record("main", "ERP-SKU", sku, field_names=["ERP-SKU"])
        main_record_id = main_rec["record_id"] if main_rec else None

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
                print(f"  ✓ {key} | {batch.get('current_stage')} {batch.get('stage_stay_days')}天")
    print(f"在途批次明细: 同步 {n} 条\n")


def sync_alerts():
    """wf6_logistics_alerts → 子表 1 物流告警"""
    b = bridge()
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT alert_id, order_no, forwarder, alert_reason, alert_level, stage,
               threshold_days, actual_stay_days, history_stage_days, excess_over_threshold,
               sku_list_json, action_owner, supervisor, required_action, feedback_fields,
               ops_status, ops_contact_log
        FROM wf6_logistics_alerts
    """).fetchall()
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
        print(f"  ✓ alert#{r[0]} | {r[1]} | {r[3]} ({r[4]}) → {r[15]}")
    print(f"物流告警: 同步 {n} 条\n")


def sync_replenishment():
    """wf6_replenishment_queue → 子表 3 经营决策（按 SKU 聚合）"""
    b = bridge()
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT sku, SUM(lost_qty) AS lost, MAX(week_tag) AS week_tag,
               GROUP_CONCAT(order_no, ', ') AS orders
        FROM wf6_replenishment_queue
        WHERE consumed_at IS NULL
        GROUP BY sku
    """).fetchall()
    conn.close()

    n = 0
    for r in rows:
        sku, lost, week_tag, orders = r
        main_rec = b.find_record("main", "ERP-SKU", sku, field_names=["ERP-SKU"])
        main_record_id = main_rec["record_id"] if main_rec else None

        fields = {
            "SKU":         sku,
            "丢货必补":     int(lost),
            "本周必补总量": int(lost),
            "触发原因":     ["丢货补货"],
            "周标签":       week_tag,
            "操作状态":     "未处理",
        }
        if main_record_id:
            fields["关联主表-SKU"] = [main_record_id]
        b.upsert_by_field("decisions", "SKU", sku, fields)
        n += 1
        print(f"  ✓ {sku}: 丢货必补 {lost} (来源 {orders})")
    print(f"经营决策(丢货必补): 同步 {n} 条\n")


def sync_warehouse_appt():
    """wf6 蓝色告警(清关完成-需约仓) → 子表 4 约仓动作"""
    b = bridge()
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT order_no, sku_list_json, ops_status
        FROM wf6_logistics_alerts
        WHERE alert_reason = '清关完成-需约仓'
    """).fetchall()
    conn.close()

    n = 0
    for order_no, sku_list_json, ops_status in rows:
        sku_list = json.loads(sku_list_json or "[]")
        sku_str = ", ".join(f"{s['sku']}×{s['qty']}" for s in sku_list)
        # 约仓状态映射 wf6 ops_status → 子表 4 状态
        wh_status = "待约仓"
        if ops_status == "已约仓": wh_status = "已约仓"
        fields = {
            "货单号":  order_no,
            "SKU列表": sku_str,
            "状态":    wh_status,
            "责任人":  "运营",
        }
        b.upsert_by_field("warehouse_appt", "货单号", order_no, fields)
        n += 1
        print(f"  ✓ {order_no} | {sku_str} | {wh_status}")
    print(f"约仓动作: 同步 {n} 条\n")


if __name__ == "__main__":
    print("=== 1) 同步 wf3_logistics_hub → 子表 2 在途批次明细 ===")
    sync_hub()
    print("=== 2) 同步 wf6_logistics_alerts → 子表 1 物流告警 ===")
    sync_alerts()
    print("=== 3) 同步 wf6_replenishment_queue → 子表 3 经营决策 ===")
    sync_replenishment()
    print("=== 4) 同步 wf6 蓝色告警 → 子表 4 约仓动作 ===")
    sync_warehouse_appt()
    print("✓ 全部同步完成")

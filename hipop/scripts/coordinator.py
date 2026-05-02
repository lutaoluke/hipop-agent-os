"""
Coordinator skill：意图识别 + 路由（基于规则匹配，不依赖 LLM）

支持的 5 类意图：
  1. 查 SKU [list]      → 跑 wf3 + wf6 + sync，IM 卡片汇总
  2. 查货单 PDXXXXXXX  → 找该货单的告警 + 涉及 SKU，IM 卡片
  3. 反馈状态           → "PDxxx 已确认丢货 备注:..." → update_alert_status
  4. 周报/全量          → 跑全量 wf3+wf6+sync，IM 推送
  5. 看 [国家+平台]     → 主表过滤展示

CLI:
  python3 -m scripts.coordinator "查 TBJ0057A"
  python3 -m scripts.coordinator "PDZ0027158 已确认丢货 备注:战争丢失"
  python3 -m scripts.coordinator "看 noon UAE"
  python3 -m scripts.coordinator "周报"
"""
import os
import re
import sys
import sqlite3
import argparse
from typing import Optional, List, Tuple, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = "/Users/luke/Downloads/点购工作流/hipop.db"

# ── 意图识别 ───────────────────────────────────────────
SKU_RE = re.compile(r"\b([A-Z]{2,4}\d{4,6}[A-Z]?)\b")
ORDER_RE = re.compile(r"\b(PD[A-Z]\d{7})\b")
COUNTRY_RE = re.compile(r"\b(KSA|UAE)\b", re.I)
PLATFORM_RE = re.compile(r"(noon|amazon)", re.I)

ACTION_KEYWORDS = {
    "已确认推进": ["确认推进", "已推进", "继续推进", "确认在途"],
    "已确认丢货": ["确认丢货", "已丢货", "确认丢失", "丢了", "丢货"],
    "已约仓":     ["已约仓", "约仓完成", "约好了"],
    "已结案":     ["结案", "关闭", "已完成"],
    "处理中":     ["处理中", "在处理", "正在处理"],
}

REPORT_KEYWORDS = ["周报", "全量", "全量跑", "全部", "跑全部"]
QUERY_KEYWORDS = ["查", "看", "看下", "看一下"]

NOTE_RE = re.compile(r"备注[:：](.+?)$|理由[:：](.+?)$|原因[:：](.+?)$", re.S)


def parse_intent(text: str) -> Dict:
    """
    返回 {"intent": "...", "params": {...}}
    """
    text = text.strip()

    # 1. 周报/全量
    if any(k in text for k in REPORT_KEYWORDS):
        return {"intent": "weekly_report", "params": {}}

    # 2. 反馈状态：含告警关键词 + 货单号
    for status, keywords in ACTION_KEYWORDS.items():
        if any(k in text for k in keywords):
            order_m = ORDER_RE.search(text)
            note_m = NOTE_RE.search(text)
            note = ""
            if note_m:
                note = next(g for g in note_m.groups() if g) or ""
            return {
                "intent": "update_status",
                "params": {
                    "order_no": order_m.group(1) if order_m else None,
                    "status": status,
                    "note": note.strip(),
                },
            }

    # 3. 查货单
    order_m = ORDER_RE.search(text)
    if order_m and any(k in text for k in QUERY_KEYWORDS + ["怎么样", "情况", "状态"]):
        return {"intent": "query_order", "params": {"order_no": order_m.group(1)}}

    # 4. 看店铺概览
    country_m = COUNTRY_RE.search(text)
    platform_m = PLATFORM_RE.search(text)
    if (country_m or platform_m) and any(k in text for k in QUERY_KEYWORDS):
        return {
            "intent": "scope_overview",
            "params": {
                "country": country_m.group(1).upper() if country_m else None,
                "platform": platform_m.group(1).lower() if platform_m else "noon",
            },
        }

    # 5. 查 SKU（最后兜底，因为 SKU 模糊匹配范围大）
    skus = SKU_RE.findall(text)
    if skus:
        return {"intent": "query_sku", "params": {"skus": list(set(skus))}}

    return {"intent": "unknown", "params": {"raw": text}}


# ── 路由执行 ──────────────────────────────────────────
def handle_query_sku(skus: List[str]) -> str:
    """跑 wf3 + wf6 + sync，IM 卡片汇总"""
    # 不重新跑 ERP（耗时），直接读 hub
    conn = sqlite3.connect(DB)
    lines = [f"**查询 SKU：{', '.join(skus)}**\n"]
    for sku in skus:
        row = conn.execute("""
            SELECT in_transit_total_qty, in_transit_batch_count, has_stuck_batch, needs_ops_input
            FROM wf3_logistics_hub WHERE sku=?
        """, (sku,)).fetchone()
        if not row:
            lines.append(f"\n— **{sku}**: hub 中无数据，需先跑 `wf_logistics_status.py {sku}`")
            continue
        qty, bcount, stuck, ops = row
        flags = []
        if stuck: flags.append("⚠️ 卡单")
        if ops: flags.append("🔔 待运营")
        lines.append(f"\n— **{sku}**: 在途 {qty} 件 / {bcount} 批  {' '.join(flags)}")
        # 该 SKU 的告警
        alerts = conn.execute("""
            SELECT alert_id, alert_level, alert_reason, order_no, ops_status
            FROM wf6_logistics_alerts
            WHERE sku_list_json LIKE ? AND resolved_at IS NULL
            ORDER BY CASE alert_level WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 WHEN '蓝' THEN 4 ELSE 5 END
        """, (f'%"sku": "{sku}"%',)).fetchall()
        for a in alerts:
            lines.append(f"  • [{a[1]}] {a[3]} {a[2]} ({a[4]})")
    conn.close()
    return "\n".join(lines)


def handle_query_order(order_no: str) -> str:
    """显示该货单的告警 + 关联 SKU"""
    import json
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT alert_id, alert_level, alert_reason, sku_list_json, ops_status, actual_stay_days, history_stage_days
        FROM wf6_logistics_alerts WHERE order_no=?
        ORDER BY created_at DESC
    """, (order_no,)).fetchall()
    conn.close()

    if not rows:
        return f"**{order_no}**: 无告警记录"

    lines = [f"**货单 {order_no} 的告警历史**\n"]
    for r in rows:
        sku_list = json.loads(r[3] or "[]")
        sku_str = ", ".join(f"{s['sku']}×{s['qty']}" for s in sku_list)
        stay = f"{r[5]}天" if r[5] is not None else "—"
        hist = f"{r[6]}天" if r[6] is not None else "—"
        lines.append(f"\n• alert#{r[0]} [{r[1]}] {r[2]}")
        lines.append(f"  停留 {stay} / 历史均 {hist} | 状态 {r[4]}")
        lines.append(f"  涉及: {sku_str}")
    return "\n".join(lines)


def handle_update_status(order_no: Optional[str], status: str, note: str) -> str:
    """运营/刘鹤反馈：PDxxx + 状态 + 备注 → update_alert_status"""
    if not order_no:
        return "⚠️ 没识别到货单号"

    from workflows.wf_logistics_alerts import update_alert_status
    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT alert_id, alert_reason FROM wf6_logistics_alerts
        WHERE order_no=? AND resolved_at IS NULL
    """, (order_no,)).fetchall()
    conn.close()

    if not rows:
        return f"⚠️ {order_no} 没有 active 告警"

    out = []
    for alert_id, reason in rows:
        update_alert_status(alert_id, status, note or None, "运营/刘鹤")
        out.append(f"✓ alert#{alert_id} ({reason}) → {status}")

    # 触发同步
    try:
        from scripts.feishu_sync import sync_all
        sync_all(tables=["alerts", "decisions", "warehouse_appt"], verbose=False)
        out.append(f"\n→ 已同步到飞书")
    except Exception as e:
        out.append(f"⚠️ 同步飞书失败: {e}")

    return "\n".join(out)


def handle_scope_overview(country: Optional[str], platform: str = "noon") -> str:
    """店铺概览（暂用 hipop.db 数据）"""
    import json
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT sku, groups_json FROM wf3_logistics_hub").fetchall()
    conn.close()

    in_scope_skus = []
    total_in_transit = 0
    stuck_skus = []
    for sku, gj in rows:
        groups = json.loads(gj)
        for g in groups:
            if country and g.get("country") != country: continue
            if g.get("platform") != platform: continue
            qty = g.get("in_transit_qty", 0)
            if qty > 0:
                in_scope_skus.append(sku)
                total_in_transit += qty
                for b in g.get("in_transit_batches", []):
                    if b.get("is_stuck"):
                        stuck_skus.append(sku); break

    scope = f"{platform} {country or '全部'}"
    lines = [f"**{scope} 概览**\n"]
    lines.append(f"- 有在途的 SKU: {len(set(in_scope_skus))} 个")
    lines.append(f"- 在途总件数: {total_in_transit}")
    lines.append(f"- 卡单 SKU: {len(set(stuck_skus))} 个 — {sorted(set(stuck_skus))[:5]}")
    return "\n".join(lines)


def handle_weekly_report() -> str:
    """跑全量 + sync + 推送"""
    return ("**周报触发**\n请用 CLI 跑：\n"
            "```\n"
            "python3 workflows/wf_logistics_status.py    # 全量 (5-10 分钟)\n"
            "python3 workflows/wf_logistics_alerts.py    # 生成告警 + 自动 sync\n"
            "```\n"
            "或在 schedule 里挂这两个命令。")


# ── 主入口 ──────────────────────────────────────────
def route(text: str, push_card: bool = False) -> str:
    intent = parse_intent(text)
    name = intent["intent"]
    p = intent["params"]

    if name == "query_sku":         out = handle_query_sku(p["skus"])
    elif name == "query_order":     out = handle_query_order(p["order_no"])
    elif name == "update_status":   out = handle_update_status(p["order_no"], p["status"], p["note"])
    elif name == "scope_overview":  out = handle_scope_overview(p.get("country"), p.get("platform","noon"))
    elif name == "weekly_report":   out = handle_weekly_report()
    else:                            out = f"⚠️ 没识别到意图: {p.get('raw','')}\n\n支持的形式：\n- 查 SKU TBJ0057A\n- 查货单 PDZ0027158\n- PDZ0027158 已确认丢货 备注:...\n- 看 noon UAE\n- 周报"

    if push_card:
        from scripts.feishu_bridge import bridge
        title_map = {"query_sku":"📦 SKU 查询","query_order":"🚚 货单查询","update_status":"✅ 状态更新","scope_overview":"🌍 店铺概览","weekly_report":"📊 周报"}
        color_map = {"query_sku":"blue","query_order":"blue","update_status":"green","scope_overview":"indigo","weekly_report":"purple","unknown":"grey"}
        bridge().send_card(title_map.get(name, "🤖 Coordinator"), out, color=color_map.get(name, "blue"))

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="+", help="自然语言指令")
    ap.add_argument("--push", action="store_true", help="推送到飞书群")
    args = ap.parse_args()
    text = " ".join(args.text)
    print(route(text, push_card=args.push))


if __name__ == "__main__":
    main()

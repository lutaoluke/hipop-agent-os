"""
工作流六：物流状态预警及操作状态更新

读 wf3_logistics_hub，按 (货单 × 物流公司) 维度逐批次评估 7 类风险，写入 wf6_logistics_alerts。
运营/刘鹤反馈通过 update_alert_status 回填；确认丢货时联动写入 wf6_replenishment_queue 供工作流五消费。

- 7 种 alert_reason：国内仓滞留 / 装柜出港滞留 / 海运超时 / 海运频繁推迟 / 清关超时 / 清关完成滞留 / 清关完成-需约仓
- 同一货单同时触发 海运超时 + 频繁推迟 → 合并一条「海运超时+频繁推迟」，级别取严
- 级别（按超阈值天数）：黄(1-3) / 橙(4-10) / 红(>10) / 蓝(信息) / 已到货 / 正常

CLI:
  python3 wf_logistics_alerts.py                              # 全量从 hub 生成告警
  python3 wf_logistics_alerts.py --list [--level 红]         # 列出 active 告警
  python3 wf_logistics_alerts.py --resolve <ID> --status <S> [--note "..."] [--owner "..."]
  python3 wf_logistics_alerts.py --table                      # 终端打印含正常的全量表
"""
import os
import sys
import json
import re
import sqlite3
import argparse
import unicodedata
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from workflows.wf_logistics_status import (
    filter_nodes, compute_stages, build_pool, find_history,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")

# ── 阈值与分级 ────────────────────────────────────────────
STAGE_THRESHOLDS = {
    "国内仓":     ("国内仓滞留",     3),
    "装柜出港":   ("装柜出港滞留",   3),
    "海运中":    ("海运超时",      14),
    "到港待清关": ("清关超时",      10),
    "清关完成":  ("清关完成滞留",   5),
}
LEVEL_NUM = {"正常": 0, "已到货": 0, "蓝": 1, "黄": 2, "橙": 3, "红": 4}

def grade(excess):
    if excess is None or excess <= 0: return None
    if excess <= 3: return "黄"
    if excess <= 10: return "橙"
    return "红"

# ── 频繁推迟检测（C 规则：仅看到港 ETA 推迟，排除离港 ETA）──
def detect_frequent_delay(nodes):
    cnt, etas = 0, set()
    for n in nodes:
        s, sl = n["status"], n["status"].lower()
        if any(k in s for k in ["晚到", "甩箱", "改港", "战争"]):
            cnt += 1
        if "delayed" in sl and ("arrive" in sl or "arrival" in sl):
            cnt += 1
        m_cn = re.search(r"预计(\d+-\d+)号?到港", s)
        if m_cn: etas.add(m_cn.group(1))
        m_en = re.search(r"arrive\s+at[^\n]*?\bon\s+([a-z]+)\s*(\d+)", sl)
        if m_en: etas.add(f"{m_en.group(1)} {m_en.group(2)}")
    return cnt >= 2 or len(etas) >= 2

# ── 责任与动作映射 ────────────────────────────────────────
ACTIONS = {
    "国内仓滞留":      ("刘鹤", "运营", "联系物流确认推进/丢货风险", "物流回复 / 是否丢货 / 处理状态"),
    "装柜出港滞留":    ("刘鹤", "运营", "联系物流确认推进/丢货风险", "物流回复 / 是否丢货 / 处理状态"),
    "海运超时":        ("刘鹤", "运营", "联系物流核查船位/丢货确认", "物流回复 / 是否丢货 / 实际ETA / 处理状态"),
    "海运频繁推迟":    ("刘鹤", "运营", "确认真实到港预估", "物流回复 / 实际ETA / 处理状态"),
    "海运超时+频繁推迟":("刘鹤", "运营", "联系物流核查船位/丢货+核实ETA", "物流回复 / 是否丢货 / 实际ETA / 处理状态"),
    "清关超时":        ("刘鹤", "运营", "核查清关失败/提柜风险", "物流回复 / 是否清关失败 / 处理状态"),
    "清关完成滞留":    ("刘鹤", "运营", "确认提柜进度", "物流回复 / 提柜进度 / 处理状态"),
    "清关完成-需约仓": ("运营", "刘鹤", "准备约仓文件", "约仓时间 / 处理状态"),
    "正常":            ("—",   "—",   "—",         "—"),
    "已到货":          ("—",   "—",   "—",         "—"),
}

# ── DB Schema ─────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS wf6_logistics_alerts (
    alert_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no              TEXT NOT NULL,
    forwarder             TEXT NOT NULL,
    alert_reason          TEXT NOT NULL,
    alert_level           TEXT NOT NULL,
    stage                 TEXT NOT NULL,
    threshold_days        INTEGER,
    actual_stay_days      REAL,
    history_stage_days    REAL,
    excess_over_threshold REAL,
    sku_list_json         TEXT NOT NULL,
    action_owner          TEXT NOT NULL DEFAULT '刘鹤',
    supervisor            TEXT NOT NULL DEFAULT '运营',
    required_action       TEXT,
    feedback_fields       TEXT,
    ops_status            TEXT NOT NULL DEFAULT '待处理',
    ops_contact_log       TEXT,
    ops_status_updated_at DATETIME,
    resolved_at           DATETIME,
    created_at            DATETIME NOT NULL,
    updated_at            DATETIME NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wf6_active
    ON wf6_logistics_alerts (order_no, alert_reason)
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS wf6_replenishment_queue (
    sku            TEXT NOT NULL,
    lost_qty       INTEGER NOT NULL,
    order_no       TEXT NOT NULL,
    forwarder      TEXT NOT NULL,
    confirmed_at   DATETIME NOT NULL,
    week_tag       TEXT NOT NULL,
    consumed_at    DATETIME,
    PRIMARY KEY (sku, order_no)
);
"""

def ensure_schema(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()

# ── 从 hub 重建 (货单×物流公司) 视图 ──────────────────────
def aggregate_orders_from_hub(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT sku, groups_json FROM wf3_logistics_hub").fetchall()
    conn.close()

    # 先收集所有批次用于建 pool
    all_batches = []
    for sku, gj in rows:
        for g in json.loads(gj):
            fw = g["forwarder"]
            for b in g.get("in_transit_batches", []):
                if b.get("note") == "needs_ops_input": continue
                stages = compute_stages(filter_nodes(b.get("nodes", [])))
                all_batches.append({"forwarder": fw, "stages": stages,
                                     "origin": "in_transit", "sku": sku, "order_no": b["order_no"]})
            for cb in g.get("completed_recent3", [])[:3]:
                if cb.get("note") == "needs_ops_input": continue
                stages = compute_stages(filter_nodes(cb.get("nodes", [])))
                all_batches.append({"forwarder": fw, "stages": stages,
                                     "origin": "completed", "sku": sku, "order_no": cb["order_no"]})
    pool = build_pool(all_batches)

    # 重新解析 in_transit 批次,聚合到 (order_no, forwarder)
    today = datetime.now()
    order_view = {}
    for sku, gj in rows:
        for g in json.loads(gj):
            fw = g["forwarder"]
            for b in g.get("in_transit_batches", []):
                if b.get("note") == "needs_ops_input": continue
                stages = compute_stages(filter_nodes(b.get("nodes", [])))
                if not stages: continue
                cur = stages[-1]
                cat = cur["category"]
                stay = (today - cur["start"]).days if cur.get("start") else None
                hist = find_history(cat, fw, pool)
                key = (b["order_no"], fw)
                if key not in order_view:
                    order_view[key] = {
                        "order_no":            b["order_no"],
                        "forwarder":           fw,
                        "current_stage":       cat,
                        "stage_stay_days":     stay,
                        "history_stage_days":  hist["avg_days"] if hist else None,
                        "nodes":               filter_nodes(b.get("nodes", [])),
                        "sku_list":            [],
                    }
                order_view[key]["sku_list"].append({"sku": sku, "qty": b.get("qty", 0)})
    return list(order_view.values())

# ── 评估单个货单 ──────────────────────────────────────────
def evaluate_order(batch):
    """返回告警 dict 或 None（如批次正常/已到货）。
    如果同时触发 海运超时 + 频繁推迟，合并一条，级别取严。"""
    stage = batch["current_stage"]
    stay = batch["stage_stay_days"]
    hist = batch["history_stage_days"]

    triggered = []
    if stage in STAGE_THRESHOLDS and stay is not None and hist is not None:
        reason, threshold = STAGE_THRESHOLDS[stage]
        excess = stay - hist - threshold
        level = grade(excess)
        if level:
            triggered.append({"reason": reason, "level": level, "threshold": threshold,
                              "excess": round(excess, 1)})
    if stage == "海运中" and detect_frequent_delay(batch.get("nodes", [])):
        triggered.append({"reason": "海运频繁推迟", "level": "橙", "threshold": None, "excess": None})

    if triggered:
        reasons = [t["reason"] for t in triggered]
        levels = [t["level"] for t in triggered]
        thresholds = [t["threshold"] for t in triggered if t["threshold"] is not None]
        excesses = [t["excess"] for t in triggered if t["excess"] is not None]
        merged_reason = ("海运超时+频繁推迟" if set(reasons) == {"海运超时", "海运频繁推迟"}
                         else (reasons[0] if len(reasons) == 1 else "+".join(reasons)))
        merged_level = max(levels, key=lambda x: LEVEL_NUM[x])
        return {**batch, "alert_reason": merged_reason, "alert_level": merged_level,
                "threshold": thresholds[0] if thresholds else None,
                "excess": max(excesses) if excesses else None}

    if stage == "清关完成":
        return {**batch, "alert_reason": "清关完成-需约仓", "alert_level": "蓝",
                "threshold": None, "excess": None}
    if stage == "海外仓":
        return {**batch, "alert_reason": "已到货", "alert_level": "已到货",
                "threshold": None, "excess": None}
    return {**batch, "alert_reason": "正常", "alert_level": "正常",
            "threshold": None, "excess": None}

# ── 幂等写库 ──────────────────────────────────────────────
def upsert_alert(alert, db_path=DB_PATH):
    """同一 (order_no, alert_reason) active 状态下复用一条记录；不写入正常/已到货。"""
    if alert["alert_level"] in ("正常", "已到货"):
        return None, "skipped"
    now = datetime.now().isoformat(timespec="seconds")
    owner, supervisor, action_text, feedback = ACTIONS.get(alert["alert_reason"], ACTIONS["正常"])
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    existing = cur.execute("""
        SELECT alert_id FROM wf6_logistics_alerts
        WHERE order_no = ? AND alert_reason = ? AND resolved_at IS NULL
    """, (alert["order_no"], alert["alert_reason"])).fetchone()
    if existing:
        cur.execute("""
            UPDATE wf6_logistics_alerts
            SET alert_level = ?, stage = ?, actual_stay_days = ?, history_stage_days = ?,
                excess_over_threshold = ?, sku_list_json = ?, updated_at = ?
            WHERE alert_id = ?
        """, (alert["alert_level"], alert["current_stage"], alert["stage_stay_days"],
              alert["history_stage_days"], alert["excess"],
              json.dumps(alert["sku_list"], ensure_ascii=False), now, existing[0]))
        action = "updated"; alert_id = existing[0]
    else:
        cur.execute("""
            INSERT INTO wf6_logistics_alerts
            (order_no, forwarder, alert_reason, alert_level, stage,
             threshold_days, actual_stay_days, history_stage_days, excess_over_threshold,
             sku_list_json, action_owner, supervisor, required_action, feedback_fields,
             created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (alert["order_no"], alert["forwarder"], alert["alert_reason"], alert["alert_level"],
              alert["current_stage"], alert["threshold"], alert["stage_stay_days"],
              alert["history_stage_days"], alert["excess"],
              json.dumps(alert["sku_list"], ensure_ascii=False),
              owner, supervisor, action_text, feedback, now, now))
        action = "inserted"; alert_id = cur.lastrowid
    conn.commit(); conn.close()
    return alert_id, action

# ── 反馈接口 ──────────────────────────────────────────────
TERMINAL_STATUSES = {"已确认推进", "已确认丢货", "已约仓", "已结案"}
LOST_STATUS = "已确认丢货"

def update_alert_status(alert_id, ops_status, contact_note=None, owner=None, db_path=DB_PATH):
    """更新告警状态。终态写 resolved_at。已确认丢货 → 联动写入补货队列。"""
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    row = cur.execute("""
        SELECT order_no, forwarder, sku_list_json, ops_contact_log
        FROM wf6_logistics_alerts WHERE alert_id = ?
    """, (alert_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"alert {alert_id} 不存在")
    order_no, fw, sku_list_json, contact_log_json = row
    log = json.loads(contact_log_json) if contact_log_json else []
    if contact_note:
        log.append({"at": now_iso, "by": owner or "未知", "content": contact_note})
    resolved = now_iso if ops_status in TERMINAL_STATUSES else None
    cur.execute("""
        UPDATE wf6_logistics_alerts
        SET ops_status = ?, ops_contact_log = ?, ops_status_updated_at = ?,
            resolved_at = ?, updated_at = ?
        WHERE alert_id = ?
    """, (ops_status, json.dumps(log, ensure_ascii=False), now_iso, resolved, now_iso, alert_id))
    if ops_status == LOST_STATUS:
        sku_list = json.loads(sku_list_json)
        week_tag = now.strftime("%Y-W%V")
        # 按 forwarder 名字推 entity（义特无忧KSA → SA / 安时达UAE → AE 等）
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        from sales_entity import load_entities, replenish_queue_table, ensure_tables
        ensure_tables(conn)
        country_for_fw = "SA" if "KSA" in (fw or "") else ("AE" if "UAE" in (fw or "") else None)
        target_entities = [e for e in load_entities() if e["country"] == country_for_fw]
        if not target_entities:
            print(f"⚠️ alert {alert_id} forwarder={fw} 未匹配任何 sales_entity（已跳过丢货必补写入）", file=sys.stderr)
        for ent in target_entities:
            t = replenish_queue_table(ent["alias"])
            for item in sku_list:
                cur.execute(f"""
                    INSERT OR REPLACE INTO {t}
                    (partner_sku, lost_qty, order_no, forwarder, confirmed_at, week_tag, consumed_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                """, (item["sku"], item["qty"], order_no, fw, now_iso, week_tag))
    conn.commit(); conn.close()

# ── 主流程 ────────────────────────────────────────────────
def generate_alerts(verbose=True, db_path=DB_PATH):
    ensure_schema(db_path)
    orders = aggregate_orders_from_hub(db_path)
    rows = [evaluate_order(b) for b in orders]
    new_count = upd_count = 0
    by_level = defaultdict(int)
    for r in rows:
        by_level[r["alert_level"]] += 1
        aid, action = upsert_alert(r, db_path)
        if action == "inserted": new_count += 1
        elif action == "updated": upd_count += 1
        if verbose and aid:
            print(f"  {action} #{aid} [{r['alert_level']}] {r['order_no']} | {r['forwarder']} | {r['alert_reason']}")
    if verbose:
        print(f"\n汇总：新增 {new_count}，更新 {upd_count}")
        print(f"  级别分布：" + " ".join(f"{k}={v}" for k, v in by_level.items()))
    return rows

def list_active_alerts(level=None, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    sql = """SELECT alert_id, order_no, forwarder, alert_reason, alert_level, stage,
                    actual_stay_days, history_stage_days, excess_over_threshold,
                    ops_status, sku_list_json, action_owner, supervisor, required_action
             FROM wf6_logistics_alerts WHERE resolved_at IS NULL"""
    args = []
    if level:
        sql += " AND alert_level = ?"; args.append(level)
    sql += " ORDER BY CASE alert_level WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 WHEN '蓝' THEN 4 ELSE 5 END, updated_at DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows

# ── 终端表格渲染（含正常）──────────────────────────────────
def _w(s):
    s = str(s)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)

def _pad(s, width):
    s = str(s)
    return s + " " * (width - _w(s))

def render_full_table(db_path=DB_PATH):
    """跑一遍但不写库，打印终端表格。"""
    orders = aggregate_orders_from_hub(db_path)
    rows = [evaluate_order(b) for b in orders]
    rows.sort(key=lambda a: (-LEVEL_NUM.get(a["alert_level"], 0), a["forwarder"], a["order_no"]))

    cols = ["#", "级别", "货代", "货单号", "涉及 SKU", "阶段", "原因", "停留", "历史", "阈值", "超出", "主责", "协同", "需要的动作", "需回填"]
    data = []
    for i, a in enumerate(rows, 1):
        skus = ", ".join(f"{s['sku']}×{s['qty']}" for s in a["sku_list"])
        owner, supervisor, action, feedback = ACTIONS.get(a["alert_reason"], ACTIONS["正常"])
        data.append([
            str(i), a["alert_level"], a["forwarder"], a["order_no"], skus,
            a["current_stage"], a["alert_reason"],
            f"{a['stage_stay_days']}d" if a["stage_stay_days"] is not None else "—",
            f"{a['history_stage_days']}d" if a["history_stage_days"] is not None else "—",
            str(a["threshold"]) if a["threshold"] is not None else "—",
            str(a["excess"]) if a["excess"] is not None else "—",
            owner, supervisor, action, feedback,
        ])
    widths = [max(_w(cols[i]), max((_w(r[i]) for r in data), default=0)) for i in range(len(cols))]

    def line(l, m, r): return l + m.join("─" * (wd + 2) for wd in widths) + r
    print(line("┌", "┬", "┐"))
    print("│ " + " │ ".join(_pad(cols[i], widths[i]) for i in range(len(cols))) + " │")
    print(line("├", "┼", "┤"))
    for r in data:
        print("│ " + " │ ".join(_pad(r[i], widths[i]) for i in range(len(cols))) + " │")
    print(line("└", "┴", "┘"))

# ── CLI ───────────────────────────────────────────────────
def _sync_to_feishu(tables):
    try:
        from scripts.feishu_sync import sync_all
        print(f"\n→ 同步到飞书: {tables}")
        sync_all(tables=tables, verbose=True)
    except Exception as e:
        print(f"  ⚠️ 同步飞书失败: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--level")
    ap.add_argument("--resolve", type=int, metavar="ALERT_ID")
    ap.add_argument("--status")
    ap.add_argument("--note")
    ap.add_argument("--owner", default="刘鹤")
    ap.add_argument("--table", action="store_true", help="终端打印含正常的全量表（不写库）")
    ap.add_argument("--no-sync", action="store_true", help="跳过同步到飞书")
    args = ap.parse_args()

    if args.resolve:
        if not args.status:
            print("--resolve 需要配合 --status"); sys.exit(1)
        update_alert_status(args.resolve, args.status, args.note, args.owner)
        print(f"alert#{args.resolve} → {args.status}")
        if not args.no_sync:
            _sync_to_feishu(["alerts", "decisions", "warehouse_appt"])
        return
    if args.table:
        render_full_table(); return
    if args.list:
        rows = list_active_alerts(args.level)
        print(f"{'ID':<5}{'级别':<6}{'货代':<14}{'货单号':<14}{'阶段':<10}{'原因':<22}{'停留':<7}{'状态':<10}{'主责':<8}{'SKU 数'}")
        for r in rows:
            sku_n = len(json.loads(r[10]))
            print(f"{r[0]:<5}{r[4]:<6}{r[2]:<14}{r[1]:<14}{r[5]:<10}{r[3]:<22}"
                  f"{(str(r[6]) + 'd' if r[6] is not None else '-'):<7}{r[9]:<10}{r[11]:<8}{sku_n}")
        return
    # 默认：生成告警写库
    generate_alerts(verbose=True)
    if not args.no_sync:
        _sync_to_feishu(["alerts", "warehouse_appt"])

if __name__ == "__main__":
    main()

"""
飞书 → DB 反向同步：拉取运营/刘鹤在飞书子表里更新的字段，回写 hipop.db。

支持的回写：
- 子表 1 物流告警: 操作状态 / 物流回复 → wf6_logistics_alerts.ops_status / ops_contact_log
- 子表 4 约仓动作: 状态 → 对应 wf6 alert (alert_reason='清关完成-需约仓') 的 ops_status

幂等：飞书值 == DB 值时跳过；不会和 sync_to_feishu 形成循环（值一致时双方都不动）。

CLI:
  python3 -m scripts.feishu_pull          # 拉一次
  python3 -m scripts.feishu_pull --watch  # 持续轮询（默认 5 分钟）
"""
import os
import sys
import json
import sqlite3
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.feishu_bridge import bridge

DB = "/Users/luke/code/hipop/hipop.db"


def _text(v):
    """飞书字段值标准化为字符串"""
    if v is None: return ""
    if isinstance(v, str): return v
    if isinstance(v, (int, float)): return str(v)
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return v[0].get("text", "") or v[0].get("link", "") or ""
        return " ".join(str(x) for x in v)
    if isinstance(v, dict):
        return v.get("text", "") or v.get("link", "") or ""
    return str(v)


def pull_alerts(verbose=True) -> int:
    """飞书子表 1 → wf6_logistics_alerts"""
    b = bridge()
    fs_records = b.list_records("alerts",
        field_names=["告警ID", "操作状态", "物流回复", "是否丢货"])

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    changes = 0
    from workflows.wf_logistics_alerts import update_alert_status

    for rec in fs_records:
        f = rec["fields"]
        alert_id_str = _text(f.get("告警ID"))
        if not alert_id_str:
            continue
        try:
            alert_id = int(alert_id_str)
        except ValueError:
            continue

        row = cur.execute(
            "SELECT ops_status, ops_contact_log FROM wf6_logistics_alerts WHERE alert_id=?",
            (alert_id,)
        ).fetchone()
        if not row:
            if verbose: print(f"  ⚠️ alert#{alert_id} 在 DB 不存在，跳过")
            continue
        db_status, db_log_json = row

        fs_status = _text(f.get("操作状态")) or "待处理"
        fs_reply = _text(f.get("物流回复"))

        log = json.loads(db_log_json or "[]")
        existing_contents = {l.get("content") for l in log}
        new_note = fs_reply if (fs_reply and fs_reply not in existing_contents) else None

        if fs_status != db_status or new_note:
            update_alert_status(alert_id, fs_status, new_note, "运营/刘鹤")
            if verbose:
                msg = f"  ⇄ alert#{alert_id}: {db_status} → {fs_status}"
                if new_note: msg += f"  +log({fs_reply[:20]}…)"
                print(msg)
            changes += 1
    conn.close()
    return changes


OPS_ACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS wf5_ops_actions (
    sku        TEXT NOT NULL,
    week_tag   TEXT NOT NULL,
    actual_qty INTEGER,
    ops_status TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (sku, week_tag)
);
"""

def _ensure_ops_schema():
    conn = sqlite3.connect(DB)
    conn.executescript(OPS_ACTIONS_DDL)
    conn.commit(); conn.close()

def _to_int(v):
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    s = _text(v)
    try: return int(float(s)) if s else None
    except (ValueError, TypeError): return None


def pull_decisions(verbose=True) -> int:
    """飞书子表 3 经营决策 → wf5_ops_actions；'已下单' 触发 wf6_replenishment_queue.consumed_at"""
    from datetime import datetime
    _ensure_ops_schema()
    b = bridge()
    fs_records = b.list_records("decisions",
        field_names=["SKU", "操作状态", "实际下单数", "周标签"])

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    n = 0
    now = datetime.now().isoformat(timespec="seconds")

    for rec in fs_records:
        f = rec["fields"]
        sku = _text(f.get("SKU"))
        ops_status = _text(f.get("操作状态")) or "未处理"
        actual_qty = _to_int(f.get("实际下单数"))
        week_tag = _text(f.get("周标签"))
        if not sku or ops_status == "未处理":
            continue

        prev = cur.execute(
            "SELECT ops_status, actual_qty FROM wf5_ops_actions WHERE sku=? AND week_tag=?",
            (sku, week_tag)).fetchone()
        prev_status, prev_qty = (prev[0], prev[1]) if prev else (None, None)
        if ops_status == prev_status and actual_qty == prev_qty:
            continue

        cur.execute("""
            INSERT OR REPLACE INTO wf5_ops_actions
            (sku, week_tag, actual_qty, ops_status, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (sku, week_tag, actual_qty, ops_status, now))
        n += 1
        if verbose:
            print(f"  ⇄ {sku} ({week_tag}): {prev_status or '未处理'} → {ops_status}"
                  f"{f', 实际下单 {actual_qty}件' if actual_qty is not None else ''}")

        if ops_status == "已下单":
            # SKU 可能在多个 entity 的 queue 里有未消费记录，逐个 entity 表 update
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
            from sales_entity import load_entities, replenish_queue_table
            consumed_total = 0
            for ent in load_entities():
                t = replenish_queue_table(ent["alias"])
                try:
                    consumed_total += cur.execute(f"""
                        UPDATE {t}
                        SET consumed_at = ?
                        WHERE partner_sku = ? AND consumed_at IS NULL AND week_tag = ?
                    """, (now, sku, week_tag)).rowcount
                except sqlite3.OperationalError:
                    pass  # 表可能尚未建好，忽略
            if consumed_total and verbose:
                print(f"     · 触发 wf6_<entity>_replenishment_queue.consumed_at: {consumed_total} 条")
    conn.commit(); conn.close()
    return n


def pull_warehouse_appt(verbose=True) -> int:
    """飞书子表 4 约仓动作 → wf6 alerts (清关完成-需约仓) 的 ops_status"""
    b = bridge()
    fs_records = b.list_records("warehouse_appt",
        field_names=["货单号", "状态", "约仓时间"])

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    changes = 0
    from workflows.wf_logistics_alerts import update_alert_status

    for rec in fs_records:
        f = rec["fields"]
        order_no = _text(f.get("货单号"))
        wh_status = _text(f.get("状态"))
        if not order_no:
            continue

        row = cur.execute("""
            SELECT alert_id, ops_status FROM wf6_logistics_alerts
            WHERE order_no=? AND alert_reason='清关完成-需约仓'
            ORDER BY created_at DESC LIMIT 1
        """, (order_no,)).fetchone()
        if not row:
            continue
        alert_id, db_ops = row

        target_ops = None
        if wh_status == "已约仓": target_ops = "已约仓"
        elif wh_status == "已入仓": target_ops = "已结案"

        if target_ops and target_ops != db_ops:
            update_alert_status(alert_id, target_ops, f"飞书约仓表 → {wh_status}", "运营")
            if verbose: print(f"  ⇄ {order_no} 约仓: {db_ops} → {target_ops}")
            changes += 1
    conn.close()
    return changes


def pull_all(verbose=True) -> int:
    if verbose: print("→ 拉取物流告警变更")
    n1 = pull_alerts(verbose)
    if verbose: print(f"   {n1} 条变更回写")
    if verbose: print("→ 拉取约仓动作变更")
    n2 = pull_warehouse_appt(verbose)
    if verbose: print(f"   {n2} 条变更回写")
    if verbose: print("→ 拉取经营决策变更（运营/采购操作）")
    n3 = pull_decisions(verbose)
    if verbose: print(f"   {n3} 条变更回写")
    return n1 + n2 + n3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="持续轮询")
    ap.add_argument("--interval", type=int, default=300, help="轮询间隔秒数（默认 300=5分钟）")
    args = ap.parse_args()

    if args.watch:
        print(f"轮询模式：每 {args.interval} 秒拉一次。Ctrl+C 退出。")
        while True:
            try:
                n = pull_all(verbose=True)
                if n: print(f"⇄ 本轮回写 {n} 条")
                else: print("· 无变更")
            except Exception as e:
                print(f"✗ 拉取失败: {e}")
            time.sleep(args.interval)
    else:
        pull_all(verbose=True)


if __name__ == "__main__":
    main()

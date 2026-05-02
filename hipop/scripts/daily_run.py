"""
每日 09:00 自动跑：wf3 物流采集 + wf6 告警生成 + 推送日报卡片

仅做"每日要跟的"事——物流追踪 + 告警，不跑商品/销量/补货（那是周一 weekly_run 的事）。

依赖：
  - sa_main 提供全量 SKU 列表（同 weekly_run 的 step_wf3）
  - wf3_logistics_hub 写入
  - wf6_logistics_alerts 生成
  - 飞书 alerts/in_transit/warehouse_appt 同步
  - 群推日报卡片（含告警分布 + 卡单 + 待运营）
"""
import os, sys, sqlite3, traceback
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _run_step(name, fn):
    print(f"\n{'='*60}\n[{datetime.now():%H:%M:%S}] {name}\n{'='*60}", flush=True)
    try:
        fn(); print(f"✓ {name} 完成", flush=True); return True
    except Exception:
        print(f"✗ {name} 失败:", flush=True); traceback.print_exc(); return False


def step_wf3():
    from workflows.wf_logistics_status import analyze_skus
    conn = sqlite3.connect(DB)
    skus = [r[0] for r in conn.execute(
        'SELECT DISTINCT "ERP-SKU" FROM sa_main WHERE "ERP-SKU" IS NOT NULL'
    ).fetchall()]
    conn.close()
    print(f"  全量模式: {len(skus)} 个 SKU")
    analyze_skus(skus, write_db=True, verbose=True)
    from scripts.feishu_sync import sync_all
    sync_all(skus=skus, tables=["hub"], verbose=False)


def step_wf6():
    from workflows.wf_logistics_alerts import generate_alerts
    generate_alerts(verbose=True)
    from scripts.feishu_sync import sync_all
    sync_all(tables=["alerts", "warehouse_appt"], verbose=False)


def step_daily_card():
    """日报：告警分布 + 卡单 + 待运营 SKU。"""
    from scripts.feishu_bridge import bridge
    conn = sqlite3.connect(DB)
    by_level = dict(conn.execute("""
        SELECT alert_level, COUNT(*) FROM wf6_logistics_alerts
        WHERE resolved_at IS NULL GROUP BY alert_level
    """).fetchall())
    hub_rows = conn.execute(
        "SELECT sku, has_stuck_batch, needs_ops_input FROM wf3_logistics_hub"
    ).fetchall()
    stuck = [r[0] for r in hub_rows if r[1]]
    need_ops = [r[0] for r in hub_rows if r[2]]
    conn.close()

    BASE = "PFX9bJWdTaSaSEsSwlKc1XaSnEg"
    TBL = {"alerts": "tblfooSYqDvgh03E", "in_transit": "tblrD1DLSmLgnlRL", "wh": "tblzHCjW5ZQV5mm8"}
    LK = lambda k: f"https://my.feishu.cn/base/{BASE}?table={TBL[k]}"

    today = datetime.now().strftime("%Y-%m-%d (%a)")
    lines = [f"📅 **{today} · 物流日报**\n"]
    lines.append("**🚨 物流告警**")
    parts = []
    for lv, emoji in [("红","🔴"),("橙","🟠"),("黄","🟡"),("蓝","🔵")]:
        if by_level.get(lv): parts.append(f"{emoji}{lv} × {by_level[lv]}")
    lines.append(("- " + " / ".join(parts)) if parts else "- ✅ 无 active 告警")
    lines.append(f"- 👉 [打开物流告警表]({LK('alerts')})")

    if stuck:
        lines.append(f"\n**⚠️ 卡单 SKU ({len(stuck)})**: {', '.join(stuck[:8])}")
        if len(stuck) > 8: lines.append(f"... 共 {len(stuck)} 个")
    if need_ops:
        lines.append(f"\n**🔔 待运营手动 ({len(need_ops)})**: {', '.join(need_ops[:8])}")

    lines.append("\n---\n👉 操作入口")
    lines.append(f"- [物流告警]({LK('alerts')}) — 刘鹤填操作状态/物流回复")
    lines.append(f"- [在途批次]({LK('in_transit')}) — 详细批次状态")
    lines.append(f"- [约仓动作]({LK('wh')}) — 运营填约仓时间")

    color = "red" if by_level.get("红") else ("orange" if by_level.get("橙") else "blue")
    bridge().send_card(f"📦 物流日报 · {today.split()[0]}", "\n".join(lines), color=color)
    print("✓ 日报卡片已推送")


def main():
    print(f"\n🚀 [{datetime.now()}] 开始每日例行跑")
    ok1 = _run_step("WF3 物流采集", step_wf3)
    ok2 = _run_step("WF6 告警生成", step_wf6)
    ok3 = _run_step("日报卡片", step_daily_card)
    print(f"\n{'='*60}")
    print(f"完成: wf3={ok1} wf6={ok2} card={ok3}")
    sys.exit(0 if all([ok1, ok2, ok3]) else 1)


if __name__ == "__main__":
    main()

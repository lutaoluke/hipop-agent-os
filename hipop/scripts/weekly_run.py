"""
每周一 10:00 自动跑：wf1 + wf2 → wf3 → wf6 → wf5 → 推送周报卡片

依次执行：
  1. wf1 商品库存          （ERP 6 仓库存 + noon Inventory CSV + 聚合 + 飞书同步）
  2. wf2 商品总表+销量    （ERP 商品库 + 销量价格 + noon CSV 累加 + 聚合 + 飞书同步）
  3. wf_logistics_status   （ERP 全量拉单 + 物流追踪 + 写 hub）
  4. wf_logistics_alerts   （生成告警 + sync alerts/warehouse_appt）
  5. wf_sales_cycle        （销售周期 + 补货建议 + sync decisions）
  6. weekly summary card   （汇总卡片推送到群）
"""
import os
import sys
import json
import sqlite3
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _run_step(name, fn):
    print(f"\n{'='*60}\n[{datetime.now():%H:%M:%S}] {name}\n{'='*60}", flush=True)
    try:
        fn()
        print(f"✓ {name} 完成", flush=True)
        return True
    except Exception:
        print(f"✗ {name} 失败:", flush=True)
        traceback.print_exc()
        return False


def step_wf1():
    """工作流一：库存（ERP 6 仓 + noon Inventory CSV + 聚合 + 飞书同步）"""
    # 1) ERP 库存（义乌/东莞/沙特×2/UAE×2）
    import scripts.ingest_erp_stock as es
    es.run()
    # 2) noon Inventory CSV（如果 inbox 里有）
    import scripts.ingest_noon_stock_csv as ns
    ns.run()
    # 3) 聚合 total_stock
    from workflows.wf_stock_static import run as st
    st()
    # 4) 飞书同步
    import scripts.wf1_feishu_sync as fs1
    fs1.run()


def step_wf2():
    """工作流二：商品总表 + 销量 + 飞书同步"""
    # 1) 商品库
    import scripts.ingest_erp_products as p
    p.run()
    # 2) ERP 销量价格
    import scripts.ingest_erp_sales as s
    s.run()
    # 3) noon CSV 增量
    import scripts.ingest_noon_csv as n
    n.run()
    # 4) 聚合（评级/异常/预测/is_listed）
    from workflows.wf_sales_static import run as agg_run
    agg_run()
    # 5) 飞书同步
    import scripts.wf2_feishu_sync as fs
    fs.run()


def step_wf3():
    """从 sa_main 读全量 SKU，跑 wf3 + 自动 sync"""
    from workflows.wf_logistics_status import analyze_skus
    conn = sqlite3.connect(DB)
    skus = [r[0] for r in conn.execute(
        'SELECT DISTINCT "ERP-SKU" FROM sa_main WHERE "ERP-SKU" IS NOT NULL'
    ).fetchall()]
    conn.close()
    print(f"  全量模式: {len(skus)} 个 SKU")
    analyze_skus(skus, write_db=True, verbose=True)
    # 同步 hub → 主表「发货在途」+ 子表 2 在途批次
    from scripts.feishu_sync import sync_all
    sync_all(skus=skus, tables=["hub"], verbose=False)


def step_wf6():
    from workflows.wf_logistics_alerts import generate_alerts
    generate_alerts(verbose=True)
    from scripts.feishu_sync import sync_all
    sync_all(tables=["alerts", "warehouse_appt"], verbose=False)


def step_wf5():
    """每个 sales_entity 跑独立 wf5_<alias>_sales_cycle，并同步对应飞书表"""
    from workflows.wf_sales_cycle import run as wf5_run
    wf5_run(entity_aliases=None, write_db=True, verbose=True)
    from scripts.feishu_sync import sync_decisions
    sync_decisions()


def step_summary_card():
    from scripts.feishu_bridge import bridge

    conn = sqlite3.connect(DB)
    # 告警分布
    by_level = dict(conn.execute("""
        SELECT alert_level, COUNT(*) FROM wf6_logistics_alerts
        WHERE resolved_at IS NULL GROUP BY alert_level
    """).fetchall())
    # 本周必补：各 sales_entity 独立汇总
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sales_entity import load_entities, sales_cycle_table
    must_replenish = []   # [(entity, sku, qty, urgency, advice), ...]
    for ent in load_entities():
        t = sales_cycle_table(ent["alias"])
        try:
            for sku, qty, urg, advice in conn.execute(f"""
                SELECT partner_sku, weekly_total_replenish, urgency, ops_advice
                FROM {t} WHERE weekly_total_replenish > 0
                ORDER BY CASE urgency WHEN '立即' THEN 1 WHEN '本周' THEN 2 ELSE 3 END
            """).fetchall():
                must_replenish.append((ent["alias"], sku, qty, urg, advice))
        except sqlite3.OperationalError:
            pass
    # 卡单 + 待运营
    hub_rows = conn.execute(
        "SELECT sku, has_stuck_batch, needs_ops_input FROM wf3_logistics_hub"
    ).fetchall()
    stuck = [r[0] for r in hub_rows if r[1]]
    need_ops = [r[0] for r in hub_rows if r[2]]
    conn.close()

    BASE = "PFX9bJWdTaSaSEsSwlKc1XaSnEg"
    TBL = {
        "alerts":     "tblfooSYqDvgh03E",
        "decisions":  "tblPVllRk7Aerlva",
        "in_transit": "tblrD1DLSmLgnlRL",
        "wh":         "tblzHCjW5ZQV5mm8",
    }
    LK = lambda k: f"https://my.feishu.cn/base/{BASE}?table={TBL[k]}"

    today = datetime.now().strftime("%Y-%m-%d (%a)")
    lines = [f"📅 **{today} · 本周点购周报**\n"]

    # 告警
    lines.append("**🚨 物流告警**")
    parts = []
    for lv, emoji in [("红","🔴"),("橙","🟠"),("黄","🟡"),("蓝","🔵")]:
        if by_level.get(lv): parts.append(f"{emoji}{lv} × {by_level[lv]}")
    lines.append(("- 共 " + " / ".join(parts)) if parts else "- ✅ 当前无 active 告警")
    lines.append(f"- 👉 [打开物流告警表]({LK('alerts')})")

    # 本周必补
    lines.append(f"\n**📦 本周必补 ({len(must_replenish)} 个 SKU)**")
    if must_replenish:
        for entity, sku, total, urg, _ in must_replenish[:8]:
            lines.append(f"- [{entity}] {sku} × {total} 件 ({urg})")
        if len(must_replenish) > 8:
            lines.append(f"- ... 还有 {len(must_replenish)-8} 个")
        lines.append(f"- 👉 各 entity 经营决策表见 config/hipop.json -> sales_entities[*].feishu_decisions_table_id")
    else:
        lines.append("- ✅ 本周无补货")

    # 卡单
    if stuck:
        lines.append(f"\n**⚠️ 卡单 SKU ({len(stuck)})**: {', '.join(stuck[:8])}")
        if len(stuck) > 8: lines.append(f"... 共 {len(stuck)} 个")
    if need_ops:
        lines.append(f"\n**🔔 阳光UAE 待运营手动 ({len(need_ops)})**: {', '.join(need_ops[:8])}")

    lines.append("\n---\n👉 操作入口")
    lines.append(f"- [物流告警]({LK('alerts')}) — 刘鹤填操作状态/物流回复")
    lines.append(f"- [经营决策]({LK('decisions')}) — 运营审核必补量+下单后填实际数")
    lines.append(f"- [约仓动作]({LK('wh')}) — 运营填约仓时间")
    lines.append(f"- [在途批次]({LK('in_transit')}) — 详细批次状态")

    color = "red" if by_level.get("红") else ("orange" if by_level.get("橙") else "blue")
    bridge().send_card(f"📊 本周点购周报 · {today.split()[0]}", "\n".join(lines), color=color)
    print("✓ 周报卡片已推送")


def main():
    print(f"\n🚀 [{datetime.now()}] 开始周一例行跑")
    ok_w1 = _run_step("WF1 商品库存+飞书同步", step_wf1)
    ok0 = _run_step("WF2 商品总表+销量+飞书同步", step_wf2)
    ok1 = _run_step("WF3 物流采集", step_wf3)
    ok2 = _run_step("WF6 告警生成", step_wf6)
    ok3 = _run_step("WF5 销售周期+补货", step_wf5)
    ok4 = _run_step("周报卡片", step_summary_card)
    print(f"\n{'='*60}")
    print(f"完成: wf1={ok_w1} wf2={ok0} wf3={ok1} wf6={ok2} wf5={ok3} card={ok4}")
    sys.exit(0 if all([ok_w1, ok0, ok1, ok2, ok3, ok4]) else 1)


if __name__ == "__main__":
    main()

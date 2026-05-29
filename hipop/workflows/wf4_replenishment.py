"""
工作流四：补货建议
- 基于工作流三的分析，计算每个 SKU 的补货量
- 考虑分批采购策略（降低仓储成本）
- 推送补货清单到飞书，等待确认
"""
import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.notify import send_card
from workflows.wf3_sales_cycle import analyze, LOGISTICS_MONTHS, TARGET_MONTHS, URGENT_THRESHOLD, WARNING_THRESHOLD

DB_PATH = "/Users/luke/code/hipop/hipop.db"

# 补货配置
BATCH_RATIO = 0.6      # 首批采购比例（剩余40%后续补），降低仓库积压
MIN_ORDER = 30         # 最低补货量（小于此值不建议单独采）

def calc_replenishment(results):
    suggestions = []

    for r in results:
        if r["风险"] in ("无销量", "⚠️ 库存积压"):
            continue

        monthly = r["月均销量"]
        immediate = r["即时可售月"]
        total = r["含在途月"]

        # 零库存：即便月均销量极低也要补货
        if r["风险"] == "⛔ 零库存":
            total_replenish = max(MIN_ORDER, round(TARGET_MONTHS * monthly))
        else:
            if monthly <= 0:
                continue
            # 目标：含在途库存达到 TARGET_MONTHS 个月
            gap_months = TARGET_MONTHS - total
            if gap_months <= 0:
                continue  # 库存充足
            total_replenish = round(gap_months * monthly)
            if total_replenish < MIN_ORDER:
                continue

        # 分批策略：首批 60%，后续 40%
        batch1 = round(total_replenish * BATCH_RATIO / 10) * 10  # 取整到10
        batch2 = total_replenish - batch1

        # 紧急程度影响优先级
        if r["风险"] == "🔴 紧急":
            priority = "🔴 立即"
        elif immediate < LOGISTICS_MONTHS:
            priority = "🔴 立即"  # 即时库存撑不过物流周期
        elif r["风险"] == "🟡 预警":
            priority = "🟡 本周"
        else:
            priority = "🟢 本月"

        suggestions.append({
            "SKU": r["sku"],
            "月均销量": monthly,
            "即时可售月": immediate,
            "含在途月": total,
            "趋势": r["趋势"],
            "建议总补货": total_replenish,
            "首批": batch1,
            "后续批": batch2,
            "优先级": priority,
            "利润率": r["利润率"],
        })

    # 按优先级+即时可售月排序
    priority_order = {"🔴 立即": 0, "🟡 本周": 1, "🟢 本月": 2}
    suggestions.sort(key=lambda x: (priority_order.get(x["优先级"], 9), x["即时可售月"]))
    return suggestions

def push_replenishment(suggestions):
    immediate = [s for s in suggestions if s["优先级"] == "🔴 立即"]
    this_week  = [s for s in suggestions if s["优先级"] == "🟡 本周"]
    this_month = [s for s in suggestions if s["优先级"] == "🟢 本月"]

    total_units = sum(s["建议总补货"] for s in suggestions)

    # 汇总卡片
    summary = f"""**补货建议汇总｜共 {len(suggestions)} 个 SKU 需补货**

| 优先级 | 数量 | 说明 |
|--------|------|------|
| 🔴 立即补货 | {len(immediate)} | 即时库存已撑不过物流周期 |
| 🟡 本周安排 | {len(this_week)} | 库存余量 <3 个月 |
| 🟢 本月安排 | {len(this_month)} | 库存余量偏低 |

**建议总补货量：{total_units} 件**

> 补货量基于目标库存 {TARGET_MONTHS} 个月计算
> 首批建议采 {int(BATCH_RATIO*100)}%，剩余 {int((1-BATCH_RATIO)*100)}% 视情况跟进
> ⚠️ 以下为建议值，请结合供应商交期、最低起订量综合判断"""

    send_card("📦 工作流四：补货建议", summary, color="blue")

    # 立即补货清单
    if immediate:
        lines = ["**库存已告急，建议立即下单：**\n"]
        lines.append("| SKU | 月均销 | 即时月 | 含在途月 | 趋势 | 首批 | 后续 | 利润率 |")
        lines.append("|-----|--------|--------|---------|------|------|------|-------|")
        for s in immediate:
            lines.append(
                f"| {s['SKU']} | {s['月均销量']} | **{s['即时可售月']}** "
                f"| {s['含在途月']} | {s['趋势']} "
                f"| **{s['首批']}** | {s['后续批']} | {int(s['利润率']*100)}% |"
            )
        lines.append(f"\n**请回复「确认补货」或指定修改的 SKU 和数量**")
        send_card(f"🔴 立即补货清单｜{len(immediate)} 个 SKU", "\n".join(lines), color="red")

    # 本周补货清单
    if this_week:
        top = this_week[:25]
        lines = [f"**本周建议安排补货（共{len(this_week)}个，显示前{len(top)}个）：**\n"]
        lines.append("| SKU | 月均销 | 即时月 | 趋势 | 首批 | 后续 |")
        lines.append("|-----|--------|--------|------|------|------|")
        for s in top:
            lines.append(
                f"| {s['SKU']} | {s['月均销量']} | {s['即时可售月']} "
                f"| {s['趋势']} | {s['首批']} | {s['后续批']} |"
            )
        send_card(f"🟡 本周补货清单｜{len(this_week)} 个 SKU", "\n".join(lines), color="yellow")

    # 本月补货清单（汇总）
    if this_month:
        lines = [f"**本月安排补货（共{len(this_month)}个 SKU，总计{sum(s['建议总补货'] for s in this_month)}件）：**\n"]
        top = this_month[:20]
        lines.append("| SKU | 月均销 | 含在途月 | 建议补货 |")
        lines.append("|-----|--------|---------|---------|")
        for s in top:
            lines.append(f"| {s['SKU']} | {s['月均销量']} | {s['含在途月']} | {s['建议总补货']} |")
        if len(this_month) > 20:
            lines.append(f"\n...还有 {len(this_month)-20} 个 SKU")
        send_card(f"🟢 本月补货清单｜{len(this_month)} 个 SKU", "\n".join(lines), color="green")

    print(f"✓ 工作流四完成｜立即:{len(immediate)} 本周:{len(this_week)} 本月:{len(this_month)}")
    return suggestions

if __name__ == "__main__":
    print("正在计算补货建议...")
    analysis = analyze()
    suggestions = calc_replenishment(analysis)
    push_replenishment(suggestions)
    print("✓ 补货建议已推送到飞书，等待你确认")

"""
日常运营全链路：wf0（物流在途）→ wf3（销售周期）→ wf4（补货建议）

运行方式：
  python3 hipop/workflows/wf_daily.py          # 全量扫描
  python3 hipop/workflows/wf_daily.py --skip-logistics  # 跳过物流（wf3+wf4）

约 20-40 分钟（全量），适合每天早上自动触发。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.notify import send_card, send_text

def step_logistics(skip=False):
    """wf0：在途数量 + 到货估算 + 写回 DB + 写回 Bitable"""
    if skip:
        print("\n[跳过] wf0 物流分析（--skip-logistics）\n")
        return []

    print("\n" + "="*60)
    print("  STEP 1 / 3  ·  wf0 物流在途分析")
    print("="*60)
    from workflows.wf0_logistics import (
        get_erp_token, get_skus_from_db, scan_in_transit_skus,
        analyze_sku, push_result, push_summary,
        write_transit_to_db, write_to_bitable,
    )

    token    = get_erp_token()
    all_skus = get_skus_from_db()
    transit_skus = scan_in_transit_skus(all_skus, token)

    if not transit_skus:
        print("当前无任何 SKU 有在途库存，wf0 跳过后续分析。")
        return []

    all_results = []
    for sku, _, _ in transit_skus:
        r = analyze_sku(sku, verbose=True)
        push_result(r)
        all_results.append(r)

    if len(all_results) > 1:
        push_summary(all_results)

    write_transit_to_db(all_results)
    write_to_bitable(all_results)
    return all_results

def step_sales():
    """wf3：销售周期分析 → 写回 DB → 推送飞书"""
    print("\n" + "="*60)
    print("  STEP 2 / 3  ·  wf3 销售周期分析")
    print("="*60)
    from workflows.wf3_sales_cycle import analyze, write_back, push_report

    results = analyze()
    write_back(results)
    push_report(results)
    return results

def step_restock(sales_results):
    """wf4：补货建议 → 推送飞书"""
    print("\n" + "="*60)
    print("  STEP 3 / 3  ·  wf4 补货建议")
    print("="*60)
    from workflows.wf4_replenishment import calc_replenishment, push_replenishment

    suggestions = calc_replenishment(sales_results)
    push_replenishment(suggestions)
    return suggestions

def push_daily_summary(logistics_count, sales_results, suggestions):
    urgent  = [r for r in sales_results if r["风险"] == "🔴 紧急"]
    warning = [r for r in sales_results if r["风险"] == "🟡 预警"]
    restock_now = [s for s in suggestions if s["优先级"] == "🔴 立即"]

    lines = [
        f"**今日日报已完成** | {__import__('datetime').date.today()}",
        "",
        f"| 项目 | 结果 |",
        f"|------|------|",
        f"| 在途 SKU | {logistics_count} 个有在途货 |",
        f"| 🔴 紧急断货 | {len(urgent)} 个 SKU |",
        f"| 🟡 补货预警 | {len(warning)} 个 SKU |",
        f"| 立即下单 | {len(restock_now)} 个 SKU，{sum(s['首批'] for s in restock_now)} 件 |",
        "",
        "> 详细清单见上方各卡片 | wf0→wf3→wf4 全链路完成",
    ]
    send_card("📋 每日运营日报", "\n".join(lines), color="green")

if __name__ == "__main__":
    skip_logistics = "--skip-logistics" in sys.argv

    send_text(f"🚀 日常运营流程启动{'（跳过物流）' if skip_logistics else '（全量）'}，预计{'5' if skip_logistics else '20-40'}分钟完成...")

    logistics_results = step_logistics(skip=skip_logistics)
    sales_results     = step_sales()
    suggestions       = step_restock(sales_results)

    push_daily_summary(len(logistics_results), sales_results, suggestions)

    print("\n" + "="*60)
    print("  全链路完成 ✓")
    print("="*60)

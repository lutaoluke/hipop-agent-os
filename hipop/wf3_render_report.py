"""
渲染工作流三 Phase B 结果为可核对的 Markdown 报表
"""
import json

INP = "/tmp/wf3_phase_b.json"
OUT = "/tmp/wf3_report.md"

def render():
    d = json.load(open(INP, encoding="utf-8"))
    lines = ["# 工作流三验证报告 — 10 个 SKU\n"]

    for sku_data in d:
        sku = sku_data["sku"]
        lines.append(f"\n## {sku}\n")
        if not sku_data["groups"]:
            lines.append("_无任何货单_\n")
            continue

        for g in sku_data["groups"]:
            tag = f"{g['country']} / {g['platform']} / {g['forwarder']} / {g['shipping_method']}"
            need_ops = g["forwarder"] == "阳光UAE"
            ops_flag = " 🔔 **需运营手动填入**" if need_ops else ""
            lines.append(f"\n### 分组：{tag}{ops_flag}\n")

            # ① ② 在途批次 + 总商品数
            if g["in_transit_count"] == 0:
                lines.append(f"- **① 在途批次：0 批**\n- **② 在途总商品数：0 件**\n")
            else:
                lines.append(f"- **① 在途批次：{g['in_transit_count']} 批**")
                lines.append(f"- **② 在途总商品数：{g['in_transit_qty']} 件**\n")

                # ③ ④ 最新状态 + 停留天数
                lines.append("\n**③ ④ 在途批次最新状态 & 停留天数：**\n")
                lines.append("| 货单号 | 单号 | 件数 | 发货日 | 最新状态 | 停留天数 |")
                lines.append("|---|---|---|---|---|---|")
                for b in g["in_transit_batches"]:
                    status = b.get("latest_status") or ""
                    if b.get("note") == "needs_ops_input":
                        status = "_（待运营填入）_"
                    stay = b.get("current_state_stay_days")
                    stay_s = f"{stay}天" if stay is not None else "—"
                    lines.append(f"| {b['order_no']} | {b['tracking_no']} | {b['qty']} | {b['delivery_at']} | {status[:60]} | {stay_s} |")

            # ⑤ ⑥ 已完成 top3
            if g["completed_recent3"]:
                lines.append("\n**⑤ ⑥ 已完成近 3 笔总耗时 & 状态间耗时：**\n")
                for i, b in enumerate(g["completed_recent3"], 1):
                    if b.get("note") == "needs_ops_input":
                        lines.append(f"- 已完成 #{i} `{b['order_no']}` (发货 {b['delivery_at']}, 入仓 {b['in_storage_at']})：_待运营手动填入_\n")
                        continue
                    total = b.get("total_days")
                    total_s = f"{total} 天" if total is not None else "—"
                    lines.append(f"- 已完成 #{i} `{b['order_no']}` (发货 {b['delivery_at']}, 入仓 {b['in_storage_at']})：**总耗时 {total_s}**")
                    if b["intervals"]:
                        lines.append("\n  | 上一状态 | 下一状态 | 耗时 |")
                        lines.append("  |---|---|---|")
                        for itv in b["intervals"]:
                            lines.append(f"  | {itv['from'][:40]} | {itv['to'][:40]} | {itv['days']} 天 |")
                        lines.append("")
                    else:
                        lines.append("  _节点不足，无法分解_\n")
            else:
                lines.append("\n**⑤ ⑥ 已完成：无近 3 笔记录**\n")

    text = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已写入 {OUT}（{len(text):,} 字）")


if __name__ == "__main__":
    render()

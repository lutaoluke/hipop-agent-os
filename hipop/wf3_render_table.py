"""
工作流三 - 紧凑汇总表（按 SKU × 货代分组）
列：SKU/分组、在途总商品数、在途批次及商品数、在途批次当前物流状态、当前状态耗时、已完成批次总耗时、当前状态在已完成批次耗时
"""
import json, re

INP = "/tmp/wf3_phase_b.json"
OUT = "/tmp/wf3_table.md"


def normalize_state(s):
    """归一化状态名，便于跨批次匹配（去掉日期等动态部分）"""
    if not s:
        return ""
    s = s.strip()
    # 中文：取「，」前部分
    if "，" in s:
        s = s.split("，")[0]
    # 英文：去除 "on March 15"、"until January 23"、"by December 5" 等日期尾巴
    s = re.sub(r"\bon\s+\w+\s*\d*", "", s, flags=re.I)
    s = re.sub(r"\buntil\s+\w+\s*\d*", "", s, flags=re.I)
    s = re.sub(r"\bby\s+\w+\s*\d+", "", s, flags=re.I)
    # 英文：去除括号
    s = re.sub(r"\([^)]*\)", "", s)
    # 英文：去除独立的月+日（"March 15"）
    s = re.sub(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s*\d*\b", "", s, flags=re.I)
    # 折叠多余空白
    s = re.sub(r"\s+", " ", s).strip().rstrip(",.")
    return s


def find_state_history(in_transit_state, completed):
    """
    在已完成批次的 intervals 里，找到 from 字段匹配 in_transit_state 的间隔
    返回 (avg_days, count, durations_list)
    """
    target = normalize_state(in_transit_state)
    matches = []
    for cb in completed:
        if cb.get("note") == "needs_ops_input":
            continue
        for itv in cb.get("intervals", []):
            if normalize_state(itv["from"]) == target:
                matches.append(itv["days"])
    if not matches:
        return None, 0, []
    return round(sum(matches) / len(matches), 1), len(matches), matches


def truncate(s, n=45):
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + "…"


def render():
    d = json.load(open(INP, encoding="utf-8"))
    lines = ["# 工作流三 — SKU 物流状态汇总（按 SKU × 国家/货代）\n"]
    lines.append(
        "| SKU / 分组 | 在途总件数 | 在途批次（货单：件数） | 当前物流状态 | 状态停留 | 已完成总耗时（近3笔均值） | 该状态在历史的耗时 |"
    )
    lines.append("|---|---|---|---|---|---|---|")

    no_in_transit_rows = []

    for sku_data in d:
        sku = sku_data["sku"]
        for g in sku_data["groups"]:
            country = g["country"]
            fw = g["forwarder"]
            need_ops = fw == "阳光UAE"
            ops_tag = " 🔔" if need_ops else ""
            group_label = f"**{sku}**<br>{country} / {fw}{ops_tag}"

            # 已完成均值（不论是否有在途，都算）
            completed_valid = [
                c for c in g["completed_recent3"]
                if c.get("total_days") is not None and c.get("note") != "needs_ops_input"
            ]
            if completed_valid:
                avg_total = round(sum(c["total_days"] for c in completed_valid) / len(completed_valid))
                avg_total_cell = f"{avg_total}天 (n={len(completed_valid)})"
            elif need_ops:
                avg_total_cell = "🔔 待运营"
            else:
                avg_total_cell = "—"

            if g["in_transit_count"] == 0:
                # 收集成单独段落，不进主表
                no_in_transit_rows.append(
                    (group_label, avg_total_cell)
                )
                continue

            # 把每个在途批次拼成多行
            batches_str, states_str, stays_str, hist_str = [], [], [], []
            for b in g["in_transit_batches"]:
                qty = b["qty"]
                batches_str.append(f"{b['order_no']}：{qty}件")

                if b.get("note") == "needs_ops_input":
                    states_str.append("🔔 待运营填入")
                    stays_str.append("—")
                    hist_str.append("—")
                    continue

                state = b.get("latest_status") or "—"
                stay = b.get("current_state_stay_days")
                states_str.append(truncate(state))
                stays_str.append(f"{stay}天" if stay is not None else "—")

                avg_d, cnt, durations = find_state_history(state, g["completed_recent3"])
                if avg_d is None:
                    hist_str.append("无匹配")
                else:
                    durs = "/".join(f"{d}" for d in durations)
                    hist_str.append(f"均{avg_d}天 (n={cnt})")

            row = (
                f"| {group_label} "
                f"| {g['in_transit_qty']} "
                f"| {'<br>'.join(batches_str)} "
                f"| {'<br>'.join(states_str)} "
                f"| {'<br>'.join(stays_str)} "
                f"| {avg_total_cell} "
                f"| {'<br>'.join(hist_str)} |"
            )
            lines.append(row)

    if no_in_transit_rows:
        lines.append("\n## 当前无在途批次的分组（仅作历史参考）\n")
        lines.append("| SKU / 分组 | 已完成总耗时（近3笔均值） |")
        lines.append("|---|---|")
        for label, cell in no_in_transit_rows:
            lines.append(f"| {label} | {cell} |")

    text = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已写入 {OUT}")


if __name__ == "__main__":
    render()

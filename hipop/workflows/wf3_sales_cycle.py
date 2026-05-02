"""
wf3 运营与补货分析
用法:
  python3 -m workflows.wf3_sales_cycle              # 全量
  python3 -m workflows.wf3_sales_cycle TBJ0057A     # 指定 SKU
  python3 -m workflows.wf3_sales_cycle TBJ0057A TBC0168A
"""
import sys, os, sqlite3, statistics, json, requests
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from workflows.wf0_logistics import (
    analyze_sku, get_erp_token, get_all_orders, get_order_detail_qty, get_skus_from_db
)

# ── 配置 ──────────────────────────────────────────────────
_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
with open(_cfg_path, encoding="utf-8") as _f:
    _CFG = json.load(_f)

DB_PATH       = _CFG["db"]["path"]
FEISHU_HOOK   = _CFG.get("feishu", {}).get("webhook")
TODAY         = date.today()
TRANSFER_DAYS = 7
ORDER_CYCLE   = 7


# ── 工具函数 ──────────────────────────────────────────────
def safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "0.0", "0") else default
    except Exception:
        return default


# ── 趋势计算 ──────────────────────────────────────────────
def calc_trend_forecast(d10, d30, d60, d180):
    r10 = d10 / 10 if d10 > 0 else 0
    r30 = d30 / 30 if d30 > 0 else 0
    r60 = d60 / 60 if d60 > 0 else 0

    if d30 == 0:
        return "无销量", 0, 0, 0

    short_accel = r10 > r30 * 1.3

    if short_accel and r30 >= r60 * 0.9:
        daily = r10 * (r10 / r30)
        trend = "↑↑ 加速增长"
    elif r30 > r60 * 1.15:
        daily = r10 if r10 > 0 else r30
        trend = "↑ 增长"
    elif r30 < r60 * 0.7:
        daily = max(r30, r60 * 0.7)
        trend = "↓↓ 急速下降"
    elif r30 < r60 * 0.85:
        daily = r30
        trend = "↓ 下降"
    elif short_accel:
        daily = r60
        trend = "→ 波动"
    else:
        daily = r30
        trend = "→ 平稳"

    return trend, daily, round(daily * 10), round(daily * 30)


# ── 时间线模拟 ────────────────────────────────────────────
def simulate_sellable_days(immediate, transfer_qty, transit_batches, daily_rate):
    if daily_rate <= 0:
        return 9999, ["日均销量为0，无法估算"], []

    events = [(0, immediate, "即时可售")]
    if transfer_qty > 0:
        events.append((TRANSFER_DAYS, transfer_qty, "腾挪可售"))
    for b in transit_batches:
        days = b.get("remaining_days")
        if days is not None and b["sku_qty"] > 0:
            events.append((int(days), b["sku_qty"], f"在途({b['logistics']})"))

    events.sort(key=lambda x: x[0])
    last_transit_day = events[-1][0] if len(events) > 1 else 0

    stock, day, details, gaps = 0, 0, [], []

    for event_day, qty, label in events:
        gap  = event_day - day
        need = gap * daily_rate

        if stock < need:
            exhausted_day = day + stock / daily_rate if stock > 0 else day
            shortage_days = event_day - exhausted_day
            gap_type = "在途期间" if event_day <= last_transit_day else "在途全到后"
            gaps.append({
                "start": int(exhausted_day), "end": event_day,
                "gap_days": round(shortage_days), "type": gap_type,
                "next_label": label,
            })
            details.append(
                f"🔴 第{exhausted_day:.0f}天库存耗尽（{gap_type}断货{shortage_days:.0f}天）"
                f" → [{label}] 第{event_day}天到货{qty}件补上"
            )
            stock = qty
        else:
            stock -= need
            details.append(f"   [{label}] 第{event_day}天到货{qty}件（到货前余{stock:.0f}件）")
            stock += qty

        day = event_day

    if stock > 0 and daily_rate > 0:
        final_days = day + stock / daily_rate
        details.append(f"   全部在途到货后，可售至第{final_days:.0f}天")
    else:
        final_days = float(day)

    return final_days, details, gaps


# ── 单 SKU 分析 ───────────────────────────────────────────
def analyze_wf3(sku, token=None, verbose=True):
    """
    分析单个 SKU，打印完整报告，返回结果字典供汇总使用。
    token 可复用，避免每次重新登录 ERP。
    """
    # 1. 读 DB
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    cur.execute('SELECT * FROM sa_main WHERE "ERP-SKU" = ?', (sku,))
    row  = cur.fetchone()
    conn.close()

    if not row:
        print(f"❌ DB 中找不到 SKU: {sku}")
        return None

    d10  = safe_float(row["10.0"])
    d30  = safe_float(row["30.0"])
    d60  = safe_float(row["60.0"])
    d180 = safe_float(row["180.0"])
    margin = safe_float(row["最新利润率"])

    immediate    = safe_float(row["noon平台"]) + safe_float(row["送仓未上架"])
    transfer_qty = safe_float(row["海外仓可用库存"])
    domestic_qty = safe_float(row["义乌仓"]) + safe_float(row["东莞仓"])

    trend, daily_rate, fc10, fc30 = calc_trend_forecast(d10, d30, d60, d180)
    slow_mover = 0 < d30 / 30 < 0.1     # 绝对销量过低（30天基准<3件/月，不受趋势放大影响）
    low_margin = margin != 0 and margin < 0.20  # 利润率偏低或亏损（<20%，0表示无数据不触发）

    # 2. wf0：实时在途 + 物流均值（含超期修正）
    if verbose:
        print(f"正在从 ERP 获取 {sku} 在途数据...\n")
    wf0   = analyze_sku(sku, verbose=False)
    token = token or get_erp_token()

    # 历史批次参考（近8笔）
    hist_orders = get_all_orders(sku, token)
    hist_qtys   = [get_order_detail_qty(o.get("id", ""), sku, token)
                   for o in hist_orders[:8]]
    hist_qtys   = [q for q in hist_qtys if q > 0]
    hist_med    = round(statistics.median(hist_qtys)) if hist_qtys else None
    hist_avg    = round(sum(hist_qtys) / len(hist_qtys)) if hist_qtys else None
    hist_min    = min(hist_qtys) if hist_qtys else None
    hist_max    = max(hist_qtys) if hist_qtys else None

    transit_batches  = wf0.get("transit_batches", [])
    avg_transit_days = wf0.get("avg_transit_days")
    fastest          = wf0.get("fastest_batch")
    total_transit    = wf0.get("total_transit_qty", 0)

    # 3. 时间线模拟
    sellable_days, timeline, gaps = simulate_sellable_days(
        immediate, transfer_qty, transit_batches, daily_rate
    )
    gaps_during = [g for g in gaps if g["type"] == "在途期间"]

    # 4. 决策窗口
    if avg_transit_days and daily_rate > 0 and sellable_days < 9999:
        decision_days = sellable_days - avg_transit_days
    else:
        decision_days = None

    # 5. 补货数量（周期性补货 + 历史中位批量）
    if avg_transit_days and daily_rate > 0:
        pipeline_target  = (avg_transit_days + ORDER_CYCLE) * daily_rate
        current_pipeline = immediate + transfer_qty + total_transit + domestic_qty
        total_shortfall  = max(0, round(pipeline_target - current_pipeline))
        weekly_rate      = max(1, round(ORDER_CYCLE * daily_rate))
        batch_suggest    = hist_med if hist_med else weekly_rate

        if total_shortfall <= 0:
            replenish_qty  = 0
            replenish_note = (
                f"目标管道={pipeline_target:.0f}件"
                f"（({avg_transit_days}+{ORDER_CYCLE})天×{daily_rate:.2f}/天），"
                f"当前管道={current_pipeline:.0f}件"
            )
        else:
            replenish_qty  = batch_suggest
            batches_needed = -(-total_shortfall // batch_suggest)
            replenish_note = (
                f"管道缺口{total_shortfall}件，按历史中位批量{batch_suggest}件下单，"
                f"共需约{batches_needed}批追平"
                f"（目标{pipeline_target:.0f}件，当前{current_pipeline:.0f}件）"
            )
    else:
        total_shortfall = 0
        replenish_qty   = 0
        replenish_note  = "数据不足，无法计算"

    # 慢销品 / 低利润率 override（覆盖补货建议）
    if slow_mover:
        replenish_qty  = 0
        replenish_note = f"慢销品（30天基准日均{d30/30:.2f}件，30天仅{d30:.0f}件），绝对销量过低，建议评估 EOL，无需按常规模型补货"
    elif low_margin:
        replenish_qty  = 0
        replenish_note = f"利润率{margin:.0%}偏低（<20%），暂缓补货，建议调价提利润或评估 EOL"

    # 6. 双维度风险判断
    # 维度A：运营策略
    if daily_rate == 0:
        status_ops = "⚪ 无销量"
    elif immediate == 0 and total_transit == 0:
        status_ops = "⛔ 零库存，立即停止广告"
    elif gaps_during:
        total_gap = sum(g["gap_days"] for g in gaps_during)
        if total_gap == 0:
            status_ops = "🟡 库存极紧，控制流量 / 适当调价保利润"
        else:
            status_ops = f"🔴 在途期间断货{total_gap}天，立即调价控流 / 下架部分变体"
    elif trend in ("↓↓ 急速下降", "↓ 下降") and sellable_days > 180:
        status_ops = "⚠️  滞销积压，考虑降价促销或参加 Deal 清库"
    elif trend in ("↑↑ 加速增长", "↑ 增长"):
        status_ops = "🟢 销量上涨，保持当前运营 / 可适当提价测试"
    else:
        status_ops = "🟢 正常运营，维持现状"

    # 维度B：采购决策
    if slow_mover:
        status_buy = f"⚪ 慢销品（30天{d30:.0f}件），暂不采购"
    elif low_margin:
        status_buy = f"⚪ 利润率{margin:.0%}偏低，暂缓补货"
    elif daily_rate == 0:
        status_buy = "⚪ 无销量，暂不采购"
    elif immediate == 0 and total_transit == 0:
        status_buy = f"🔴 本周立即采购 {replenish_qty}件（零库存）"
    elif avg_transit_days and decision_days is not None and decision_days <= 0:
        if replenish_qty > 0:
            status_buy = f"🔴 本周立即采购 {replenish_qty}件（已过补货窗口）"
        else:
            status_buy = "🟡 窗口已过但管道暂足，本周密切关注（下周三重新评估）"
    elif avg_transit_days and decision_days is not None and decision_days < ORDER_CYCLE * 2:
        action = f"采购{replenish_qty}件" if replenish_qty > 0 else "保持每周刷新，下周按量采购"
        status_buy = f"🔴 本周必须下单：{action}（窗口仅剩{int(decision_days)}天）"
    elif avg_transit_days and sellable_days < avg_transit_days:
        status_buy = f"🔴 本周立即采购 {replenish_qty}件（可售不足一个物流周期）"
    elif avg_transit_days and sellable_days < avg_transit_days * 1.5:
        if replenish_qty > 0:
            status_buy = f"🟡 {int(decision_days)}天内采购 {replenish_qty}件"
        else:
            status_buy = f"🟡 窗口剩{int(decision_days)}天，本周管道暂足（下周三再审视）"
    elif replenish_qty > 0:
        status_buy = f"🟢 {int(decision_days)}天后采购约 {replenish_qty}件"
    else:
        status_buy = "🟢 管道充足，本周无需采购"

    # 7. 紧迫度分级（供汇总用）
    if slow_mover or low_margin:
        urgency = "grey"
    elif "🔴" in status_buy or "⛔" in status_ops or "🔴" in status_ops:
        urgency = "red"
    elif "🟡" in status_buy or "⚠️" in status_ops:
        urgency = "yellow"
    elif "⚪" in status_buy:
        urgency = "grey"
    else:
        urgency = "green"

    # 8. 打印详细报告
    if verbose:
        sep = "═" * 58
        print(sep)
        print(f"  SKU: {sku}   {TODAY}")
        print(sep)
        if slow_mover:
            print(f"  ⚠️  慢销品：30天基准日均{d30/30:.2f}件（30天仅{d30:.0f}件），绝对销量过低，补货建议已关闭")
        if low_margin:
            print(f"  ⚠️  低利润率：{margin:.0%}（<20%），补货建议已关闭")

        print("\n【销量趋势】")
        print(f"  数据  : 10天:{d10:.0f}  30天:{d30:.0f}  60天:{d60:.0f}  180天:{d180:.0f}")
        print(f"  趋势  : {trend}   日均: {daily_rate:.2f} 件/天")
        print(f"  预测  : 未来10天 {fc10}件 | 未来30天 {fc30}件")

        print("\n【库存分层】")
        print(f"  即时可售  : {immediate:.0f}件  (noon:{safe_float(row['noon平台']):.0f} + 送仓未上架:{safe_float(row['送仓未上架']):.0f})")
        print(f"  腾挪可售  : {transfer_qty:.0f}件  (海外仓，+{TRANSFER_DAYS}天入仓)")
        print(f"  在途      : {total_transit}件  ({len([b for b in transit_batches if b.get('remaining_days') is not None])}批有ETA)")
        if fastest:
            print(f"  最快到货  : {fastest['sku_qty']}件，{fastest['est_arrival']}（还需{fastest['remaining_days']}天）")
        if avg_transit_days:
            print(f"  物流均值  : {avg_transit_days}天（近3笔跨SKU加权，含超期修正）")
        print(f"  国内仓    : {domestic_qty:.0f}件  (义乌:{safe_float(row['义乌仓']):.0f} + 东莞:{safe_float(row['东莞仓']):.0f})")

        print("\n【时间线模拟】")
        for line in timeline:
            print(f"  {line}")

        print(f"\n{'═'*58}")
        print(f"  输出字段")
        print(f"{'═'*58}")
        cov = f"（物流均值{avg_transit_days}天）" if avg_transit_days else ""
        print(f"  可售时长  : {sellable_days:.0f}天 ≈ {sellable_days/30:.1f}个月  {cov}")
        print(f"  趋势判断  : {trend}  日均{daily_rate:.2f}件  预测10天{fc10}件/30天{fc30}件")
        print(f"  断货风险  :")
        print(f"    在途期间 → {status_ops}")
        print(f"    采购决策 → {status_buy}")
        print(f"  本周补货  : {replenish_qty}件")
        print(f"    {replenish_note}")
        if hist_avg:
            print(f"  历史批次  : 中位{hist_med}件  均值{hist_avg}件  最小{hist_min}件  最大{hist_max}件"
                  f"  （近{len(hist_qtys)}笔：{hist_qtys}）")
        print()

    return {
        "sku":              sku,
        "trend":            trend,
        "daily_rate":       daily_rate,
        "fc10":             fc10,
        "fc30":             fc30,
        "sellable_days":    sellable_days,
        "avg_transit_days": avg_transit_days,
        "immediate":        immediate,
        "total_transit":    total_transit,
        "status_ops":       status_ops,
        "status_buy":       status_buy,
        "replenish_qty":    replenish_qty,
        "replenish_note":   replenish_note,
        "hist_med":         hist_med,
        "urgency":          urgency,
        "slow_mover":       slow_mover,
        "low_margin":       low_margin,
        "margin":           margin,
    }


# ── 飞书汇总推送 ──────────────────────────────────────────
def push_summary(results):
    if not FEISHU_HOOK:
        print("⚠️  未配置飞书 Webhook，跳过推送")
        return

    red    = [r for r in results if r["urgency"] == "red"]
    yellow = [r for r in results if r["urgency"] == "yellow"]
    green  = [r for r in results if r["urgency"] == "green"]
    grey   = [r for r in results if r["urgency"] == "grey"]

    def sku_line(r):
        med = f"（中位{r['hist_med']}件）" if r["hist_med"] else ""
        return (f"**{r['sku']}**  {r['trend']}  日均{r['daily_rate']:.1f}件  "
                f"可售{r['sellable_days']:.0f}天  本周补{r['replenish_qty']}件{med}\n"
                f"  ↳ {r['status_buy']}")

    lines = [f"**wf3 运营与补货分析 — {TODAY}**\n"]

    if red:
        lines.append(f"🔴 **立即处理（{len(red)}个）**")
        lines.extend(sku_line(r) for r in red)
    if yellow:
        lines.append(f"\n🟡 **本周关注（{len(yellow)}个）**")
        lines.extend(sku_line(r) for r in yellow)
    if green:
        lines.append(f"\n🟢 **正常（{len(green)}个）**：" +
                     "  ".join(r["sku"] for r in green))
    if grey:
        lines.append(f"\n⚪ **无需采购（{len(grey)}个）**：" +
                     "  ".join(r["sku"] for r in grey))

    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    try:
        resp = requests.post(FEISHU_HOOK, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ 飞书汇总已推送")
        else:
            print(f"⚠️  飞书推送失败：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"⚠️  飞书推送异常：{e}")


# ── 汇总打印 ──────────────────────────────────────────────
def print_summary(results):
    red    = [r for r in results if r["urgency"] == "red"]
    yellow = [r for r in results if r["urgency"] == "yellow"]
    green  = [r for r in results if r["urgency"] == "green"]
    grey   = [r for r in results if r["urgency"] == "grey"]

    print(f"\n{'═'*58}")
    print(f"  本周补货汇总 — {TODAY}  共{len(results)}个SKU")
    print(f"{'═'*58}")

    if red:
        print(f"\n🔴 立即处理（{len(red)}个）")
        for r in red:
            print(f"  {r['sku']:12s}  补{r['replenish_qty']:>3}件"
                  f"  可售{r['sellable_days']:>4.0f}天  {r['status_buy']}")
    if yellow:
        print(f"\n🟡 本周关注（{len(yellow)}个）")
        for r in yellow:
            print(f"  {r['sku']:12s}  补{r['replenish_qty']:>3}件"
                  f"  可售{r['sellable_days']:>4.0f}天  {r['status_buy']}")
    if green:
        print(f"\n🟢 正常（{len(green)}个）：" + "  ".join(r["sku"] for r in green))
    if grey:
        print(f"\n⚪ 无需采购（{len(grey)}个）：" + "  ".join(r["sku"] for r in grey))
    print()


# ── 入口 ─────────────────────────────────────────────────
def run(skus=None):
    """
    skus=None  → 全量模式，从 DB 读取所有 SKU
    skus=[...] → 指定模式
    """
    if not skus:
        skus = get_skus_from_db()
        print(f"全量模式：共 {len(skus)} 个 SKU\n")

    token   = get_erp_token()
    results = []
    for sku in skus:
        r = analyze_wf3(sku, token=token, verbose=True)
        if r:
            results.append(r)

    if len(results) > 1:
        print_summary(results)
        push_summary(results)

    return results


if __name__ == "__main__":
    skus = sys.argv[1:] if len(sys.argv) > 1 else None
    run(skus)

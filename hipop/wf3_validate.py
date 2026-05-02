"""
wf3 验证脚本 — 单 SKU 全流程测试
用法: python3 wf3_validate.py TBJ0057A
"""
import sys, os, sqlite3, statistics
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from workflows.wf0_logistics import analyze_sku, get_erp_token, get_all_orders, get_order_detail_qty

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "hipop.db")
TODAY   = date.today()

TRANSFER_DAYS = 7    # 海外仓腾挪入仓天数
URGENT_DAYS   = 30
WARNING_DAYS  = 60


def safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "0.0", "0") else default
    except Exception:
        return default


def calc_trend_forecast(d10, d30, d60, d180):
    """
    返回 (趋势标签, 日均销量, 预测10天, 预测30天)

    规则：
    - ↑↑ 加速增长：r10>r30×1.3 且 r30≥r60×0.9（短中期都在涨）
                   日均 = r10 × (r10/r30) 动量加权
    - ↑  增长     ：r30>r60×1.15
                   日均 = (r10+r30)/2
    - ↓↓ 急速下降：r30<r60×0.7
                   日均 = max(r30, r60×0.7)  防止近期零销量撑出虚假高值
    - ↓  下降     ：r30<r60×0.85
                   日均 = r30
    - → 波动      ：r10>r30×1.3 但 r30<r60×0.9（短期反弹但中期走低）
                   日均 = r60（更稳定的基准）
    - → 平稳      ：其余
                   日均 = r30
    """
    r10  = d10  / 10  if d10  > 0 else 0
    r30  = d30  / 30  if d30  > 0 else 0
    r60  = d60  / 60  if d60  > 0 else 0

    if d30 == 0:
        return "无销量", 0, 0, 0

    short_accel = r10 > r30 * 1.3  # 近10天明显加速

    if short_accel and r30 >= r60 * 0.9:
        # 短期中期都在涨 → 真实加速
        momentum = r10 / r30
        daily    = r10 * momentum
        trend    = "↑↑ 加速增长"
    elif r30 > r60 * 1.15:
        daily = r10 if r10 > 0 else r30   # 增长趋势取最近日均，保守估计消耗
        trend = "↑ 增长"
    elif r30 < r60 * 0.7:
        daily = max(r30, r60 * 0.7)
        trend = "↓↓ 急速下降"
    elif r30 < r60 * 0.85:
        daily = r30
        trend = "↓ 下降"
    elif short_accel:
        # 近期反弹但中期走低 → 波动，取 r60 作稳定基准
        daily = r60
        trend = "→ 波动"
    else:
        daily = r30
        trend = "→ 平稳"

    forecast_10 = round(daily * 10)
    forecast_30 = round(daily * 30)
    return trend, daily, forecast_10, forecast_30


def simulate_sellable_days(immediate, transfer_qty, transit_batches, daily_rate):
    """
    模拟库存消耗时间线（不含国内仓）
    返回 (可售总天数, 详情列表, 断货缺口列表)

    断货缺口格式: {"start": day, "end": day, "gap_days": n, "type": "在途期间" | "在途全到后"}
    - 在途期间: 在最后一批在途到货之前就出现断货缺口
    - 在途全到后: 所有在途都到了之后才耗尽库存
    """
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

    stock   = 0
    day     = 0
    details = []
    gaps    = []

    for event_day, qty, label in events:
        gap  = event_day - day
        need = gap * daily_rate

        if stock < need:
            if stock > 0:
                exhausted_day = day + stock / daily_rate
            else:
                exhausted_day = day
            shortage_days = event_day - exhausted_day
            gap_type = "在途期间" if event_day <= last_transit_day else "在途全到后"
            gaps.append({
                "start":     int(exhausted_day),
                "end":       event_day,
                "gap_days":  round(shortage_days),
                "type":      gap_type,
                "next_label": label,
            })
            details.append(
                f"🔴 第{exhausted_day:.0f}天库存耗尽（{gap_type}断货{shortage_days:.0f}天）"
                f" → [{label}] 第{event_day}天到货{qty}件补上"
            )
            stock = qty
        else:
            stock -= need
            details.append(
                f"   [{label}] 第{event_day}天到货{qty}件（到货前余{stock:.0f}件）"
            )
            stock += qty

        day = event_day

    if stock > 0 and daily_rate > 0:
        final_days = day + stock / daily_rate
        details.append(f"   全部在途到货后，可售至第{final_days:.0f}天")
    else:
        final_days = float(day)

    return final_days, details, gaps


def analyze_wf3(sku):
    # ── 1. 读 DB ──────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()
    cur.execute('SELECT * FROM sa_main WHERE "ERP-SKU" = ?', (sku,))
    row  = cur.fetchone()
    conn.close()

    if not row:
        print(f"❌ DB 中找不到 SKU: {sku}")
        return

    d10  = safe_float(row["10.0"])
    d30  = safe_float(row["30.0"])
    d60  = safe_float(row["60.0"])
    d180 = safe_float(row["180.0"])

    immediate    = safe_float(row["noon平台"]) + safe_float(row["送仓未上架"])
    transfer_qty = safe_float(row["海外仓可用库存"])
    domestic_qty = safe_float(row["义乌仓"]) + safe_float(row["东莞仓"])

    trend, daily_rate, fc10, fc30 = calc_trend_forecast(d10, d30, d60, d180)

    # ── 2. 调用 wf0 获取在途数据（含加权物流均值）─────────
    print(f"正在从 ERP 获取 {sku} 在途数据...\n")
    wf0   = analyze_sku(sku, verbose=False)
    token = get_erp_token()

    # 历史批次参考（近8笔）
    hist_orders = get_all_orders(sku, token)
    hist_qtys   = [get_order_detail_qty(o.get("id", ""), sku, token)
                   for o in hist_orders[:8]]
    hist_qtys   = [q for q in hist_qtys if q > 0]
    hist_avg    = round(sum(hist_qtys) / len(hist_qtys)) if hist_qtys else None
    hist_med    = round(statistics.median(hist_qtys))    if hist_qtys else None
    hist_min    = min(hist_qtys) if hist_qtys else None
    hist_max    = max(hist_qtys) if hist_qtys else None

    transit_batches  = wf0.get("transit_batches", [])
    avg_transit_days = wf0.get("avg_transit_days")
    fastest          = wf0.get("fastest_batch")
    total_transit    = wf0.get("total_transit_qty", 0)

    # ── 3. 模拟可售时间线（不含国内仓）──────────────────
    sellable_days, timeline, gaps = simulate_sellable_days(
        immediate, transfer_qty, transit_batches, daily_rate
    )

    immediate_days = (immediate / daily_rate) if daily_rate > 0 else 9999

    # 断货缺口分类
    gaps_during  = [g for g in gaps if g["type"] == "在途期间"]
    gaps_after   = [g for g in gaps if g["type"] == "在途全到后"]

    # ── 4. 补货决策时间点 ─────────────────────────────────
    if avg_transit_days and daily_rate > 0 and sellable_days < 9999:
        decision_days = sellable_days - avg_transit_days
        decision_date = TODAY + timedelta(days=max(0, int(decision_days)))
        decision_str  = (
            f"{decision_date}（{max(0,int(decision_days))}天后）"
            if decision_days > 0
            else "⚠️ 已过补货决策时间！"
        )
    else:
        decision_days = None
        decision_str = "库存充足，暂无需决策"

    # ── 5. 补货数量建议（周期性补货模型，每周三补货）──────
    # 周期性补货：目标管道 = (物流均值 + 审视周期) × 日均，保证下次发货窗口前不断货
    ORDER_CYCLE = 7   # 每周补货审视周期（天）
    if avg_transit_days and daily_rate > 0:
        pipeline_target  = (avg_transit_days + ORDER_CYCLE) * daily_rate
        current_pipeline = immediate + transfer_qty + total_transit + domestic_qty
        total_shortfall  = max(0, round(pipeline_target - current_pipeline))
        weekly_rate      = max(1, round(ORDER_CYCLE * daily_rate))

        # 建议批量：优先用历史中位数，无历史数据则退回每周销量
        batch_suggest = hist_med if hist_med else weekly_rate

        if total_shortfall <= 0:
            replenish_qty  = 0
            replenish_note = (
                f"目标管道={pipeline_target:.0f}件（({avg_transit_days}+{ORDER_CYCLE})天×{daily_rate:.2f}/天），"
                f"当前管道={current_pipeline:.0f}件"
            )
        else:
            replenish_qty  = batch_suggest
            batches_needed = -(-total_shortfall // batch_suggest)  # 向上取整
            replenish_note = (
                f"管道缺口{total_shortfall}件，按历史中位批量{batch_suggest}件下单，"
                f"共需约{batches_needed}批追平"
                f"（目标{pipeline_target:.0f}件，当前{current_pipeline:.0f}件）"
            )
    else:
        replenish_qty  = 0
        replenish_note = "数据不足，无法计算"

    # ── 6. 双维度风险判断 ──────────────────────────────────
    # 维度A：在途期间是否会断货（→ 运营策略）
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

    # 维度B：采购决策（基于周期性补货，每周三审视）
    # 紧迫度由决策窗口决定；补货量由周期公式给出（可以为0）
    if daily_rate == 0:
        status_buy = "⚪ 无销量，暂不采购"
    elif immediate == 0 and total_transit == 0:
        status_buy = f"🔴 本周立即采购 {replenish_qty}件（零库存）"
    elif avg_transit_days and decision_days is not None and decision_days <= 0:
        status_buy = f"🔴 本周立即采购 {replenish_qty}件（已过补货窗口）"
    elif avg_transit_days and decision_days is not None and decision_days < ORDER_CYCLE * 2:
        # 窗口不足2个审视周期 → 本周必须下单，否则来不及
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

    # ── 8. 输出报告 ────────────────────────────────────────
    sep = "═" * 58
    print(sep)
    print(f"  SKU: {sku}   {TODAY}")
    print(sep)

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
        print(f"  物流均值  : {avg_transit_days}天（近3笔跨SKU加权）")
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


if __name__ == "__main__":
    # 支持多SKU：同一进程内共享货代均值缓存，超期修正结果跨SKU生效
    skus = sys.argv[1:] if len(sys.argv) > 1 else ["TBJ0057A"]
    for sku in skus:
        analyze_wf3(sku)

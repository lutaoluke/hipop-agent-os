"""
工作流五：销售周期与补货分析（按销售主体物理切分）

每个 sales_entity 独立分析，输出到独立表：
  wf5_<alias>_sales_cycle      销售周期 + 补货建议
  wf6_<alias>_replenishment_queue  丢货必补队列（消费来源）

读：
  - wf2_<alias>_sku                 销量(10/30/60/180) + 利润率（noon orders 实时聚合）
  - wf3_logistics_hub               在途数据（按国别过滤 groups_json）
  - wf6_<alias>_replenishment_queue 丢货必补
  - wf1_<alias>_stock               库存 (per-entity, 已切换, 不再从 sa_main 读)

写：
  - wf5_<alias>_sales_cycle         per-SKU 销售周期 + 补货建议（合并丢货）

跑完默认自动 sync 到飞书子表 3「经营决策」（per-entity）。

CLI:
  python3 wf_sales_cycle.py
  python3 wf_sales_cycle.py --entities hipop_ksa
  python3 wf_sales_cycle.py --entities hipop_ksa --skus TBB0116A
  python3 wf_sales_cycle.py --no-sync
"""
import os, sys, json, sqlite3, argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from sales_entity import (load_entities, ensure_tables,
                          sku_table, sales_cycle_table, replenish_queue_table,
                          stock_table)

DB = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
ORDER_CYCLE = 7  # 每周补货审视周期

# entity.country (SA/AE) → wf3_logistics_hub.groups_json.country (KSA/UAE)
COUNTRY_HUB_KEY = {"SA": "KSA", "AE": "UAE"}


# ── 工具 ──────────────────────────────────────────────
def safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "0.0", "0") else default
    except Exception:
        return default


def calc_trend_forecast(d10, d30, d60, d180):
    """六态趋势 + 日均 + 预测10天/30天

    规则:
    - d30 == 0     → 无销量
    - d30 < 5      → 样本太小（30 天 ≤ 5 件），不走 momentum/急速下降判定，直接平稳路径
                     （否则 r10/r30 比值放大噪音，把 30 天卖 1-3 件的低销品误判为"加速增长"
                     造成全量补货建议噪音；2026-05-08 修）
    - 其他          → 六态趋势
    """
    r10 = d10 / 10 if d10 > 0 else 0
    r30 = d30 / 30 if d30 > 0 else 0
    r60 = d60 / 60 if d60 > 0 else 0
    if d30 == 0:
        return "无销量", 0.0, 0, 0
    if d30 < 5:
        # 低销噪音区间 — 用 r30 当 daily，趋势归"低销平稳"，下游 slow_mover 拦下来
        return "低销", r30, round(r30 * 10), round(r30 * 30)
    short_accel = r10 > r30 * 1.3
    if short_accel and r30 >= r60 * 0.9:
        momentum = r10 / r30
        daily = r10 * momentum
        trend = "加速增长"
    elif r30 > r60 * 1.15:
        daily = r10 if r10 > 0 else r30
        trend = "增长"
    elif r30 < r60 * 0.7:
        daily = max(r30, r60 * 0.7)
        trend = "急速下降"
    elif r30 < r60 * 0.85:
        daily = r30
        trend = "下降"
    elif short_accel:
        daily = r60
        trend = "波动"
    else:
        daily = r30
        trend = "平稳"
    return trend, daily, round(daily * 10), round(daily * 30)


# ── 数据读取 ────────────────────────────────────────────
def read_sales(partner_sku, alias, conn):
    """从 wf2_<alias>_sku 读销量+利润率, 从 wf1_<alias>_stock 读库存. 不再走 sa_main."""
    sku_tbl = sku_table(alias)
    row = conn.execute(f"""
        SELECT sales_10d, sales_30d, sales_60d, sales_180d, latest_profit_rate
        FROM {sku_tbl} WHERE partner_sku = ?
    """, (partner_sku,)).fetchone()
    if not row:
        return None
    s10, s30, s60, s180, profit = row

    # 1) 优先从 wf1_<alias>_stock 读 (per-entity, 跟 wf2 对齐)
    immediate = transfer = domestic = 0.0
    stock_tbl = stock_table(alias)
    try:
        st_row = conn.execute(f"""
            SELECT noon_saleable_qty, pending_inbound_qty, overseas_total_qty,
                   yiwu_qty, dongguan_qty
            FROM {stock_tbl} WHERE partner_sku = ?
        """, (partner_sku,)).fetchone()
    except sqlite3.OperationalError:
        st_row = None
    if st_row:
        immediate = safe_float(st_row[0]) + safe_float(st_row[1])
        transfer = safe_float(st_row[2])
        domestic = safe_float(st_row[3]) + safe_float(st_row[4])
    # 注: 已彻底切换到 wf1_<alias>_stock, 不再走 sa_main (KSA-only 老表).
    # 若 wf1 漏 SKU, current_pipeline=0 是 ground truth, 应补全 wf1 ingest (不靠 sa_main 兜底掩盖)

    return {
        "d10":         safe_float(s10),
        "d30":         safe_float(s30),
        "d60":         safe_float(s60),
        "d180":        safe_float(s180),
        "immediate":   immediate,
        "transfer":    transfer,
        "domestic":    domestic,
        "profit_rate": safe_float(profit),
    }


def read_hub(partner_sku, country, conn):
    """按 entity 国别过滤 groups_json. 返回 in-transit 批次明细 + 历史批量 (供 v1 综合算法用)."""
    hub_country = COUNTRY_HUB_KEY.get(country, country)
    row = conn.execute(
        "SELECT in_transit_total_qty, groups_json FROM wf3_logistics_hub WHERE sku=?",
        (partner_sku,)
    ).fetchone()
    if not row:
        return None
    _total_all, gj = row
    all_groups = json.loads(gj or "[]")
    groups = [g for g in all_groups if g.get("country") == hub_country]

    country_transit = sum(g.get("in_transit_qty", 0) for g in groups)
    # 历史均值: 凡是有 completed_avg_total_days 的 forwarder 都用 (不要求当前在途>0,
    # 否则 "无在途 SKU" 会拿不到 lead_time, 下游 target 算不出)
    fw_avgs = [g["completed_avg_total_days"] for g in groups
               if g.get("completed_avg_total_days")]
    avg_transit = round(sum(fw_avgs) / len(fw_avgs)) if fw_avgs else None

    # ── v1 综合算法所需: 在途批次明细 + 历史批量 ──
    transit_batches = []
    hist_qtys = []
    today = datetime.now().date()
    for g in groups:
        fw = g.get("forwarder", "?")
        for b in g.get("in_transit_batches") or []:
            qty = b.get("qty", 0)
            if not qty:
                continue
            hist_qtys.append(qty)
            # ETA 估算: 用 forwarder 的 completed_avg_total_days - 已发货天数
            #   - 已到海外仓 → remaining=0 (立即可售, 但需走最后入仓时间, 简化为 0)
            #   - 否则 max(0, fw_avg - days_since_delivery)
            stage = b.get("current_stage")
            if stage == "海外仓":
                remaining = 0
            else:
                fw_avg = g.get("completed_avg_total_days") or avg_transit or 30
                delivery_at = b.get("delivery_at")
                try:
                    d = datetime.fromisoformat(delivery_at).date() if delivery_at else None
                    days_since = (today - d).days if d else 0
                except Exception:
                    days_since = 0
                remaining = max(0, fw_avg - days_since)
            transit_batches.append({
                "order_no": b.get("order_no"),
                "qty": qty,
                "remaining_days": remaining,
                "logistics": fw,
                "stage": stage,
                "is_stuck": b.get("is_stuck", False),
            })

    return {"total_transit_qty": country_transit,
            "avg_transit_days": avg_transit,
            "transit_batches": transit_batches,
            "hist_qtys": hist_qtys,
            "groups": groups}


# ── v1 综合算法: 时间线模拟, 找在途期间是否断货 ──────────────
TRANSFER_DAYS = 7  # 海外仓 → 平台仓 的入仓天数


def simulate_sellable_days(immediate, transfer_qty, transit_batches, daily_rate):
    """按事件 (各 batch ETA) 顺序消耗库存, 找出 gaps (断货空窗) 列表.
    返回: (sellable_days_total, timeline_lines, gaps_list)."""
    if daily_rate <= 0:
        return 9999, ["日均销量为0, 无法估算"], []

    events = [(0, immediate, "即时可售")]
    if transfer_qty > 0:
        events.append((TRANSFER_DAYS, transfer_qty, "腾挪可售"))
    for b in transit_batches:
        days = b.get("remaining_days")
        if days is not None and b["qty"] > 0:
            events.append((int(days), b["qty"], f"在途({b['logistics']})"))

    events.sort(key=lambda x: x[0])
    last_transit_day = events[-1][0] if len(events) > 1 else 0

    stock, day, details, gaps = 0, 0, [], []

    for event_day, qty, label in events:
        gap = event_day - day
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
                f"🔴 第{exhausted_day:.0f}天库存耗尽({gap_type}断货{shortage_days:.0f}天)"
                f" → [{label}] 第{event_day}天到货{qty}件补上"
            )
            stock = qty
        else:
            stock -= need
            details.append(f"  [{label}] 第{event_day}天到货{qty}件 (余{stock:.0f}件)")
            stock += qty
        day = event_day

    if stock > 0 and daily_rate > 0:
        final_days = day + stock / daily_rate
        details.append(f"  全部在途到货后, 可售至第{final_days:.0f}天")
    else:
        final_days = float(day)

    return final_days, details, gaps


def read_lost_qty(partner_sku, alias, conn):
    t = replenish_queue_table(alias)
    row = conn.execute(
        f"SELECT COALESCE(SUM(lost_qty), 0) FROM {t} WHERE partner_sku=? AND consumed_at IS NULL",
        (partner_sku,)
    ).fetchone()
    return int(row[0]) if row else 0


# ── 分析单 SKU（在某 entity 上下文下, v1 综合算法）────────────
def analyze_one(partner_sku, ent, conn):
    """v1 综合算法: 时间线模拟 + 双维度 status (运营/采购) + 历史批量驱动补货量"""
    alias   = ent["alias"]
    country = ent["country"]

    sales = read_sales(partner_sku, alias, conn)
    if not sales:
        return None
    hub = read_hub(partner_sku, country, conn) or {
        "total_transit_qty": 0, "avg_transit_days": None,
        "transit_batches": [], "hist_qtys": [], "groups": []
    }
    lost = read_lost_qty(partner_sku, alias, conn)

    trend, daily_rate, fc10, fc30 = calc_trend_forecast(
        sales["d10"], sales["d30"], sales["d60"], sales["d180"]
    )

    # ── 慢销定义 v2 (B+C 组合, 比裸 spec sales_30d<3 严密) ──
    # 三条件 AND: 短期销量低 AND 没 momentum 信号 AND 库存能撑过物流周期
    # 见 wf5 skill: "慢销定义 v2"; 之前裸 d30<3 反复出现"加速增长爆款被拦"+
    # "急速下降库存干涸不补"两类 bug, v2 自带 momentum 保护 + 断货保护.
    # 慢销门槛 d30<10（2026-05-08 从 d30<3 提高，配合 trend 低销分支）：
    # 30 天卖 < 10 件 = 月均 < 1 件/3 天，无补货价值；除非有真"加速增长"信号才补
    is_slow_d30 = sales["d30"] < 10
    is_growth_trend = trend in ("加速增长", "增长", "波动")
    low_margin = sales["profit_rate"] is not None and 0 < sales["profit_rate"] < 0.20
    margin = sales["profit_rate"] or 0

    immediate = sales["immediate"]
    transfer_qty = sales["transfer"]
    domestic_qty = sales["domestic"]
    total_transit = hub["total_transit_qty"]
    avg_transit_days = hub["avg_transit_days"]
    transit_batches = hub.get("transit_batches", [])
    hist_qtys = hub.get("hist_qtys", [])

    current_pipeline = immediate + transfer_qty + total_transit + domestic_qty
    # 库存安全判断: 必须有 avg_transit_days + daily_rate>0 + 撑过 lead+ORDER_CYCLE 才算安全
    # 任一条件不满足 → 不安全 → 不算 slow (= 默认补一批, 防断货)
    is_pipeline_safe = bool(
        avg_transit_days
        and daily_rate > 0
        and (current_pipeline / daily_rate) >= (avg_transit_days + ORDER_CYCLE)
    )
    slow_mover = is_slow_d30 and (not is_growth_trend) and is_pipeline_safe

    # === v1 步骤 1: 时间线模拟 — 找在途期间是否断货 (gaps) ===
    sellable_days, timeline, gaps = simulate_sellable_days(
        immediate, transfer_qty, transit_batches, daily_rate
    )
    gaps_during = [g for g in gaps if g["type"] == "在途期间"]

    # === v1 步骤 2: 决策窗口 ===
    if avg_transit_days and daily_rate > 0 and sellable_days < 9999:
        decision_days = sellable_days - avg_transit_days
    else:
        decision_days = None

    # === v1 步骤 3: 风险标签 ===
    if daily_rate <= 0:
        risk = "无"
    elif avg_transit_days and (immediate + transfer_qty) / daily_rate < avg_transit_days:
        risk = "在途断货"
    elif avg_transit_days and sellable_days < avg_transit_days + ORDER_CYCLE:
        risk = "到齐后断货"
    else:
        risk = "无"

    # === v1 步骤 4: 补货量 — hist_med 历史中位批量 + 多批追平管道缺口 ===
    if avg_transit_days and daily_rate > 0:
        pipeline_target = (avg_transit_days + ORDER_CYCLE) * daily_rate
        total_shortfall = max(0, round(pipeline_target - current_pipeline))
        weekly_rate = max(1, round(ORDER_CYCLE * daily_rate))
        # hist_med: 在途批次 qty 中位数 (代表常规批量), 没数据时用 7 天日销
        if hist_qtys:
            sorted_q = sorted(hist_qtys)
            hist_med = sorted_q[len(sorted_q) // 2]
        else:
            hist_med = None
        batch_suggest = hist_med if hist_med else weekly_rate

        if total_shortfall <= 0:
            replenish_qty = 0
            batches_needed = 0
            replenish_note = (
                f"目标管道 {pipeline_target:.0f} 件 "
                f"(({avg_transit_days}+{ORDER_CYCLE})×{daily_rate:.2f}), "
                f"当前 {current_pipeline:.0f}, 充足"
            )
        else:
            replenish_qty = batch_suggest
            batches_needed = max(1, -(-total_shortfall // batch_suggest))
            replenish_note = (
                f"管道缺口 {total_shortfall} 件, 按历史批量 {batch_suggest} 件下单, "
                f"约 {batches_needed} 批追平 (目标 {pipeline_target:.0f} / 当前 {current_pipeline:.0f})"
            )
    else:
        pipeline_target = 0
        total_shortfall = 0
        replenish_qty = 0
        hist_med = None
        batches_needed = 0
        replenish_note = "数据不足 (无历史物流均值或日销=0)"

    # 慢销/低利润 override
    if slow_mover:
        replenish_qty = 0
        replenish_note = (
            f"慢销品 (30 天仅 {sales['d30']:.0f} 件, 日均 {sales['d30']/30:.2f}), "
            "绝对销量过低, 建议评估 EOL, 暂不补货"
        )
    elif low_margin:
        replenish_qty = 0
        replenish_note = f"利润率 {margin:.0%} 偏低 (<20%), 暂缓补货, 建议调价提利润或 EOL"

    # === v1 步骤 5: 双维度 status ===
    # 维度 A: 运营策略
    if daily_rate == 0:
        status_ops = "⚪ 无销量"
    elif immediate == 0 and total_transit == 0:
        status_ops = "⛔ 零库存, 立即停止广告"
    elif gaps_during:
        total_gap = sum(g["gap_days"] for g in gaps_during)
        if total_gap == 0:
            status_ops = "🟡 库存极紧, 控流 / 适当调价保利润"
        else:
            status_ops = f"🔴 在途期间断货 {total_gap} 天, 立即调价控流 / 下架部分变体"
    elif trend in ("急速下降", "下降") and sellable_days > 180:
        status_ops = "⚠️ 滞销积压, 考虑降价促销或参加 Deal 清库"
    elif trend in ("加速增长", "增长"):
        status_ops = "🟢 销量上涨, 保持运营 / 可适当提价测试"
    else:
        status_ops = "🟢 正常运营, 维持现状"

    # 维度 B: 采购决策
    if slow_mover:
        status_buy = f"⚪ 慢销品 (30 天 {sales['d30']:.0f} 件), 暂不采购"
    elif low_margin:
        status_buy = f"⚪ 利润率 {margin:.0%} 偏低, 暂缓补货"
    elif daily_rate == 0:
        status_buy = "⚪ 无销量, 暂不采购"
    elif immediate == 0 and total_transit == 0:
        status_buy = f"🔴 本周立即采购 {replenish_qty} 件 (零库存)"
    elif avg_transit_days and decision_days is not None and decision_days <= 0:
        if replenish_qty > 0:
            status_buy = f"🔴 本周立即采购 {replenish_qty} 件 (已过补货窗口)"
        else:
            status_buy = "🟡 窗口已过但管道暂足, 本周密切关注 (下周再评估)"
    elif avg_transit_days and decision_days is not None and decision_days < ORDER_CYCLE * 2:
        action = f"采购 {replenish_qty} 件" if replenish_qty > 0 else "保持每周刷新, 下周按量采购"
        status_buy = f"🔴 本周必须下单: {action} (窗口仅剩 {int(decision_days)} 天)"
    elif avg_transit_days and sellable_days < avg_transit_days:
        status_buy = f"🔴 本周立即采购 {replenish_qty} 件 (可售不足一个物流周期)"
    elif avg_transit_days and sellable_days < avg_transit_days * 1.5:
        if replenish_qty > 0:
            status_buy = f"🟡 {int(decision_days)} 天内采购 {replenish_qty} 件"
        else:
            status_buy = f"🟡 窗口剩 {int(decision_days)} 天, 本周管道暂足"
    elif replenish_qty > 0:
        status_buy = f"🟢 {int(decision_days)} 天后采购约 {replenish_qty} 件"
    else:
        status_buy = "🟢 管道充足, 本周无需采购"

    # === 兼容老字段 (wf5_qty / weekly_total / urgency / advice / target) ===
    wf5_qty = replenish_qty  # 老字段名, 保留 ABI
    weekly_total = wf5_qty + lost
    target = int(pipeline_target) if pipeline_target else 0

    reasons = []
    if wf5_qty > 0: reasons.append("正常补货")
    if lost > 0: reasons.append("丢货补货")
    if slow_mover: reasons.append("慢销")
    if low_margin: reasons.append("低利润")
    if not reasons: reasons.append("正常补货")

    # urgency 分级 (映射 status_buy 颜色)
    if slow_mover or low_margin:
        urgency = "无需采购"
    elif "🔴" in status_buy or "⛔" in status_ops:
        urgency = "立即"
    elif "🟡" in status_buy:
        urgency = "本周"
    elif weekly_total == 0:
        urgency = "无需采购"
    else:
        urgency = "正常"

    # advice = 双维度 + replenish_note 拼接, 给前端 + 飞书 一段可读文本
    advice = f"[运营] {status_ops}  |  [采购] {status_buy}  |  {replenish_note}"
    if lost > 0:
        advice += f"  |  丢货必补 {lost} 件已计入本周"

    return {
        "partner_sku":            partner_sku,
        "trend":                  trend,
        "daily_rate":             round(daily_rate, 2),
        "forecast_10_days":       int(fc10),
        "forecast_30_days":       int(fc30),
        "risk_label":             risk,
        "current_pipeline":       int(current_pipeline),
        "target_pipeline":        int(target),
        "wf5_replenish_qty":      int(wf5_qty),
        "lost_replenish_qty":     int(lost),
        "weekly_total_replenish": int(weekly_total),
        "trigger_reasons":        reasons,
        "urgency":                urgency,
        "ops_advice":             advice,
        "week_tag":               datetime.now().strftime("%Y-W%V"),
        # ── v1 综合算法新增字段 ──
        "sellable_days":          round(sellable_days, 1) if sellable_days < 9999 else None,
        "decision_days":          int(decision_days) if decision_days is not None else None,
        "status_ops":             status_ops,
        "status_buy":             status_buy,
        "hist_med":               int(hist_med) if hist_med else None,
        "batches_needed":         int(batches_needed) if batches_needed else 0,
        "gaps_during_json":       json.dumps(gaps_during, ensure_ascii=False),
    }


# ── 写库 ─────────────────────────────────────────────────
_NEW_COLUMNS_ENSURED = set()


def _ensure_v1_columns(conn, alias):
    """v1 综合算法新增字段, 用 ALTER TABLE IF NOT EXISTS 风格自动加列."""
    t = sales_cycle_table(alias)
    if t in _NEW_COLUMNS_ENSURED:
        return
    new_cols = [
        ("sellable_days",   "REAL"),
        ("decision_days",   "INTEGER"),
        ("status_ops",      "TEXT"),
        ("status_buy",      "TEXT"),
        ("hist_med",        "INTEGER"),
        ("batches_needed",  "INTEGER"),
        ("gaps_during_json","TEXT"),
    ]
    cur = conn.execute(f"PRAGMA table_info({t})")
    existing = {r[1] for r in cur.fetchall()}
    for col, ctype in new_cols:
        if col not in existing:
            try:
                conn.execute(f'ALTER TABLE {t} ADD COLUMN {col} {ctype}')
            except sqlite3.OperationalError:
                pass  # 并发竞争
    conn.commit()
    _NEW_COLUMNS_ENSURED.add(t)


def write_record(rec, alias, conn):
    _ensure_v1_columns(conn, alias)
    t = sales_cycle_table(alias)
    conn.execute(f"""
        INSERT OR REPLACE INTO {t}
        (partner_sku, trend, daily_rate, forecast_10_days, forecast_30_days, risk_label,
         current_pipeline, target_pipeline, wf5_replenish_qty, lost_replenish_qty,
         weekly_total_replenish, trigger_reasons, urgency, ops_advice, week_tag, updated_at,
         sellable_days, decision_days, status_ops, status_buy, hist_med, batches_needed, gaps_during_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["partner_sku"], rec["trend"], rec["daily_rate"], rec["forecast_10_days"],
        rec["forecast_30_days"], rec["risk_label"], rec["current_pipeline"],
        rec["target_pipeline"], rec["wf5_replenish_qty"], rec["lost_replenish_qty"],
        rec["weekly_total_replenish"], json.dumps(rec["trigger_reasons"], ensure_ascii=False),
        rec["urgency"], rec["ops_advice"], rec["week_tag"],
        datetime.now().isoformat(timespec="seconds"),
        rec.get("sellable_days"), rec.get("decision_days"),
        rec.get("status_ops"), rec.get("status_buy"),
        rec.get("hist_med"), rec.get("batches_needed"),
        rec.get("gaps_during_json"),
    ))


# ── 主流程：按 entity 跑 ─────────────────────────────────
def analyze_entity(ent, skus=None, write_db=True, verbose=True, conn=None):
    alias = ent["alias"]
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB)
        ensure_tables(conn)

    if not skus:
        skus = [r[0] for r in conn.execute(f"SELECT partner_sku FROM {sku_table(alias)}").fetchall()]

    out = []
    for psk in skus:
        rec = analyze_one(psk, ent, conn)
        if not rec:
            if verbose: print(f"  [{alias}] ⚠️ {psk}: 无 wf2 数据，跳过")
            continue
        if write_db:
            write_record(rec, alias, conn)
        out.append(rec)
        if verbose:
            print(f"  [{alias}] ✓ {psk} | 趋势={rec['trend']} 日均={rec['daily_rate']} | "
                  f"必补={rec['weekly_total_replenish']}({rec['urgency']}) | 风险={rec['risk_label']}")

    if write_db:
        conn.commit()
    if own_conn:
        conn.close()
    return out


def run(entity_aliases=None, skus=None, write_db=True, verbose=True):
    entities = [e for e in load_entities()
                if not entity_aliases or e["alias"] in entity_aliases]
    if not entities:
        sys.exit("no matching sales_entities")

    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    all_results = {}
    for ent in entities:
        print(f"\n[entity {ent['alias']}] country={ent['country']} store={ent['store']}",
              file=sys.stderr)
        res = analyze_entity(ent, skus=skus, write_db=write_db, verbose=verbose, conn=conn)
        all_results[ent["alias"]] = res
        print(f"  → {len(res)} skus 写入 {sales_cycle_table(ent['alias'])}", file=sys.stderr)
    conn.close()
    return all_results


# 兼容入口 (被 weekly_run 等引用; v1 wf_daily 已删)
def analyze_skus(skus=None, write_db=True, verbose=True):
    """兼容老接口: 跑全部 sales_entities, 可选 SKU 过滤. 返回 list (合并各 entity 结果, 加 entity 字段)."""
    out = []
    for alias, recs in run(entity_aliases=None, skus=skus, write_db=write_db, verbose=verbose).items():
        for r in recs:
            r2 = dict(r); r2["entity"] = alias; r2["sku"] = r["partner_sku"]
            out.append(r2)
    return out


# ── CLI ────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=None, help="逗号分隔，例 hipop_ksa")
    ap.add_argument("--skus", nargs="*", help="指定 SKU（默认: 各 entity 全部）")
    ap.add_argument("--no-sync", action="store_true")
    args = ap.parse_args()

    aliases = args.entities.split(",") if args.entities else None
    results = run(entity_aliases=aliases, skus=args.skus or None, write_db=True, verbose=True)

    total = sum(len(v) for v in results.values())
    print(f"\n完成：共 {total} 个 SKU 写入各 wf5_<alias>_sales_cycle")
    for alias, recs in results.items():
        by_urg = {}
        for r in recs:
            by_urg[r["urgency"]] = by_urg.get(r["urgency"], 0) + 1
        print(f"  [{alias}] " + " ".join(f"{k}={v}" for k, v in by_urg.items()))

    if not args.no_sync and total > 0:
        try:
            from scripts.feishu_sync import sync_all
            print("\n→ 同步到飞书 (decisions 表)...")
            sync_all(tables=["decisions"], verbose=True)
        except Exception as e:
            print(f"  ⚠️ sync 失败: {e}")


if __name__ == "__main__":
    main()

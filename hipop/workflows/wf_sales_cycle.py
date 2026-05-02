"""
工作流五：销售周期与补货分析（按销售主体物理切分）

每个 sales_entity 独立分析，输出到独立表：
  wf5_<alias>_sales_cycle      销售周期 + 补货建议
  wf6_<alias>_replenishment_queue  丢货必补队列（消费来源）

读：
  - wf2_<alias>_sku                 销量(10/30/60/180) + 利润率（noon orders 实时聚合）
  - wf3_logistics_hub               在途数据（按国别过滤 groups_json）
  - wf6_<alias>_replenishment_queue 丢货必补
  - sa_main                         库存（暂时 fallback，等工作流一覆盖）

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
                          sku_table, sales_cycle_table, replenish_queue_table)

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
    """六态趋势 + 日均 + 预测10天/30天"""
    r10 = d10 / 10 if d10 > 0 else 0
    r30 = d30 / 30 if d30 > 0 else 0
    r60 = d60 / 60 if d60 > 0 else 0
    if d30 == 0:
        return "无销量", 0.0, 0, 0
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
    """从 wf2_<alias>_sku 读销量+利润率，从 sa_main 读库存（fallback）。"""
    sku_tbl = sku_table(alias)
    row = conn.execute(f"""
        SELECT sales_10d, sales_30d, sales_60d, sales_180d, latest_profit_rate
        FROM {sku_tbl} WHERE partner_sku = ?
    """, (partner_sku,)).fetchone()
    if not row:
        return None
    s10, s30, s60, s180, profit = row

    # 库存暂从 sa_main fallback；TODO: 工作流一做完后切到 wf2_<alias>_sku 库存字段
    immediate = transfer = domestic = 0.0
    sa_row = conn.execute('SELECT * FROM sa_main WHERE "ERP-SKU" = ?', (partner_sku,)).fetchone()
    if sa_row:
        keys = [d[0] for d in conn.execute('SELECT * FROM sa_main LIMIT 1').description]
        g = lambda k: sa_row[keys.index(k)] if k in keys else None
        immediate = safe_float(g("noon平台")) + safe_float(g("送仓未上架"))
        transfer = safe_float(g("海外仓可用库存"))
        domestic = safe_float(g("义乌仓")) + safe_float(g("东莞仓"))

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
    """按 entity 国别过滤 groups_json。"""
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
    fw_avgs = [g["completed_avg_total_days"] for g in groups
               if g.get("completed_avg_total_days") and g.get("in_transit_qty", 0) > 0]
    avg_transit = round(sum(fw_avgs) / len(fw_avgs)) if fw_avgs else None
    return {"total_transit_qty": country_transit,
            "avg_transit_days": avg_transit,
            "groups": groups}


def read_lost_qty(partner_sku, alias, conn):
    t = replenish_queue_table(alias)
    row = conn.execute(
        f"SELECT COALESCE(SUM(lost_qty), 0) FROM {t} WHERE partner_sku=? AND consumed_at IS NULL",
        (partner_sku,)
    ).fetchone()
    return int(row[0]) if row else 0


# ── 分析单 SKU（在某 entity 上下文下）────────────────────────
def analyze_one(partner_sku, ent, conn):
    alias   = ent["alias"]
    country = ent["country"]

    sales = read_sales(partner_sku, alias, conn)
    if not sales:
        return None
    hub = read_hub(partner_sku, country, conn) or {"total_transit_qty": 0, "avg_transit_days": None, "groups": []}
    lost = read_lost_qty(partner_sku, alias, conn)

    trend, daily_rate, fc10, fc30 = calc_trend_forecast(
        sales["d10"], sales["d30"], sales["d60"], sales["d180"]
    )

    is_slow = sales["d30"] < 3
    is_low_profit = sales["profit_rate"] is not None and 0 < sales["profit_rate"] < 0.20

    total_transit = hub["total_transit_qty"]
    avg_transit = hub["avg_transit_days"]

    current_pipeline = sales["immediate"] + sales["transfer"] + total_transit + sales["domestic"]
    sellable_days = (current_pipeline / daily_rate) if daily_rate > 0 else 9999

    if daily_rate <= 0:
        risk = "无"
    elif avg_transit and (sales["immediate"] + sales["transfer"]) / daily_rate < avg_transit:
        risk = "在途断货"
    elif avg_transit and sellable_days < avg_transit + ORDER_CYCLE:
        risk = "到齐后断货"
    else:
        risk = "无"

    if is_slow or is_low_profit or daily_rate <= 0:
        wf5_qty = 0
        target = 0
    elif avg_transit:
        target = round((avg_transit + ORDER_CYCLE) * daily_rate)
        wf5_qty = max(0, target - int(current_pipeline))
    else:
        target = 0
        wf5_qty = 0

    weekly_total = wf5_qty + lost

    reasons = []
    if wf5_qty > 0: reasons.append("正常补货")
    if lost > 0: reasons.append("丢货补货")
    if is_slow: reasons.append("慢销")
    if is_low_profit: reasons.append("低利润")
    if not reasons: reasons.append("正常补货")

    if weekly_total == 0:
        urgency = "无需采购"
    elif sales["immediate"] + total_transit == 0:
        urgency = "立即"
    elif risk == "在途断货":
        urgency = "立即"
    elif avg_transit and sellable_days < avg_transit:
        urgency = "立即"
    elif avg_transit and sellable_days < avg_transit * 1.5:
        urgency = "本周"
    else:
        urgency = "正常"

    advice_lines = []
    if is_slow:
        advice_lines.append("慢销品（30 天 < 3 件），暂不补货，建议评估 EOL")
    if is_low_profit:
        advice_lines.append(f"低利润 {sales['profit_rate']*100:.0f}%，建议先调价提利润")
    if risk == "在途断货":
        advice_lines.append("⚠️ 在途期间将断货，调价控流 / 下架部分变体")
    elif risk == "到齐后断货":
        advice_lines.append("⚠️ 在途到齐后仍不够卖，立即下单补货")
    if lost > 0:
        advice_lines.append(f"丢货必补 {lost} 件已计入本周")
    if not advice_lines:
        if trend in ("加速增长", "增长"):
            advice_lines.append("销量上涨，可考虑提价测试")
        else:
            advice_lines.append("正常运营")
    advice = " / ".join(advice_lines)

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
    }


# ── 写库 ─────────────────────────────────────────────────
def write_record(rec, alias, conn):
    t = sales_cycle_table(alias)
    conn.execute(f"""
        INSERT OR REPLACE INTO {t}
        (partner_sku, trend, daily_rate, forecast_10_days, forecast_30_days, risk_label,
         current_pipeline, target_pipeline, wf5_replenish_qty, lost_replenish_qty,
         weekly_total_replenish, trigger_reasons, urgency, ops_advice, week_tag, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["partner_sku"], rec["trend"], rec["daily_rate"], rec["forecast_10_days"],
        rec["forecast_30_days"], rec["risk_label"], rec["current_pipeline"],
        rec["target_pipeline"], rec["wf5_replenish_qty"], rec["lost_replenish_qty"],
        rec["weekly_total_replenish"], json.dumps(rec["trigger_reasons"], ensure_ascii=False),
        rec["urgency"], rec["ops_advice"], rec["week_tag"],
        datetime.now().isoformat(timespec="seconds"),
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


# 兼容老接口（被 weekly_run / wf_daily 等引用）
def analyze_skus(skus=None, write_db=True, verbose=True):
    """兼容老接口：跑全部 sales_entities，可选 SKU 过滤。返回 list（合并各 entity 结果，加 entity 字段）。"""
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

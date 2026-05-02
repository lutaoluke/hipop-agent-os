"""
人类指令查询助手——按 SKU / 类目 / 状态 / 紧急度直接捞数。

CLI 例子：
  python3 hipop_query.py sku TBB0116A           # 查一个 SKU 的全景
  python3 hipop_query.py sku TBB0116A --entity hipop_ksa
  python3 hipop_query.py top --entity hipop_ksa --n 10   # 销量榜
  python3 hipop_query.py replenish --entity hipop_ksa    # 本周必补
  python3 hipop_query.py alerts                          # 当前 active 物流告警
  python3 hipop_query.py stuck                           # 卡单 SKU
  python3 hipop_query.py low_stock --entity hipop_ksa    # noon 可售=0 但海外仓有货
  python3 hipop_query.py nostock --entity hipop_ksa      # 完全断货
"""
import os, sys, json, sqlite3, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import (load_entities, sku_table, orders_table,
                          sales_cycle_table, replenish_queue_table, stock_table)

DB = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _entity(alias):
    for e in load_entities():
        if e["alias"] == alias: return e
    sys.exit(f"unknown entity {alias}; available: {[e['alias'] for e in load_entities()]}")


def cmd_sku(args):
    """查单 SKU 在某 entity 下的全景（商品/销量/库存/补货/告警）。"""
    sku = args.sku
    aliases = [args.entity] if args.entity else [e["alias"] for e in load_entities()]
    conn = _conn()
    for alias in aliases:
        print(f"\n━━━━━━━━ {alias} ━━━━━━━━")
        # wf2 商品+销量
        r = conn.execute(f"SELECT * FROM {sku_table(alias)} WHERE partner_sku=?", (sku,)).fetchone()
        if r:
            print(f"商品：{r['title'] or '(无 title)'}")
            print(f"  product_id={r['product_id']}  noon_sku={r['noon_sku']}  brand={r['brand']}")
            print(f"  cost={r['cost_price']}  最新价={r['latest_price']} {r['currency']}  利润率={r['latest_profit_rate']}")
            print(f"  销量 10/30/60/90/120/180d = {r['sales_10d']}/{r['sales_30d']}/{r['sales_60d']}/{r['sales_90d']}/{r['sales_120d']}/{r['sales_180d']}")
            print(f"  total_orders={r['total_orders']}  cancel_rate={r['cancel_rate']}  return_rate={r['return_rate']}")
            print(f"  评级={r['sales_grade']}  is_listed={r['is_listed']}  最新出单={r['latest_order_date']}")
            if r["anomalies_json"]:
                print(f"  ⚠️ 异常: {r['anomalies_json']}")
        else:
            print(f"商品：(wf2 表无该 SKU)")
        # wf1 库存
        r1 = conn.execute(f"SELECT * FROM {stock_table(alias)} WHERE partner_sku=?", (sku,)).fetchone()
        if r1:
            print(f"库存：noon={r1['noon_total_qty']}({r1['noon_saleable_qty']} 可售) "
                  f"海外={r1['overseas_total_qty']} 义乌={r1['yiwu_qty']} 东莞={r1['dongguan_qty']} "
                  f"合计={r1['total_stock']}")
            if r1["overseas_breakdown_json"]:
                print(f"  海外仓明细: {r1['overseas_breakdown_json']}")
        # wf5 销售周期
        r5 = conn.execute(f"SELECT * FROM {sales_cycle_table(alias)} WHERE partner_sku=?", (sku,)).fetchone()
        if r5:
            print(f"销售周期：趋势={r5['trend']} 日均={r5['daily_rate']} 风险={r5['risk_label']} "
                  f"必补={r5['weekly_total_replenish']}({r5['urgency']})")
            if r5["ops_advice"]: print(f"  建议: {r5['ops_advice']}")
        # wf6 丢货
        rq = conn.execute(f"SELECT SUM(lost_qty) FROM {replenish_queue_table(alias)} WHERE partner_sku=? AND consumed_at IS NULL", (sku,)).fetchone()
        if rq and rq[0]:
            print(f"  丢货必补未消费: {rq[0]} 件")
    # 物流 hub（全局）
    r3 = conn.execute("SELECT in_transit_total_qty, has_stuck_batch, needs_ops_input, groups_json FROM wf3_logistics_hub WHERE sku=?", (sku,)).fetchone()
    if r3:
        print(f"\n━━━━━━━━ 物流（hub）━━━━━━━━")
        print(f"在途总量={r3['in_transit_total_qty']}  卡单={r3['has_stuck_batch']}  待运营={r3['needs_ops_input']}")
        for g in json.loads(r3["groups_json"] or "[]"):
            print(f"  [{g.get('country')}/{g.get('forwarder')}] 在途={g.get('in_transit_qty')}  历史均值={g.get('completed_avg_total_days')}d")
    # 物流告警
    alerts = conn.execute("""
        SELECT alert_id, alert_level, alert_reason, ops_status FROM wf6_logistics_alerts
        WHERE sku_list_json LIKE ? AND resolved_at IS NULL
    """, (f'%"{sku}"%',)).fetchall()
    if alerts:
        print(f"\n━━━━━━━━ 当前告警 ━━━━━━━━")
        for a in alerts:
            print(f"  #{a['alert_id']} [{a['alert_level']}] {a['alert_reason']} (ops={a['ops_status']})")
    conn.close()


def cmd_top(args):
    """销量 Top N（按 sales_30d）"""
    ent = _entity(args.entity)
    conn = _conn()
    print(f"━━ {ent['alias']} 销量 Top {args.n}（30d）━━")
    for r in conn.execute(f"""
        SELECT partner_sku, title, sales_30d, sales_180d, sales_grade, latest_profit_rate
        FROM {sku_table(ent['alias'])} WHERE sales_30d > 0
        ORDER BY sales_30d DESC LIMIT ?
    """, (args.n,)).fetchall():
        print(f"  {r['partner_sku']:15s} {(r['title'] or '')[:25]:25s}  "
              f"30d={r['sales_30d']:4d} 180d={r['sales_180d']:5d} {r['sales_grade']} 利润={r['latest_profit_rate']}")
    conn.close()


def cmd_replenish(args):
    """本周必补（按紧急度排序）"""
    ent = _entity(args.entity)
    conn = _conn()
    print(f"━━ {ent['alias']} 本周必补 ━━")
    for r in conn.execute(f"""
        SELECT partner_sku, weekly_total_replenish, urgency, ops_advice
        FROM {sales_cycle_table(ent['alias'])} WHERE weekly_total_replenish > 0
        ORDER BY CASE urgency WHEN '立即' THEN 1 WHEN '本周' THEN 2 ELSE 3 END
    """).fetchall():
        print(f"  {r['partner_sku']:15s} × {r['weekly_total_replenish']:4d} 件 ({r['urgency']})")
        if r["ops_advice"]: print(f"    {r['ops_advice']}")
    conn.close()


def cmd_alerts(args):
    """当前 active 物流告警"""
    conn = _conn()
    rows = conn.execute("""
        SELECT alert_id, alert_level, order_no, forwarder, alert_reason, actual_stay_days, ops_status
        FROM wf6_logistics_alerts WHERE resolved_at IS NULL
        ORDER BY CASE alert_level WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 ELSE 4 END
    """).fetchall()
    print(f"━━ 当前 active 告警 ({len(rows)}) ━━")
    for r in rows:
        print(f"  #{r['alert_id']} [{r['alert_level']}] {r['order_no']} | {r['forwarder']} | {r['alert_reason']} "
              f"(停留 {r['actual_stay_days']}d, ops={r['ops_status'] or '未处理'})")
    conn.close()


def cmd_stuck(args):
    """卡单 SKU"""
    conn = _conn()
    rows = conn.execute("SELECT sku FROM wf3_logistics_hub WHERE has_stuck_batch=1").fetchall()
    print(f"━━ 卡单 SKU ({len(rows)}) ━━")
    for r in rows: print(f"  {r['sku']}")
    conn.close()


def cmd_low_stock(args):
    """noon 可售=0 但海外仓有货（需补 noon FBN）"""
    ent = _entity(args.entity)
    conn = _conn()
    rows = conn.execute(f"""
        SELECT partner_sku, title, noon_saleable_qty, overseas_total_qty
        FROM {stock_table(ent['alias'])}
        WHERE COALESCE(noon_saleable_qty,0)=0 AND COALESCE(overseas_total_qty,0)>0
        ORDER BY overseas_total_qty DESC
    """).fetchall()
    print(f"━━ {ent['alias']} noon 缺货但海外仓有 ({len(rows)}) ━━")
    for r in rows:
        print(f"  {r['partner_sku']:15s} {(r['title'] or '')[:25]:25s} 海外仓={r['overseas_total_qty']}")
    conn.close()


def cmd_nostock(args):
    """完全断货：noon=0 + 海外仓=0"""
    ent = _entity(args.entity)
    conn = _conn()
    rows = conn.execute(f"""
        SELECT s.partner_sku, s.title, s.yiwu_qty, s.dongguan_qty, k.sales_30d, k.is_listed
        FROM {stock_table(ent['alias'])} s
        LEFT JOIN {sku_table(ent['alias'])} k ON s.partner_sku = k.partner_sku
        WHERE COALESCE(s.noon_saleable_qty,0)=0 AND COALESCE(s.overseas_total_qty,0)=0
          AND k.is_listed = 1
        ORDER BY k.sales_30d DESC NULLS LAST
    """).fetchall()
    print(f"━━ {ent['alias']} 完全断货且在卖 ({len(rows)}) ━━")
    for r in rows:
        print(f"  {r['partner_sku']:15s} 30d销量={r['sales_30d']} 国内仓={r['yiwu_qty']}/{r['dongguan_qty']}")
    conn.close()


COMMANDS = {
    "sku": cmd_sku, "top": cmd_top, "replenish": cmd_replenish,
    "alerts": cmd_alerts, "stuck": cmd_stuck,
    "low_stock": cmd_low_stock, "nostock": cmd_nostock,
}


def main():
    ap = argparse.ArgumentParser(description="hipop 查询助手")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sku = sub.add_parser("sku", help="单 SKU 全景")
    p_sku.add_argument("sku")
    p_sku.add_argument("--entity", default=None)

    p_top = sub.add_parser("top", help="销量 Top N")
    p_top.add_argument("--entity", required=True)
    p_top.add_argument("--n", type=int, default=10)

    for name in ["replenish", "low_stock", "nostock"]:
        p = sub.add_parser(name)
        p.add_argument("--entity", required=True)

    sub.add_parser("alerts", help="当前 active 物流告警")
    sub.add_parser("stuck",  help="卡单 SKU")

    args = ap.parse_args()
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()

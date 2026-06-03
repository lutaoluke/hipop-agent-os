"""
工作流二：每个销售主体独立聚合 / 异常检测 / 评级 / 预测。

对 config 里每个 sales_entity，扫它对应的 wf2_<alias>_sku + wf2_<alias>_orders：
  1. 从 orders 重算 noon 视角的 total/valid/cancel/return（覆盖 ERP 数据）
  2. noon vs ERP 差异写到 anomalies_json
  3. 评级 ABCD（sales_grading 确定性算法，消费销量趋势 + 销售净值）
  4. 预测 10/30 天（sales_grading：多窗口混合日均 × 趋势倍率）
  5. is_listed 由 ingest_erp_products 决定（= 是否绑定 noon platform_sku_id），本脚本不再覆写
  6. 收集订单号集合 → order_item_nrs_json

CLI:
  python3 wf_sales_static.py
  python3 wf_sales_static.py --entities hipop_ksa
  python3 wf_sales_static.py --sku TBB0116A
"""
import os, sys, json, sqlite3, argparse
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from sales_entity import load_entities, ensure_tables, sku_table, orders_table

# 评级 ABCD / 10-30 天预测的确定性算法抽到 sales_grading（可配置、供两条
# 生产路径与看板/导出复用）。这里 re-export，保持 wf_sales_static_v2 的 import 不变。
# 兼容包路径（hipop.workflows.*）与直跑（sys.path）两种 import 上下文。
try:
    from hipop.workflows.sales_grading import grade_sku, forecast, compute_metrics  # noqa: F401
except ModuleNotFoundError:
    from sales_grading import grade_sku, forecast, compute_metrics  # type: ignore  # noqa: F401

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")


# 价格差异判异阈值：相对差 >5% 且绝对差 >1 才算 price_mismatch。
PRICE_MISMATCH_REL = 0.05
PRICE_MISMATCH_ABS = 1.0


def detect_anomalies(rec, noon_view):
    """
    noon vs ERP 异常检测（确定性规则，纯函数，供两条生产路径复用）。

    异常分三类，全部写成**结构化字段**而非自由文案，便于看板/导出/下游消费：
      1. no_noon_orders —— ERP 显示有动销（sales_180d>0）但 noon CSV 无该 SKU 订单。
                           说明 noon 导出不全或 SKU 已下架。
      2. price_mismatch —— noon 最新价 vs ERP 最新价相对差 >5%（且绝对差 >1）。
      3. noon_only      —— noon CSV 里有该 SKU 订单，但 ERP 商品库无对应记录
                           （erp_sku_id 为空）→ 漏建档 / 选品外采，需人工确认。

    每条异常统一带：type / field / noon / erp / diff / source_window，
    （销量窗口口径差异默认不比：ERP sales_* 是累计含义，与 noon 时段窗口本不可比；
      若业务确认要比，须另起独立规则 + smoke，见 issue WS-18，不在此偷偷加。）
    """
    anomalies = []

    # —— 1. noon 无该 SKU 订单 ——
    if not noon_view:
        erp_sales = rec.get("sales_180d") or 0
        if erp_sales > 0:
            anomalies.append({
                "type": "no_noon_orders",
                "field": "sales_180d",
                "noon": 0,
                "erp": erp_sales,
                "diff": erp_sales,
                "source_window": "sales_180d",
                "note": "ERP 显示有动销但 noon CSV 中无订单，建议补 noon 导出",
            })
        return anomalies

    # —— 2. noon 有订单但 ERP 商品库无对应记录（noon-only）——
    if not rec.get("erp_sku_id"):
        n_orders = noon_view.get("total_orders") or 0
        anomalies.append({
            "type": "noon_only",
            "field": "erp_sku_id",
            "noon": n_orders,
            "erp": None,
            "diff": None,
            "source_window": "all_orders",
            "note": "noon 有订单但 ERP 商品库无此 SKU，建议补建档或确认外采",
        })

    # —— 3. noon 价 vs ERP 价差异 ——
    e_price = rec.get("latest_price")
    n_price = noon_view.get("latest_price")
    if e_price and n_price:
        diff = abs(e_price - n_price)
        base = max(abs(e_price), abs(n_price), 1)
        if diff > PRICE_MISMATCH_ABS and diff / base > PRICE_MISMATCH_REL:
            anomalies.append({
                "type": "price_mismatch",
                "field": "latest_price",
                "noon": n_price,
                "erp": e_price,
                "diff": round(diff, 4),
                "source_window": "latest_order",
            })
    return anomalies


def noon_view_for_sku(cur, ord_table, partner_sku):
    """
    从订单明细表算 noon 视角的所有聚合字段。
    时间窗以"运行日（now）"为基准向前推：sales_<N>d = 过去 N 天 非 cancelled 订单数。
    每周新增 CSV 累加（item_nr 去重），180 天前的订单自然滑出窗口。
    """
    cur.execute(f"""
        SELECT
          COUNT(*) AS total_orders,
          SUM(CASE WHEN is_cancelled = 0 THEN 1 ELSE 0 END) AS valid_orders,
          SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) AS cancel_count,
          SUM(CASE WHEN is_return    = 1 THEN 1 ELSE 0 END) AS return_count,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-10 days')  THEN 1 ELSE 0 END) AS sales_10d,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-30 days')  THEN 1 ELSE 0 END) AS sales_30d,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-60 days')  THEN 1 ELSE 0 END) AS sales_60d,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-90 days')  THEN 1 ELSE 0 END) AS sales_90d,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-120 days') THEN 1 ELSE 0 END) AS sales_120d,
          SUM(CASE WHEN is_cancelled = 0 AND order_date >= DATE('now','-180 days') THEN 1 ELSE 0 END) AS sales_180d,
          MAX(order_date) AS latest_order_date,
          (SELECT seller_price FROM {ord_table}
            WHERE partner_sku=? ORDER BY order_date DESC LIMIT 1)  AS latest_price,
          AVG(seller_price)  AS avg_price,
          SUM(customer_paid) AS total_revenue
        FROM {ord_table}
        WHERE partner_sku=?
    """, (partner_sku, partner_sku))
    row = cur.fetchone()
    if not row or row[0] == 0:
        return None
    keys = ["total_orders", "valid_orders", "cancel_count", "return_count",
            "sales_10d", "sales_30d", "sales_60d", "sales_90d", "sales_120d", "sales_180d",
            "latest_order_date", "latest_price", "avg_price", "total_revenue"]
    v = dict(zip(keys, row))
    if v["total_orders"]:
        v["cancel_rate"] = (v["cancel_count"] or 0) / v["total_orders"]
    if v["valid_orders"]:
        v["return_rate"] = (v["return_count"] or 0) / v["valid_orders"]
    return v


def order_item_nrs(cur, ord_table, partner_sku):
    cur.execute(f"""
        SELECT item_nr FROM {ord_table}
        WHERE partner_sku=? ORDER BY order_date DESC
    """, (partner_sku,))
    return [r[0] for r in cur.fetchall()]


def run_entity(conn, ent, only_sku=None):
    alias    = ent["alias"]
    sku_tbl  = sku_table(alias)
    ord_tbl  = orders_table(alias)

    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if only_sku:
        cur.execute(f"SELECT * FROM {sku_tbl} WHERE partner_sku = ?", (only_sku,))
    else:
        cur.execute(f"SELECT * FROM {sku_tbl}")
    rows = [dict(r) for r in cur.fetchall()]
    conn.row_factory = None

    upd_cur = conn.cursor()
    sub_cur = conn.cursor()
    n_updated = 0
    n_anomalous = 0
    n_no_noon = 0
    for rec in rows:
        partner_sku = rec["partner_sku"]
        noon_v = noon_view_for_sku(sub_cur, ord_tbl, partner_sku)
        anomalies = detect_anomalies(rec, noon_v or {})
        if anomalies:
            n_anomalous += 1
        if not noon_v and (rec.get("sales_180d") or 0) > 0:
            n_no_noon += 1

        merged = dict(rec)
        if noon_v:
            # noon 优先：覆盖 total/valid/cancel/return + 各时间窗销量 + 价格
            for k in ["total_orders", "valid_orders", "cancel_count", "return_count",
                      "cancel_rate", "return_rate",
                      "sales_10d", "sales_30d", "sales_60d", "sales_90d",
                      "sales_120d", "sales_180d",
                      "latest_price", "avg_price", "total_revenue", "latest_order_date"]:
                if noon_v.get(k) is not None:
                    merged[k] = noon_v[k]
        # 没 noon 时各 sales_<n>d 保持 ingest_erp_sales 写入的 ERP 值（兜底）

        # latest_profit_rate fallback：ERP 没给但 latest_price + cost_price 都在 → 自己算
        if (not merged.get("latest_profit_rate")
                and merged.get("latest_price") and merged.get("cost_price")):
            try:
                lp = float(merged["latest_price"])
                cp = float(merged["cost_price"])
                if lp > 0:
                    merged["latest_profit_rate"] = round((lp - cp) / lp, 4)
            except (TypeError, ValueError):
                pass

        grade = grade_sku(merged)
        fc = forecast(merged)
        item_nrs = order_item_nrs(sub_cur, ord_tbl, partner_sku)
        # is_listed 由 ingest_erp_products 写入（= 是否绑定 noon platform_sku_id），
        # 这里不再覆盖；wf2_<alias>_sku 全表含 1418 条 SKU，has noon binding=1 的为已上架。

        upd_cur.execute(f"""
            UPDATE {sku_tbl}
            SET total_orders   = ?,
                valid_orders   = ?,
                cancel_count   = ?,
                return_count   = ?,
                cancel_rate    = ?,
                return_rate    = ?,
                sales_10d      = COALESCE(?, sales_10d),
                sales_30d      = COALESCE(?, sales_30d),
                sales_60d      = COALESCE(?, sales_60d),
                sales_90d      = COALESCE(?, sales_90d),
                sales_120d     = COALESCE(?, sales_120d),
                sales_180d     = COALESCE(?, sales_180d),
                latest_price   = ?,
                avg_price      = ?,
                latest_profit_rate = COALESCE(?, latest_profit_rate),
                total_revenue  = ?,
                latest_order_date = ?,
                sales_grade    = ?,
                forecast_10d   = ?,
                forecast_30d   = ?,
                anomalies_json = ?,
                order_item_nrs_json = ?
            WHERE partner_sku=?
        """, (
            merged.get("total_orders"), merged.get("valid_orders"),
            merged.get("cancel_count"), merged.get("return_count"),
            merged.get("cancel_rate"),  merged.get("return_rate"),
            merged.get("sales_10d"), merged.get("sales_30d"), merged.get("sales_60d"),
            merged.get("sales_90d"), merged.get("sales_120d"), merged.get("sales_180d"),
            merged.get("latest_price"), merged.get("avg_price"),
            merged.get("latest_profit_rate"),
            merged.get("total_revenue"), merged.get("latest_order_date"),
            grade, fc["forecast_10d"], fc["forecast_30d"],
            json.dumps(anomalies, ensure_ascii=False) if anomalies else None,
            json.dumps(item_nrs, ensure_ascii=False) if item_nrs else None,
            partner_sku,
        ))
        n_updated += 1

    conn.commit()
    print(f"[{alias}] updated {n_updated} sku rows, {n_anomalous} with anomalies "
          f"({n_no_noon} of which 'no_noon_orders')",
          file=sys.stderr)


def run(entity_aliases=None, only_sku=None):
    entities = [e for e in load_entities()
                if not entity_aliases or e["alias"] in entity_aliases]
    if not entities:
        sys.exit("no matching sales_entities")

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    for ent in entities:
        run_entity(conn, ent, only_sku=only_sku)
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=None, help="逗号分隔，例 hipop_ksa")
    ap.add_argument("--sku", default=None)
    args = ap.parse_args()
    run(
        entity_aliases=args.entities.split(",") if args.entities else None,
        only_sku=args.sku,
    )

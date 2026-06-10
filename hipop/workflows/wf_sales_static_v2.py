"""工作流二 · v2 列存版的 noon 销量合并（merge）。

背景 / 为什么存在
-----------------
v2 多租户改造把 `wf2_<alias>_sku` 物理切表合成了单表 `wf2_sku`
(tenant_id, entity_alias 列存)。noon CSV 入库走 `ingest_noon_csv_v2.process_csv_v2`，
窗口销量走 `ingest_noon_csv_v2.aggregate_sales_v2`。

但 aggregate_sales_v2 **只**重算了 sales_10/30/60/90/120/180d 六个窗口，
下面这些"销量录入数据契约"字段在 v2 路径里**没有任何入口去写**——
schema 建了列，却没人算（典型"占位假数据 / 死代码"）：

    latest_customer_paid   最新成交价（最近一单 noon 实付）
    order_item_nrs_json    订单号 item_nr 集合
    anomalies_json         noon vs ERP 异常（价格不符 / 无 noon 订单）
    total_orders / valid_orders / cancel_count / return_count
    cancel_rate / return_rate
    avg_price / latest_price（noon 视角）/ total_revenue / latest_order_date
    sales_grade / forecast_10d / forecast_30d

本模块补齐这一步：从 `wf2_orders` 按 (tenant_id, entity_alias, partner_sku)
重算 noon 视角字段并 merge 回 `wf2_sku`。语义与老的物理切表版
`wf_sales_static.run_entity` 对齐，新增 latest_customer_paid。

调用点（避免"接线缺失"死法）：
    api._run_pipeline_v2 在 aggregate_sales_v2 之后调 merge_entity_v2，
    所以 noon CSV 一上传，契约字段就被算出来。

SQL 用 `?` 占位 + 无 SQLite 专有函数（不用 DATE('now',...)），
data.conn() 的 PG 包装层会把 ? → %s，两边都能跑。
"""
from __future__ import annotations

import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                   # 让 sibling wf_sales_static 可直接 import
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))    # wf_sales_static 依赖 sales_entity

# 复用老版的纯函数（无 DB 依赖）：评级 / 预测 / 异常检测。
# 兼容两种 import 上下文：包路径（hipop.workflows.*）/ 直跑（sys.path）。
try:
    from hipop.workflows.wf_sales_static import grade_sku, forecast, detect_anomalies
except ModuleNotFoundError:
    from wf_sales_static import grade_sku, forecast, detect_anomalies  # type: ignore


def _val(row, idx, key):
    """sqlite Row 用 row[idx]，PG RealDictRow 用 row[key]。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def _noon_view(conn, tenant_id, entity_alias, partner_sku):
    """从 wf2_orders 算该 SKU 的 noon 视角聚合（时间窗无关字段）。

    sales_10/30/.../180d 由 aggregate_sales_v2 负责，这里不重复算窗口。
    """
    row = conn.execute(
        """
        SELECT
          COUNT(*)                                                AS total_orders,
          SUM(CASE WHEN is_cancelled = 0 THEN 1 ELSE 0 END)       AS valid_orders,
          SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END)       AS cancel_count,
          SUM(CASE WHEN is_return    = 1 THEN 1 ELSE 0 END)       AS return_count,
          MAX(order_date)                                         AS latest_order_date,
          AVG(seller_price)                                       AS avg_price,
          SUM(customer_paid)                                      AS total_revenue
        FROM wf2_orders
        WHERE tenant_id = ? AND entity_alias = ? AND partner_sku = ?
        """,
        (tenant_id, entity_alias, partner_sku),
    ).fetchone()
    total = _val(row, 0, "total_orders") or 0
    if not total:
        return None

    # 最近一单的 seller_price / customer_paid（latest_price=noon 视角、latest_customer_paid）
    latest = conn.execute(
        """
        SELECT seller_price, customer_paid
        FROM wf2_orders
        WHERE tenant_id = ? AND entity_alias = ? AND partner_sku = ?
        ORDER BY order_date DESC, item_nr DESC
        LIMIT 1
        """,
        (tenant_id, entity_alias, partner_sku),
    ).fetchone()

    valid = _val(row, 1, "valid_orders") or 0
    cancel = _val(row, 2, "cancel_count") or 0
    ret = _val(row, 3, "return_count") or 0
    v = {
        "total_orders":         total,
        "valid_orders":         valid,
        "cancel_count":         cancel,
        "return_count":         ret,
        "latest_order_date":    _val(row, 4, "latest_order_date"),
        "avg_price":            _val(row, 5, "avg_price"),
        "total_revenue":        _val(row, 6, "total_revenue"),
        "latest_price":         _val(latest, 0, "seller_price"),
        "latest_customer_paid": _val(latest, 1, "customer_paid"),
    }
    v["cancel_rate"] = (cancel / total) if total else None
    v["return_rate"] = (ret / valid) if valid else None
    return v


def _order_item_nrs(conn, tenant_id, entity_alias, partner_sku):
    rows = conn.execute(
        """
        SELECT item_nr FROM wf2_orders
        WHERE tenant_id = ? AND entity_alias = ? AND partner_sku = ?
        ORDER BY order_date DESC, item_nr DESC
        """,
        (tenant_id, entity_alias, partner_sku),
    ).fetchall()
    return [_val(r, 0, "item_nr") for r in rows]


def merge_entity_v2(tenant_id: int, entity_alias: str, conn) -> int:
    """把 noon 订单视角 merge 回 wf2_sku 的契约字段。

    Returns: 处理的 SKU 行数。
    """
    sku_rows = conn.execute(
        "SELECT * FROM wf2_sku WHERE tenant_id = ? AND entity_alias = ?",
        (tenant_id, entity_alias),
    ).fetchall()

    # 本次刷新写入时刻（localtime，与 verifiers._started_at_iso / data._date10 同口径）。
    # 整批用同一时刻，保证「本 run 刷新过的行」时间戳一致 >= started_at。
    import time as _time
    refreshed_at = _time.strftime("%Y-%m-%d %H:%M:%S")

    # 列名 → index（sqlite description / PG RealDictRow 都支持转 dict）
    n = 0
    for raw in sku_rows:
        rec = dict(raw) if not isinstance(raw, dict) else dict(raw)
        partner_sku = rec["partner_sku"]
        noon_v = _noon_view(conn, tenant_id, entity_alias, partner_sku)

        # 异常：先用 ERP 的 latest_price（rec）对比 noon，再考虑覆盖
        anomalies = detect_anomalies(rec, noon_v or {})

        merged = dict(rec)
        if noon_v:
            for k in ("total_orders", "valid_orders", "cancel_count", "return_count",
                      "cancel_rate", "return_rate", "latest_price", "avg_price",
                      "total_revenue", "latest_order_date", "latest_customer_paid"):
                if noon_v.get(k) is not None:
                    merged[k] = noon_v[k]

        # latest_profit_rate 兜底：ERP 没给但能从 latest_price + cost_price 算
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
        item_nrs = _order_item_nrs(conn, tenant_id, entity_alias, partner_sku)

        # T07 fix: advance as_of_date to the noon latest_order_date when it's more recent
        # than the current as_of_date (preserves ERP's as_of_date if it's fresher).
        # This ensures get_data_health.erp_sales.latest reflects the true data coverage date.
        current_as_of = rec.get("as_of_date") or ""
        noon_order_date = noon_v.get("latest_order_date") if noon_v else None
        new_as_of_date = noon_order_date if (noon_order_date and noon_order_date > current_as_of) else None

        conn.execute(
            """
            UPDATE wf2_sku SET
              total_orders        = ?,
              valid_orders        = ?,
              cancel_count        = ?,
              return_count        = ?,
              cancel_rate         = ?,
              return_rate         = ?,
              latest_price        = COALESCE(?, latest_price),
              avg_price           = COALESCE(?, avg_price),
              latest_customer_paid = ?,
              latest_profit_rate  = COALESCE(?, latest_profit_rate),
              total_revenue       = ?,
              latest_order_date   = COALESCE(?, latest_order_date),
              sales_grade         = ?,
              forecast_10d        = ?,
              forecast_30d        = ?,
              anomalies_json      = ?,
              order_item_nrs_json = ?,
              imported_at         = ?,
              as_of_date          = COALESCE(?, as_of_date)
            WHERE tenant_id = ? AND entity_alias = ? AND partner_sku = ?
            """,
            (
                merged.get("total_orders"), merged.get("valid_orders"),
                merged.get("cancel_count"), merged.get("return_count"),
                merged.get("cancel_rate"), merged.get("return_rate"),
                merged.get("latest_price"), merged.get("avg_price"),
                merged.get("latest_customer_paid"),
                merged.get("latest_profit_rate"),
                merged.get("total_revenue"), merged.get("latest_order_date"),
                grade, fc["forecast_10d"], fc["forecast_30d"],
                json.dumps(anomalies, ensure_ascii=False) if anomalies else None,
                json.dumps(item_nrs, ensure_ascii=False) if item_nrs else None,
                refreshed_at,
                new_as_of_date,
                tenant_id, entity_alias, partner_sku,
            ),
        )
        n += 1

    conn.commit()
    return n


def run_v2(tenant_id: int, conn=None, as_of=None) -> dict:
    """按需 / 周期「销量刷新」编排（WS-21）。

    对 tenant 下所有**已有 noon 订单**的 entity，重算窗口销量
    (aggregate_sales_v2) + 合并 noon 视角契约字段 + 评级/预测 (merge_entity_v2)。

    为什么单独有这个入口
    --------------------
    上传 noon CSV 走 api._run_pipeline_v2（CSV → ingest → aggregate → merge）；
    但「不再上传新 CSV、只想用现有订单重刷一遍销量/评级」此前**没有任何注册入口**
    （scheduler 的 refresh_all_v2 只拉 ERP 销量，从不重算 noon 聚合/评级）——
    典型「接线缺失」：评级算法在、merge 在，却没人在按需/每周路径上调它。

    本函数把「现有 wf2_orders → 窗口聚合 → 评级」这条链抽成一个可被
    runner（按需 /run-workflow）与 refresh_all_v2（每周/每日 scheduler）共用的入口，
    复用 upload pipeline 同一对函数 aggregate_sales_v2 / merge_entity_v2，
    保证三条触发路径口径一致、不漂移。

    conn 为 None 时自建连接（生产/runner 用）；传入则复用（测试/事务内用）。
    """
    from hipop.server import data as _data
    try:
        from hipop.scripts.ingest_noon_csv_v2 import aggregate_sales_v2
    except ModuleNotFoundError:
        from ingest_noon_csv_v2 import aggregate_sales_v2  # type: ignore

    own = conn is None
    if own:
        _data.set_current_tenant(tenant_id)
        conn = _data.conn()
    try:
        aliases = [
            (r[0] if not isinstance(r, dict) else r["entity_alias"])
            for r in conn.execute(
                "SELECT DISTINCT entity_alias FROM wf2_orders WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        ]
        windows = 0
        graded = 0
        for alias in aliases:
            windows += aggregate_sales_v2(tenant_id, alias, conn, as_of=as_of) or 0
            graded += merge_entity_v2(tenant_id, alias, conn)
        return {"entities": len(aliases), "windows_aggregated": windows,
                "skus_graded": graded}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass

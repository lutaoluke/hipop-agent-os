"""ingest_erp_sales v2 — 真多租户

签名：run_v2(tenant_id: int, windows=None, max_pages=None)

机制：
1. 用 _erp_auth.get_erp_token_for_tenant(tenant_id) 拿 token（同 products_v2）
2. 从 sales_entities 拿 tenant 的所有 entity
3. 每个 entity 按国别 nation_id 跑 6 个销量窗口（10/30/60/90/120/180）
4. 写 wf2_sku v2 表（仅 UPDATE 销量/价格/利润率字段，不 INSERT — 因为 products_v2 应该先跑过建好基础记录）

依赖：products_v2 必须先跑（已建 partner_sku 行）。本脚本只 UPDATE 销量数字。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from ingest_erp_sales import (
    fetch_window, parse_country_value, parse_int, parse_money, parse_pct,
    fetch_sku_cost_detail, parse_sku_cost_orders,
    NATION_TO_ID, NOON_PLATFORM_ID, WINDOWS,
)
from sales_entity_v2 import list_entities_for_tenant
from server._erp_auth import get_erp_token_for_tenant
from server import data as _data
try:
    from server.runtime import tick, set_progress
except ImportError:
    tick = lambda *a, **k: None
    set_progress = lambda *a, **k: None


# WS-17 新增的 wf2_orders 成本/利润列。schema_v2.sql 已加，但生产里早建好的
# DB 不会因 CREATE TABLE IF NOT EXISTS 而补列 → 这里幂等 ALTER，避免"列在 schema
# 里有、生产表里没有 → 写入静默失败"的接线缺失死法。SQLite/PG 都支持 ADD COLUMN。
_ORDER_COST_COLS = [
    ("cost_local", "REAL"), ("cost_pack", "REAL"), ("cost_intl", "REAL"),
    ("profit", "REAL"), ("profit_rate", "REAL"),
]


def _existing_columns(conn, table: str) -> set:
    if _data.is_postgres():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
        return {(r[0] if not isinstance(r, dict) else r["column_name"]) for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _ensure_order_cost_cols(conn):
    existing = _existing_columns(conn, "wf2_orders")
    for col, ctype in _ORDER_COST_COLS:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE wf2_orders ADD COLUMN {col} {ctype}")
            except Exception as e:
                print(f"[migrate] add {col} skip: {str(e)[:80]}", file=sys.stderr)
    conn.commit()


def _upsert_order_costs(conn, tenant_id, alias, rec, nation_id, token):
    """拉该 SKU 的 ERP 订单级成本/利润，按 item_nr upsert 进 wf2_orders。

    只写 ERP 负责的成本/利润列 + 关联键；noon 订单字段（seller_price/customer_paid）
    若已存在不被覆盖（确定性规则：ERP 不盖 noon 订单字段）。返回写入行数。
    """
    erp_sku = rec["partner_sku"]
    try:
        detail = fetch_sku_cost_detail(token, erp_sku, nation_id=nation_id)
    except Exception as e:
        print(f"  [{alias}/{erp_sku}] cost detail fail: {str(e)[:80]}", file=sys.stderr)
        return 0
    orders = parse_sku_cost_orders(detail, country=rec.get("country"))
    n = 0
    for od in orders:
        sql = """
            INSERT INTO wf2_orders
              (tenant_id, entity_alias, partner_sku, noon_sku, item_nr, order_date,
               currency, cost_local, cost_pack, cost_intl, profit, profit_rate, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (tenant_id, entity_alias, partner_sku, item_nr) DO UPDATE SET
              -- 确定性规则：ERP 只补成本/利润，noon 订单字段不被覆盖。已有 noon 行优先
              -- （noon_sku / order_date / currency 都保 noon）；seller_price/customer_paid/
              -- status/fulfillment 不在 SET 里 → 天然保留。order_date 曾误走 excluded 优先，
              -- 会把 noon 订单日期盖成 ERP detail 日期，污染最新订单/销量窗口口径（WS-17 红队打回）。
              noon_sku    = COALESCE(wf2_orders.noon_sku, excluded.noon_sku),
              order_date  = COALESCE(wf2_orders.order_date, excluded.order_date),
              currency    = COALESCE(wf2_orders.currency, excluded.currency),
              cost_local  = excluded.cost_local,
              cost_pack   = excluded.cost_pack,
              cost_intl   = excluded.cost_intl,
              profit      = excluded.profit,
              profit_rate = excluded.profit_rate
        """
        conn.execute(sql, (
            tenant_id, alias, erp_sku, rec.get("noon_sku"), od["item_nr"],
            od["order_date"], rec.get("currency"),
            od["cost_local"], od["cost_pack"], od["cost_intl"],
            od["profit"], od["profit_rate"], "erp",
        ))
        n += 1
    return n


def run_v2(tenant_id: int, windows: list | None = None, max_pages: int | None = None) -> dict:
    print(f"\n=== ingest_erp_sales v2 tenant={tenant_id} ===", file=sys.stderr)

    entities = list_entities_for_tenant(tenant_id)
    if not entities:
        raise RuntimeError(f"tenant={tenant_id} 没配 sales_entities")
    win_list = [w for w in WINDOWS if not windows or w in windows]

    token = get_erp_token_for_tenant(tenant_id)
    if not token:
        raise RuntimeError(f"tenant={tenant_id} ERP token 拿不到")

    conn = _data.conn()
    _ensure_order_cost_cols(conn)
    counts = {}
    order_counts = {}
    skip_order_cost = os.environ.get("SMOKE_SKIP_ORDER_COST") == "1"

    for ent in entities:
        alias = ent["alias"]
        country = ent["country"]
        store = ent["store"]
        currency = ent.get("currency")
        nation_id = NATION_TO_ID.get(country)
        if not nation_id:
            print(f"[skip {alias}] unknown country {country}", file=sys.stderr)
            continue

        print(f"\n[{alias}] country={country} store={store}", file=sys.stderr)
        tick(f"start entity {alias} country={country}")

        # partner_sku -> 累积 dict
        bucket = {}
        for w_idx, days in enumerate(win_list, 1):
            max_items = max_pages * 50 if max_pages else None
            items = fetch_window(token, nation_id, days, max_items=max_items)
            print(f"  {days}d: {len(items)} skus", file=sys.stderr)
            tick(f"[{alias}] window {w_idx}/{len(win_list)} ({days}d): {len(items)} skus")
            set_progress({"current_entity": alias, "windows_done": w_idx, "windows_total": len(win_list)})
            for it in items:
                erp_sku = it.get("sku_id")
                if not erp_sku:
                    continue
                sku = it.get("sku") or {}
                for psk in sku.get("platform_sku_ids") or []:
                    store_obj = psk.get("store") or {}
                    plat_obj  = psk.get("platform") or {}
                    if plat_obj.get("id") != NOON_PLATFORM_ID:
                        continue
                    if store_obj.get("name") != store:
                        continue
                    rec = bucket.setdefault(erp_sku, {
                        "partner_sku": erp_sku,
                        "noon_sku":    psk.get("platform_sku_id"),
                        "image_url":   sku.get("sku_image"),
                        "currency":    currency,
                        "country":     country,
                    })
                    sales_str = parse_country_value(it.get("sales_count"), country)
                    rec[f"sales_{days}d"] = parse_int(sales_str)
                    if days == 180:
                        avg_p, c1 = parse_money(parse_country_value(it.get("avg_price"), country))
                        new_p, c2 = parse_money(parse_country_value(it.get("newest_sale_price"), country))
                        rec["avg_price"]    = avg_p
                        rec["latest_price"] = new_p
                        if not rec.get("currency"):
                            rec["currency"] = c1 or c2 or currency
                        rec["latest_profit_rate"] = parse_pct(parse_country_value(it.get("newest_profit_rate"), country))
                        rec["latest_order_date"] = (it.get("newest_sale_time") or "")[:10] or None
                    break

        today_iso = datetime.now().date().isoformat()
        n = 0
        for rec in bucket.values():
            # UPDATE wf2_sku（必须先存在；如不存在跳过，让 products_v2 先跑）
            ts_expr = "datetime('now','localtime')"
            sql = f"""
                UPDATE wf2_sku SET
                  noon_sku=COALESCE(?, noon_sku),
                  image_url=COALESCE(?, image_url),
                  currency=COALESCE(?, currency),
                  sales_10d=?, sales_30d=?, sales_60d=?, sales_90d=?, sales_120d=?, sales_180d=?,
                  avg_price=COALESCE(?, avg_price),
                  latest_price=COALESCE(?, latest_price),
                  latest_profit_rate=COALESCE(?, latest_profit_rate),
                  latest_order_date=COALESCE(?, latest_order_date),
                  as_of_date=?,
                  imported_at={ts_expr}
                WHERE tenant_id=? AND entity_alias=? AND partner_sku=?
            """
            params = (
                rec.get("noon_sku"), rec.get("image_url"), rec.get("currency"),
                rec.get("sales_10d"), rec.get("sales_30d"), rec.get("sales_60d"),
                rec.get("sales_90d"), rec.get("sales_120d"), rec.get("sales_180d"),
                rec.get("avg_price"), rec.get("latest_price"),
                rec.get("latest_profit_rate"), rec.get("latest_order_date"),
                today_iso,
                tenant_id, alias, rec["partner_sku"],
            )
            conn.execute(sql, params)
            n += 1
        conn.commit()
        counts[alias] = n
        print(f"[{alias}] +{n} sku updated", file=sys.stderr)

        # ── ERP 订单级成本/利润 → wf2_orders（WS-17 新增的承重墙）──
        # 不接这一步，cost_local/cost_pack/cost_intl/profit/profit_rate 永远是空列。
        if skip_order_cost:
            print(f"[{alias}] SMOKE_SKIP_ORDER_COST=1 → 跳过订单成本利润 ingest（模拟改动前）",
                  file=sys.stderr)
        else:
            n_orders = 0
            for rec in bucket.values():
                n_orders += _upsert_order_costs(conn, tenant_id, alias, rec, nation_id, token)
            conn.commit()
            order_counts[alias] = n_orders
            tick(f"[{alias}] +{n_orders} order cost/profit rows")
            print(f"[{alias}] +{n_orders} wf2_orders cost/profit rows", file=sys.stderr)

    conn.close()
    print(f"\n[done] tenant={tenant_id} sku={counts} orders={order_counts}", file=sys.stderr)
    return counts


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run_v2(args.tenant, max_pages=args.max_pages)

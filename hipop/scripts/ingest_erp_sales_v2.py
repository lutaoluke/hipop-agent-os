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
    NATION_TO_ID, NOON_PLATFORM_ID, WINDOWS,
)
from sales_entity_v2 import list_entities_for_tenant
from server._erp_auth import get_erp_token_for_tenant
from server import data as _data


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
    counts = {}

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

        # partner_sku -> 累积 dict
        bucket = {}
        for days in win_list:
            max_items = max_pages * 50 if max_pages else None
            items = fetch_window(token, nation_id, days, max_items=max_items)
            print(f"  {days}d: {len(items)} skus", file=sys.stderr)
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

    conn.close()
    print(f"\n[done] tenant={tenant_id} {counts}", file=sys.stderr)
    return counts


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run_v2(args.tenant, max_pages=args.max_pages)

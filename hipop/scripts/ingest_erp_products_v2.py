"""ingest_erp_products v2 — 真多租户

签名：run_v2(tenant_id: int, max_pages=None) -> dict[alias, n_rows]

机制：
1. 用 _erp_auth.get_erp_token_for_tenant(tenant_id) headless 登 ERP 拿 token
2. 从 sales_entities 拿该 tenant 的所有 entity（含 store_id）
3. 每个 entity 调 ERP /admin/product?store_ids=<store_id> 拉商品
4. 写到 wf2_sku（v2 列存表）按 (tenant_id, entity_alias, partner_sku) 主键

跟老 ingest_erp_products.py 共享：fetch_products() / NOON_PLATFORM_ID / NATION_BY_ID
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))   # for server.*

from ingest_erp_products import fetch_products, NOON_PLATFORM_ID, NATION_BY_ID
from sales_entity_v2 import list_entities_for_tenant
from server._erp_auth import get_erp_token_for_tenant
from server import data as _data
# 长任务 heartbeat — 无 HIPOP_TASK_ID env 时自动 no-op
try:
    from server.runtime import tick, set_progress
except ImportError:
    tick = lambda *a, **k: None
    set_progress = lambda *a, **k: None


def run_v2(tenant_id: int, max_pages: int | None = None) -> dict:
    """跑该 tenant 的所有 entity，把 ERP 商品写入 wf2_sku v2 表"""
    print(f"\n=== ingest_erp_products v2 tenant={tenant_id} ===", file=sys.stderr)

    entities = list_entities_for_tenant(tenant_id)
    if not entities:
        raise RuntimeError(f"tenant={tenant_id} 没配 sales_entities，先在 onboarding 配店铺")

    token = get_erp_token_for_tenant(tenant_id)
    if not token:
        raise RuntimeError(f"tenant={tenant_id} ERP 凭据缺失或登录失败 — 检查 onboarding 时填的 ERP 用户名密码")
    print(f"[token ok] for tenant={tenant_id} (length={len(token)})", file=sys.stderr)

    # PG 用 ?→%s 转换；SQLite 直接 ?；走 _data.conn() 抽象
    conn = _data.conn()
    # PG 模式：必须设 tenant context 才能写 RLS 表
    if _data.is_postgres():
        # _data.conn() 已经 SET app.current_tenant，但 sqlite3.connect() 没。这里 conn() 已处理
        pass

    counts = {}
    for ent in entities:
        alias = ent["alias"]
        store_id = ent.get("store_id")
        if not store_id:
            print(f"[{alias}] 跳过：sales_entities.store_id 没配", file=sys.stderr)
            continue
        print(f"\n[{alias}] fetch ERP store_ids={store_id}...", file=sys.stderr)
        tick(f"start entity {alias} (store_id={store_id})")

        rows_for_entity = []
        for product in fetch_products(token, max_pages=max_pages, store_id=store_id):
            product_id = product.get("product_id")
            title      = product.get("name") or ""
            brand_obj  = product.get("brand") or {}
            brand      = brand_obj.get("name")
            cat_detail = product.get("product_category_detail")
            admin_obj  = product.get("product_choose_admin") or {}
            admin      = admin_obj.get("username")
            created_at = product.get("created_at")
            imgs = product.get("images") or product.get("noon_images") or []
            main_image = imgs[0] if imgs else None

            for sku in product.get("skus") or []:
                sku_id    = sku.get("sku_id")
                sku_image = sku.get("sku_image") or main_image
                cost      = sku.get("cost_price")
                try:
                    cost = float(cost) if cost is not None else None
                except (TypeError, ValueError):
                    cost = None

                noon_sku = None
                for psk in sku.get("platform_sku_ids") or []:
                    plat_obj  = psk.get("platform") or {}
                    if plat_obj.get("id") != NOON_PLATFORM_ID:
                        continue
                    store_obj = psk.get("store") or {}
                    if store_obj.get("name") != ent.get("store"):
                        continue
                    noon_sku = psk.get("platform_sku_id")
                    break

                rows_for_entity.append({
                    "tenant_id":   tenant_id,
                    "entity_alias": alias,
                    "partner_sku": sku_id,
                    "erp_sku_id":  sku_id,
                    "noon_sku":    noon_sku,
                    "product_id":  product_id,
                    "title":       title,
                    "image_url":   sku_image,
                    "brand":       brand,
                    "product_category_detail": cat_detail,
                    "cost_price":  cost,
                    "erp_created_at": created_at,
                    "product_choose_admin": admin,
                    "currency":    ent.get("currency"),
                    "is_listed":   1 if noon_sku else 0,
                })

        # upsert v2 表（imported_at 用 DB 函数刷新，UPSERT 时也更新）
        cols = ["tenant_id","entity_alias","partner_sku","erp_sku_id","noon_sku","product_id",
                "title","image_url","brand","product_category_detail","cost_price",
                "erp_created_at","product_choose_admin","currency","is_listed"]
        placeholders = ",".join(["?"] * len(cols))
        # SQLite 用 datetime('now','localtime')；PG 用 NOW()。data._convert_sql_for_pg 自动转
        ts_expr = "datetime('now','localtime')"
        update_set = ",".join(f"{c}=excluded.{c}" for c in cols
                               if c not in ("tenant_id", "entity_alias", "partner_sku"))
        update_set += f", imported_at={ts_expr}"
        sql = (
            f"INSERT INTO wf2_sku ({','.join(cols)}, imported_at) "
            f"VALUES ({placeholders}, {ts_expr}) "
            f"ON CONFLICT (tenant_id, entity_alias, partner_sku) DO UPDATE SET {update_set}"
        )
        n = 0
        total = len(rows_for_entity)
        for rec in rows_for_entity:
            try:
                conn.execute(sql, tuple(rec.get(c) for c in cols))
                n += 1
                if n % 100 == 0:
                    tick(f"[{alias}] upserted {n}/{total} rows")
            except Exception as e:
                print(f"[{alias}] row fail: {str(e)[:100]}", file=sys.stderr)
                break
        conn.commit()
        counts[alias] = n
        set_progress({"by_entity": counts})
        tick(f"[{alias}] done +{n} rows")
        print(f"[{alias}] +{n} rows upserted to wf2_sku", file=sys.stderr)

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

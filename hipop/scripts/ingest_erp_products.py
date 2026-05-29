"""
工作流二（前置）：从 ERP 商品库 /admin/product 拉全量商品，写入每个销售主体的 sku 主表。

数据范围：config/hipop.json -> sales_entities[]，每个 entity 对应一张 wf2_<alias>_sku 表。
ingest 时按 (country, store) 路由 SKU 到对应 entity 的表。

字段：商品基础（不动销量，让 ingest_erp_sales 后跑覆盖）
  product_id, noon_sku, title, image_url, brand, product_category_detail,
  cost_price, erp_created_at, product_choose_admin

CLI:
  python3 ingest_erp_products.py             # 全量
  python3 ingest_erp_products.py --max-pages N
"""
import os, sys, json, sqlite3, argparse, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, ensure_tables, sku_table, entity_for
# 长任务 heartbeat — 无 HIPOP_TASK_ID env 时自动 no-op
try:
    from hipop.server.runtime import tick, set_progress
except ImportError:
    tick = lambda *a, **k: None
    set_progress = lambda *a, **k: None

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
NOON_PLATFORM_ID = 2
NATION_BY_ID = {1: "SA", 2: "AE"}


def get_token():
    from ingest_erp_sales import get_token as _get
    return _get()


def erp_get(token, path, params=None, retries=6):
    import requests
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(
                "https://erp-api.dbuyerp.com/admin" + path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )
            data = r.json()
            msg = data.get("msg") or ""
            if "处理中" in msg or "重复" in msg or "频繁" in msg or r.status_code == 429:
                wait = min(2 ** attempt, 30)
                print(f"  [throttled] retry in {wait}s ({msg})", file=sys.stderr)
                time.sleep(wait)
                continue
            return data
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    if last:
        raise last
    raise RuntimeError("erp_get exhausted retries")


def fetch_products(token, page_size=50, max_pages=None, store_id=None):
    """拉 /admin/product 列表。store_id 给定时按 ERP 后台口径过滤（store_ids=<id>）。"""
    page = 1
    while True:
        params = {"keyword_type": 1, "page": page, "limit": page_size}
        if store_id is not None:
            params["store_ids"] = store_id
        data = erp_get(token, "/product", params)
        if data.get("code") != 200:
            raise RuntimeError(f"ERP product list error: {data.get('msg')} page={page}")
        items = data.get("data") or []
        if not items:
            break
        for it in items:
            yield it
        meta = data.get("meta") or {}
        total = meta.get("total") or 0
        print(f"  page {page}: {len(items)} products (total={total})", file=sys.stderr)
        tick(f"fetch page {page} ({len(items)} products, total={total})")
        if len(items) < page_size:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(0.4)


def run(max_pages=None):
    entities = load_entities()
    if not entities:
        sys.exit("config/hipop.json missing sales_entities")
    print(f"[entities] {[e['alias'] for e in entities]}", file=sys.stderr)

    token = get_token()
    if not token:
        sys.exit("ERP token not available")

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)

    # alias -> [rec, ...]
    by_entity = {e["alias"]: [] for e in entities}

    # 按 entity 独立拉取（带 store_ids 过滤，对齐 ERP 后台筛选店铺时的口径）
    for ent in entities:
        alias = ent["alias"]
        store_id = ent.get("store_id")
        if not store_id:
            print(f"[{alias}] WARNING: 没配 store_id，跳过 (在 hipop.json sales_entities 加 store_id)",
                  file=sys.stderr)
            continue
        print(f"\n[entity {alias}] fetch store_ids={store_id}...", file=sys.stderr)
        tick(f"start entity {alias} store_id={store_id}")

        seen_skus = 0
        bound_skus = 0
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
                seen_skus += 1
                sku_id    = sku.get("sku_id")  # =PSKU
                sku_image = sku.get("sku_image") or main_image
                cost      = sku.get("cost_price")
                try:
                    cost = float(cost) if cost is not None else None
                except (TypeError, ValueError):
                    cost = None

                # 找该 SKU 在本 entity 下的 noon platform_sku_id（可能没有 = 草稿/未上架）
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
                if noon_sku:
                    bound_skus += 1

                by_entity[alias].append({
                    "partner_sku": sku_id,
                    "erp_sku_id":  sku_id,
                    "noon_sku":    noon_sku,
                    "product_id":  product_id,
                    "title":       title,
                    "image_url":   sku_image,
                    "brand":       brand,
                    "product_category_detail": cat_detail,
                    "cost_price":  cost,
                    "erp_created_at":         created_at,
                    "product_choose_admin":   admin,
                    "currency":    ent.get("currency"),
                    "is_listed":   1 if noon_sku else 0,  # 是否已绑定 noon 平台 SKU
                })

        print(f"[{alias}] seen {seen_skus} skus, {bound_skus} bound to noon platform",
              file=sys.stderr)

    # 写库：每个主体一张表
    cur = conn.cursor()
    cols = [
        "partner_sku", "erp_sku_id", "noon_sku", "product_id",
        "title", "image_url", "brand", "product_category_detail",
        "cost_price", "erp_created_at", "product_choose_admin", "currency",
        "is_listed",
    ]
    placeholders = ",".join(["?"] * len(cols))
    update_set = ",".join(f"{c}=COALESCE(excluded.{c},{c})"
                          for c in cols if c != "partner_sku")
    for alias, rows in by_entity.items():
        t = sku_table(alias)
        sql = f"""
            INSERT INTO {t} ({",".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(partner_sku) DO UPDATE SET
              {update_set},
              imported_at = datetime('now','localtime')
        """
        for rec in rows:
            cur.execute(sql, tuple(rec.get(c) for c in cols))
        print(f"  {alias}: {len(rows)} rows upserted into {t}", file=sys.stderr)
        tick(f"upserted {len(rows)} rows into {t}")
    conn.commit()
    conn.close()
    print("[done]", file=sys.stderr)
    set_progress({"done": True, "by_entity": {a: len(r) for a, r in by_entity.items()}})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run(max_pages=args.max_pages)

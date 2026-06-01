"""
工作流一：从 ERP /admin/stock 拉国内仓 + 海外仓库存，写入每个销售主体的 wf1_<alias>_stock 表。

策略：
  - 对每个 warehouse_id 翻页拉全量 /stock
  - 每条记录的 platform_sku_ids[] 含绑定的 store；按 sales_entity.store 过滤
  - 国内仓（义乌/东莞）数据所有 entity 共用（多家店共享国内仓库存）
  - 海外仓按 entity.country 过滤路由

字段写入：
  yiwu_qty, dongguan_qty, overseas_total_qty, overseas_breakdown_json
  以及 partner_sku/product_id/title/image_url/family（兜底，不覆盖 wf2 已写值）

CLI:
  python3 ingest_erp_stock.py
  python3 ingest_erp_stock.py --entities hipop_ksa
  python3 ingest_erp_stock.py --max-pages 1
"""
import os, sys, json, sqlite3, argparse, time
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import (load_entities, ensure_tables, stock_table,
                          WAREHOUSES, overseas_warehouses_for, domestic_warehouses)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
# 受控配置入口：API base 可由 env 覆盖（生产走真实 dbuyerp；测试/自托管可换）。
# token/cookie 不在这里硬编码 —— 走 _erp_auth.get_erp_token_for_tenant（受审计 env/runtime）。
ERP_API_BASE = os.environ.get("ERP_API_BASE", "https://erp-api.dbuyerp.com/admin")
NOON_PLATFORM_ID = 2


def get_token():
    from ingest_erp_sales import get_token as _get
    return _get()


def erp_get(token, path, params=None, retries=8):
    import requests
    from requests.adapters import HTTPAdapter
    last = None
    for attempt in range(retries):
        try:
            sess = requests.Session()
            sess.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0))
            r = sess.get(
                ERP_API_BASE + path, params=params,
                headers={"Authorization": f"Bearer {token}", "Connection": "close"},
                timeout=60,
            )
            data = r.json()
            sess.close()
            msg = data.get("msg") or ""
            if "处理中" in msg or "重复" in msg or "频繁" in msg or r.status_code == 429:
                wait = min(2 ** attempt, 60)
                print(f"  [throttled] retry in {wait}s ({msg})", file=sys.stderr)
                time.sleep(wait)
                continue
            return data
        except Exception as e:
            last = e
            wait = min(5 + 2 ** attempt, 60)
            print(f"  [conn err] retry in {wait}s ({type(e).__name__})", file=sys.stderr)
            time.sleep(wait)
    if last:
        raise last
    raise RuntimeError("erp_get exhausted retries")


def fetch_warehouse_stock(token, warehouse_id, page_size=50, max_pages=None):
    """翻页拉某仓库全量库存。"""
    page = 1
    out = []
    while True:
        data = erp_get(token, "/stock",
                       {"warehouse_id": warehouse_id, "page": page, "limit": page_size})
        if data.get("code") != 200:
            raise RuntimeError(f"stock error: {data.get('msg')} warehouse={warehouse_id} page={page}")
        items = data.get("data") or []
        out.extend(items)
        meta = data.get("meta") or {}
        total = meta.get("total") or 0
        print(f"    wh={warehouse_id} page {page}: {len(items)} items (total={total})", file=sys.stderr)
        if len(items) < page_size:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(0.4)
    return out


def has_store_binding(item, store_name):
    """检查 SKU 是否绑定到指定 store（noon 平台）。"""
    for psk in item.get("platform_sku_ids") or []:
        plat = psk.get("platform") or {}
        store = psk.get("store") or {}
        if plat.get("id") == NOON_PLATFORM_ID and store.get("name") == store_name:
            return psk.get("platform_sku_id")
    return None


def safe_int(v):
    if v is None: return 0
    try: return int(float(v))
    except (TypeError, ValueError): return 0


def run(entity_aliases=None, max_pages=None):
    entities = [e for e in load_entities()
                if not entity_aliases or e["alias"] in entity_aliases]
    if not entities:
        sys.exit("no matching sales_entities")

    token = get_token()
    if not token:
        sys.exit("ERP token not available")

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)

    today_iso = datetime.now().date().isoformat()

    # entity_alias → partner_sku → 库存累计 dict
    bucket = {e["alias"]: {} for e in entities}

    # 拉每个 warehouse（去重：每个 warehouse 只拉一次）
    needed_wh = set(domestic_warehouses())
    for ent in entities:
        needed_wh.update(overseas_warehouses_for(ent["country"]))

    print(f"[entities] {[e['alias'] for e in entities]}", file=sys.stderr)
    print(f"[warehouses to fetch] {sorted(needed_wh)}", file=sys.stderr)

    for wid in sorted(needed_wh):
        w = WAREHOUSES[wid]
        print(f"\n[warehouse {wid} {w['name']} ({w['scope']}/{w['country'] or '-'})]", file=sys.stderr)
        items = fetch_warehouse_stock(token, wid, max_pages=max_pages)

        # 每条 SKU 路由到相关 entity（看 platform_sku_ids 是否绑该 entity 的 store）
        for it in items:
            partner_sku = it.get("sku_id")
            if not partner_sku:
                continue
            qty = safe_int(it.get("stock_total_available_count"))
            if qty <= 0:
                # 0 库存的也记录，便于运营看；如果你只想要有库存的可改这里
                pass

            for ent in entities:
                # 判定该 SKU 是否归本 entity（绑定了该 entity 的 store）
                if not has_store_binding(it, ent["store"]):
                    continue
                rec = bucket[ent["alias"]].setdefault(partner_sku, {
                    "partner_sku": partner_sku,
                    "product_id":  None,
                    "noon_sku":    None,
                    "title":       it.get("product_name"),
                    "image_url":   it.get("sku_image"),
                    "family":      None,
                    "yiwu_qty":             0,
                    "dongguan_qty":         0,
                    "overseas_total_qty":   0,
                    "_overseas_breakdown":  {},
                })
                # 兜底：noon_sku / product_id 取首个绑该 entity 的 noon mapping
                if not rec["noon_sku"]:
                    psk = has_store_binding(it, ent["store"])
                    rec["noon_sku"] = psk
                # product_id 从 platform_sku_ids 数据拿
                for psk_obj in it.get("platform_sku_ids") or []:
                    if (psk_obj.get("store") or {}).get("name") == ent["store"]:
                        rec["product_id"] = psk_obj.get("product_id")
                        break

                # 把 qty 累到对应字段
                if w["alias"] == "yiwu":
                    rec["yiwu_qty"] = qty
                elif w["alias"] == "dongguan":
                    rec["dongguan_qty"] = qty
                elif w["scope"] == "overseas" and w["country"] == ent["country"]:
                    rec["overseas_total_qty"] += qty
                    if qty:
                        rec["_overseas_breakdown"][w["name"]] = qty

    # 写入数据库
    cur = conn.cursor()
    cols = [
        "partner_sku", "product_id", "noon_sku", "title", "image_url", "family",
        "yiwu_qty", "dongguan_qty", "overseas_total_qty", "overseas_breakdown_json",
        "as_of_date",
    ]
    placeholders = ",".join(["?"] * len(cols))
    update_set = ",".join(f"{c}=excluded.{c}" for c in cols if c != "partner_sku")
    update_set += ", imported_at=datetime('now','localtime')"

    for alias, recs in bucket.items():
        t = stock_table(alias)
        sql = f"""
            INSERT INTO {t} ({",".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(partner_sku) DO UPDATE SET
              product_id  = COALESCE(excluded.product_id, {t}.product_id),
              noon_sku    = COALESCE(excluded.noon_sku, {t}.noon_sku),
              title       = COALESCE(excluded.title, {t}.title),
              image_url   = COALESCE(excluded.image_url, {t}.image_url),
              family      = COALESCE(excluded.family, {t}.family),
              yiwu_qty    = excluded.yiwu_qty,
              dongguan_qty= excluded.dongguan_qty,
              overseas_total_qty      = excluded.overseas_total_qty,
              overseas_breakdown_json = excluded.overseas_breakdown_json,
              as_of_date  = excluded.as_of_date,
              imported_at = datetime('now','localtime')
        """
        for rec in recs.values():
            cur.execute(sql, (
                rec["partner_sku"], rec["product_id"], rec["noon_sku"],
                rec["title"], rec["image_url"], rec["family"],
                rec["yiwu_qty"], rec["dongguan_qty"], rec["overseas_total_qty"],
                json.dumps(rec["_overseas_breakdown"], ensure_ascii=False) if rec["_overseas_breakdown"] else None,
                today_iso,
            ))
        conn.commit()
        print(f"  [{alias}] wrote {len(recs)} rows to {t}", file=sys.stderr)

    conn.close()
    print("\n[done]", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=None, help="逗号分隔，例 hipop_ksa")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run(
        entity_aliases=args.entities.split(",") if args.entities else None,
        max_pages=args.max_pages,
    )

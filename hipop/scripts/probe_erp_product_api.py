"""
调 /admin/product 看产品列表返回结构。
"""
import json
from playwright.sync_api import sync_playwright

JS = """
async ({path, params, token}) => {
  const u = new URL('https://erp-api.dbuyerp.com/admin' + path);
  for (const [k, v] of Object.entries(params || {})) {
    if (Array.isArray(v)) v.forEach(x => u.searchParams.append(k, x));
    else u.searchParams.set(k, v);
  }
  const r = await fetch(u, {headers: {Authorization: 'Bearer ' + token}});
  return {status: r.status, body: await r.text()};
}
"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    erp = next(pg for pg in ctx.pages if "dbuyerp.com" in pg.url)

    auth = {"v": None}
    def grab(req):
        h = {k.lower(): v for k, v in req.headers.items()}
        a = h.get("authorization")
        if a and "erp-api" in req.url and not auth["v"]:
            auth["v"] = a
    erp.on("request", grab)

    erp.reload(wait_until="load", timeout=20000)
    erp.wait_for_timeout(4000)
    if not auth["v"]:
        print("no token")
        raise SystemExit(1)
    token = auth["v"].replace("Bearer ", "").strip()

    print("=== /product (page1, limit3) ===")
    r = erp.evaluate(JS, {"path": "/product", "params": {"keyword_type": 1, "page": 1, "limit": 3}, "token": token})
    print(f"status={r['status']}")
    body = json.loads(r["body"])
    print(f"top keys: {list(body.keys())}")
    if "meta" in body:
        print(f"meta: {body['meta']}")
    if body.get("data"):
        print(f"data count: {len(body['data'])}")
        item = body["data"][0]
        print(f"\nproduct top keys: {list(item.keys())}")
        # 重点字段
        for k in ["product_id", "name", "title", "name_zh", "description", "category", "category_id",
                  "category_name", "status", "is_eol", "created_at", "updated_at",
                  "brand", "product_choose_admin"]:
            if k in item:
                v = item[k]
                if isinstance(v, dict):
                    print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
                else:
                    print(f"  {k}: {v}")
        # SKUs
        print(f"\nsku count: {len(item.get('skus', []))}")
        if item.get("skus"):
            sku0 = item["skus"][0]
            print(f"sku keys: {list(sku0.keys())}")
            print(f"  sku_id: {sku0.get('sku_id')}")
            print(f"  cost_price: {sku0.get('cost_price')}")
            print(f"  platform_sku_ids count: {len(sku0.get('platform_sku_ids', []))}")
            print(f"  noon_sku_ids: {len(sku0.get('noon_sku_ids', []))}")
            if sku0.get('platform_sku_ids'):
                print(f"  first platform_sku_id: {json.dumps(sku0['platform_sku_ids'][0], ensure_ascii=False)[:300]}")

    print(f"\n=== meta ===")
    print(json.dumps(body.get("meta") or body.get("pagination") or {}, ensure_ascii=False, indent=2))

    # 试 limit=200 看 meta total
    print("\n=== /product (page1, limit=1) for total ===")
    r2 = erp.evaluate(JS, {"path": "/product", "params": {"keyword_type": 1, "page": 1, "limit": 1}, "token": token})
    body2 = json.loads(r2["body"])
    print(json.dumps(body2.get("meta") or {}, ensure_ascii=False, indent=2)[:300])
    # 看根 keys
    print(f"resp top keys: {list(body2.keys())}")

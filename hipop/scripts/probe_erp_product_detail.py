"""
看 /admin/product 列表里某个老 SKU（确认有 noon 绑定的）的 platform_sku_ids 内容。
对比 /admin/product/{id} 详情接口。
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
    token = auth["v"].replace("Bearer ", "").strip()

    # 用 keyword 搜一个已知有销量的 SAB0433
    print("=== /product?keyword=SAB0433 ===")
    r = erp.evaluate(JS, {"path": "/product", "params": {"keyword": "SAB0433", "keyword_type": 1, "page": 1, "limit": 3}, "token": token})
    body = json.loads(r["body"])
    if body.get("data"):
        item = body["data"][0]
        print(f"product_id: {item.get('product_id')}, name: {item.get('name')}")
        for sku in item.get("skus", []):
            print(f"  sku_id={sku.get('sku_id')}")
            print(f"  platform_sku_ids count: {len(sku.get('platform_sku_ids') or [])}")
            print(f"  noon_sku_ids count:     {len(sku.get('noon_sku_ids') or [])}")
            if sku.get("platform_sku_ids"):
                print(f"  first platform_sku_id: {json.dumps(sku['platform_sku_ids'][0], ensure_ascii=False)[:300]}")
            if sku.get("noon_sku_ids"):
                print(f"  first noon_sku_id: {json.dumps(sku['noon_sku_ids'][0], ensure_ascii=False)[:300]}")

    # 查 product 详情接口
    print("\n=== /product/SAB0433 详情 ===")
    r2 = erp.evaluate(JS, {"path": "/product/SAB0433", "params": {}, "token": token})
    print(f"status={r2['status']}, body[:200]={r2['body'][:200]}")
    if r2["status"] == 200:
        body2 = json.loads(r2["body"])
        if body2.get("data"):
            item = body2["data"]
            for sku in item.get("skus", []):
                print(f"  sku_id={sku.get('sku_id')} platform_sku_ids count={len(sku.get('platform_sku_ids') or [])}")
                if sku.get("platform_sku_ids"):
                    print(f"    {json.dumps(sku['platform_sku_ids'][0], ensure_ascii=False)[:300]}")

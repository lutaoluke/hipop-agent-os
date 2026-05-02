"""
通过 navigate 到产品数据页触发 axios 请求来抓 token。
"""
from playwright.sync_api import sync_playwright
import json

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

    # 导航到产品数据页（如果不是的话）
    if "data-statistics/product" not in erp.url:
        erp.goto("https://www.dbuyerp.com/data-statistics/product", wait_until="load", timeout=20000)
    else:
        erp.reload(wait_until="load", timeout=20000)
    erp.wait_for_timeout(5000)

    if not auth["v"]:
        print("还是没抓到 Authorization")
        raise SystemExit(1)
    token = auth["v"].replace("Bearer ", "").strip()
    print(f"token: {token[:30]}...{token[-10:]}")
    print(f"len: {len(token)}")

    # 用这个 token 调几个我们关心的接口
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
    def call(path, params=None):
        return erp.evaluate(JS, {"path": path, "params": params or {}, "token": token})

    print("\n=== product-order-statistics 单 SKU 完整字段 ===")
    r = call("/product-order-statistics", {
        "nation_id": 1, "platform_id": 2,
        "ordered_time_section[]": ["2026-4-23", "2026-4-30"],
        "keyword_type": 1, "page": 1, "limit": 1,
    })
    print(f"status={r['status']}")
    body = json.loads(r["body"])
    if body.get("data"):
        item = body["data"][0]
        print("Top-level keys:", list(item.keys()))
        print("\nsku keys:", list(item.get("sku", {}).keys()))
        # 看 sku 详情
        sku = item.get("sku", {})
        print("\nsku.product_name:", sku.get("product_name") or sku.get("name") or sku.get("title"))
        print("sku.sku_image:", sku.get("sku_image"))
        # 完整 sku
        print("\nFull sku:")
        print(json.dumps(sku, ensure_ascii=False, indent=2)[:2000])

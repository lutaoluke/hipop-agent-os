"""
直接复用 9222 chrome 里已登录的 ERP page 调 API（page.evaluate fetch）。
免去 token 抓取的折腾。
"""
import json
from playwright.sync_api import sync_playwright

JS = """
async ({path, params}) => {
  const u = new URL('https://erp-api.dbuyerp.com/admin' + path);
  for (const [k, v] of Object.entries(params || {})) {
    if (Array.isArray(v)) v.forEach(x => u.searchParams.append(k, x));
    else u.searchParams.set(k, v);
  }
  // token 在 localStorage 或 cookie 里取，先试 localStorage
  let token = '';
  for (const k of Object.keys(localStorage)) {
    const v = localStorage.getItem(k);
    if (v && v.length > 30 && v.length < 2000 && /^[a-zA-Z0-9._-]+$/.test(v)) {
      token = v; break;
    }
  }
  const r = await fetch(u, {headers: {Authorization: 'Bearer ' + token}});
  return {status: r.status, body: await r.text()};
}
"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    erp = next(pg for pg in ctx.pages if "dbuyerp.com" in pg.url)
    print(f"using page: {erp.url}\n")

    def call(path, params=None):
        return erp.evaluate(JS, {"path": path, "params": params or {}})

    print("=== product-order-statistics single ===")
    r = call("/product-order-statistics", {
        "nation_id": 1, "platform_id": 2,
        "ordered_time_section[]": ["2026-4-23", "2026-4-30"],
        "keyword_type": 1, "page": 1, "limit": 1,
    })
    print(f"status={r['status']}")
    body = json.loads(r["body"]) if r["body"].startswith("{") else r["body"][:300]
    if isinstance(body, dict) and body.get("data"):
        print("ALL KEYS in data[0]:", list(body["data"][0].keys()))
        print()
        print(json.dumps(body["data"][0], ensure_ascii=False, indent=2))
    else:
        print(body)

    # 试 sku 详情
    for path in ["/sku/SAB0433A", "/product/SAB0433", "/platform-sku?keyword=SAB0433A"]:
        print(f"\n=== try {path} ===")
        if "?" in path:
            base, q = path.split("?")
            params = dict(p.split("=") for p in q.split("&"))
            r = call(base, params)
        else:
            r = call(path)
        print(f"status={r['status']}, body[:300]={r['body'][:300]}")

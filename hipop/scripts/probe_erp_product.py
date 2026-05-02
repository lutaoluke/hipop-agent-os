"""
嗅探 ERP 产品数据页面的 API 请求。
连 9222 现有 chrome，attach 到 ERP tab，刷新一次，记录所有 erp-api 请求。
"""
import json
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    erp_page = next((pg for pg in ctx.pages if "dbuyerp.com" in pg.url), None)
    if not erp_page:
        print("ERP tab not found")
        raise SystemExit(1)

    captured = []
    def on_request(req):
        if "erp-api" in req.url or "/api/" in req.url:
            captured.append({
                "method": req.method,
                "url": req.url,
                "post_data": req.post_data,
                "resource": req.resource_type,
            })
    erp_page.on("request", on_request)

    print(f"Attached to: {erp_page.url}")
    print("Reloading and capturing for 10s...")
    try:
        erp_page.reload(wait_until="load", timeout=15000)
    except Exception as e:
        print(f"reload warn: {e}")
    erp_page.wait_for_timeout(8000)

    print(f"\nCaptured {len(captured)} api requests:")
    seen = set()
    for r in captured:
        key = (r["method"], r["url"].split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['method']} {r['url'][:200]}")
        if r["post_data"]:
            print(f"     body: {r['post_data'][:300]}")

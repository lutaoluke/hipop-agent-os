"""
找出 ERP 页面里 Authorization token 真正存在哪。
"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    erp = next(pg for pg in ctx.pages if "dbuyerp.com" in pg.url)

    # 监听一次 erp-api 请求拿 Authorization
    captured = []
    def grab(req):
        if "erp-api" in req.url and "authorization" in {h.lower() for h in req.headers}:
            captured.append(req.headers.get("authorization") or req.headers.get("Authorization"))
    erp.on("request", grab)

    # 触发一次 API 调用：刷新或者 evaluate fetch 到任意 erp-api
    erp.evaluate("fetch('https://erp-api.dbuyerp.com/admin/authorization/info').then(r=>r.text())")
    erp.wait_for_timeout(2000)

    print("Captured auth headers:")
    for a in captured:
        print(" ", a[:120])

    # dump storage
    print("\nlocalStorage keys:")
    for k in erp.evaluate("Object.keys(localStorage)"):
        v = erp.evaluate(f"localStorage.getItem({k!r})")
        if v and len(v) > 20:
            print(f"  {k}: {v[:120]}")

    print("\nsessionStorage keys:")
    for k in erp.evaluate("Object.keys(sessionStorage)"):
        v = erp.evaluate(f"sessionStorage.getItem({k!r})")
        if v and len(v) > 20:
            print(f"  {k}: {v[:120]}")

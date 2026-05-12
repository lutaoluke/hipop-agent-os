"""ERP token 按 tenant 拿（解密 tenant_erp_credentials → playwright headless 登录 → 缓存）

主入口:
    get_erp_token_for_tenant(tenant_id: int) -> str | None

机制:
1. SELECT username_enc, password_enc FROM tenant_erp_credentials WHERE tenant_id=?
2. _crypto.decrypt 解出明文
3. playwright headless 登 dbuyerp，拦截 erp-api 请求拿 Authorization: Bearer
4. 缓存 token（per-tenant），20 分钟 TTL
5. 失败 → 返回 None，调用方决定是否报错

不同 tenant 的 ERP 凭据不同，不能复用 _token_cache。
"""
from __future__ import annotations

import os
import time
import threading
from typing import Optional

from . import data as _data
from . import _crypto


_TOKEN_TTL = 20 * 60   # 20 min
_lock = threading.Lock()
_cache: dict = {}      # tenant_id -> {"token": str, "exp": ts}


def _get_creds(tenant_id: int) -> Optional[tuple]:
    """从 DB 解密拿 (username, password, erp_url)。"""
    _data.set_current_tenant(tenant_id)  # 兜底设 RLS context，否则查不到 tenant=N 的凭据
    rows = _data._fetch(
        "SELECT username_enc, password_enc, erp_url FROM tenant_erp_credentials "
        "WHERE tenant_id=?",
        (tenant_id,),
    )
    if not rows:
        return None
    r = rows[0]
    user = _crypto.decrypt(r.get("username_enc"))
    pw   = _crypto.decrypt(r.get("password_enc"))
    url  = r.get("erp_url") or "https://www.dbuyerp.com"
    if not user or not pw:
        return None
    return user, pw, url


def _login_headless(username: str, password: str, erp_url: str) -> Optional[str]:
    """用密码 headless 登 ERP，拦截 erp-api 请求拿 Bearer token。
    伪装真实 Chrome（user-agent + viewport），绕 dbuyerp 对 HeadlessChrome 的风控。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright 未装：pip install playwright && playwright install chromium")

    REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    captured = {"token": None, "after_url": "", "errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=REAL_UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = ctx.new_page()
        # 抹掉 navigator.webdriver
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page.on("request", lambda r: captured.update(
            {"token": r.headers["authorization"].replace("Bearer ", "")}
        ) if r.headers.get("authorization", "").startswith("Bearer ")
          and "erp-api" in r.url and not captured["token"] else None)
        page.on("response", lambda r: captured["errors"].append(
            f"{r.status} {r.url[:80]}"
        ) if r.status >= 400 and "dbuyerp" in r.url else None)

        try:
            page.goto(erp_url, wait_until="networkidle", timeout=20000)
            # dbuyerp 是中文界面 placeholder=账号/密码；老 hipop 内部账号 UA 下可能给英文
            # 用 OR selector + name 兜底
            page.fill(
                'input[name="username"], input[placeholder="账号"], input[placeholder="Username"]',
                username,
            )
            page.fill(
                'input[name="password"], input[placeholder="密码"], input[placeholder="Password"]',
                password,
            )
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)
            captured["after_url"] = page.url
            # 进任意内页让 ERP-API 真发起请求
            page.goto(erp_url + "/#/system/delivery/list",
                      wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(2500)
        except Exception as e:
            captured["errors"].append(f"playwright error: {e}")
        finally:
            ctx.close()
            browser.close()

    if not captured["token"]:
        # 关键诊断信息（之前是静默 None）
        print(
            f"[erp_auth] login FAILED for user={username!r} url={erp_url!r}: "
            f"after_submit_url={captured['after_url']!r} errors={captured['errors'][:5]}"
        )
    return captured["token"]


def get_erp_token_for_tenant(tenant_id: int, force_refresh: bool = False) -> Optional[str]:
    """获取 tenant 的 ERP token。缓存 20 min。"""
    with _lock:
        if not force_refresh:
            entry = _cache.get(tenant_id)
            if entry and time.time() < entry["exp"]:
                return entry["token"]

        creds = _get_creds(tenant_id)
        if not creds:
            return None
        user, pw, url = creds
        token = _login_headless(user, pw, url)
        if token:
            _cache[tenant_id] = {"token": token, "exp": time.time() + _TOKEN_TTL}
        return token


def invalidate(tenant_id: int):
    """token 失效（401）时调用，下次重登。"""
    with _lock:
        _cache.pop(tenant_id, None)

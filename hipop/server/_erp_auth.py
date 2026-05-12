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
    """用密码 headless 登 ERP，拦截 erp-api 请求拿 Bearer token。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright 未装：pip install playwright && playwright install chromium")

    captured = {"token": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("request", lambda r: captured.update(
            {"token": r.headers["authorization"].replace("Bearer ", "")}
        ) if r.headers.get("authorization", "").startswith("Bearer ")
          and "erp-api" in r.url and not captured["token"] else None)

        try:
            page.goto(erp_url, wait_until="networkidle", timeout=20000)
            page.fill('input[placeholder="Username"]', username)
            page.fill('input[placeholder="Password"]', password)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3500)
            # 进任意内页让 ERP-API 真发起请求
            page.goto(erp_url + "/#/system/delivery/list",
                      wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[erp_auth] login error: {e}")
        finally:
            browser.close()
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

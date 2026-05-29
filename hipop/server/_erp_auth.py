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
import json
import threading
from typing import Optional

from . import data as _data
from . import _crypto


_TOKEN_TTL = 20 * 60   # 20 min
_lock = threading.Lock()
_cache: dict = {}      # tenant_id -> {"token": str, "exp": ts}

# dbuyerp 自动登 anti-bot 过不去，改方案：Luke 手动登一次，浏览器持久化
# token 到 ~/hipop/erp_token_<user>.json (有效期 ~2 周)
_PERSIST_DIR = os.path.expanduser("~/hipop")


def check_persist_token_expiry() -> dict:
    """扫所有 ~/hipop/erp_token_*.json，返回各 token 剩余天数 + needs_refresh 总开关。

    needs_refresh=True 时建议跑 skill `/refresh-dbuyerp-token` 刷新（手动登 dbuyerp）。
    在 main.py startup hook 和 /api/health 都用此函数。
    """
    import glob
    results = []
    for path in glob.glob(os.path.join(_PERSIST_DIR, "erp_token_*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            exp_ms = d.get("token_expire_time")
            if exp_ms:
                days_left = round((int(exp_ms) / 1000 - time.time()) / 86400, 1)
            elif d.get("saved_at"):
                # 老格式 fallback：saved + 7 天
                days_left = round((d["saved_at"] + 7 * 86400 - time.time()) / 86400, 1)
            else:
                days_left = -999  # 无效
            results.append({
                "user": d.get("user") or os.path.basename(path),
                "days_left": days_left,
                "expired": days_left < 0,
                "warn": days_left < 3,
                "path": path,
            })
        except Exception:
            pass
    return {
        "tokens": results,
        "needs_refresh": any(r["warn"] for r in results) if results else False,
    }


def _load_persist_token(username: str) -> Optional[dict]:
    """读 ~/hipop/erp_token_<user>.json，返回 {token, exp_ms} 或 None"""
    path = os.path.join(_PERSIST_DIR, f"erp_token_{username}.json")
    try:
        with open(path) as f:
            d = json.load(f)
        tok = d.get("token")
        # token_expire_time 是 dbuyerp cookie 里的毫秒戳；没拿到就用 saved_at + 7 天 fallback
        exp_ms = d.get("token_expire_time")
        if exp_ms and int(exp_ms) / 1000 > time.time() + 300:  # 留 5 min 余量
            return {"token": tok, "exp": int(exp_ms) / 1000}
        # 老格式只有 saved_at，给 7 天有效期
        if d.get("saved_at") and time.time() - d["saved_at"] < 7 * 86400:
            return {"token": tok, "exp": d["saved_at"] + 7 * 86400}
    except Exception:
        pass
    return None


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
            slow_mo=150,  # dbuyerp Vue input 需要节奏，纯 0ms fill 会被当空表单提交
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
            # load 等所有 chunk 下载（dbuyerp 的 chunk-libs.271e5e31.js 比较大）
            page.goto(erp_url, wait_until="load", timeout=45000)
            # 等 input 出现（dbuyerp 是 Vue SPA，要等组件挂载）
            # dbuyerp 是 Vue SPA，commit 后还要等 chunk-libs/elementUI 加载和组件挂载，给足 30s
            page.wait_for_selector(
                'input[name="username"], input[placeholder="账号"], input[placeholder="Username"]',
                timeout=30000,
            )
            user_sel = ('input[name="username"], input[placeholder="账号"], '
                        'input[placeholder="Username"]')
            pw_sel = ('input[name="password"], input[placeholder="密码"], '
                      'input[placeholder="Password"]')
            # fill + dispatchEvent('input') —— Vue v-model 必须收到 input 事件才会更新 data
            page.fill(user_sel, username)
            page.eval_on_selector(user_sel,
                "el => { el.dispatchEvent(new Event('input', {bubbles:true})); "
                "el.dispatchEvent(new Event('change', {bubbles:true})); }")
            page.wait_for_timeout(300)
            page.fill(pw_sel, password)
            page.eval_on_selector(pw_sel,
                "el => { el.dispatchEvent(new Event('input', {bubbles:true})); "
                "el.dispatchEvent(new Event('change', {bubbles:true})); }")
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)
            captured["after_url"] = page.url
            # 进任意内页让 ERP-API 真发起请求
            page.goto(erp_url + "/#/system/delivery/list",
                      wait_until="commit", timeout=30000)
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
    """获取 tenant 的 ERP token。三级回落:
    1) 进程内 _cache (20 min TTL，按 (user,url) key)
    2) 磁盘持久化 ~/hipop/erp_token_<user>.json (Luke 手动登一次, ~2 周有效)
    3) playwright headless 自动登 —— dbuyerp anti-bot 经常过不去，最后兜底
    """
    with _lock:
        creds = _get_creds(tenant_id)
        if not creds:
            return None
        user, pw, url = creds
        cache_key = (user, url)

        # 1) 内存 cache
        if not force_refresh:
            entry = _cache.get(cache_key) or _cache.get(tenant_id)
            if entry and time.time() < entry["exp"]:
                return entry["token"]

        # 2) 磁盘持久化 token (Luke 手动登一次抓的)
        if not force_refresh:
            persisted = _load_persist_token(user)
            if persisted:
                _cache[cache_key] = persisted   # 拉进内存 cache
                return persisted["token"]

        # 3) playwright 自动登 (经常被 dbuyerp anti-bot 拒)
        token = _login_headless(user, pw, url)
        if token:
            _cache[cache_key] = {"token": token, "exp": time.time() + _TOKEN_TTL}
        return token


def invalidate(tenant_id: int):
    """token 失效（401）时调用，下次重登。"""
    with _lock:
        # 清 tenant + (user, url) 两层
        _cache.pop(tenant_id, None)
        try:
            creds = _get_creds(tenant_id)
            if creds:
                _cache.pop((creds[0], creds[2]), None)
        except Exception: pass

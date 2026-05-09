"""
紫鸟开放平台 OpenAPI 客户端 - 复杂鉴权模式

签名协议（docId=136）：
- 所有非 sign 参数按 key 升序拼 k=v，& 连接
- RSA-SHA256 + PKCS#1 v1.5，private key PKCS#8 base64
- Base64 编码后塞 sign 字段
- POST 时整个 JSON body（包括 sign）
- 请求 URL: https://sbappstoreapi.ziniao.com
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config", "hipop.json")

BASE_URL = "https://sbappstoreapi.ziniao.com"


def _load():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _config import load_config as _load_expanded
    return _load_expanded(CFG)


def _load_private_key(b64_pkcs8: str):
    der = base64.b64decode(b64_pkcs8)
    return serialization.load_der_private_key(der, password=None)


def _sign(params: dict, private_key) -> str:
    sorted_keys = sorted(k for k in params.keys() if k != "sign" and params[k] is not None)
    payload = "&".join(f"{k}={params[k]}" for k in sorted_keys).encode("utf-8")
    sig = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def call(method: str,
         biz_content: Optional[dict | str] = None,
         params_content: Optional[dict | str] = None,
         user_access_token: Optional[str] = None,
         app_auth_token: Optional[str] = None,
         http_method: str = "POST") -> dict:
    cfg = _load()["ziniao_openapi"]
    app_id = cfg["app_id"]
    pk = _load_private_key(cfg["app_secret"])

    if isinstance(biz_content, dict):
        biz_content = json.dumps(biz_content, ensure_ascii=False, separators=(",", ":"))
    if isinstance(params_content, dict):
        params_content = json.dumps(params_content, ensure_ascii=False, separators=(",", ":"))

    common = {
        "app_id": app_id,
        "charset": "UTF-8",
        "method": method,
        "format": "json",
        "sign_type": "RSA2",
        "version": "1.0",
        "timestamp": str(int(time.time() * 1000)),
        "sdk_version": "1.0",
    }
    if biz_content is not None:
        common["biz_content"] = biz_content
    if params_content is not None:
        common["params_content"] = params_content
    if user_access_token:
        common["user_access_token"] = user_access_token
    if app_auth_token:
        common["app_auth_token"] = app_auth_token

    common["sign"] = _sign(common, pk)

    if http_method.upper() == "GET":
        r = requests.get(BASE_URL, params=common, timeout=30)
    else:
        r = requests.post(BASE_URL, json=common,
                          headers={"Content-Type": "application/json"},
                          timeout=30)
    try:
        return r.json()
    except Exception:
        return {"http_status": r.status_code, "raw": r.text[:2000]}


def get_app_token() -> dict:
    return call("/auth/get_app_token")


def get_company_info(app_token: str) -> dict:
    return call("/app/builtin/company", app_auth_token=app_token, http_method="GET")


def get_stores(company_id: int | str, app_token: str, page: int = 1, limit: int = 50) -> dict:
    return call("/superbrowser/rest/v1/erp/store/list",
                biz_content={"companyId": str(company_id), "page": page, "limit": limit},
                app_auth_token=app_token)


if __name__ == "__main__":
    import sys
    print("=== /auth/get_app_token ===")
    r = get_app_token()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])
    if not (r.get("code") == "0" or r.get("code") == 0):
        sys.exit(1)
    app_token = (r.get("data") or {}).get("appToken") or (r.get("data") or {}).get("app_token")
    if not app_token:
        # try another schema
        d = r.get("data")
        if isinstance(d, dict):
            app_token = d.get("token") or d.get("access_token")
    print(f"\napp_token = {app_token}\n")

    if app_token:
        print("=== /app/builtin/company ===")
        c = get_company_info(app_token)
        print(json.dumps(c, ensure_ascii=False, indent=2)[:2000])
        company_id = (c.get("data") or {}).get("companyId") if isinstance(c.get("data"), dict) else None
        print(f"\ncompanyId = {company_id}\n")

        if company_id:
            print("=== /superbrowser/rest/v1/erp/store/list ===")
            s = get_stores(company_id, app_token)
            print(json.dumps(s, ensure_ascii=False, indent=2)[:3500])

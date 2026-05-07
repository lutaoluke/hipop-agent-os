"""
Anthropic 客户端工厂：复用 Claude Code 的 OAuth 登录（macOS Keychain）。

优先级:
  1. ANTHROPIC_API_KEY 环境变量（开发者直连 / API quota）
  2. macOS Keychain 中 Claude Code 的 OAuth accessToken（走 claude.ai 订阅 quota）

不需要在 shell 里 export 任何东西——只要本机已用 `claude` CLI 登录过即可。
"""
import os, json, subprocess
from typing import Optional
import anthropic

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_OAUTH_BETA_HEADER = "oauth-2025-04-20"
_cached_client: Optional[anthropic.Anthropic] = None


def _read_keychain_token() -> Optional[str]:
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode().strip()
        return json.loads(raw)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def get_client() -> anthropic.Anthropic:
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    if os.environ.get("ANTHROPIC_API_KEY"):
        _cached_client = anthropic.Anthropic()
        return _cached_client
    tok = _read_keychain_token()
    if not tok:
        raise RuntimeError(
            "未找到 Anthropic 凭证：既无 ANTHROPIC_API_KEY 环境变量，"
            "也未在 Keychain 中找到 Claude Code OAuth token。"
            "请在终端先跑一遍 `claude` 登录，或者 export ANTHROPIC_API_KEY。"
        )
    _cached_client = anthropic.Anthropic(
        auth_token=tok,
        default_headers={"anthropic-beta": _OAUTH_BETA_HEADER},
    )
    return _cached_client


def reset():
    """token 过期后调用，重新从 keychain 读最新值。"""
    global _cached_client
    _cached_client = None

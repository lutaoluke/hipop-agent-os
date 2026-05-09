"""
飞书机器人：消息接收 & 发送
"""
import hashlib
import hmac
import json
import time
import requests

# 从 hipop.json 读取配置
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json as _json

_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "hipop.json")
with open(_cfg_path) as f:
    _cfg = _json.load(f)

APP_ID     = os.environ.get("FEISHU_APP_ID")     or "cli_a96a395aaafa5cb5"
APP_SECRET = os.environ.get("FEISHU_APP_SECRET") or ""
if not APP_SECRET:
    import warnings
    warnings.warn("FEISHU_APP_SECRET env 未设，飞书集成将不可用")

# ── 获取 tenant_access_token（用于发消息）──────────────────
_tat_cache = {}

def _get_tenant_token():
    now = time.time()
    if _tat_cache.get("token") and now < _tat_cache.get("expires_at", 0):
        return _tat_cache["token"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10
    ).json()
    token = resp.get("tenant_access_token")
    _tat_cache["token"] = token
    _tat_cache["expires_at"] = now + resp.get("expire", 7200) - 60
    return token

# ── 发文本消息 ───────────────────────────────────────────
def send_text(chat_id: str, text: str, msg_type="text"):
    token = _get_tenant_token()
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        },
        timeout=10
    )

# ── 发 Markdown 卡片 ─────────────────────────────────────
def send_card(chat_id: str, title: str, content: str, color: str = "blue"):
    token = _get_tenant_token()
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [{"tag": "markdown", "content": content}],
    }
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        },
        timeout=10
    )

# ── 回复某条消息 ─────────────────────────────────────────
def reply_text(message_id: str, text: str):
    token = _get_tenant_token()
    requests.post(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"msg_type": "text", "content": json.dumps({"text": text})},
        timeout=10
    )

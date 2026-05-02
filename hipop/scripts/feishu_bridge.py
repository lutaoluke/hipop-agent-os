"""
飞书桥接：统一封装 Bitable 读写 + IM 推送 + Token 自动 refresh

用法：
  from scripts.feishu_bridge import bridge
  b = bridge()
  rec = b.find_record("main", "ERP-SKU", "TBJ0057A")
  b.update_record("main", rec["record_id"], {"发货在途": "32"})
  b.send_card("标题", "**内容** markdown", color="green")

CLI:
  python3 -m scripts.feishu_bridge --test
"""
import os
import json
import time
import requests
from typing import Optional, List, Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
CARD_COLORS = {"red", "orange", "yellow", "green", "blue", "purple", "grey", "indigo", "carmine", "turquoise"}


class FeishuBridge:
    def __init__(self, config_path: str = CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        fs = cfg["feishu"]
        self.app_id = fs["app_id"]
        self.app_secret = fs["app_secret"]
        self.webhook = fs.get("webhook")
        self.token_file = fs["user_token_file"]
        self.base_id = fs["bitable"]["base_id"]
        self.tables = fs["bitable"]["tables"]
        self._user_token: Optional[str] = None

    # ── Token ──────────────────────────────────────────
    def _load_token_file(self) -> Dict[str, Any]:
        with open(self.token_file, encoding="utf-8") as f:
            return json.load(f)

    def _save_token_file(self, data: Dict[str, Any]) -> None:
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _get_app_token(self) -> str:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        d = r.json()
        if "app_access_token" not in d:
            raise RuntimeError(f"app_access_token 失败: {d}")
        return d["app_access_token"]

    def _refresh_user_token(self) -> str:
        old = self._load_token_file()
        app_token = self._get_app_token()
        last_err = None
        for url in [
            "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
            "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token",
        ]:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
                json={"grant_type": "refresh_token", "refresh_token": old["refresh_token"]},
                timeout=10,
            )
            res = r.json()
            data = res.get("data") or res
            new_token = data.get("access_token")
            if new_token:
                old["user_access_token"] = new_token
                old["access_token"] = new_token
                if data.get("refresh_token"): old["refresh_token"] = data["refresh_token"]
                if data.get("expires_in"): old["expires_in"] = data["expires_in"]
                old["expires_at"] = int(time.time()) + data.get("expires_in", 7200)
                self._save_token_file(old)
                return new_token
            last_err = res
        raise RuntimeError(f"refresh failed: {last_err}")

    def user_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._user_token:
            return self._user_token
        d = self._load_token_file()
        # 还有 5 分钟以上才用缓存的
        if not force_refresh and d.get("expires_at") and d["expires_at"] - 300 > time.time():
            self._user_token = d["user_access_token"]
            return self._user_token
        self._user_token = self._refresh_user_token()
        return self._user_token

    def _hdr(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.user_token()}", "Content-Type": "application/json"}

    def _request(self, method, url, **kw) -> Dict:
        kw.setdefault("timeout", 15)
        kw["headers"] = {**(kw.get("headers") or {}), **self._hdr()}
        r = requests.request(method, url, **kw)
        res = r.json()
        # token 失效自动刷新一次重试
        if res.get("code") == 99991677:
            self.user_token(force_refresh=True)
            kw["headers"] = {**kw["headers"], **self._hdr()}
            r = requests.request(method, url, **kw)
            res = r.json()
        return res

    # ── Bitable ─────────────────────────────────────────
    def _table_id(self, key: str) -> str:
        if key in self.tables: return self.tables[key]
        if key.startswith("tbl"): return key
        raise ValueError(f"未知表 key: {key}")

    def list_records(self, table_key: str, filter: Optional[Dict] = None,
                     field_names: Optional[List[str]] = None, page_size: int = 100,
                     max_records: Optional[int] = None) -> List[Dict]:
        tid = self._table_id(table_key)
        out, page_token = [], None
        while True:
            body = {}
            if filter: body["filter"] = filter
            if field_names: body["field_names"] = field_names
            params = {"page_size": page_size}
            if page_token: params["page_token"] = page_token
            res = self._request("POST",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records/search",
                json=body, params=params)
            if res.get("code") != 0:
                raise RuntimeError(f"list_records failed: {res}")
            out.extend(res["data"].get("items", []))
            if max_records and len(out) >= max_records:
                return out[:max_records]
            if not res["data"].get("has_more"): break
            page_token = res["data"].get("page_token")
        return out

    def find_record(self, table_key: str, field: str, value: Any,
                    field_names: Optional[List[str]] = None) -> Optional[Dict]:
        items = self.list_records(table_key,
            filter={"conjunction": "and", "conditions": [
                {"field_name": field, "operator": "is", "value": [str(value)]}
            ]},
            field_names=field_names, page_size=2, max_records=1)
        return items[0] if items else None

    def insert_record(self, table_key: str, fields: Dict) -> Dict:
        tid = self._table_id(table_key)
        res = self._request("POST",
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records",
            json={"fields": fields})
        if res.get("code") != 0:
            raise RuntimeError(f"insert failed: {res}")
        return res["data"]["record"]

    def batch_insert(self, table_key: str, records: List[Dict], batch_size: int = 500) -> List[Dict]:
        tid = self._table_id(table_key)
        out = []
        for i in range(0, len(records), batch_size):
            chunk = records[i:i+batch_size]
            res = self._request("POST",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records/batch_create",
                json={"records": [{"fields": f} for f in chunk]})
            if res.get("code") != 0:
                raise RuntimeError(f"batch_insert failed: {res}")
            out.extend(res["data"]["records"])
        return out

    def update_record(self, table_key: str, record_id: str, fields: Dict) -> Dict:
        tid = self._table_id(table_key)
        res = self._request("PUT",
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records/{record_id}",
            json={"fields": fields})
        if res.get("code") != 0:
            raise RuntimeError(f"update failed: {res}")
        return res["data"]["record"]

    def batch_update(self, table_key: str, updates: List[Dict], batch_size: int = 500) -> List[Dict]:
        """updates: [{record_id, fields}]"""
        tid = self._table_id(table_key)
        out = []
        for i in range(0, len(updates), batch_size):
            chunk = updates[i:i+batch_size]
            res = self._request("POST",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records/batch_update",
                json={"records": [{"record_id": u["record_id"], "fields": u["fields"]} for u in chunk]})
            if res.get("code") != 0:
                raise RuntimeError(f"batch_update failed: {res}")
            out.extend(res["data"]["records"])
        return out

    def delete_record(self, table_key: str, record_id: str) -> bool:
        tid = self._table_id(table_key)
        res = self._request("DELETE",
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.base_id}/tables/{tid}/records/{record_id}")
        return res.get("code") == 0

    def upsert_by_field(self, table_key: str, key_field: str, key_value: Any, fields: Dict) -> Dict:
        """按 key_field 查找：找到 update，找不到 insert (key_field 自动塞入 fields)"""
        existing = self.find_record(table_key, key_field, key_value, field_names=[key_field])
        if existing:
            return self.update_record(table_key, existing["record_id"], fields)
        full = {key_field: str(key_value), **fields}
        return self.insert_record(table_key, full)

    # ── IM webhook（出站）────────────────────────────────
    def send_text(self, text: str) -> Dict:
        r = requests.post(self.webhook, json={"msg_type": "text", "content": {"text": text}}, timeout=10)
        return r.json()

    def send_card(self, title: str, content: str, color: str = "blue") -> Dict:
        if color not in CARD_COLORS: color = "blue"
        body = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
            },
        }
        r = requests.post(self.webhook, json=body, timeout=10)
        return r.json()


# 单例
_bridge: Optional[FeishuBridge] = None
def bridge() -> FeishuBridge:
    global _bridge
    if _bridge is None:
        _bridge = FeishuBridge()
    return _bridge


# ── CLI 自检 ──────────────────────────────────────────
def _self_test():
    b = bridge()
    print(f"✓ user_token: {b.user_token()[:24]}...")
    rec = b.find_record("main", "ERP-SKU", "TBJ0057A", field_names=["ERP-SKU", "发货在途"])
    if not rec:
        print("✗ 主表找不到 TBJ0057A"); return
    f = rec["fields"]
    in_transit = f.get("发货在途")
    if isinstance(in_transit, list) and in_transit:
        in_transit = in_transit[0].get("text", in_transit)
    print(f"✓ 主表 TBJ0057A.发货在途 = {in_transit}")

    # 子表数验证
    for key in ["alerts", "in_transit", "decisions", "warehouse_appt"]:
        items = b.list_records(key, max_records=1)
        print(f"✓ {key}: 表通，当前 {len(items)} 条记录（已读 max_records=1）")

    # 不发卡片，避免群里刷屏
    print(f"\n（跳过 send_card，避免刷群；调用 b.send_card(title, content, color) 即可发）")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        _self_test()

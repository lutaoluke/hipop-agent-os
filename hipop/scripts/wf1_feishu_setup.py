"""
为每个 sales_entity 在飞书 Bitable 建一张库存数据表 wf1_<alias>_stock。

跑完写回 config: sales_entities[i].feishu_stock_table_id

CLI:
  python3 wf1_feishu_setup.py
  python3 wf1_feishu_setup.py --alias hipop_ksa
"""
import os, sys, json, argparse, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities
from feishu_bridge import bridge

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1/apps"

F_TEXT, F_NUMBER, F_URL = 1, 2, 15

FIELDS = [
    {"name": "partner_sku",          "type": F_TEXT, "is_primary": True},
    {"name": "product_id",           "type": F_TEXT},
    {"name": "noon_sku",             "type": F_TEXT},
    {"name": "title",                "type": F_TEXT},
    {"name": "image_url",            "type": F_URL},
    {"name": "family",               "type": F_TEXT},
    # noon 官方仓
    {"name": "noon_total_qty",       "type": F_NUMBER},
    {"name": "noon_saleable_qty",    "type": F_NUMBER},
    {"name": "noon_unsaleable_qty",  "type": F_NUMBER},
    {"name": "pending_inbound_qty",  "type": F_NUMBER},
    # 海外仓
    {"name": "overseas_total_qty",   "type": F_NUMBER},
    {"name": "overseas_breakdown_json", "type": F_TEXT},
    # 国内仓
    {"name": "yiwu_qty",             "type": F_NUMBER},
    {"name": "dongguan_qty",         "type": F_NUMBER},
    # 合计
    {"name": "total_stock",          "type": F_NUMBER},
    {"name": "as_of_date",           "type": F_TEXT},
]


def _api(method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", {}))
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, {"raw": r.text}


def create_table(b, name):
    primary = next(f for f in FIELDS if f.get("is_primary"))
    body = {"table": {"name": name, "default_view_name": "默认视图",
                      "fields": [{"field_name": primary["name"], "type": primary["type"]}]}}
    code, resp = _api("POST", f"{BITABLE_API}/{b.base_id}/tables", b.user_token(), json=body)
    if resp.get("code") != 0:
        raise RuntimeError(f"create_table failed: {resp}")
    print(f"  created {name} → {resp['data']['table_id']}")
    return resp["data"]["table_id"]


def add_field(b, table_id, field):
    body = {"field_name": field["name"], "type": field["type"]}
    if "property" in field: body["property"] = field["property"]
    code, resp = _api("POST", f"{BITABLE_API}/{b.base_id}/tables/{table_id}/fields", b.user_token(), json=body)
    if resp.get("code") != 0:
        print(f"    [warn] add_field {field['name']} failed: {resp.get('msg') or resp}")


def setup_entity(b, ent):
    alias = ent["alias"]
    table_name = f"wf1_{alias}_stock"
    if ent.get("feishu_stock_table_id"):
        print(f"[{alias}] already has table_id={ent['feishu_stock_table_id']}, skip")
        return ent["feishu_stock_table_id"]
    print(f"[{alias}] creating {table_name}...")
    tid = create_table(b, table_name)
    for f in FIELDS:
        if f.get("is_primary"): continue
        add_field(b, tid, f)
        time.sleep(0.15)
    return tid


def write_back(alias_to_tid):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for ent in cfg.get("sales_entities") or []:
        if ent["alias"] in alias_to_tid:
            ent["feishu_stock_table_id"] = alias_to_tid[ent["alias"]]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[config] wrote {len(alias_to_tid)} table_id(s)")


def run(only_alias=None):
    b = bridge()
    entities = [e for e in load_entities() if not only_alias or e["alias"] == only_alias]
    new_tids = {}
    for ent in entities:
        tid = setup_entity(b, ent)
        if tid and tid != ent.get("feishu_stock_table_id"):
            new_tids[ent["alias"]] = tid
    if new_tids: write_back(new_tids)
    else: print("[done] no new tables")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()
    run(only_alias=args.alias)

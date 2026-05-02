"""
为每个 sales_entity 在飞书 Bitable 自动建一张数据表。

表名：wf2_<alias>_sku（跟数据库表名一致）
字段：跟数据库 wf2_<alias>_sku 表对齐（30 个字段）

跑完后自动把 table_id 写回 hipop.json -> sales_entities[i].feishu_table_id

CLI:
  python3 wf2_feishu_setup.py                    # 给所有还没建飞书表的 entity 建
  python3 wf2_feishu_setup.py --alias hipop_ksa
"""
import os, sys, json, argparse, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities
from feishu_bridge import bridge

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1/apps"

# 飞书 Bitable 字段类型 ID
F_TEXT     = 1   # 多行文本
F_NUMBER   = 2   # 数字
F_SELECT   = 3   # 单选
F_DATE     = 5
F_CHECKBOX = 7
F_URL      = 15  # 超链接

# 字段定义（顺序就是飞书表里列的顺序）
FIELDS = [
    {"name": "partner_sku",          "type": F_TEXT,   "is_primary": True},
    {"name": "product_id",           "type": F_TEXT},
    {"name": "noon_sku",             "type": F_TEXT},
    {"name": "title",                "type": F_TEXT},
    {"name": "image_url",            "type": F_URL},
    {"name": "brand",                "type": F_TEXT},
    {"name": "family",               "type": F_TEXT},
    {"name": "product_category_detail", "type": F_TEXT},
    {"name": "fulfillment",          "type": F_TEXT},
    {"name": "currency",             "type": F_TEXT},

    {"name": "cost_price",           "type": F_NUMBER},
    {"name": "latest_price",         "type": F_NUMBER},
    {"name": "avg_price",            "type": F_NUMBER},
    {"name": "latest_profit_rate",   "type": F_NUMBER, "property": {"formatter": "0.00%"}},

    {"name": "total_orders",         "type": F_NUMBER},
    {"name": "valid_orders",         "type": F_NUMBER},
    {"name": "sales_10d",            "type": F_NUMBER},
    {"name": "sales_30d",            "type": F_NUMBER},
    {"name": "sales_60d",            "type": F_NUMBER},
    {"name": "sales_90d",            "type": F_NUMBER},
    {"name": "sales_120d",           "type": F_NUMBER},
    {"name": "sales_180d",           "type": F_NUMBER},
    {"name": "total_revenue",        "type": F_NUMBER},

    {"name": "return_count",         "type": F_NUMBER},
    {"name": "return_rate",          "type": F_NUMBER, "property": {"formatter": "0.00%"}},
    {"name": "cancel_rate",          "type": F_NUMBER, "property": {"formatter": "0.00%"}},

    {"name": "latest_order_date",    "type": F_TEXT},
    {"name": "as_of_date",           "type": F_TEXT},
    {"name": "erp_created_at",       "type": F_TEXT},
    {"name": "product_choose_admin", "type": F_TEXT},

    {"name": "sales_grade",          "type": F_SELECT,
     "property": {"options": [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]}},
    {"name": "forecast_10d",         "type": F_NUMBER},
    {"name": "forecast_30d",         "type": F_NUMBER},
    {"name": "is_listed",            "type": F_CHECKBOX},

    {"name": "anomalies_json",       "type": F_TEXT},
    {"name": "order_item_nrs_json",  "type": F_TEXT},
]


def _api_call(method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", {}))
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def create_table(b, table_name):
    """创建表，并把第一个字段（partner_sku）作为主键。返回 table_id。"""
    primary = next(f for f in FIELDS if f.get("is_primary"))
    body = {
        "table": {
            "name": table_name,
            "default_view_name": "默认视图",
            "fields": [{"field_name": primary["name"], "type": primary["type"]}],
        }
    }
    url = f"{BITABLE_API}/{b.base_id}/tables"
    code, resp = _api_call("POST", url, b.user_token(), json=body)
    if resp.get("code") != 0:
        raise RuntimeError(f"create_table failed: HTTP {code} {resp}")
    tid = resp["data"]["table_id"]
    print(f"  created table {table_name} → {tid}")
    return tid


def add_field(b, table_id, field):
    """加一个字段。失败时打印不抛错（可能字段已存在）。"""
    body = {"field_name": field["name"], "type": field["type"]}
    if "property" in field:
        body["property"] = field["property"]
    url = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/fields"
    code, resp = _api_call("POST", url, b.user_token(), json=body)
    if resp.get("code") != 0:
        msg = resp.get("msg") or resp
        print(f"    [warn] add_field {field['name']} failed: {msg}")
        return False
    return True


def setup_entity_table(b, ent):
    alias = ent["alias"]
    table_name = f"wf2_{alias}_sku"
    if ent.get("feishu_table_id"):
        print(f"[{alias}] already has feishu_table_id={ent['feishu_table_id']}, skip")
        return ent["feishu_table_id"]

    print(f"[{alias}] creating feishu table {table_name}...")
    table_id = create_table(b, table_name)

    # 添加除主键外的字段
    for f in FIELDS:
        if f.get("is_primary"):
            continue
        add_field(b, table_id, f)
        time.sleep(0.15)   # 飞书 API 限流

    return table_id


def write_back_config(alias_to_tid):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for ent in cfg.get("sales_entities") or []:
        if ent["alias"] in alias_to_tid:
            ent["feishu_table_id"] = alias_to_tid[ent["alias"]]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[config] wrote {len(alias_to_tid)} table_id(s) to {CONFIG_PATH}")


def run(only_alias=None):
    b = bridge()
    entities = [e for e in load_entities() if not only_alias or e["alias"] == only_alias]
    if not entities:
        sys.exit(f"no entity matches alias={only_alias}")

    new_tids = {}
    for ent in entities:
        tid = setup_entity_table(b, ent)
        if tid and tid != ent.get("feishu_table_id"):
            new_tids[ent["alias"]] = tid

    if new_tids:
        write_back_config(new_tids)
    else:
        print("[done] no new tables created")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()
    run(only_alias=args.alias)

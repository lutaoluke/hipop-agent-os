"""
为每个 sales_entity 在飞书 Bitable 建一张「经营决策」数据表。

表名：wf5_<alias>_decisions
跑完写回 config: sales_entities[i].feishu_decisions_table_id

CLI:
  python3 wf5_feishu_setup.py
  python3 wf5_feishu_setup.py --alias hipop_ksa
"""
import os, sys, json, argparse, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities
from feishu_bridge import bridge

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1/apps"

F_TEXT, F_NUMBER, F_SELECT, F_CHECKBOX = 1, 2, 3, 7

# 字段定义（顺序就是飞书表里列的顺序）
FIELDS = [
    {"name": "SKU",            "type": F_TEXT, "is_primary": True},   # = partner_sku
    {"name": "趋势",           "type": F_SELECT,
     "property": {"options": [{"name": s} for s in
       ["加速增长", "增长", "平稳", "波动", "下降", "急速下降", "无销量"]]}},
    {"name": "日均销量",       "type": F_NUMBER},
    {"name": "预测10天",       "type": F_NUMBER},
    {"name": "预测30天",       "type": F_NUMBER},
    {"name": "断货风险",       "type": F_SELECT,
     "property": {"options": [{"name": s} for s in ["无", "在途断货", "到齐后断货"]]}},
    {"name": "当前管道量",     "type": F_NUMBER},
    {"name": "目标管道量",     "type": F_NUMBER},
    {"name": "wf5建议补货",    "type": F_NUMBER},
    {"name": "丢货必补",       "type": F_NUMBER},
    {"name": "本周必补总量",   "type": F_NUMBER},
    {"name": "触发原因",       "type": F_TEXT},
    {"name": "紧急度",         "type": F_SELECT,
     "property": {"options": [{"name": s} for s in ["立即", "本周", "正常", "无需采购"]]}},
    {"name": "运营建议",       "type": F_TEXT},
    {"name": "周标签",         "type": F_TEXT},
    # 运营填写
    {"name": "操作状态",       "type": F_SELECT,
     "property": {"options": [{"name": s} for s in
       ["未处理", "进行中", "已下单", "已完成", "已取消"]]}},
    {"name": "实际下单数",     "type": F_NUMBER},
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
    primary = next(f for f in FIELDS if f.get("is_primary"))
    body = {"table": {"name": table_name, "default_view_name": "默认视图",
                      "fields": [{"field_name": primary["name"], "type": primary["type"]}]}}
    code, resp = _api_call("POST", f"{BITABLE_API}/{b.base_id}/tables", b.user_token(), json=body)
    if resp.get("code") != 0:
        raise RuntimeError(f"create_table failed: {resp}")
    print(f"  created {table_name} → {resp['data']['table_id']}")
    return resp["data"]["table_id"]


def add_field(b, table_id, field):
    body = {"field_name": field["name"], "type": field["type"]}
    if "property" in field:
        body["property"] = field["property"]
    code, resp = _api_call("POST",
        f"{BITABLE_API}/{b.base_id}/tables/{table_id}/fields", b.user_token(), json=body)
    if resp.get("code") != 0:
        print(f"    [warn] add_field {field['name']} failed: {resp.get('msg') or resp}")


def setup_entity(b, ent):
    alias = ent["alias"]
    table_name = f"wf5_{alias}_decisions"
    if ent.get("feishu_decisions_table_id"):
        print(f"[{alias}] already has table_id={ent['feishu_decisions_table_id']}, skip")
        return ent["feishu_decisions_table_id"]
    print(f"[{alias}] creating {table_name}...")
    tid = create_table(b, table_name)
    for f in FIELDS:
        if f.get("is_primary"):
            continue
        add_field(b, tid, f)
        time.sleep(0.15)
    return tid


def write_back(alias_to_tid):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for ent in cfg.get("sales_entities") or []:
        if ent["alias"] in alias_to_tid:
            ent["feishu_decisions_table_id"] = alias_to_tid[ent["alias"]]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[config] wrote {len(alias_to_tid)} table_id(s)")


def run(only_alias=None):
    b = bridge()
    entities = [e for e in load_entities() if not only_alias or e["alias"] == only_alias]
    new_tids = {}
    for ent in entities:
        tid = setup_entity(b, ent)
        if tid and tid != ent.get("feishu_decisions_table_id"):
            new_tids[ent["alias"]] = tid
    if new_tids:
        write_back(new_tids)
    else:
        print("[done] no new tables")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()
    run(only_alias=args.alias)

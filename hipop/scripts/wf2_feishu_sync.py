"""
工作流二飞书同步：把每个销售主体的 wf2_<alias>_sku 数据库表同步到对应飞书 Bitable 数据表。

流程：
  1. 读 config 拿 sales_entities 和每个 entity 的 feishu_table_id（由 wf2_feishu_setup 写入）
  2. 对每个 entity：
     a. 飞书 list_records 拿所有现有记录的 partner_sku → record_id
     b. 数据库读全表
     c. 对每行：飞书已有 → batch_update；无 → batch_create
  3. 批量 API（每批 500 条），减少 API 调用

CLI:
  python3 wf2_feishu_sync.py
  python3 wf2_feishu_sync.py --alias hipop_ksa
"""
import os, sys, json, sqlite3, argparse, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, sku_table
from feishu_bridge import bridge
from wf2_feishu_setup import FIELDS

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1/apps"
BATCH_SIZE = 500

# 字段名 → 类型（用于值转换）
FIELD_TYPE_MAP = {f["name"]: f["type"] for f in FIELDS}

# 飞书字段类型常量
F_TEXT, F_NUMBER, F_SELECT, F_DATE, F_CHECKBOX, F_URL = 1, 2, 3, 5, 7, 15


def to_feishu(value, field_type):
    """数据库值 → 飞书字段值。None / 空跳过。"""
    if value is None or value == "":
        return None
    if field_type == F_URL:
        s = str(value)
        return {"link": s, "text": s[:60]}
    if field_type == F_CHECKBOX:
        return bool(value)
    if field_type == F_SELECT:
        return str(value)
    if field_type == F_NUMBER:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return str(value)


def row_to_fields(row):
    """db row dict → 飞书 fields dict（跳过 None 和空字符串）"""
    out = {}
    for f in FIELDS:
        name = f["name"]
        if name not in row.keys():
            continue
        v = to_feishu(row[name], f["type"])
        if v is not None:
            out[name] = v
    return out


def list_all_records(b, table_id):
    """全量翻 records，返回 partner_sku → record_id。"""
    out = {}
    page_token = None
    url_base = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records"
    while True:
        params = {"page_size": 500, "field_names": json.dumps(["partner_sku"])}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            url_base, params=params,
            headers={"Authorization": f"Bearer {b.user_token()}"},
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"list_records failed: {d}")
        data = d.get("data") or {}
        for rec in data.get("items") or []:
            psk = (rec.get("fields") or {}).get("partner_sku")
            if isinstance(psk, list) and psk:
                psk = psk[0].get("text") if isinstance(psk[0], dict) else psk[0]
            if psk:
                out[psk] = rec["record_id"]
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return out


def batch_create(b, table_id, items):
    """items = [{fields: {...}}, ...]"""
    if not items:
        return
    url = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records/batch_create"
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i+BATCH_SIZE]
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {b.user_token()}", "Content-Type": "application/json"},
            json={"records": chunk},
            timeout=60,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"batch_create failed: {d}")
        print(f"    batch_create {len(chunk)} rows ✓", file=sys.stderr)
        time.sleep(0.3)


def batch_update(b, table_id, items):
    """items = [{record_id, fields}, ...]"""
    if not items:
        return
    url = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records/batch_update"
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i+BATCH_SIZE]
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {b.user_token()}", "Content-Type": "application/json"},
            json={"records": chunk},
            timeout=60,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"batch_update failed: {d}")
        print(f"    batch_update {len(chunk)} rows ✓", file=sys.stderr)
        time.sleep(0.3)


def sync_entity(b, ent):
    alias    = ent["alias"]
    table_id = ent.get("feishu_table_id")
    if not table_id:
        print(f"[{alias}] no feishu_table_id, run wf2_feishu_setup first. skip.", file=sys.stderr)
        return

    print(f"\n[{alias}] syncing wf2_{alias}_sku → feishu table {table_id}", file=sys.stderr)
    print(f"  fetching existing records...", file=sys.stderr)
    existing = list_all_records(b, table_id)
    print(f"  feishu has {len(existing)} existing records", file=sys.stderr)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {sku_table(alias)}").fetchall()
    conn.close()
    print(f"  db has {len(rows)} sku rows", file=sys.stderr)

    to_create = []
    to_update = []
    for row in rows:
        psk = row["partner_sku"]
        fields = row_to_fields(row)
        if psk in existing:
            to_update.append({"record_id": existing[psk], "fields": fields})
        else:
            to_create.append({"fields": fields})

    print(f"  → create={len(to_create)}, update={len(to_update)}", file=sys.stderr)
    batch_create(b, table_id, to_create)
    batch_update(b, table_id, to_update)


def run(alias=None):
    b = bridge()
    entities = [e for e in load_entities() if not alias or e["alias"] == alias]
    if not entities:
        sys.exit(f"no entity matches alias={alias}")
    for ent in entities:
        sync_entity(b, ent)
    print("\n[done]", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()
    run(alias=args.alias)

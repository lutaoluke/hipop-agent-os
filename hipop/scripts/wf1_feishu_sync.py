"""
工作流一飞书同步：每个 sales_entity 的 wf1_<alias>_stock → 对应飞书数据表。

流程同 wf2_feishu_sync：list 现有 records → 比对 → batch_create 新 + batch_update 已存。

CLI:
  python3 wf1_feishu_sync.py
  python3 wf1_feishu_sync.py --alias hipop_ksa
"""
import os, sys, json, sqlite3, argparse, time, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, stock_table
from feishu_bridge import bridge
from wf1_feishu_setup import FIELDS

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
BITABLE_API = "https://open.feishu.cn/open-apis/bitable/v1/apps"
BATCH_SIZE = 500

FIELD_TYPE = {f["name"]: f["type"] for f in FIELDS}

F_TEXT, F_NUMBER, F_URL = 1, 2, 15


def to_feishu(value, field_type):
    if value is None or value == "":
        return None
    if field_type == F_URL:
        s = str(value); return {"link": s, "text": s[:60]}
    if field_type == F_NUMBER:
        try: return float(value)
        except (TypeError, ValueError): return None
    return str(value)


def row_to_fields(row):
    out = {}
    for f in FIELDS:
        n = f["name"]
        if n not in row.keys(): continue
        v = to_feishu(row[n], f["type"])
        if v is not None: out[n] = v
    return out


def list_records(b, table_id):
    out = {}
    page_token = None
    base = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records"
    while True:
        params = {"page_size": 500, "field_names": json.dumps(["partner_sku"])}
        if page_token: params["page_token"] = page_token
        for attempt in range(5):
            try:
                r = requests.get(base, params=params,
                                 headers={"Authorization": f"Bearer {b.user_token()}", "Connection": "close"},
                                 timeout=30)
                d = r.json()
                break
            except Exception as e:
                print(f"  [list retry {attempt+1}] {type(e).__name__}", file=sys.stderr)
                time.sleep(2)
        if d.get("code") != 0:
            raise RuntimeError(f"list_records failed: {d}")
        for rec in d.get("data", {}).get("items") or []:
            psk = (rec.get("fields") or {}).get("partner_sku")
            if isinstance(psk, list) and psk:
                psk = psk[0].get("text") if isinstance(psk[0], dict) else str(psk[0])
            if psk: out[psk] = rec["record_id"]
        if not d["data"].get("has_more"): break
        page_token = d["data"].get("page_token")
    return out


def batch_create(b, table_id, items):
    if not items: return
    url = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records/batch_create"
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i+BATCH_SIZE]
        for attempt in range(5):
            try:
                r = requests.post(url,
                    headers={"Authorization": f"Bearer {b.user_token()}", "Content-Type": "application/json", "Connection": "close"},
                    json={"records": chunk}, timeout=60)
                d = r.json()
                break
            except Exception as e:
                print(f"  [create retry {attempt+1}] {type(e).__name__}", file=sys.stderr)
                time.sleep(3)
        if d.get("code") != 0:
            raise RuntimeError(f"batch_create failed: {d}")
        print(f"    create {len(chunk)} ✓", file=sys.stderr)
        time.sleep(0.3)


def batch_update(b, table_id, items):
    if not items: return
    url = f"{BITABLE_API}/{b.base_id}/tables/{table_id}/records/batch_update"
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i+BATCH_SIZE]
        for attempt in range(5):
            try:
                r = requests.post(url,
                    headers={"Authorization": f"Bearer {b.user_token()}", "Content-Type": "application/json", "Connection": "close"},
                    json={"records": chunk}, timeout=60)
                d = r.json()
                break
            except Exception as e:
                print(f"  [update retry {attempt+1}] {type(e).__name__}", file=sys.stderr)
                time.sleep(3)
        if d.get("code") != 0:
            raise RuntimeError(f"batch_update failed: {d}")
        print(f"    update {len(chunk)} ✓", file=sys.stderr)
        time.sleep(0.3)


def sync_entity(b, ent):
    alias = ent["alias"]
    table_id = ent.get("feishu_stock_table_id")
    if not table_id:
        print(f"[{alias}] no feishu_stock_table_id, run wf1_feishu_setup first.", file=sys.stderr)
        return

    print(f"\n[{alias}] syncing {stock_table(alias)} → feishu {table_id}", file=sys.stderr)
    existing = list_records(b, table_id)
    print(f"  existing feishu records: {len(existing)}", file=sys.stderr)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {stock_table(alias)}").fetchall()
    conn.close()
    print(f"  db rows: {len(rows)}", file=sys.stderr)

    to_create, to_update = [], []
    for row in rows:
        psk = row["partner_sku"]
        fields = row_to_fields(row)
        if psk in existing:
            to_update.append({"record_id": existing[psk], "fields": fields})
        else:
            to_create.append({"fields": fields})
    print(f"  → create={len(to_create)} update={len(to_update)}", file=sys.stderr)
    batch_create(b, table_id, to_create)
    batch_update(b, table_id, to_update)


def run(alias=None):
    b = bridge()
    entities = [e for e in load_entities() if not alias or e["alias"] == alias]
    for ent in entities:
        sync_entity(b, ent)
    print("\n[done]", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()
    run(alias=args.alias)

"""ingest_noon_stock_csv v2 — Noon my inventory → v2 wf1_stock.noon_* producer

WS-10。背景：v2 `wf1_stock.noon_*` 当前是一次性 backfill（updated_at 卡在
2026-05-09），全仓没有任何活跃 v2 脚本写它；唯一的 noon 写入器
`ingest_noon_stock_csv`(v1) 走的是 stale 的 per-alias `wf1_<alias>_stock`。
本模块补上 noon my inventory → v2 `wf1_stock` 的 producer。

签名：run_v2(tenant_id: int, file=None, inbox=None, dry_run=False) -> dict

机制（对齐 v1 ingest_noon_stock_csv 的聚合口径，但落 v2 单表）：
- CSV 每行 = (warehouse × SKU × inventory_type) 的库存条目
- 按 country_code 路由 entity（get_entity_by_country）
- SKU 键：优先 partner_sku 列；没有则用平台 SKU（sku/noon_sku 列）经
  sales_entity_v2.noon_sku_map 回到 partner_sku
- 按 partner_sku 聚合：
    noon_total_qty       = SUM(qty)
    noon_saleable_qty    = SUM(qty WHERE inventory_type='saleable')
    noon_unsaleable_qty  = noon_total - noon_saleable
    noon_warehouses_json = [{warehouse_code, qty, inventory_type}, ...]
- **部分 upsert**：只写 noon_* 四列 + updated_at，绝不碰 ERP 写的
  yiwu/dongguan/overseas/total_stock，也不碰 pending_inbound_qty（归 WS-11）。
- 绝不写 v1 `wf1_<alias>_stock`（active runtime = v2，PK 见 WS-9 核实）。

CLI:
  python3 ingest_noon_stock_csv_v2.py --tenant 1 --file <Inventory.csv>
  python3 ingest_noon_stock_csv_v2.py --tenant 1            # 扫 inbox/
"""
from __future__ import annotations

import os
import sys
import csv
import json
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from sales_entity_v2 import get_entity_by_country, noon_sku_map
from server import data as _data

INBOX_DIR = os.path.join(HERE, "..", "..", "inbox")

# noon 库存类型，可售只算 saleable
SALEABLE_TYPES = {"saleable"}

# 部分 upsert：只动 noon_* 四列，保护 ERP 列与 pending_inbound_qty
_NOON_COLS = ("noon_total_qty", "noon_saleable_qty",
              "noon_unsaleable_qty", "noon_warehouses_json")


def safe_int(v):
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def is_inventory_csv(path) -> bool:
    """noon Inventory CSV 必含 qty + inventory_type + country_code，
    以及 partner_sku 或 sku(平台 SKU) 之一。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            cols = set(csv.DictReader(f).fieldnames or [])
        base = {"qty", "inventory_type", "country_code"}.issubset(cols)
        keyed = ("partner_sku" in cols) or ("sku" in cols) or ("noon_sku" in cols)
        return base and keyed
    except Exception:
        return False


def _resolve_partner_sku(row, sku_map) -> str | None:
    """优先显式 partner_sku；否则平台 SKU 经映射回 partner_sku。"""
    psk = (row.get("partner_sku") or "").strip()
    if psk:
        return psk
    noon_sku = (row.get("noon_sku") or row.get("sku") or "").strip()
    if noon_sku:
        return sku_map.get(noon_sku)
    return None


def _aggregate(path, tenant_id):
    """读 CSV → {entity_alias: {partner_sku: agg}}，并统计映射未命中行。"""
    bucket = defaultdict(lambda: defaultdict(lambda: {
        "noon_total_qty": 0,
        "noon_saleable_qty": 0,
        "noon_unsaleable_qty": 0,
        "warehouses": [],
        "noon_sku": None,
    }))
    entities_by_country: dict[str, dict] = {}
    sku_maps: dict[str, dict] = {}
    n_rows = 0
    n_unmapped = 0

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            n_rows += 1
            country = (row.get("country_code") or "").strip().upper()
            if not country:
                continue
            if country not in entities_by_country:
                entities_by_country[country] = get_entity_by_country(tenant_id, country)
            ent = entities_by_country[country]
            if not ent:
                continue  # 不在该 tenant 的销售主体白名单
            alias = ent["alias"]
            if alias not in sku_maps:
                sku_maps[alias] = noon_sku_map(tenant_id, alias)

            partner_sku = _resolve_partner_sku(row, sku_maps[alias])
            if not partner_sku:
                n_unmapped += 1
                continue

            qty = safe_int(row.get("qty"))
            inv_type = (row.get("inventory_type") or "").strip().lower()
            agg = bucket[alias][partner_sku]
            agg["noon_total_qty"] += qty
            if inv_type in SALEABLE_TYPES:
                agg["noon_saleable_qty"] += qty
            else:
                agg["noon_unsaleable_qty"] += qty
            agg["warehouses"].append({
                "warehouse_code": (row.get("warehouse_code") or "").strip(),
                "qty": qty,
                "inventory_type": inv_type,
            })
            if not agg["noon_sku"]:
                agg["noon_sku"] = (row.get("noon_sku") or row.get("sku") or "").strip() or None

    return bucket, n_rows, n_unmapped


def _upsert(conn, tenant_id, bucket) -> dict:
    ts = "datetime('now','localtime')"
    cols = ["tenant_id", "entity_alias", "partner_sku", *_NOON_COLS]
    placeholders = ",".join(["?"] * len(cols))
    update_set = ",".join(f"{c}=excluded.{c}" for c in _NOON_COLS) + f", updated_at={ts}"
    sql = (
        f"INSERT INTO wf1_stock ({','.join(cols)}, imported_at, updated_at) "
        f"VALUES ({placeholders}, {ts}, {ts}) "
        f"ON CONFLICT (tenant_id, entity_alias, partner_sku) DO UPDATE SET {update_set}"
    )
    counts = {}
    for alias, skus in bucket.items():
        n = 0
        for psk, agg in skus.items():
            conn.execute(sql, (
                tenant_id, alias, psk,
                agg["noon_total_qty"], agg["noon_saleable_qty"],
                agg["noon_unsaleable_qty"],
                json.dumps(agg["warehouses"], ensure_ascii=False),
            ))
            n += 1
        counts[alias] = n
    conn.commit()
    return counts


def run_v2(tenant_id: int, file: str | None = None,
           inbox: str | None = None, dry_run: bool = False) -> dict:
    print(f"\n=== ingest_noon_stock v2 tenant={tenant_id} ===", file=sys.stderr)
    inbox = inbox or INBOX_DIR

    if file:
        files = [file]
    else:
        files = []
        if os.path.isdir(inbox):
            for fn in sorted(os.listdir(inbox)):
                if fn.endswith(".csv") and not fn.startswith("."):
                    full = os.path.join(inbox, fn)
                    if is_inventory_csv(full):
                        files.append(full)
    if not files:
        print(f"[ingest_noon_stock_v2] no inventory CSV ({file or inbox})", file=sys.stderr)
        return {"files": 0, "rows": 0, "skus": 0, "unmapped": 0, "by_alias": {}}

    _data.set_current_tenant(tenant_id)
    conn = _data.conn()
    total_rows = total_unmapped = 0
    by_alias: dict[str, int] = {}
    try:
        for path in files:
            bucket, n_rows, n_unmapped = _aggregate(path, tenant_id)
            total_rows += n_rows
            total_unmapped += n_unmapped
            print(f"  {os.path.basename(path)}: {n_rows} rows, "
                  f"{n_unmapped} unmapped", file=sys.stderr)
            if dry_run:
                continue
            counts = _upsert(conn, tenant_id, bucket)
            for alias, n in counts.items():
                by_alias[alias] = by_alias.get(alias, 0) + n
                print(f"  [{alias}] {n} skus → wf1_stock.noon_*", file=sys.stderr)
    finally:
        conn.close()

    result = {
        "files": len(files),
        "rows": total_rows,
        "skus": sum(by_alias.values()),
        "unmapped": total_unmapped,
        "by_alias": by_alias,
    }
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--file", default=None)
    ap.add_argument("--inbox", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_v2(args.tenant, file=args.file, inbox=args.inbox, dry_run=args.dry_run)

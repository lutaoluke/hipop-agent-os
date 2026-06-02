"""ingest_erp_stock v2 — 真多租户

签名：run_v2(tenant_id: int, max_pages=None)

机制：跟 ingest_erp_stock.run() 同逻辑，但：
- token 通过 _erp_auth.get_erp_token_for_tenant(tenant_id) 拿
- 写到 wf1_stock v2 表（按 tenant_id + entity_alias + partner_sku 主键）

注：sales_entity（v2 schema）和 sales_entity_v1 (config/hipop.json) 字段一致，
所以可以直接复用 sales_entity 模块的 WAREHOUSES / overseas_warehouses_for / domestic_warehouses。
"""
from __future__ import annotations

import os
import sys
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from ingest_erp_stock import (
    fetch_warehouse_stock, has_store_binding, safe_int,
)
from sales_entity import (
    WAREHOUSES, overseas_warehouses_for, domestic_warehouses,
)
from sales_entity_v2 import list_entities_for_tenant
from server._erp_auth import get_erp_token_for_tenant
from server import data as _data
try:
    from server.runtime import tick, set_progress
except ImportError:
    tick = lambda *a, **k: None
    set_progress = lambda *a, **k: None


def run_v2(tenant_id: int, max_pages: int | None = None,
           token: str | None = None, fetch_fn=None) -> dict:
    """拉 ERP 各仓库存 → wf1_stock 的 ERP 列（v2）。

    受控配置 / 可测性：
    - token：默认从受审计的 _erp_auth.get_erp_token_for_tenant 取（env/runtime
      secret，不在代码/fixture 里）。测试可注入 fake token。
    - fetch_fn：默认真实 HTTP fetch_warehouse_stock；测试可注入 fixture fetcher。
    - **缺 token 直接 raise（红灯），绝不回落到 backfill / 假数据。**
    """
    print(f"\n=== ingest_erp_stock v2 tenant={tenant_id} ===", file=sys.stderr)

    entities = list_entities_for_tenant(tenant_id)
    if not entities:
        raise RuntimeError(f"tenant={tenant_id} 没配 sales_entities")

    token = token or get_erp_token_for_tenant(tenant_id)
    if not token:
        # 红灯：不写库、不回落旧数据，让上游知道 ERP 库存这条断了。
        raise RuntimeError(f"tenant={tenant_id} ERP token 拿不到（缺失/过期）；"
                           f"拒绝回落到 backfill/假数据")
    fetch = fetch_fn or fetch_warehouse_stock

    conn = _data.conn()
    today_iso = datetime.now().date().isoformat()
    bucket = {e["alias"]: {} for e in entities}

    # 拉每个仓（去重）
    needed_wh = set(domestic_warehouses())
    for ent in entities:
        needed_wh.update(overseas_warehouses_for(ent["country"]))
    print(f"[warehouses] {sorted(needed_wh)}", file=sys.stderr)

    wh_total = len(sorted(needed_wh))
    for wh_idx, wid in enumerate(sorted(needed_wh), 1):
        w = WAREHOUSES[wid]
        print(f"\n[wh {wid} {w['name']} ({w['scope']}/{w['country'] or '-'})]", file=sys.stderr)
        tick(f"warehouse {wh_idx}/{wh_total}: {w['name']}")
        items = fetch(token, wid, max_pages=max_pages)
        set_progress({"warehouses_done": wh_idx, "warehouses_total": wh_total, "current_wh": w['name']})
        for it in items:
            partner_sku = it.get("sku_id")
            if not partner_sku:
                continue
            qty = safe_int(it.get("stock_total_available_count"))
            for ent in entities:
                if not has_store_binding(it, ent["store"]):
                    continue
                rec = bucket[ent["alias"]].setdefault(partner_sku, {
                    "partner_sku": partner_sku,
                    "yiwu_qty": 0, "dongguan_qty": 0,
                    "overseas_total_qty": 0,
                    "_overseas_breakdown": {},
                })
                if w["alias"] == "yiwu":       rec["yiwu_qty"] = qty
                elif w["alias"] == "dongguan": rec["dongguan_qty"] = qty
                elif w["scope"] == "overseas" and w["country"] == ent["country"]:
                    rec["overseas_total_qty"] += qty
                    if qty:
                        rec["_overseas_breakdown"][w["name"]] = qty

    # 写库
    counts = {}
    ts_expr = "datetime('now','localtime')"
    for alias, recs in bucket.items():
        cols = ["tenant_id", "entity_alias", "partner_sku",
                "yiwu_qty", "dongguan_qty", "overseas_total_qty",
                "overseas_breakdown_json", "total_stock"]
        placeholders = ",".join(["?"] * len(cols))
        update_set = ",".join(f"{c}=excluded.{c}" for c in cols
                               if c not in ("tenant_id", "entity_alias", "partner_sku"))
        update_set += f", imported_at={ts_expr}, updated_at={ts_expr}"
        sql = (
            f"INSERT INTO wf1_stock ({','.join(cols)}, imported_at, updated_at) "
            f"VALUES ({placeholders}, {ts_expr}, {ts_expr}) "
            f"ON CONFLICT (tenant_id, entity_alias, partner_sku) DO UPDATE SET {update_set}"
        )
        n = 0
        for rec in recs.values():
            total_stock = (rec.get("yiwu_qty", 0) + rec.get("dongguan_qty", 0)
                           + rec.get("overseas_total_qty", 0))
            ob = rec.get("_overseas_breakdown") or {}
            try:
                conn.execute(sql, (
                    tenant_id, alias, rec["partner_sku"],
                    rec["yiwu_qty"], rec["dongguan_qty"], rec["overseas_total_qty"],
                    json.dumps(ob, ensure_ascii=False) if ob else None,
                    total_stock,
                ))
                n += 1
            except Exception as e:
                print(f"[{alias}] row fail: {str(e)[:100]}", file=sys.stderr)
                break
        conn.commit()
        counts[alias] = n
        print(f"[{alias}] +{n} stock rows", file=sys.stderr)

    conn.close()
    print(f"\n[done] tenant={tenant_id} {counts}", file=sys.stderr)
    return counts


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run_v2(args.tenant, max_pages=args.max_pages)

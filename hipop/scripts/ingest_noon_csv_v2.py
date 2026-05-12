"""ingest_noon_csv v2 — 真多租户：直接写 v2 表（wf2_orders / wf2_sku 列存）

签名：process_csv_v2(tenant_id, csv_path, conn, dry_run=False)
- tenant_id 从 onboarding 配的 sales_entities 拿对应 entity_alias
- 不再走 hipop.json，从 DB sales_entities 表查
- 同时写 v2 wf2_orders + 更新 wf2_sku.title/image_url 等元信息
- 老物理切表 wf2_<alias>_orders 不写（旧路径走原 ingest_noon_csv.py）
"""
from __future__ import annotations

import os
import sys
import json
import csv

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 复用旧 ingest_noon_csv 的解析助手
from ingest_noon_csv import (
    COLUMN_MAP, STATUS_CANCELLED, STATUS_RETURN,
    build_header_index, get_col, parse_money, parse_date,
    country_from_filename,
)
# 走包路径，避免 sys.modules 双实例导致 contextvar 不共享（PG RLS 会拒）
import importlib
try:
    _sev2 = importlib.import_module("hipop.scripts.sales_entity_v2")
    get_entity_by_country = _sev2.get_entity_by_country
except ModuleNotFoundError:
    from sales_entity_v2 import get_entity_by_country  # type: ignore


def process_csv_v2(tenant_id: int, path: str, conn,
                   entity_alias: str = None, dry_run: bool = False) -> int:
    """
    写 v2 表（wf2_orders + wf2_sku 元信息更新），按 tenant_id 隔离。

    Returns: 处理的订单行数
    """
    print(f"\n=== [tenant={tenant_id}] {path} ===", file=sys.stderr)

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("  (empty CSV)", file=sys.stderr)
            return 0

        # 决定 entity_alias：参数 > 文件名国别推断
        alias = entity_alias
        if not alias:
            country = country_from_filename(path)
            if not country:
                print("  [skip] cannot infer country from filename", file=sys.stderr)
                return 0
            ent = get_entity_by_country(tenant_id, country)
            if not ent:
                print(f"  [skip] tenant={tenant_id} no entity for country={country}",
                      file=sys.stderr)
                return 0
            alias = ent["alias"]
        print(f"  → tenant={tenant_id} entity={alias}", file=sys.stderr)

        if dry_run:
            return 0

        header = reader.fieldnames
        header_idx = build_header_index(header)
        cur = conn.cursor()
        n = 0
        sku_meta = {}

        for row in reader:
            partner_sku = get_col(row, header_idx, COLUMN_MAP["partner_sku"])
            item_nr     = get_col(row, header_idx, COLUMN_MAP["item_nr"])
            noon_sku    = get_col(row, header_idx, COLUMN_MAP["noon_sku"])
            if not (partner_sku and item_nr):
                continue
            status   = get_col(row, header_idx, COLUMN_MAP["status"]) or ""
            status_l = status.strip().lower()
            is_cancelled = 1 if status_l in STATUS_CANCELLED else 0
            is_return    = 1 if status_l in STATUS_RETURN else 0

            seller_price, cur_a  = parse_money(get_col(row, header_idx, COLUMN_MAP["seller_price"]))
            customer_paid, cur_b = parse_money(get_col(row, header_idx, COLUMN_MAP["customer_paid"]))
            currency   = get_col(row, header_idx, COLUMN_MAP["currency"]) or cur_a or cur_b
            order_date = parse_date(get_col(row, header_idx, COLUMN_MAP["order_date"]))
            fulfillment = get_col(row, header_idx, COLUMN_MAP["fulfillment"])

            cur.execute("""
                INSERT INTO wf2_orders
                  (tenant_id, entity_alias,
                   partner_sku, noon_sku, item_nr, order_date, status,
                   is_cancelled, is_return, seller_price, customer_paid, currency,
                   fulfillment, destination, source, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'noon', ?)
                ON CONFLICT(tenant_id, entity_alias, partner_sku, item_nr) DO UPDATE SET
                  noon_sku=excluded.noon_sku,
                  order_date=excluded.order_date,
                  status=excluded.status,
                  is_cancelled=excluded.is_cancelled,
                  is_return=excluded.is_return,
                  seller_price=excluded.seller_price,
                  customer_paid=excluded.customer_paid,
                  currency=excluded.currency,
                  fulfillment=excluded.fulfillment,
                  destination=excluded.destination,
                  imported_at=datetime('now','localtime')
            """, (
                tenant_id, alias,
                partner_sku, noon_sku, item_nr, order_date, status,
                is_cancelled, is_return, seller_price, customer_paid, currency,
                fulfillment,
                get_col(row, header_idx, COLUMN_MAP["destination"]),
                json.dumps({k: row.get(k) for k in header}, ensure_ascii=False),
            ))
            n += 1

            # 累积 SKU 元信息（标题/图片/品牌 — CSV 第一行有）
            if partner_sku not in sku_meta:
                sku_meta[partner_sku] = {
                    "noon_sku":    noon_sku,
                    "title":       get_col(row, header_idx, COLUMN_MAP["title"]),
                    "image_url":   get_col(row, header_idx, COLUMN_MAP["image_url"]),
                    "fulfillment": fulfillment,
                    "family":      get_col(row, header_idx, COLUMN_MAP["family"]),
                    "brand":       get_col(row, header_idx, COLUMN_MAP["brand"]),
                }

        # 把 SKU 元信息 upsert 到 wf2_sku（首次见的 SKU 自动建条记录，之后只填空字段）
        for sku, meta in sku_meta.items():
            cur.execute("""
                INSERT INTO wf2_sku
                  (tenant_id, entity_alias, partner_sku, noon_sku, title, image_url,
                   fulfillment, family, brand, is_listed, imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,1, datetime('now','localtime'))
                ON CONFLICT(tenant_id, entity_alias, partner_sku) DO UPDATE SET
                  noon_sku    = COALESCE(wf2_sku.noon_sku,    excluded.noon_sku),
                  title       = COALESCE(wf2_sku.title,       excluded.title),
                  image_url   = COALESCE(wf2_sku.image_url,   excluded.image_url),
                  fulfillment = COALESCE(wf2_sku.fulfillment, excluded.fulfillment),
                  family      = COALESCE(wf2_sku.family,      excluded.family),
                  brand       = COALESCE(wf2_sku.brand,       excluded.brand),
                  is_listed   = 1,
                  imported_at = datetime('now','localtime')
            """, (
                tenant_id, alias, sku,
                meta.get("noon_sku"), meta.get("title"), meta.get("image_url"),
                meta.get("fulfillment"), meta.get("family"), meta.get("brand"),
            ))

        conn.commit()
        print(f"  [done] tenant={tenant_id} entity={alias}: "
              f"{n} order rows, {len(sku_meta)} sku meta updates",
              file=sys.stderr)
        return n


def aggregate_sales_v2(tenant_id: int, entity_alias: str, conn) -> int:
    """从 wf2_orders 重算 wf2_sku 的 sales_10/30/60/90/120/180d。
    Returns: 更新的 SKU 数。
    """
    import datetime as _dt
    cur = conn.cursor()

    def _scalar(row):
        """sqlite Row 用 row[0]，PG RealDictRow 用 next(iter(row.values()))。"""
        if row is None: return None
        if isinstance(row, dict): return next(iter(row.values()))
        return row[0]

    # 获取该 tenant+entity 所有 SKU
    skus = [_scalar(r) for r in cur.execute(
        "SELECT DISTINCT partner_sku FROM wf2_orders WHERE tenant_id=? AND entity_alias=?",
        (tenant_id, entity_alias)
    ).fetchall()]
    today = _dt.date.today()
    n = 0
    for sku in skus:
        windows = {}
        for days in [10, 30, 60, 90, 120, 180]:
            cutoff = (today - _dt.timedelta(days=days)).isoformat()
            count = _scalar(cur.execute(
                "SELECT COUNT(*) FROM wf2_orders "
                "WHERE tenant_id=? AND entity_alias=? AND partner_sku=? "
                "AND is_cancelled=0 "
                "AND order_date >= ?",
                (tenant_id, entity_alias, sku, cutoff)
            ).fetchone()) or 0
            windows[f"sales_{days}d"] = count
        cur.execute(
            "UPDATE wf2_sku SET "
            "sales_10d=?, sales_30d=?, sales_60d=?, sales_90d=?, sales_120d=?, sales_180d=? "
            "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
            (windows["sales_10d"], windows["sales_30d"], windows["sales_60d"],
             windows["sales_90d"], windows["sales_120d"], windows["sales_180d"],
             tenant_id, entity_alias, sku)
        )
        n += 1
    conn.commit()
    return n

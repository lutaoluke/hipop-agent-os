"""sales_entity v2 — 多租户 + 列存（Phase A，2026-05-09）

关键变化：
- 数据源从 hipop.json 文件 → DB sales_entities 表（按 tenant_id 隔离）
- 业务表名不再 hipop 硬编码：wf2_<alias>_sku → wf2_sku WHERE tenant_id=? AND entity_alias=?
- 同时**保持向后兼容**：旧 sales_entity.load_entities() 仍可用（fallback 到 hipop.json）

主要 API:
    list_entities_for_tenant(tenant_id)       → [{alias, country, platform, store, store_id, currency}]
    get_entity(tenant_id, alias)              → entity dict 或 None
    upsert_entity(tenant_id, alias, ...)      → 创建/更新
    sku_filter(tenant_id, entity_alias)       → 用于 query 的 (sql_where, params)
    sku_table_v2()                            → 'wf2_sku'  (统一表名)
"""
from __future__ import annotations

import os
import sys
from typing import List, Dict, Optional, Tuple

# data 模块在 server 包里
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # hipop/
# 强制走 hipop.server.data 同一个 module 实例（避免 sys.path hack 导致 contextvar 不共享，
# 否则 RLS 上下文与 FastAPI 请求里的不是同一个，会被 RLS 拒绝）。
import importlib
try:
    _data = importlib.import_module("hipop.server.data")
except ModuleNotFoundError:
    from server import data as _data  # 仅 CLI 直跑时回退


# ── 表名常量（v2 列存版）───────────────────────────────
T_SKU            = "wf2_sku"
T_ORDERS         = "wf2_orders"
T_STOCK          = "wf1_stock"
T_SALES_CYCLE    = "wf5_sales_cycle"
T_REPLENISH_Q    = "wf6_replenishment_queue_v2"
T_LOGISTICS_HUB  = "wf3_logistics_hub_v2"
T_LOGISTICS_ALERTS = "wf6_logistics_alerts_v2"


# ── tenant 配置：sales_entities 表 CRUD ────────────────
def list_entities_for_tenant(tenant_id: int) -> List[Dict]:
    """返回某租户配置的全部销售主体。"""
    _data.set_current_tenant(tenant_id)  # 兜底，让 PG RLS USING 子句通过
    rows = _data._fetch(
        "SELECT id, tenant_id, alias, country, platform, store_name AS store, "
        "store_id, currency, feishu_table_id, feishu_decisions_table_id, "
        "feishu_stock_table_id, active "
        "FROM sales_entities WHERE tenant_id=? AND active=1 ORDER BY id",
        (tenant_id,),
    )
    return rows


def get_entity(tenant_id: int, alias: str) -> Optional[Dict]:
    _data.set_current_tenant(tenant_id)
    rows = _data._fetch(
        "SELECT id, tenant_id, alias, country, platform, store_name AS store, "
        "store_id, currency FROM sales_entities WHERE tenant_id=? AND alias=? AND active=1",
        (tenant_id, alias),
    )
    return rows[0] if rows else None


def get_entity_by_country(tenant_id: int, country: str) -> Optional[Dict]:
    """按国别拿（CSV ingest 路由用）。"""
    _data.set_current_tenant(tenant_id)
    rows = _data._fetch(
        "SELECT id, tenant_id, alias, country, platform, store_name AS store, "
        "store_id, currency FROM sales_entities "
        "WHERE tenant_id=? AND country=? AND active=1 LIMIT 1",
        (tenant_id, country),
    )
    return rows[0] if rows else None


def upsert_entity(
    tenant_id: int, alias: str, country: str, platform: str,
    store_name: str, store_id: Optional[int] = None,
    currency: Optional[str] = None,
    feishu_table_id: Optional[str] = None,
    feishu_decisions_table_id: Optional[str] = None,
    feishu_stock_table_id: Optional[str] = None,
) -> int:
    """upsert，返回 id"""
    existing = get_entity(tenant_id, alias)
    if existing:
        with _data.conn() as c:
            c.execute(
                "UPDATE sales_entities SET country=?, platform=?, store_name=?, "
                "store_id=?, currency=?, feishu_table_id=?, "
                "feishu_decisions_table_id=?, feishu_stock_table_id=? "
                "WHERE id=?",
                (country, platform, store_name, store_id, currency,
                 feishu_table_id, feishu_decisions_table_id, feishu_stock_table_id,
                 existing["id"]),
            )
            c.commit()
        return existing["id"]
    is_pg = _data.is_postgres()
    sql = (
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency, "
        "feishu_table_id, feishu_decisions_table_id, feishu_stock_table_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)"
        + (" RETURNING id" if is_pg else "")
    )
    # 兜底 set RLS 上下文 — 避免 ContextVar 在 sys.path 双 module / 跨线程场景下丢失
    _data.set_current_tenant(tenant_id)
    with _data.conn() as c:
        cur = c.execute(
            sql,
            (tenant_id, alias, country, platform, store_name, store_id, currency,
             feishu_table_id, feishu_decisions_table_id, feishu_stock_table_id),
        )
        if is_pg:
            row = cur.fetchone()
            c.commit()
            return row["id"] if isinstance(row, dict) else row[0]
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── query helper：把 (tenant_id, entity_alias) 编成 WHERE ──
def filter_clause(tenant_id: int, entity_alias: Optional[str] = None,
                  prefix: str = "") -> Tuple[str, tuple]:
    """返回 (where_sql, params)，用于 query 业务表的 v2 版本。

    例:
        sql, params = filter_clause(1, 'hipop_ksa')
        # → ("WHERE tenant_id=? AND entity_alias=?", (1, 'hipop_ksa'))

        sql, params = filter_clause(1, None, prefix="t.")
        # → ("WHERE t.tenant_id=?", (1,))
    """
    parts = [f"{prefix}tenant_id=?"]
    params: list = [tenant_id]
    if entity_alias:
        parts.append(f"{prefix}entity_alias=?")
        params.append(entity_alias)
    return "WHERE " + " AND ".join(parts), tuple(params)


# ── Noon 平台 SKU ↔ partner_sku 映射（v2 ingest 路由用）──
def noon_sku_map(tenant_id: int, entity_alias: Optional[str] = None) -> Dict[str, str]:
    """返回 {noon_sku(Z 开头平台 SKU): partner_sku}，取自 wf2_sku.noon_sku。

    Noon my inventory / ASN 明细常以平台 SKU 为键，用它回到 partner_sku，
    保证写 wf1_stock / staging 时主键 (tenant, entity, partner_sku) 对齐。
    """
    _data.set_current_tenant(tenant_id)  # 兜底 PG RLS
    where, params = filter_clause(tenant_id, entity_alias)
    rows = _data._fetch(
        f"SELECT noon_sku, partner_sku FROM {T_SKU} {where} "
        f"AND noon_sku IS NOT NULL AND noon_sku != ''",
        params,
    )
    return {r["noon_sku"]: r["partner_sku"] for r in rows if r.get("partner_sku")}


# ── 兼容旧 API（hipop.json fallback）────────────────────
def load_entities_legacy() -> List[Dict]:
    """旧 sales_entity.load_entities() — 从 hipop.json 读，hipop 老代码用。"""
    import json
    cfg_path = os.path.join(HERE, "..", "config", "hipop.json")
    with open(cfg_path) as f:
        return json.load(f).get("sales_entities") or []

"""把 hipop 老物理切表数据迁到 v2 列存表（保 alpha 不断线）

迁移映射：
  wf2_hipop_ksa_sku            → wf2_sku WHERE tenant_id=1 AND entity_alias='hipop_ksa'
  wf2_hipop_uae_sku            → wf2_sku WHERE tenant_id=1 AND entity_alias='hipop_uae'
  wf2_hipop_<a>_orders         → wf2_orders
  wf1_hipop_<a>_stock          → wf1_stock
  wf5_hipop_<a>_sales_cycle    → wf5_sales_cycle
  wf6_hipop_<a>_replenishment_queue → wf6_replenishment_queue_v2
  wf3_logistics_hub            → wf3_logistics_hub_v2 (加 tenant_id=1)
  wf6_logistics_alerts         → wf6_logistics_alerts_v2 (加 tenant_id=1)

幂等：用 INSERT OR REPLACE，可重复跑

跑法：
    python3 scripts/migrate_to_v2_columnar.py [--dry-run] [--alias hipop_ksa]
"""
from __future__ import annotations

import os
import sys
import sqlite3
import argparse

DB_PATH = os.environ.get("HIPOP_DB", "/Users/luke/Downloads/点购工作流/hipop.db")
HIPOP_TENANT = 1

# (源表 pattern, 目标 v2 表, 主键列)
PER_ENTITY_TABLES = [
    # ("wf2_hipop_ksa_sku", "wf2_sku")
    {"src_prefix": "wf2_", "src_suffix": "_sku",
     "dst": "wf2_sku", "pk": ["partner_sku"]},
    {"src_prefix": "wf2_", "src_suffix": "_orders",
     "dst": "wf2_orders", "pk": ["partner_sku", "item_nr"]},
    {"src_prefix": "wf1_", "src_suffix": "_stock",
     "dst": "wf1_stock", "pk": ["partner_sku"]},
    {"src_prefix": "wf5_", "src_suffix": "_sales_cycle",
     "dst": "wf5_sales_cycle", "pk": ["partner_sku"]},
    {"src_prefix": "wf6_", "src_suffix": "_replenishment_queue",
     "dst": "wf6_replenishment_queue_v2", "pk": ["partner_sku", "alert_id"]},
]

# tenant 级共享表（不按 entity 切）
SHARED_TABLES = [
    {"src": "wf3_logistics_hub", "dst": "wf3_logistics_hub_v2", "pk": ["sku"]},
    {"src": "wf6_logistics_alerts", "dst": "wf6_logistics_alerts_v2", "pk": ["alert_id"]},
]


def _list_entity_aliases(conn) -> list:
    """从 sales_entities 表读 hipop tenant 配的所有 entity alias"""
    rows = conn.execute(
        "SELECT alias FROM sales_entities WHERE tenant_id=? AND active=1",
        (HIPOP_TENANT,)
    ).fetchall()
    return [r[0] for r in rows]


def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(r)


def _columns(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate_per_entity(conn, alias: str, dry_run: bool = False) -> dict:
    out = {}
    for spec in PER_ENTITY_TABLES:
        src = f"{spec['src_prefix']}hipop_{alias.replace('hipop_', '')}{spec['src_suffix']}"
        # 容错：实际命名 wf2_hipop_ksa_sku（alias 已含 hipop_ 前缀，所以裁一下）
        # 修正：alias = 'hipop_ksa'，src 应该是 wf2_hipop_ksa_sku
        src = f"{spec['src_prefix']}{alias}{spec['src_suffix']}"
        dst = spec["dst"]
        if not _table_exists(conn, src):
            out[src] = "skip (no source table)"
            continue
        src_cols = _columns(conn, src)
        dst_cols = _columns(conn, dst)
        # 只迁两张表都有的列（除 tenant_id / entity_alias 由 migrator 注入）
        common = [c for c in src_cols if c in dst_cols and c not in ("tenant_id", "entity_alias")]
        if not common:
            out[src] = "skip (no common columns)"
            continue
        n_src = conn.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
        if dry_run:
            out[src] = f"DRY: would migrate {n_src} rows → {dst}"
            continue
        col_list = ",".join(common)
        placeholders = ",".join(["?"] * (len(common) + 2))  # +2 for tenant_id, entity_alias
        sql = (
            f"INSERT OR REPLACE INTO {dst} (tenant_id, entity_alias, {col_list}) "
            f"SELECT {HIPOP_TENANT}, '{alias}', {col_list} FROM {src}"
        )
        conn.execute(sql)
        n_dst = conn.execute(
            f"SELECT COUNT(*) FROM {dst} WHERE tenant_id=? AND entity_alias=?",
            (HIPOP_TENANT, alias),
        ).fetchone()[0]
        out[src] = f"+{n_src} rows → {dst} (now {n_dst} for {alias})"
    conn.commit()
    return out


def migrate_shared(conn, dry_run: bool = False) -> dict:
    out = {}
    for spec in SHARED_TABLES:
        src, dst = spec["src"], spec["dst"]
        if not _table_exists(conn, src):
            out[src] = "skip (no source)"
            continue
        src_cols = _columns(conn, src)
        dst_cols = _columns(conn, dst)
        common = [c for c in src_cols if c in dst_cols and c != "tenant_id"]
        if not common:
            out[src] = "skip (no common columns)"
            continue
        n_src = conn.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
        if dry_run:
            out[src] = f"DRY: would migrate {n_src} rows → {dst}"
            continue
        col_list = ",".join(common)
        sql = (
            f"INSERT OR REPLACE INTO {dst} (tenant_id, {col_list}) "
            f"SELECT {HIPOP_TENANT}, {col_list} FROM {src}"
        )
        conn.execute(sql)
        n_dst = conn.execute(
            f"SELECT COUNT(*) FROM {dst} WHERE tenant_id=?", (HIPOP_TENANT,)
        ).fetchone()[0]
        out[src] = f"+{n_src} → {dst} (now {n_dst})"
    conn.commit()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--alias", help="只迁某个 alias（默认全迁）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    aliases = _list_entity_aliases(conn)
    if args.alias:
        aliases = [a for a in aliases if a == args.alias]

    print(f"=== migrate to v2 columnar (tenant={HIPOP_TENANT}, dry={args.dry_run}) ===")
    print(f"Source DB: {DB_PATH}")
    print(f"Aliases:   {aliases}")
    print()

    for alias in aliases:
        print(f"--- entity {alias} ---")
        for src, msg in migrate_per_entity(conn, alias, args.dry_run).items():
            print(f"  {src}: {msg}")

    print(f"\n--- shared tables ---")
    for src, msg in migrate_shared(conn, args.dry_run).items():
        print(f"  {src}: {msg}")

    conn.close()
    print("\n[done]")


if __name__ == "__main__":
    main()

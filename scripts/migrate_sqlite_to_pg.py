"""SQLite → Postgres 一次性迁移脚本

用法:
    DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
      python3 scripts/migrate_sqlite_to_pg.py [--sqlite hipop.db] [--dry-run] [--table TABLE]

行为:
- 先确保 PG 端 schema 已建（docker-compose 自动跑过 db/schema.sql；如未跑会报缺表）
- 按表 dump SQLite 数据 → INSERT into PG（带 ON CONFLICT DO UPDATE 兼容重跑）
- JSON 字段（_json 后缀）自动转 JSONB
- 时间字段自动从 'YYYY-MM-DD HH:MM:SS' 字符串 → TIMESTAMPTZ

不会做的事:
- 不做 schema 反向比对（schema 需先在 PG 建好）
- 不删 PG 端已有数据（用 ON CONFLICT 增量）
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3
import argparse
from typing import List, Dict, Any

DEFAULT_SQLITE = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")

# 表迁移清单 + 主键（用于 ON CONFLICT）
TABLE_PLAN = [
    # (table_name, primary_key_cols)
    ("agent_events", ("id",)),
    ("agent_actions", ("id",)),
    ("chat_messages", ("id",)),
    ("feishu_digest", ("id",)),
    ("sa_main", ("partner_sku",)),
    ("wf2_hipop_ksa_sku", ("partner_sku",)),
    ("wf2_hipop_uae_sku", ("partner_sku",)),
    ("wf2_hipop_ksa_orders", ("partner_sku", "item_nr")),
    ("wf2_hipop_uae_orders", ("partner_sku", "item_nr")),
    ("wf1_hipop_ksa_stock", ("partner_sku",)),
    ("wf1_hipop_uae_stock", ("partner_sku",)),
    ("wf3_logistics_hub", ("sku",)),
    ("wf5_hipop_ksa_sales_cycle", ("partner_sku",)),
    ("wf5_hipop_uae_sales_cycle", ("partner_sku",)),
    ("wf6_logistics_alerts", ("alert_id",)),
    ("wf6_hipop_ksa_replenishment_queue", ("partner_sku", "alert_id")),
    ("wf6_hipop_uae_replenishment_queue", ("partner_sku", "alert_id")),
]

JSON_COL_SUFFIX = "_json"


def pg_url() -> str:
    url = os.environ.get("DB_URL")
    if not url or not url.startswith(("postgresql://", "postgres://")):
        sys.exit("DB_URL 未设或不是 postgres 协议。示例:\n"
                 "  export DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
    return url


def to_json_or_passthrough(col: str, val: Any) -> Any:
    """JSON 列：SQLite 存的是 TEXT(JSON 字符串)，PG 期望 dict/list 或字符串都行（psycopg2 自动）"""
    if val is None:
        return None
    if col.endswith(JSON_COL_SUFFIX) and isinstance(val, str):
        # 验证是合法 JSON，不是的话原样穿过去（PG 会报错让我们看见问题）
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def dump_table(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows], cols


def upsert(pg_conn, table: str, cols: List[str], rows: List[Dict], pk: tuple):
    if not rows:
        return 0
    placeholders = ",".join(["%s"] * len(cols))
    update_set = ",".join(
        f'"{c}"=EXCLUDED."{c}"' for c in cols if c not in pk
    )
    sql = (
        f'INSERT INTO "{table}" ({",".join(f"{chr(34)}{c}{chr(34)}" for c in cols)}) '
        f"VALUES ({placeholders}) "
    )
    if update_set:
        sql += (
            f'ON CONFLICT ({",".join(f"{chr(34)}{c}{chr(34)}" for c in pk)}) '
            f"DO UPDATE SET {update_set}"
        )
    else:
        sql += "ON CONFLICT DO NOTHING"

    with pg_conn.cursor() as cur:
        for r in rows:
            values = tuple(to_json_or_passthrough(c, r.get(c)) for c in cols)
            cur.execute(sql, values)
    pg_conn.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default=DEFAULT_SQLITE)
    ap.add_argument("--table", help="只迁移这一张表（调试用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sqlite_path = os.path.abspath(args.sqlite)
    if not os.path.exists(sqlite_path):
        sys.exit(f"SQLite 文件不存在: {sqlite_path}")

    try:
        import psycopg2
        from psycopg2.extras import Json
    except ImportError:
        sys.exit("需要 psycopg2-binary：pip install psycopg2-binary")

    print(f"[migrate] sqlite={sqlite_path}")
    print(f"[migrate] target={pg_url()}")

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    if args.dry_run:
        print("[dry-run] 不连 PG，只 dump 行数：")
        for table, _ in TABLE_PLAN:
            if args.table and args.table != table: continue
            try:
                n = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table}: {n} 行")
            except sqlite3.OperationalError as e:
                print(f"  {table}: SKIP ({e})")
        return

    pg_conn = psycopg2.connect(pg_url())

    # 注册 dict/list → JSONB 自动转换
    psycopg2.extensions.register_adapter(dict, Json)
    psycopg2.extensions.register_adapter(list, Json)

    total = 0
    for table, pk in TABLE_PLAN:
        if args.table and args.table != table: continue
        try:
            rows, cols = dump_table(sqlite_conn, table)
        except sqlite3.OperationalError as e:
            print(f"[{table}] SKIP（SQLite 端不存在）: {e}")
            continue
        if not rows:
            print(f"[{table}] empty, skip")
            continue
        try:
            n = upsert(pg_conn, table, cols, rows, pk)
            total += n
            print(f"[{table}] +{n} rows")
        except Exception as e:
            print(f"[{table}] FAIL: {e}")
            pg_conn.rollback()
            continue

    pg_conn.close()
    sqlite_conn.close()
    print(f"\n[done] 共迁移 {total} 行")


if __name__ == "__main__":
    main()

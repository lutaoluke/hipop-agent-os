"""stock_history — wf1_stock 的按业务日历史快照层（WS-22）

latest 层 `wf1_stock`（PK tenant_id+entity_alias+partner_sku）保持"当前快照"，
现有 wf5 / 工作台读取完全不变。本模块新增 dated 层 `wf1_stock_history`
（PK 多一个 as_of_date），把某个**业务日**的 latest 全量冻结进去 —— 同一 SKU 多天
并存、互不覆盖，供 WS-12 按历史日期抽检回溯。

签名：
  record_snapshot(conn, tenant_id, as_of_date, entity_alias=None) -> dict
  run_v2(tenant_id, as_of_date, entity_alias=None) -> dict        # registry / CLI 入口
  read_snapshot(conn, tenant_id, entity_alias, partner_sku, as_of_date) -> dict | None
  list_dates(conn, tenant_id, entity_alias=None) -> list[str]

承重墙（三种死法）：
  · 占位假数据：as_of_date 是**必填运行参数**，缺失/格式非法直接 raise；
    **绝不取 today，也不从 imported_at 反推**。冻结时把 latest 的 imported_at 另存到
    source_imported_at（审计用），as_of_date 永远等于调用方传进来的业务日。
  · 接线缺失：runner(wf1_stock_snapshot_v2) + WORKFLOW_REGISTRY + verifier 都接上，
    operator 能按日期触发；read_snapshot/list_dates 是 WS-12 的查询入口。
  · 死代码短路：latest 层照常 upsert（覆盖当前快照），历史靠本表多日并存，
    两层各管各的，互不覆盖。

CLI:
  python3 stock_history.py --tenant 1 --as-of-date 2026-05-31
"""
from __future__ import annotations

import os
import re
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from server import data as _data

HISTORY_TABLE = "wf1_stock_history"

# 从 latest wf1_stock 冻结进 history 的业务列（与 db/schema_v2.sql 两表对齐）
_BUSINESS_COLS = (
    "noon_total_qty", "noon_saleable_qty", "noon_unsaleable_qty",
    "noon_warehouses_json", "pending_inbound_qty",
    "overseas_total_qty", "overseas_breakdown_json",
    "yiwu_qty", "dongguan_qty", "total_stock",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_as_of_date(as_of_date) -> str:
    """校验业务日 → 'YYYY-MM-DD'。

    缺失 / 非法格式一律 raise —— 这是挡"占位假数据"的门：调用方**必须**给真实业务日，
    不允许靠默认值悄悄回落到 today。
    """
    if as_of_date is None:
        raise ValueError(
            "as_of_date 必填（业务日 YYYY-MM-DD）；拒绝回落到 today/imported_at 假数据"
        )
    s = str(as_of_date).strip()
    # 兼容传进来带时间的 'YYYY-MM-DD HH:MM:SS' / ISO，截前 10 位
    if len(s) >= 10 and (s[10:11] in (" ", "T")):
        s = s[:10]
    if not _DATE_RE.match(s):
        raise ValueError(f"as_of_date 格式非法：{as_of_date!r}（应为 YYYY-MM-DD 业务日）")
    return s


def ensure_history_table(conn) -> None:
    """建 history 表（幂等）。DDL 与 db/schema_v2.sql 中 wf1_stock_history 一致；
    用于已跑过旧 schema 的库 / 测试库的兜底，CREATE IF NOT EXISTS 不会覆盖既有表。"""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            tenant_id                  BIGINT NOT NULL,
            entity_alias               TEXT NOT NULL,
            partner_sku                TEXT NOT NULL,
            as_of_date                 TEXT NOT NULL,
            noon_total_qty             INT,
            noon_saleable_qty          INT,
            noon_unsaleable_qty        INT,
            noon_warehouses_json       TEXT,
            pending_inbound_qty        INT,
            overseas_total_qty         INT,
            overseas_breakdown_json    TEXT,
            yiwu_qty                   INT,
            dongguan_qty               INT,
            total_stock                INT,
            source_imported_at         TEXT,
            snapshot_at                TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku, as_of_date)
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_tenant "
        f"ON {HISTORY_TABLE}(tenant_id, entity_alias, as_of_date)"
    )
    conn.commit()


def _row_get(row, key):
    """sqlite3.Row / PG RealDictRow 统一取值。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[key]  # sqlite3.Row 支持按列名下标


def record_snapshot(conn, tenant_id: int, as_of_date, entity_alias: str | None = None) -> dict:
    """把 latest `wf1_stock` 的当前行按业务日 as_of_date 冻结进 `wf1_stock_history`。

    - as_of_date：必填业务日（normalize_as_of_date 校验）。**不取 today / 不从
      imported_at 反推** —— latest 的 imported_at 另存到 source_imported_at 仅作审计。
    - entity_alias：给则只冻结该 entity，否则冻结该 tenant 全部 entity。
    - 重跑同一业务日 → ON CONFLICT 覆盖该日快照（同业务日重算是幂等的）。
    返回 {"as_of_date": ..., "rows": N, "by_alias": {...}}。
    """
    aod = normalize_as_of_date(as_of_date)
    ensure_history_table(conn)

    sel_cols = "tenant_id, entity_alias, partner_sku, imported_at, " + ",".join(_BUSINESS_COLS)
    where = "WHERE tenant_id=?"
    params: list = [tenant_id]
    if entity_alias:
        where += " AND entity_alias=?"
        params.append(entity_alias)
    src_rows = conn.execute(
        f"SELECT {sel_cols} FROM wf1_stock {where}", tuple(params)
    ).fetchall()

    ts = "datetime('now','localtime')"
    ins_cols = (["tenant_id", "entity_alias", "partner_sku", "as_of_date"]
                + list(_BUSINESS_COLS) + ["source_imported_at"])
    placeholders = ",".join(["?"] * len(ins_cols))
    update_set = (",".join(f"{c}=excluded.{c}" for c in _BUSINESS_COLS)
                  + ", source_imported_at=excluded.source_imported_at"
                  + f", snapshot_at={ts}")
    sql = (
        f"INSERT INTO {HISTORY_TABLE} ({','.join(ins_cols)}, snapshot_at) "
        f"VALUES ({placeholders}, {ts}) "
        f"ON CONFLICT (tenant_id, entity_alias, partner_sku, as_of_date) "
        f"DO UPDATE SET {update_set}"
    )

    by_alias: dict[str, int] = {}
    for r in src_rows:
        alias = _row_get(r, "entity_alias")
        vals = [tenant_id, alias, _row_get(r, "partner_sku"), aod]
        vals += [_row_get(r, c) for c in _BUSINESS_COLS]
        vals.append(_row_get(r, "imported_at"))
        conn.execute(sql, tuple(vals))
        by_alias[alias] = by_alias.get(alias, 0) + 1
    conn.commit()

    return {"as_of_date": aod, "rows": sum(by_alias.values()), "by_alias": by_alias}


def read_snapshot(conn, tenant_id: int, entity_alias: str, partner_sku: str,
                  as_of_date) -> dict | None:
    """WS-12 历史抽检入口：读某业务日的库存快照行（官方仓/海外仓/义乌/东莞/pending 等）。"""
    aod = normalize_as_of_date(as_of_date)
    row = conn.execute(
        f"SELECT * FROM {HISTORY_TABLE} "
        f"WHERE tenant_id=? AND entity_alias=? AND partner_sku=? AND as_of_date=?",
        (tenant_id, entity_alias, partner_sku, aod),
    ).fetchone()
    return dict(row) if row is not None else None


def list_dates(conn, tenant_id: int, entity_alias: str | None = None) -> list[str]:
    """列出某 tenant（可选 entity）已留存的业务日，最新在前 —— WS-12 选日期用。"""
    where = "WHERE tenant_id=?"
    params: list = [tenant_id]
    if entity_alias:
        where += " AND entity_alias=?"
        params.append(entity_alias)
    rows = conn.execute(
        f"SELECT DISTINCT as_of_date FROM {HISTORY_TABLE} {where} "
        f"ORDER BY as_of_date DESC",
        tuple(params),
    ).fetchall()
    return [_row_get(r, "as_of_date") for r in rows]


def run_v2(tenant_id: int, as_of_date=None, entity_alias: str | None = None) -> dict:
    """registry / runner / CLI 入口：按业务日冻结 latest wf1_stock → history。

    as_of_date 必填（先校验再开库）——缺失直接红灯 raise，绝不假装 today。
    """
    print(f"\n=== stock_history snapshot tenant={tenant_id} ===", file=sys.stderr)
    aod = normalize_as_of_date(as_of_date)  # 先校验，缺失/非法在碰 DB 前就 raise
    _data.set_current_tenant(tenant_id)
    conn = _data.conn()
    try:
        res = record_snapshot(conn, tenant_id, aod, entity_alias=entity_alias)
    finally:
        conn.close()
    print(f"[done] {res}", file=sys.stderr)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--as-of-date", dest="as_of_date", required=True,
                    help="业务日 YYYY-MM-DD（必填，不允许默认 today）")
    ap.add_argument("--entity-alias", dest="entity_alias", default=None)
    args = ap.parse_args()
    run_v2(args.tenant, as_of_date=args.as_of_date, entity_alias=args.entity_alias)

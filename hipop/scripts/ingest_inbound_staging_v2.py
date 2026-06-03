"""ingest_inbound_staging v2 — ERP 送仓/拣货 + Noon ASN → v2 staging（WS-10）

把运营原本在 Excel 手工合并的"在途/送仓"两路原始数据落成 v2 staging，
供 **WS-11** 计算 `wf1_stock.pending_inbound_qty`（送仓未上架）。本任务只做
producer（落 staging），不算 pending_inbound_qty、不写 wf1_stock。

staging 表 `wf1_asn_lines_staging` 的 DDL 写在代码里（ensure_staging_tables），
**不碰** CODEOWNERS 锁定的 db/schema*.sql。SQLite / PG 都用 CREATE TABLE
IF NOT EXISTS，与 server.data._ensure_chat_table 同范式。

本轮快照替换（避免历史残留 ASN 污染——验门人打回的洞）：
- staging 不是历史累计账本，而是"**本次输入**的在途/送仓快照"。每次 run，
  当某 `(source, entity_alias)` 第一次在本轮输入里出现时，先删掉该
  `(tenant_id, source, entity_alias)` 的旧 staging 行，再灌本轮文件的行
  （见 _process_file 的 `cleared` 参数）。
- 这样昨天 Scheduled、今天文件里已消失或已 GRN Completed 的旧 ASN 不会留在
  staging 里被 WS-11 继续算进 `pending_inbound_qty`；下游 `wf_sales_cycle.run_v2`
  看到的就是本次快照口径，而不是 tenant 下 staging 全历史。
- 删除按 `(tenant, source, entity_alias)` 精确收敛：本轮没碰到的 entity / source
  原样保留（per-entity / per-source 各自独立刷新，互不影响）。同一轮多份文件喂
  同一个 `(source, alias)` 时只在首次清一次，后续文件累加、不互删。

签名：run_v2(tenant_id, noon_asn_file=None, erp_inbound_file=None, dry_run=False)

两路输入（均按 partner_sku 对齐，平台 SKU 经 noon_sku_map 回 partner_sku）：
- Noon ASN（source='noon_asn'）：asn_number, status, sku/partner_sku, qty,
  country_code/entity_alias
- ERP 送仓/拣货（source='erp_inbound'）：asn_number, status(拣货/发货),
  partner_sku, qty, inbound_date, country_code/entity_alias

CLI:
  python3 ingest_inbound_staging_v2.py --tenant 1 \
      --noon-asn <noon_asn.csv> --erp-inbound <erp_inbound.csv>
"""
from __future__ import annotations

import os
import sys
import csv
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from sales_entity_v2 import get_entity_by_country, get_entity, noon_sku_map
from server import data as _data

STAGING_TABLE = "wf1_asn_lines_staging"
INBOX_DIR = os.path.join(HERE, "..", "..", "inbox")


def _classify_csv(path) -> str | None:
    """按列签名判断是 Noon ASN 还是 ERP 送仓/拣货导出；都不像则 None。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            cols = set(csv.DictReader(f).fieldnames or [])
    except Exception:
        return None
    if "asn_number" not in cols:
        return None
    has_sku = ("partner_sku" in cols) or ("sku" in cols) or ("noon_sku" in cols)
    if not has_sku:
        return None
    # ERP 送仓/拣货导出带送仓时间(inbound_date)；Noon ASN 不带。
    if "inbound_date" in cols:
        return "erp_inbound"
    return "noon_asn"


def safe_int(v):
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def ensure_staging_tables(conn) -> None:
    """建 staging 表（幂等）。DDL 在代码里，不动 db/schema*.sql（锁定）。"""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
            tenant_id     BIGINT NOT NULL,
            entity_alias  TEXT NOT NULL,
            source        TEXT NOT NULL,        -- 'noon_asn' | 'erp_inbound'
            asn_number    TEXT NOT NULL,
            partner_sku   TEXT NOT NULL,
            noon_sku      TEXT,
            qty           INT,
            status        TEXT,
            inbound_date  TEXT,
            imported_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, entity_alias, source, asn_number, partner_sku)
        )
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{STAGING_TABLE}_tenant "
        f"ON {STAGING_TABLE}(tenant_id, entity_alias)"
    )
    conn.commit()


def _route_entity(tenant_id, row, cache) -> dict | None:
    """显式 entity_alias 列优先，否则按 country_code 路由。"""
    alias = (row.get("entity_alias") or "").strip()
    if alias:
        if alias not in cache:
            cache[alias] = get_entity(tenant_id, alias)
        return cache[alias]
    country = (row.get("country_code") or "").strip().upper()
    if not country:
        return None
    key = f"country:{country}"
    if key not in cache:
        cache[key] = get_entity_by_country(tenant_id, country)
    return cache[key]


def _resolve_partner_sku(row, sku_map) -> tuple[str | None, str | None]:
    """返回 (partner_sku, noon_sku)。显式 partner_sku 优先，否则平台 SKU 映射。"""
    noon_sku = (row.get("noon_sku") or row.get("sku") or "").strip() or None
    psk = (row.get("partner_sku") or "").strip()
    if psk:
        return psk, noon_sku
    if noon_sku:
        return sku_map.get(noon_sku), noon_sku
    return None, noon_sku


def _clear_snapshot(conn, tenant_id, source, alias) -> None:
    """本轮快照替换：删掉该 (tenant, source, entity_alias) 的旧 staging 行。

    在某 (source, alias) 本轮首次出现时调用一次，确保历史残留 ASN（已消失/
    已完成的旧单）不会留在 staging 里被 WS-11 继续算进 pending_inbound_qty。
    """
    conn.execute(
        f"DELETE FROM {STAGING_TABLE} "
        f"WHERE tenant_id=? AND source=? AND entity_alias=?",
        (tenant_id, source, alias),
    )


def _process_file(conn, tenant_id, path, source, cleared: set | None = None) -> dict:
    """灌一份文件进 staging。

    cleared: 本轮已做过快照替换的 (source, alias) 集合（跨同一轮多份文件共享），
    None 时本函数自建一个（单文件调用语义不变）。每个 (source, alias) 本轮只清一次。
    """
    if cleared is None:
        cleared = set()
    ent_cache: dict = {}
    sku_maps: dict[str, dict] = {}
    rows_in = written = unmapped = 0
    sql = (
        f"INSERT INTO {STAGING_TABLE} "
        f"(tenant_id, entity_alias, source, asn_number, partner_sku, "
        f" noon_sku, qty, status, inbound_date) "
        f"VALUES (?,?,?,?,?,?,?,?,?) "
        f"ON CONFLICT (tenant_id, entity_alias, source, asn_number, partner_sku) "
        f"DO UPDATE SET noon_sku=excluded.noon_sku, qty=excluded.qty, "
        f"status=excluded.status, inbound_date=excluded.inbound_date, "
        f"imported_at=datetime('now','localtime')"
    )
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows_in += 1
            ent = _route_entity(tenant_id, row, ent_cache)
            if not ent:
                continue
            alias = ent["alias"]
            if alias not in sku_maps:
                sku_maps[alias] = noon_sku_map(tenant_id, alias)
            partner_sku, noon_sku = _resolve_partner_sku(row, sku_maps[alias])
            asn_number = (row.get("asn_number") or "").strip()
            if not (partner_sku and asn_number):
                unmapped += 1
                continue
            # 本轮快照替换：该 (source, alias) 本轮首次写入前，先清掉旧 staging 行。
            if (source, alias) not in cleared:
                _clear_snapshot(conn, tenant_id, source, alias)
                cleared.add((source, alias))
            conn.execute(sql, (
                tenant_id, alias, source, asn_number, partner_sku, noon_sku,
                safe_int(row.get("qty")),
                (row.get("status") or "").strip() or None,
                (row.get("inbound_date") or "").strip() or None,
            ))
            written += 1
    conn.commit()
    return {"rows": rows_in, "lines": written, "unmapped": unmapped}


def _scan_inbox(inbox: str) -> dict:
    """扫 inbox，按列签名把 CSV 分到 noon_asn / erp_inbound。"""
    found = {"noon_asn": [], "erp_inbound": []}
    if not os.path.isdir(inbox):
        return found
    for fn in sorted(os.listdir(inbox)):
        if not fn.endswith(".csv") or fn.startswith("."):
            continue
        kind = _classify_csv(os.path.join(inbox, fn))
        if kind:
            found[kind].append(os.path.join(inbox, fn))
    return found


def run_v2(tenant_id: int, noon_asn_file: str | None = None,
           erp_inbound_file: str | None = None,
           inbox: str | None = None, dry_run: bool = False) -> dict:
    print(f"\n=== ingest_inbound_staging v2 tenant={tenant_id} ===", file=sys.stderr)
    _data.set_current_tenant(tenant_id)

    # 没给显式文件时扫 inbox（让 /run-workflow 入口能真正触发，而不是 no-op）
    noon_files = [noon_asn_file] if noon_asn_file else []
    erp_files = [erp_inbound_file] if erp_inbound_file else []
    if not noon_files and not erp_files:
        scanned = _scan_inbox(inbox or INBOX_DIR)
        noon_files, erp_files = scanned["noon_asn"], scanned["erp_inbound"]

    conn = _data.conn()
    result = {"noon_asn": {}, "erp_inbound": {}, "asn_lines": 0}
    try:
        ensure_staging_tables(conn)
        if dry_run:
            return result
        # 本轮快照替换的去重集合：跨本轮所有文件共享，每个 (source, alias) 只清一次，
        # 避免同一轮多份同 source 文件互相删掉对方刚灌进去的行。
        cleared: set = set()
        for p in noon_files:
            r = _process_file(conn, tenant_id, p, "noon_asn", cleared)
            result["noon_asn"] = {k: result["noon_asn"].get(k, 0) + v for k, v in r.items()}
            print(f"  noon_asn {os.path.basename(p)}: {r}", file=sys.stderr)
        for p in erp_files:
            r = _process_file(conn, tenant_id, p, "erp_inbound", cleared)
            result["erp_inbound"] = {k: result["erp_inbound"].get(k, 0) + v for k, v in r.items()}
            print(f"  erp_inbound {os.path.basename(p)}: {r}", file=sys.stderr)
    finally:
        conn.close()
    result["asn_lines"] = (result["noon_asn"].get("lines", 0)
                           + result["erp_inbound"].get("lines", 0))
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--noon-asn", dest="noon_asn", default=None)
    ap.add_argument("--erp-inbound", dest="erp_inbound", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_v2(args.tenant, noon_asn_file=args.noon_asn,
           erp_inbound_file=args.erp_inbound, dry_run=args.dry_run)

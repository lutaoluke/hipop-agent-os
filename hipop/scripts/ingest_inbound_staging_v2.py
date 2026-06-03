"""ingest_inbound_staging v2 — ERP 送仓/拣货 + Noon ASN → v2 staging（WS-10 / WS-32.4）

把运营原本在 Excel 手工合并的"在途/送仓"两路原始数据落成 v2 staging，
供 **WS-11** 计算 `wf1_stock.pending_inbound_qty`（送仓未上架）。本任务只做
producer（落 staging），**不算 pending_inbound_qty、不写 wf1_stock**（仍归 WS-11）。

WS-32.4：按 stock 链（ingest_noon_stock_csv_v2）既有形状重构出 **socket** —
CSV 入口与 live 行入口共用同一 `_aggregate`/`_upsert`（不分叉）：
- `_aggregate(rows, tenant_id)`：接受 **行可迭代**（CSV 解析行 / live fetcher 行，
  同形 dict），做路由（entity_alias 优先 / country_code）+ 平台 SKU→partner_sku
  映射 + qty 严格解析（`_require_int`：缺/空/非数字 → 红灯，绝不写默认 0），产出
  归一化 staging 行（不落库）。每行 `source`（noon_asn / erp_inbound）按 `inbound_date`
  是否存在派生，与 `_classify_csv` 同口径。CSV 与 live 共用本函数（不分叉）。
- `_upsert(conn, tenant_id, lines)`：本轮快照替换 + 落 staging。
- `run_v2(...)`：CSV 入口（csv.DictReader → `_aggregate` → `_upsert`）。
- `run_live(...)`：live 行入口（producer → **逐行 `validate_row(ASN, row)` 守门** →
  `_aggregate` → `_upsert`）。行字段契约校验在此入口处施加（live 行来自抓取器，
  须以 WS-34 契约红灯缺字段 / 契约外字段）；取数失败 → 回落同一 CSV 契约；行不合
  契约 → 硬红灯（不回落掩盖）；无 CSV 可回落 → raise `LiveSourceUnavailable`。
- `set_live_row_producer / get_live_row_producer`：WS-N2.3/WS-60 真抓取器的注册
  接入点。**单一来源 = `noon_live_contract` 的 `asn` 注册表**（与 stock 链委托
  `MY_INVENTORY` 同范式），不在本脚本另起一份注册表 / 另定行字段。

**行字段契约唯一来源 = `noon_live_contract.ROW_CONTRACT[ASN]`（WS-34）**：
noon ASN（`sku` 平台 SKU）与 ERP 送仓/拣货（`partner_sku` + `inbound_date`）两路
行都属契约的 `ASN` 类（其 `known` 字段恰为两路列名的并集），本脚本不另定字段。
本条只做 socket + fixture 等价 smoke，**不实现真抓取**（真 producer 归 WS-N2.3/WS-60）。

staging 表 `wf1_asn_lines_staging` 的 DDL 写在代码里（ensure_staging_tables），
**不碰** CODEOWNERS 锁定的 db/schema*.sql。SQLite / PG 都用 CREATE TABLE
IF NOT EXISTS，与 server.data._ensure_chat_table 同范式。

本轮快照替换（避免历史残留 ASN 污染——验门人打回的洞）：
- staging 不是历史累计账本，而是"**本次输入**的在途/送仓快照"。每次 run，
  当某 `(source, entity_alias)` 第一次在本轮输入里出现时，先删掉该
  `(tenant_id, source, entity_alias)` 的旧 staging 行，再灌本轮的行（见 `_upsert`）。
- 这样昨天 Scheduled、今天文件里已消失或已 GRN Completed 的旧 ASN 不会留在
  staging 里被 WS-11 继续算进 `pending_inbound_qty`；下游 `wf_sales_cycle.run_v2`
  看到的就是本次快照口径，而不是 tenant 下 staging 全历史。
- 删除按 `(tenant, source, entity_alias)` 精确收敛：本轮没碰到的 entity / source
  原样保留（per-entity / per-source 各自独立刷新，互不影响）。

签名：
  run_v2(tenant_id, noon_asn_file=None, erp_inbound_file=None, inbox=None, dry_run=False)
  run_live(tenant_id, live_producer=None, allow_csv_fallback=True,
           noon_asn_file=None, erp_inbound_file=None, inbox=None, dry_run=False)

CLI:
  python3 ingest_inbound_staging_v2.py --tenant 1 \
      --noon-asn <noon_asn.csv> --erp-inbound <erp_inbound.csv>
"""
from __future__ import annotations

import os
import sys
import csv
import argparse
import itertools

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from sales_entity_v2 import get_entity_by_country, get_entity, noon_sku_map
from server import data as _data
import noon_live_contract as _contract  # scripts 同级模块（同 sales_entity_v2 导入方式）

STAGING_TABLE = "wf1_asn_lines_staging"
INBOX_DIR = os.path.join(HERE, "..", "..", "inbox")

# 行字段契约 / 红灯 / 注册表的唯一来源 = noon_live_contract（WS-34）。
ASN = _contract.ASN
LiveSourceUnavailable = _contract.LiveSourceUnavailable

# 本脚本内部两路 source（落 staging 的 source 列），同属契约 ASN 行类。
SRC_NOON_ASN = "noon_asn"
SRC_ERP_INBOUND = "erp_inbound"


# ── live row producer 注入点（WS-N2.3/WS-60 接入）──────────────────────
# 单一来源 = noon_live_contract 的 asn 注册表：在本脚本 set，contract / 其它
# 消费者（WS-38 收口）get 得到同一个，杜绝"统一接口漂成两套真相"。本脚本不另
# 存一份 _PRODUCERS。producer 签名 fn(tenant_id) -> Iterable[dict]，产出同形
# dict row（键 ⊆ ROW_CONTRACT[ASN]['known']）；未注册（真抓取未 land）→
# run_live 回落 CSV interim。
def set_live_row_producer(fn) -> None:
    """注册/清除 ASN live row producer（fn=None 清除）。委托给 noon_live_contract。"""
    _contract.set_live_row_producer(ASN, fn)


def get_live_row_producer():
    return _contract.get_live_row_producer(ASN)


def _classify_csv(path) -> str | None:
    """按列签名判断是 Noon ASN 还是 ERP 送仓/拣货导出；都不像则 None。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            cols = set(csv.DictReader(f).fieldnames or [])
    except Exception:
        return None
    if "asn_number" not in cols:
        return None
    has_sku = ("partner_sku" in cols) or ("sku" in cols)
    if not has_sku:
        return None
    # ERP 送仓/拣货导出带送仓时间(inbound_date)；Noon ASN 不带。
    if "inbound_date" in cols:
        return SRC_ERP_INBOUND
    return SRC_NOON_ASN


def _row_source(row) -> str:
    """逐行派生 source：带 inbound_date → ERP 送仓/拣货，否则 Noon ASN。

    与 `_classify_csv` 同口径（按 inbound_date 区分两路），使 live 单一 ASN
    producer 产出的混合行与 CSV 分文件归类落库结果一致。
    """
    return SRC_ERP_INBOUND if (row.get("inbound_date") or "").strip() else SRC_NOON_ASN


def _require_int(v, field, asn_number):
    """qty 等数量字段：缺 / 空 / 非数字 → 红灯，**绝不静默写 0**（死法③）。

    `validate_row` 只查 required 字段是否空白；数量列还可能"有值但非数字"
    （如 'N/A'）——若沿用旧 `safe_int` 会被悄悄写成 0，把"未知在途数量"算错。
    这里改成缺值 / 非数字一律 raise LiveSourceUnavailable，与 contract 红灯同语义。
    """
    s = "" if v is None else str(v).strip()
    if s == "":
        raise LiveSourceUnavailable(f"[asn] {asn_number}: 缺 {field}，不写默认 0")
    try:
        return int(float(s))
    except (TypeError, ValueError):
        raise LiveSourceUnavailable(
            f"[asn] {asn_number}: {field} 非数字 ({v!r})，不写默认 0"
        )


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
    """路由到销售主体：显式 entity_alias 列优先（运营 CSV interim 用），否则按
    契约的 country_code。live ASN 行先过 contract 守门（不含 entity_alias），故只
    会走 country_code 分支；entity_alias 仅服务历史 CSV interim 直投（WS-11 scope）。
    """
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
    """返回 (partner_sku, platform_sku)。显式 partner_sku 优先，否则平台 SKU 映射。

    平台 SKU（Z 开头，契约 `sku` 列）经 noon_sku_map 回 partner_sku；未映射 →
    partner_sku=None（上层计 unmapped、跳过不落，绝不把 Z 开头平台 SKU 当主键）。
    """
    psk = (row.get("partner_sku") or "").strip()
    plat = (row.get("sku") or "").strip() or None
    if psk:
        return psk, plat
    if plat:
        return sku_map.get(plat), plat
    return None, None


def _aggregate(rows, tenant_id):
    """row 可迭代（CSV 解析行 / live fetcher 行，同形 dict）→ 归一化 staging 行。

    CSV 入口经 csv.DictReader 解析后喂这里，live 源直接喂同形 row（live 行已先在
    `run_live` 过 `noon_live_contract.validate_row(ASN,...)` 守门），二者共用**同一**
    聚合 + 同一 `_upsert`，落库逐字段一致（不分叉）。本函数不再各自校验字段（行
    字段契约的唯一来源是 WS-34 contract，由 `run_live` 在入口处施加），只做：
      1. 路由（entity_alias 优先 / country_code）。不在白名单 → 跳过、不计 unmapped。
      2. 平台 SKU→partner_sku 映射（未映射 → 计 unmapped、跳过不落）。
      3. qty 经 `_require_int` 严格解析：缺 / 空 / 非数字 → 红灯，**绝不静默写 0**
         （死法③；CSV 与 live 同此守门，因共用本函数）。

    返回 (lines, n_rows, n_unmapped)：
      lines      — list[dict]，每条一行 staging（entity_alias/source/asn_number/
                   partner_sku/noon_sku/qty/status/inbound_date），未落库。
      n_rows     — 读入行数。
      n_unmapped — 路由成功但平台 SKU 未映射到 partner_sku 的行数（跳过不落）。
    """
    ent_cache: dict = {}
    sku_maps: dict[str, dict] = {}
    lines: list[dict] = []
    n_rows = n_unmapped = 0
    for row in rows:
        n_rows += 1
        ent = _route_entity(tenant_id, row, ent_cache)
        if not ent:
            continue  # country 不在该 tenant 的销售主体白名单
        alias = ent["alias"]
        if alias not in sku_maps:
            sku_maps[alias] = noon_sku_map(tenant_id, alias)
        partner_sku, plat = _resolve_partner_sku(row, sku_maps[alias])
        if not partner_sku:
            n_unmapped += 1
            continue
        lines.append({
            "entity_alias": alias,
            "source": _row_source(row),
            "asn_number": (row.get("asn_number") or "").strip(),
            "partner_sku": partner_sku,
            "noon_sku": plat,
            "qty": _require_int(row.get("qty"), "qty", (row.get("asn_number") or "").strip()),
            "status": (row.get("status") or "").strip() or None,
            "inbound_date": (row.get("inbound_date") or "").strip() or None,
        })
    return lines, n_rows, n_unmapped


def _upsert(conn, tenant_id, lines) -> int:
    """落 staging（本轮快照替换）。返回写入行数。

    每个本轮出现的 (source, alias) 在首次写入前清一次旧 staging 行（cleared 去重
    集合跨本次 `_upsert` 共享：每个 (source, alias) 只清一次、后续累加、不互删）。
    """
    cleared: set = set()
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
    written = 0
    for ln in lines:
        key = (ln["source"], ln["entity_alias"])
        if key not in cleared:
            conn.execute(
                f"DELETE FROM {STAGING_TABLE} "
                f"WHERE tenant_id=? AND source=? AND entity_alias=?",
                (tenant_id, ln["source"], ln["entity_alias"]),
            )
            cleared.add(key)
        conn.execute(sql, (
            tenant_id, ln["entity_alias"], ln["source"], ln["asn_number"],
            ln["partner_sku"], ln["noon_sku"], ln["qty"], ln["status"],
            ln["inbound_date"],
        ))
        written += 1
    conn.commit()
    return written


def _src_breakdown(lines) -> dict:
    """按 source 拆 lines 计数，供日志/返回（落库口径以 staging 表为准）。"""
    out = {SRC_NOON_ASN: 0, SRC_ERP_INBOUND: 0}
    for ln in lines:
        out[ln["source"]] = out.get(ln["source"], 0) + 1
    return out


def _ingest(conn, tenant_id, rows, source_tag, dry_run: bool = False) -> dict:
    """行 → staging（CSV 与 live 共用）。dry_run 只聚合统计、不落库。"""
    lines, n_rows, n_unmapped = _aggregate(rows, tenant_id)
    written = 0 if dry_run else _upsert(conn, tenant_id, lines)
    by_src = _src_breakdown(lines)
    return {
        "source": source_tag,
        "rows": n_rows,
        "lines": written,
        "unmapped": n_unmapped,
        SRC_NOON_ASN: {"lines": by_src[SRC_NOON_ASN]},
        SRC_ERP_INBOUND: {"lines": by_src[SRC_ERP_INBOUND]},
        "asn_lines": written,
    }


def _iter_csv_rows(path):
    """CSV → dict row 迭代器（键同 CSV 列 = 契约 known 字段）。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield row


def _resolve_csv_files(noon_asn_file, erp_inbound_file, inbox) -> list:
    """两路 CSV interim 文件列表（显式文件优先，否则扫 inbox）。"""
    noon = [noon_asn_file] if noon_asn_file else []
    erp = [erp_inbound_file] if erp_inbound_file else []
    if not noon and not erp:
        scanned = _scan_inbox(inbox or INBOX_DIR)
        noon, erp = scanned[SRC_NOON_ASN], scanned[SRC_ERP_INBOUND]
    return noon + erp


def _scan_inbox(inbox: str) -> dict:
    """扫 inbox，按列签名把 CSV 分到 noon_asn / erp_inbound。"""
    found = {SRC_NOON_ASN: [], SRC_ERP_INBOUND: []}
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
    """CSV 入口：noon ASN / ERP 送仓 CSV → wf1_asn_lines_staging。

    与 `run_live` 共用 `_aggregate`/`_upsert`（不分叉）。多份文件先合并成一条 row
    流，再走同一 `_aggregate`（source 逐行派生）。
    """
    print(f"\n=== ingest_inbound_staging v2 (csv) tenant={tenant_id} ===", file=sys.stderr)
    _data.set_current_tenant(tenant_id)
    files = _resolve_csv_files(noon_asn_file, erp_inbound_file, inbox)
    conn = _data.conn()
    try:
        ensure_staging_tables(conn)
        rows = itertools.chain.from_iterable(_iter_csv_rows(p) for p in files)
        result = _ingest(conn, tenant_id, rows, "csv", dry_run=dry_run)
    finally:
        conn.close()
    print(f"[done] {result}", file=sys.stderr)
    return result


def _csv_fallback_or_fail(conn, tenant_id, reason, allow_csv_fallback,
                          noon_asn_file, erp_inbound_file, inbox, dry_run) -> dict:
    """live 取数失败 → 走【同一】CSV ingest 契约回落，不短路、不写假行。

    - allow_csv_fallback=False，或无任何 CSV interim 可回落 → raise
      LiveSourceUnavailable（红灯），绝不写空 staging / 编造 ASN 冒充成功。
    - 有 CSV 可回落 → 落真实 CSV 行，结果标 source=csv_fallback + live_error。
    """
    print(f"[inbound_live_ingest] live 取数失败 → {reason}", file=sys.stderr)
    if not allow_csv_fallback:
        raise LiveSourceUnavailable(reason)
    files = _resolve_csv_files(noon_asn_file, erp_inbound_file, inbox)
    if not files:
        raise LiveSourceUnavailable(
            f"{reason}；且无 CSV interim 可回落 —— 不写假 staging 行"
        )
    rows = itertools.chain.from_iterable(_iter_csv_rows(p) for p in files)
    r = _ingest(conn, tenant_id, rows, "csv_fallback", dry_run=dry_run)
    r["live_error"] = reason
    return r


def run_live(tenant_id: int, live_producer=None, allow_csv_fallback: bool = True,
             noon_asn_file: str | None = None,
             erp_inbound_file: str | None = None,
             inbox: str | None = None, dry_run: bool = False) -> dict:
    """live 行入口：ASN/送仓 live 行 → wf1_asn_lines_staging（socket，WS-32.4）。

    读：ASN live row producer（WS-N2.3/WS-60 接入；未接入则回落 CSV interim）；
        noon_asn_file / erp_inbound_file / inbox 的 CSV（fallback 输入）。
    写：wf1_asn_lines_staging（与 CSV 入口共用 `_aggregate`/`_upsert`，不分叉，
        不算 pending_inbound_qty —— 仍归 WS-11）。

    `live_producer`：可选，覆盖注册表（测试注入用）；缺省读 `get_live_row_producer()`。
      - 有 producer → 物化行 → **逐行过 WS-34 `validate_row(ASN, row)` 守门**
        （缺 asn_number/qty/country_code、缺 SKU 主键、带契约外字段 → 红灯）→ 同一
        `_aggregate`/`_upsert` 落 staging（source=live）。
      - producer **取数失败**（抛非 LiveSourceUnavailable 异常）→ 回落 CSV interim
        （同契约，视作 transient）。
      - producer **行不合契约**（validate_row 红灯）→ **硬红灯 raise**，不回落
        掩盖（区别于取数失败：行形状错是上游 bug，绝不拿旧 CSV 冒充实时成功）。
      - 无 producer → 回落 CSV interim；无 CSV 可回落 → raise LiveSourceUnavailable
        （红灯），绝不写空 staging / 编造 ASN/qty。
    """
    print(f"\n=== ingest_inbound_staging live tenant={tenant_id} ===", file=sys.stderr)
    _data.set_current_tenant(tenant_id)
    producer = live_producer or get_live_row_producer()
    conn = _data.conn()
    try:
        ensure_staging_tables(conn)
        if producer is None:
            result = _csv_fallback_or_fail(
                conn, tenant_id, "无 live row producer（WS-N2.3/WS-60 未接入）",
                allow_csv_fallback, noon_asn_file, erp_inbound_file, inbox, dry_run)
        else:
            try:
                # 物化：把生成器里的取数错误在聚合前暴露（别让半截数据落库）
                rows = list(producer(tenant_id))
            except LiveSourceUnavailable:
                raise
            except Exception as e:
                result = _csv_fallback_or_fail(
                    conn, tenant_id,
                    f"live producer 取数失败: {type(e).__name__}: {e}",
                    allow_csv_fallback, noon_asn_file, erp_inbound_file, inbox, dry_run)
            else:
                # 行字段契约守门（唯一来源 = WS-34 noon_live_contract）：live 行进
                # _aggregate/_upsert 前逐行校验，缺字段 / 契约外字段红灯，绝不编数、
                # 绝不静默落 staging。raise 直接传出（不进 CSV 回落掩盖上游 bug）。
                for r in rows:
                    _contract.validate_row(ASN, r)
                result = _ingest(conn, tenant_id, rows, "live", dry_run=dry_run)
                result["live_error"] = None
    finally:
        conn.close()
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--noon-asn", dest="noon_asn", default=None)
    ap.add_argument("--erp-inbound", dest="erp_inbound", default=None)
    ap.add_argument("--live", action="store_true", help="走 live 行入口（socket）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.live:
        run_live(args.tenant, noon_asn_file=args.noon_asn,
                 erp_inbound_file=args.erp_inbound, dry_run=args.dry_run)
    else:
        run_v2(args.tenant, noon_asn_file=args.noon_asn,
               erp_inbound_file=args.erp_inbound, dry_run=args.dry_run)

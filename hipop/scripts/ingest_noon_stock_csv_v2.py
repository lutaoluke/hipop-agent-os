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
- **部分 upsert**：只写 noon_* 四列 + imported_at/updated_at，绝不碰 ERP 写的
  yiwu/dongguan/overseas/total_stock，也不碰 pending_inbound_qty（归 WS-11）。
  imported_at 是源库存 ingest freshness 事实源，非 ingest rollup 脚本不得改。
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


class LiveSourceUnavailable(Exception):
    """live 取数失败 / 无 producer / 无 CSV 可回落 —— 明确的失败信号。

    专门区别于「成功但 0 行」：取数挂了就红灯 raise，绝不让上游把
    0 库存 / 空仓库 JSON 当成功落库（占位假数据死法）。
    """


# WS-N2 noon FBN live row producer 接入点。WS-N2 fetcher land 后调
# set_live_row_producer(fn) 注册；fn(tenant_id) -> Iterable[dict]，产出
# **同形 dict row**（键同 noon Inventory CSV 列：country_code / sku|noon_sku|
# partner_sku / warehouse_code / qty / inventory_type）。本任务不猜 WS-N2 字段，
# 只定 row 形状契约（与 WS-N3.1 一致）；未注册（WS-N2 未 land）→ run_live 回落 CSV。
#
# WS-34 收口（单一来源）：my_inventory 的 producer 注册表 = noon_live_contract
# 的统一注册表，**不**再各持一份。stock runner 这里的 set/get 是它的 my_inventory
# 视图，与 noon_live_contract.set_live_row_producer(MY_INVENTORY, fn) 写读同一处。
# 这样「在 contract 注册 / 在 stock 注册」两个方向看到的是同一真相，
# missing_live_producers()/assert_live_producers_ready() 也据此判定，杜绝两套真相。
import noon_live_contract as _contract  # noqa: E402  （scripts 同级模块，同 sales_entity_v2 导入方式）


def set_live_row_producer(fn):
    """注册 noon my_inventory live row producer（WS-N2 接入点）。传 None 清除。

    单一来源 = noon_live_contract 的 my_inventory 注册表，避免 stock 与 contract
    各持一份导致「统一 producer 接口」漂成两套真相。
    """
    _contract.set_live_row_producer(_contract.MY_INVENTORY, fn)


def get_live_row_producer():
    return _contract.get_live_row_producer(_contract.MY_INVENTORY)


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


def _iter_csv_rows(path):
    """noon Inventory CSV → dict row 迭代器。

    把 CSV 解析从聚合逻辑里抽出来：CSV 入口和 WS-N2 live fetcher 都产出
    **同形 dict row**（键同 noon Inventory CSV 列），统一喂给 `_aggregate`，
    避免 live 与 CSV 在聚合/落库口径上分叉。
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield row


def _aggregate(rows, tenant_id):
    """row 可迭代（CSV 解析行 / live fetcher 行，同形 dict）
    → {entity_alias: {partner_sku: agg}}，并统计映射未命中行。

    `rows` 是 dict 的可迭代对象（键同 noon Inventory CSV 列）；CSV 入口经
    `_iter_csv_rows` 解析后喂这里，live 源直接喂同形 row，二者共用同一聚合 +
    同一 `_upsert`，落库结果逐字段一致。
    """
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

    for row in rows:
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
    update_set = ",".join(f"{c}=excluded.{c}" for c in _NOON_COLS) + f", imported_at={ts}, updated_at={ts}"
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
            # CSV 生产路径也走 row 接口：解析成同形 dict row 后喂同一个
            # `_aggregate`，与 live 源共用归一化 + `_upsert`（不分叉）。
            bucket, n_rows, n_unmapped = _aggregate(_iter_csv_rows(path), tenant_id)
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


def _csv_fallback_or_fail(tenant_id, reason, allow_csv_fallback,
                          file, inbox, dry_run) -> dict:
    """live 取数失败时的整链回落：走【同一】CSV ingest 契约
    （run_v2 → _iter_csv_rows → _aggregate → _upsert），不短路、不写假数据。

    - allow_csv_fallback=False，或无任何 CSV interim 可回落 → raise
      LiveSourceUnavailable（红灯），绝不写 0 库存 / 空仓库 JSON 冒充成功。
    - 有 CSV 可回落 → 落真实 CSV 数据，结果标 source=csv_fallback + live_error。
    """
    print(f"[noon_live_ingest] live 取数失败 → {reason}", file=sys.stderr)
    if not allow_csv_fallback:
        raise LiveSourceUnavailable(reason)
    res = dict(run_v2(tenant_id, file=file, inbox=inbox, dry_run=dry_run))
    if res.get("files", 0) == 0:
        # 既取不到 live、又无 CSV interim → 没有任何真数据，不能冒充成功
        raise LiveSourceUnavailable(
            f"{reason}；且无 CSV interim 可回落（file/inbox 均无 inventory CSV）—— 不写假数据"
        )
    res["source"] = "csv_fallback"
    res["live_error"] = reason
    return res


def run_live(tenant_id: int, live_producer=None, allow_csv_fallback: bool = True,
             file: str | None = None, inbox: str | None = None,
             dry_run: bool = False) -> dict:
    """Noon FBN live 行 → v2 wf1_stock.noon_*（WS-N3.2）。

    读：noon FBN live row producer（WS-N2 接入；未接入则回落 CSV interim）；
        file / inbox 的 noon Inventory CSV（fallback 输入）。
    写：wf1_stock.noon_total_qty / noon_saleable_qty / noon_unsaleable_qty /
        noon_warehouses_json（部分 upsert，保护 ERP 列与 pending_inbound_qty）。

    live 与 CSV 共用同一 `_aggregate`/`_upsert`（WS-N3.1 契约，不分叉）。
    取数失败（无 producer / producer 抛错）→ 整链回落 CSV interim（同契约）；
    无 CSV 可回落 → raise LiveSourceUnavailable，绝不写 0/空 JSON 冒充成功。
    """
    producer = live_producer or get_live_row_producer()
    if producer is None:
        return _csv_fallback_or_fail(
            tenant_id, "无 live row producer（WS-N2 fetcher 未接入）",
            allow_csv_fallback, file, inbox, dry_run)
    try:
        # 物化：把生成器里的取数错误在聚合前暴露出来（别让半截数据落库）
        rows = list(producer(tenant_id))
    except Exception as e:
        return _csv_fallback_or_fail(
            tenant_id, f"live producer 取数失败: {type(e).__name__}: {e}",
            allow_csv_fallback, file, inbox, dry_run)

    print(f"\n=== noon live ingest tenant={tenant_id}: {len(rows)} live rows ===",
          file=sys.stderr)
    _data.set_current_tenant(tenant_id)
    bucket, n_rows, n_unmapped = _aggregate(rows, tenant_id)

    # ── live 源完整性守门（落库前红灯；验收③ + 死法「空回/缺映射冒充 live 成功」）──────
    # WS-59 门2 返工：原先无条件组装 source="live"，导致两类假绿——
    #   ① live 源空回（producer→0 行 / 全部行国别不在销售主体白名单）→ bucket 无可落 SKU；
    #   ② live 行平台 SKU 无法映射回 partner_sku（缺 wf2_sku）→ 只 unmapped++ 后照样成功。
    # 两类都「没有可信真库存可落」，绝不冒充 source=live。交既有 `_csv_fallback_or_fail` 统一
    # 处理：有 CSV interim → 显式回落（source=csv_fallback + live_error，summary 即报「没走
    # live」）；无 CSV → raise LiveSourceUnavailable（blocked）。**只守 live 路径**——run_v2(CSV)
    # 仍按原口径宽松跳过 unmapped（运营导表是另一信任模型），故不动 `_aggregate`/`_upsert`。
    if n_unmapped > 0:
        return _csv_fallback_or_fail(
            tenant_id, f"live 有 {n_unmapped} 行平台 SKU 无法映射回 partner_sku"
            "（缺 wf2_sku 映射）—— 不只 unmapped++ 冒充 live 成功",
            allow_csv_fallback, file, inbox, dry_run)
    n_skus_live = sum(len(skus) for skus in bucket.values())
    if n_skus_live == 0:
        return _csv_fallback_or_fail(
            tenant_id, "live 无任何可落库存行（noon my inventory 空回 / 登录态失效 / "
            "全部行国别不在销售主体白名单）—— 不写空库存冒充 live 成功",
            allow_csv_fallback, file, inbox, dry_run)

    conn = _data.conn()
    try:
        by_alias = {} if dry_run else _upsert(conn, tenant_id, bucket)
    finally:
        conn.close()
    result = {
        "source": "live",
        "files": 0,
        "rows": n_rows,
        "skus": sum(by_alias.values()),
        "unmapped": n_unmapped,
        "by_alias": by_alias,
        "live_error": None,
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

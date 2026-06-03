"""ingest_noon_csv v2 — 真多租户：直接写 v2 表（wf2_orders / wf2_sku 列存）

CSV 入口（生产）：process_csv_v2(tenant_id, csv_path, conn, entity_alias=None, dry_run=False)
- tenant_id 从 onboarding 配的 sales_entities 拿对应 entity_alias
- 不再走 hipop.json，从 DB sales_entities 表查
- 同时写 v2 wf2_orders + 更新 wf2_sku.title/image_url 等元信息
- 老物理切表 wf2_<alias>_orders 不写（旧路径走原 ingest_noon_csv.py）

WS-35 socket（对齐 stock 链 WS-N3.1）：把订单 ingest 重构出可接「实时行来源」的
插座，CSV 与 live 共用同一 `_aggregate`/`_upsert`，绝不分叉：
- `_iter_csv_rows(path)`  CSV → 同形 dict row 迭代器
- `_aggregate(rows, tenant_id, entity_alias=None)`  行可迭代 → 落库 bucket
- `_upsert(conn, tenant_id, bucket)`  bucket → wf2_orders + wf2_sku（与旧逻辑逐字段一致）
- `run_live(...)` / `set_live_row_producer` / `get_live_row_producer` + CSV 回落
- **本模块只定 socket，不实现真抓取**（真 producer 归 WS-N2.1/WS-58）。

单一来源（WS-34 收口）：
- 行字段以 `noon_live_contract.ROW_CONTRACT[ORDERS]` 为唯一来源（键同 noon 订单
  CSV 列），不在脚本里另定字段；等价 smoke 的 fixture 用 contract 的 ORDERS fixture。
- live producer 注册表 = `noon_live_contract` 的统一注册表，**不**再各持一份。
  本模块的 set/get_live_row_producer 是它的 orders 视图，与
  `noon_live_contract.set_live_row_producer(ORDERS, fn)` 写读同一处——这样
  `missing_live_producers()` / `assert_live_producers_ready()`（WS-38 收口判定）
  能看到 orders 是否就绪，杜绝 orders 与 contract 两套真相。
"""
from __future__ import annotations

import os
import sys
import json
import csv

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

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

# noon 实时行契约（行字段 + producer 注册表的唯一来源，WS-34）。与 stock 链
# 同样用 scripts 同级 import（noon_live_contract 内部用 sys.modules 单例 holder
# 锚注册表，跨 import 路径共享，详见该模块 docstring）。
import noon_live_contract as _contract  # noqa: E402

# 对外沿用 contract 的失败信号类，整链 except 同一类型（不另起一份）。
LiveSourceUnavailable = _contract.LiveSourceUnavailable

INBOX_DIR = os.path.join(HERE, "..", "..", "inbox")


def set_live_row_producer(fn):
    """注册 noon 订单 live row producer（WS-N2.1/WS-58 接入点）。传 None 清除。

    单一来源 = noon_live_contract 的 orders 注册表，避免 ingest 与 contract
    各持一份导致「统一 producer 接口」漂成两套真相。fn(tenant_id) -> Iterable[dict]，
    产出同形 dict row（键见 noon_live_contract.ROW_CONTRACT[ORDERS]['known']）。
    """
    _contract.set_live_row_producer(_contract.ORDERS, fn)


def get_live_row_producer():
    return _contract.get_live_row_producer(_contract.ORDERS)


def is_order_csv(path) -> bool:
    """noon 订单 CSV 必含 partner_sku + item_nr 两列（其余字段见 COLUMN_MAP）。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            cols = csv.DictReader(f).fieldnames or []
        idx = build_header_index(cols)
        return bool(get_col_present(idx, COLUMN_MAP["partner_sku"]) and
                    get_col_present(idx, COLUMN_MAP["item_nr"]))
    except Exception:
        return False


def get_col_present(header_idx, candidates) -> bool:
    from ingest_noon_csv import _norm
    return any(header_idx.get(_norm(c)) for c in candidates)


def _iter_csv_rows(path):
    """noon 订单 CSV → dict row 迭代器。

    把 CSV 解析从聚合逻辑里抽出来：CSV 入口和 live fetcher 都产出 **同形 dict
    row**（键同 noon 订单 CSV 列），统一喂给 `_aggregate`，避免 live 与 CSV 在
    聚合/落库口径上分叉。
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield row


def _aggregate(rows, tenant_id, entity_alias=None, validate_kind=None):
    """行可迭代（CSV 解析行 / live fetcher 行，同形 dict）→ 落库 bucket。

    `rows` 是 dict 的可迭代对象（键 = noon 订单 CSV 列，即
    noon_live_contract.ROW_CONTRACT[ORDERS] 的字段）；CSV 入口经 `_iter_csv_rows`
    解析后喂这里，live 源直接喂同形 row，二者共用同一聚合 + 同一 `_upsert`，
    落库结果逐字段一致（不分叉）。

    entity_alias:
      - 传入（CSV 生产路径 / live 单主体）→ 所有行归该 entity，保持既有「一份
        noon 订单报表 = 一个销售主体」语义（与改前 process_csv_v2 完全一致）。
      - None → 按行 dest_country 路由 entity（对齐 stock 链 WS-N3.1 的按行国别路由）。

    validate_kind（WS-35 门2 红队补洞，验收③「字段缺失红灯，不写默认值」）:
      - None（CSV 生产路径）→ 保留对脏行/汇总行的宽松跳过：真 noon 后台导出常带
        非订单行（合计行、缺 partner_sku/item_nr 的行），CSV 入口照旧 `continue`。
      - noon_live_contract.ORDERS（live 路径）→ 每行先过 WS-34 `validate_row`：
        缺必填（partner_sku/item_nr）/ 缺 SKU 主键 / 带契约外字段 → 立即
        raise LiveSourceUnavailable（红灯），绝不静默吞坏行或写默认销量/金额。
      聚合 + 落库口径两路完全一致；仅 live 入口加这道严格的契约门（不分叉）。

    返回 (bucket, n_rows, n_unmapped)：
      bucket = {alias: {"orders": [order_dict, ...], "sku_meta": {partner_sku: meta}}}
      n_rows = 通过 partner_sku+item_nr 校验、真正入账的订单行数
      n_unmapped = 有 SKU 但无法路由到 entity 而丢弃的行数
    """
    bucket: dict = {}
    entities_by_country: dict = {}
    n_rows = 0
    n_unmapped = 0

    def _alias_for(row, header_idx):
        if entity_alias:
            return entity_alias
        dest = (get_col(row, header_idx, COLUMN_MAP["destination"]) or "").strip().upper()
        dest = {"KSA": "SA", "UAE": "AE"}.get(dest, dest)
        if not dest:
            return None
        if dest not in entities_by_country:
            entities_by_country[dest] = get_entity_by_country(tenant_id, dest)
        ent = entities_by_country[dest]
        return ent["alias"] if ent else None

    for row in rows:
        # live 路径：进聚合/落库前按 WS-34 contract 严格校验，坏行红灯（见 docstring
        # validate_kind）。CSV 路径 validate_kind=None → 跳过此门，保留宽松跳过。
        if validate_kind is not None:
            _contract.validate_row(validate_kind, row)
        header_idx = build_header_index(list(row.keys()))
        partner_sku = get_col(row, header_idx, COLUMN_MAP["partner_sku"])
        item_nr     = get_col(row, header_idx, COLUMN_MAP["item_nr"])
        noon_sku    = get_col(row, header_idx, COLUMN_MAP["noon_sku"])
        if not (partner_sku and item_nr):
            continue

        alias = _alias_for(row, header_idx)
        if not alias:
            n_unmapped += 1
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
        destination = get_col(row, header_idx, COLUMN_MAP["destination"])

        slot = bucket.setdefault(alias, {"orders": [], "sku_meta": {}})
        slot["orders"].append({
            "partner_sku": partner_sku,
            "noon_sku": noon_sku,
            "item_nr": item_nr,
            "order_date": order_date,
            "status": status,
            "is_cancelled": is_cancelled,
            "is_return": is_return,
            "seller_price": seller_price,
            "customer_paid": customer_paid,
            "currency": currency,
            "fulfillment": fulfillment,
            "destination": destination,
            "raw_json": json.dumps({k: row.get(k) for k in row}, ensure_ascii=False),
        })
        n_rows += 1

        # 累积 SKU 元信息（标题/图片/品牌/币种 — 第一行有）
        if partner_sku not in slot["sku_meta"]:
            slot["sku_meta"][partner_sku] = {
                "noon_sku":    noon_sku,
                "title":       get_col(row, header_idx, COLUMN_MAP["title"]),
                "image_url":   get_col(row, header_idx, COLUMN_MAP["image_url"]),
                "fulfillment": fulfillment,
                "family":      get_col(row, header_idx, COLUMN_MAP["family"]),
                "brand":       get_col(row, header_idx, COLUMN_MAP["brand"]),
                "currency":    currency,
            }

    return bucket, n_rows, n_unmapped


def _upsert(conn, tenant_id, bucket) -> dict:
    """bucket → wf2_orders（逐行）+ wf2_sku（元信息部分填空）。

    与改前 process_csv_v2 内联写库逐字段一致；CSV 与 live 都经此一处落库。
    """
    cur = conn.cursor()
    counts: dict = {}
    for alias, slot in bucket.items():
        for o in slot["orders"]:
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
                o["partner_sku"], o["noon_sku"], o["item_nr"], o["order_date"], o["status"],
                o["is_cancelled"], o["is_return"], o["seller_price"], o["customer_paid"],
                o["currency"], o["fulfillment"], o["destination"], o["raw_json"],
            ))

        # 把 SKU 元信息 upsert 到 wf2_sku（首次见的 SKU 自动建条记录，之后只填空字段）
        for sku, meta in slot["sku_meta"].items():
            cur.execute("""
                INSERT INTO wf2_sku
                  (tenant_id, entity_alias, partner_sku, noon_sku, title, image_url,
                   fulfillment, family, brand, currency, is_listed, imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,1, datetime('now','localtime'))
                ON CONFLICT(tenant_id, entity_alias, partner_sku) DO UPDATE SET
                  noon_sku    = COALESCE(wf2_sku.noon_sku,    excluded.noon_sku),
                  title       = COALESCE(wf2_sku.title,       excluded.title),
                  image_url   = COALESCE(wf2_sku.image_url,   excluded.image_url),
                  fulfillment = COALESCE(wf2_sku.fulfillment, excluded.fulfillment),
                  family      = COALESCE(wf2_sku.family,      excluded.family),
                  brand       = COALESCE(wf2_sku.brand,       excluded.brand),
                  -- ERP 优先：ERP 先写过 currency 就保留，noon 只补空缺
                  -- （noon-only SKU 或 ERP 尚未写 currency 时由 noon 兜底）。
                  currency    = COALESCE(wf2_sku.currency,    excluded.currency),
                  is_listed   = 1,
                  imported_at = datetime('now','localtime')
            """, (
                tenant_id, alias, sku,
                meta.get("noon_sku"), meta.get("title"), meta.get("image_url"),
                meta.get("fulfillment"), meta.get("family"), meta.get("brand"),
                meta.get("currency"),
            ))

        counts[alias] = {"orders": len(slot["orders"]), "sku": len(slot["sku_meta"])}
    conn.commit()
    return counts


def process_csv_v2(tenant_id: int, path: str, conn,
                   entity_alias: str = None, dry_run: bool = False) -> int:
    """写 v2 表（wf2_orders + wf2_sku 元信息更新），按 tenant_id 隔离。

    CSV 生产入口。WS-35 起内部统一走 `_iter_csv_rows` → `_aggregate` → `_upsert`，
    与 live 行路径共用同一聚合 + 落库（不分叉）；外部签名/返回/落库结果不变。

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

    # CSV 生产路径也走 row 接口：解析成同形 dict row 后喂同一个 `_aggregate`，
    # 与 live 源共用归一化 + `_upsert`（不分叉）。alias 已定 → 全部归该 entity。
    bucket, n, _ = _aggregate(_iter_csv_rows(path), tenant_id, entity_alias=alias)
    counts = _upsert(conn, tenant_id, bucket)
    sku_n = counts.get(alias, {}).get("sku", 0)
    print(f"  [done] tenant={tenant_id} entity={alias}: "
          f"{n} order rows, {sku_n} sku meta updates", file=sys.stderr)
    return n


def run_v2(tenant_id: int, file: str | None = None, inbox: str | None = None,
           entity_alias: str | None = None, dry_run: bool = False) -> dict:
    """CSV 入口（指定 file 或扫 inbox）→ 同一 `_aggregate`/`_upsert`。

    供 run_live 的 CSV 回落复用，确保回落与正常 CSV ingest 走完全同一契约。
    """
    inbox = inbox or INBOX_DIR
    if file:
        files = [file]
    else:
        files = []
        if os.path.isdir(inbox):
            for fn in sorted(os.listdir(inbox)):
                if fn.endswith(".csv") and not fn.startswith("."):
                    full = os.path.join(inbox, fn)
                    if is_order_csv(full):
                        files.append(full)
    if not files:
        print(f"[ingest_noon_csv_v2] no order CSV ({file or inbox})", file=sys.stderr)
        return {"files": 0, "orders": 0, "skus": 0, "by_alias": {}}

    from server import data as _data
    _data.set_current_tenant(tenant_id)
    conn = _data.conn()
    total_orders = 0
    by_alias: dict = {}
    try:
        for path in files:
            n = process_csv_v2(tenant_id, path, conn,
                               entity_alias=entity_alias, dry_run=dry_run)
            total_orders += n
    finally:
        conn.close()
    result = {
        "files": len(files),
        "orders": total_orders,
        "skus": None,
        "by_alias": by_alias,
    }
    print(f"[done] {result}", file=sys.stderr)
    return result


def _csv_fallback_or_fail(tenant_id, reason, allow_csv_fallback,
                          file, inbox, entity_alias, dry_run) -> dict:
    """live 取数失败时的整链回落：走【同一】CSV ingest 契约
    （run_v2 → process_csv_v2 → _aggregate → _upsert），不短路、不写假数据。

    - allow_csv_fallback=False，或无任何 CSV interim 可回落 → raise
      LiveSourceUnavailable（红灯），绝不写默认销量/金额冒充成功。
    - 有 CSV 可回落 → 落真实 CSV 数据，结果标 source=csv_fallback + live_error。
    """
    print(f"[noon_order_live_ingest] live 取数失败 → {reason}", file=sys.stderr)
    if not allow_csv_fallback:
        raise LiveSourceUnavailable(reason)
    res = dict(run_v2(tenant_id, file=file, inbox=inbox,
                      entity_alias=entity_alias, dry_run=dry_run))
    if res.get("files", 0) == 0:
        # 既取不到 live、又无 CSV interim → 没有任何真数据，不能冒充成功
        raise LiveSourceUnavailable(
            f"{reason}；且无 CSV interim 可回落（file/inbox 均无订单 CSV）—— 不写假数据"
        )
    res["source"] = "csv_fallback"
    res["live_error"] = reason
    return res


def run_live(tenant_id: int, live_producer=None, allow_csv_fallback: bool = True,
             file: str | None = None, inbox: str | None = None,
             entity_alias: str | None = None, dry_run: bool = False) -> dict:
    """noon 订单 live 行 → v2 wf2_orders / wf2_sku（WS-35 socket）。

    读：noon 订单 live row producer（noon_live_contract 的 ORDERS 注册表，
        WS-N2.1/WS-58 接入；未接入则回落 CSV interim）；
        file / inbox 的 noon 订单 CSV（fallback 输入）。
    写：wf2_orders（逐行）+ wf2_sku（元信息部分填空），与 CSV 路径逐字段一致。

    live 与 CSV 共用同一 `_aggregate`/`_upsert`（WS-35 契约，不分叉）。
    取数失败（无 producer / producer 抛错）→ 整链回落 CSV interim（同契约）；
    无 CSV 可回落 → raise LiveSourceUnavailable，绝不写默认销量/金额冒充成功。
    """
    producer = live_producer or get_live_row_producer()
    if producer is None:
        return _csv_fallback_or_fail(
            tenant_id, "无 live row producer（WS-N2.1/WS-58 fetcher 未接入）",
            allow_csv_fallback, file, inbox, entity_alias, dry_run)
    try:
        # 物化：把生成器里的取数错误在聚合前暴露出来（别让半截数据落库）
        rows = list(producer(tenant_id))
    except Exception as e:
        return _csv_fallback_or_fail(
            tenant_id, f"live producer 取数失败: {type(e).__name__}: {e}",
            allow_csv_fallback, file, inbox, entity_alias, dry_run)

    print(f"\n=== noon order live ingest tenant={tenant_id}: {len(rows)} live rows ===",
          file=sys.stderr)
    # live 行先过 WS-34 contract 校验（_aggregate 内 validate_kind=ORDERS，纯内存、
    # 未开库连接）：任一行缺必填 / 带契约外字段 → LiveSourceUnavailable。视同「live 源
    # 字段漂移/坏行」，与 producer 抛错同等处理 → 回落 CSV interim（同契约）或无 CSV 红灯。
    # 校验在落库前 raise，故坏行绝不会写半截/默认值进 wf2_orders/wf2_sku。
    try:
        bucket, n_rows, n_unmapped = _aggregate(
            rows, tenant_id, entity_alias=entity_alias, validate_kind=_contract.ORDERS)
    except LiveSourceUnavailable as e:
        return _csv_fallback_or_fail(
            tenant_id, f"live 行不合 WS-34 contract: {e}",
            allow_csv_fallback, file, inbox, entity_alias, dry_run)
    from server import data as _data
    _data.set_current_tenant(tenant_id)
    conn = _data.conn()
    try:
        counts = {} if dry_run else _upsert(conn, tenant_id, bucket)
    finally:
        conn.close()
    by_alias = {a: c["orders"] for a, c in counts.items()}
    result = {
        "source": "live",
        "files": 0,
        "orders": sum(by_alias.values()),
        "skus": sum(c["sku"] for c in counts.values()) if counts else 0,
        "unmapped": n_unmapped,
        "by_alias": by_alias,
        "live_error": None,
    }
    print(f"[done] {result}", file=sys.stderr)
    return result


def aggregate_sales_v2(tenant_id: int, entity_alias: str, conn, as_of=None) -> int:
    """从 wf2_orders 重算 wf2_sku 的 sales_10/30/60/90/120/180d。

    as_of: 'YYYY-MM-DD' 或 date —— 时间窗基准日。生产默认 None=今天；
           测试传固定日期让窗口计数可确定性断言（不耦合"跑测试那天"）。
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
    if as_of is None:
        today = _dt.date.today()
    elif isinstance(as_of, str):
        today = _dt.date.fromisoformat(as_of)
    else:
        today = as_of
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

"""noon live row producer 契约 —— A 批契约门的「头」(WS-32.1 / WS-34)。

**单一行字段事实源**。钉死 noon 三类数据「live row producer」的接口契约 + 确切
行字段 + canonical 行 fixture，供后续 orders/ASN socket(WS-35/37) 与抓取器
(WS-N2/WS-57) **唯一引用、不各自重定义**，从结构上消除契约漂移
（live 与手工 CSV 口径分叉）。

────────────────────────────────────────────────────────────────────────
producer 接口契约（以 stock 既有 ingest_noon_stock_csv_v2 实现为基准、推广到三类）
────────────────────────────────────────────────────────────────────────
    producer = fn(tenant_id: int) -> Iterable[dict]

  · 产出 **同形 dict row**：键 == 对应 noon 后台导出 CSV 的列名（见 ROW_CONTRACTS）。
    这样 live 源与手工 CSV interim **喂同一个 ingest 解析/聚合/落库**，逐字段一致。
  · row 必填字段缺失 → 调 `validate_rows` 红灯 raise，**绝不默认编数**
    （ingest 内部 safe_int/`or ""` 会把缺失悄悄兜成 0/空 —— 那是占位假数据死法；
     本契约在 producer 边界比 ingest 更严，强制生产端给出真实值）。
  · 注册 / 查询统一走本模块 registry（set/get_live_row_producer + missing_types）。
    其中 `noon_my_inventory` 槽位直接委派给 stock 既有生产注入点
    （noon_live_ingest runner 真消费它）—— 不另起炉灶、单一事实源。
    `noon_orders` / `noon_asn` 槽位待 WS-35/37 把对应 ingest 接上 live（socket），
    本任务只钉契约与 fixture、**不做 socket**。

DATA_TYPES / ROW_CONTRACTS / FIXTURES 三者一一对应；改字段只改这里一处。
"""
from __future__ import annotations

import os
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# 三类 noon 实时数据。键名稳定，供 registry / verifier / 抓取器统一引用。
DATA_TYPES = ("noon_orders", "noon_my_inventory", "noon_asn")


class RowContractViolation(ValueError):
    """row 不满足字段契约（缺必填 / 缺字段组之一 / 含禁用字段）。

    专用异常：让 ingest socket(WS-35/37) 与抓取器(WS-N2/WS-57) 能精确 catch
    「契约违例」并红灯，区别于普通取数错误。
    """


# ────────────────────────────────────────────────────────────────────
# 行字段契约 —— 每类「确切行字段」的单一来源。
#   required   : 必填字段（缺 = 红灯，不许 ingest 兜默认编数）
#   any_of     : 字段组列表，每组至少一个非空（SKU 键 / entity 路由键的「或」关系）
#   optional   : 出现则被 ingest 消费、缺失可接受的字段
#   forbidden  : 出现会被下游误分类的字段（如 noon ASN 不得带 inbound_date，
#                带了会被 ingest_inbound_staging_v2._classify_csv 误判成 erp_inbound）
# 字段名 == noon 后台导出 CSV 列名，正是对应 ingest 真正读取的键，故契约 == 实现。
# ────────────────────────────────────────────────────────────────────
ROW_CONTRACTS: dict[str, dict] = {
    # noon 订单 → ingest_noon_csv_v2.process_csv_v2 → wf2_orders
    #   消费键见 ingest_noon_csv.COLUMN_MAP；硬门是 partner_sku + item_nr
    #   （缺则 process_csv_v2 丢行），其余经济字段强制随行以免利润口径编数。
    "noon_orders": {
        "required": ["partner_sku", "item_nr", "order_timestamp",
                     "status", "offer_price", "gmv_lcy", "currency_code"],
        "any_of": [],
        "optional": ["sku", "fulfillment_model", "dest_country", "family", "brand_code"],
        "forbidden": [],
    },
    # noon 可售库存（my inventory）→ ingest_noon_stock_csv_v2.run_live/_aggregate
    #   → wf1_stock.noon_*。fixture 复用既有 tests/fixtures/wf1_ingest_v2/noon_inventory.csv。
    "noon_my_inventory": {
        "required": ["country_code", "warehouse_code", "qty", "inventory_type"],
        "any_of": [["partner_sku", "sku", "noon_sku"]],
        "optional": ["title"],
        "forbidden": [],
    },
    # noon ASN 送仓 → ingest_inbound_staging_v2.run_v2(source='noon_asn')
    #   → wf1_asn_lines_staging（供 WS-11 算 pending_inbound_qty）。
    "noon_asn": {
        "required": ["asn_number", "qty", "status"],
        "any_of": [["partner_sku", "sku", "noon_sku"],          # SKU 键
                   ["country_code", "entity_alias"]],            # entity 路由键
        "optional": [],
        # noon ASN 不带送仓时间；带了会被 _classify_csv 误判成 erp_inbound。
        "forbidden": ["inbound_date"],
    },
}

# 既有磁盘 fixture（reuse，不另造）：契约的 canonical 行 == 这些文件的内容。
# smoke 断言它们逐行满足 ROW_CONTRACTS，把「复用的 CSV」锁回契约、防止漂移。
FIXTURE_CSV = {
    "noon_orders":      os.path.join(HERE, "..", "..", "tests", "fixtures", "sales", "noon_SA_20260531.csv"),
    "noon_my_inventory": os.path.join(HERE, "..", "..", "tests", "fixtures", "wf1_ingest_v2", "noon_inventory.csv"),
    "noon_asn":         os.path.join(HERE, "..", "..", "tests", "fixtures", "wf1_ingest_v2", "noon_asn.csv"),
}

# canonical 行 fixture（in-memory，键 == noon 导出 CSV 列）。
# WS-35/37/N2 直接 import 这些行作为契约样例；smoke 把它们喂真实 ingest 证明
# 「真实行字段能进 _aggregate/_upsert」（非常量 stub）。值统一用 str，与 CSV 列同形。
FIXTURES: dict[str, list[dict]] = {
    "noon_orders": [
        {"partner_sku": "TBB0116A", "sku": "NOON-TBB0116A-SA", "item_nr": "PSA001",
         "order_timestamp": "2026-05-30 09:00:00", "status": "delivered",
         "fulfillment_model": "FBN", "offer_price": "100.00 SAR", "gmv_lcy": "110.00 SAR",
         "currency_code": "SAR", "dest_country": "SA", "family": "FAM1", "brand_code": "BR1"},
        {"partner_sku": "TBB0116A", "sku": "NOON-TBB0116A-SA", "item_nr": "PSA003",
         "order_timestamp": "2026-05-20 10:00:00", "status": "cancelled",
         "fulfillment_model": "FBN", "offer_price": "100.00 SAR", "gmv_lcy": "0.00 SAR",
         "currency_code": "SAR", "dest_country": "SA", "family": "FAM1", "brand_code": "BR1"},
        {"partner_sku": "TBB0400N", "sku": "NOON-TBB0400N-SA", "item_nr": "PSN001",
         "order_timestamp": "2026-05-31 12:00:00", "status": "delivered",
         "fulfillment_model": "FBN", "offer_price": "80.00 SAR", "gmv_lcy": "85.00 SAR",
         "currency_code": "SAR", "dest_country": "SA", "family": "FAM4", "brand_code": "BR4"},
    ],
    "noon_my_inventory": [
        {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whA", "qty": "10",
         "inventory_type": "saleable", "title": "SKU A title"},
        {"country_code": "SA", "sku": "ZSA001", "warehouse_code": "whB", "qty": "5",
         "inventory_type": "unsaleable", "title": "SKU A title"},
        {"country_code": "SA", "sku": "ZSA002", "warehouse_code": "whA", "qty": "20",
         "inventory_type": "saleable", "title": "SKU B title"},
        {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": "7",
         "inventory_type": "saleable", "title": "SKU C title"},
        {"country_code": "AE", "sku": "ZAE001", "warehouse_code": "whC", "qty": "3",
         "inventory_type": "unsaleable", "title": "SKU C title"},
    ],
    "noon_asn": [
        {"asn_number": "ASN001", "status": "in_transit", "sku": "ZSA001", "qty": "50", "country_code": "SA"},
        {"asn_number": "ASN001", "status": "in_transit", "sku": "ZSA002", "qty": "30", "country_code": "SA"},
        {"asn_number": "ASN002", "status": "received", "sku": "ZAE001", "qty": "25", "country_code": "AE"},
    ],
}


def _check_type(data_type: str) -> str:
    if data_type not in DATA_TYPES:
        raise KeyError(f"未知 noon 数据类型 {data_type!r}；合法值: {DATA_TYPES}")
    return data_type


def _empty(v) -> bool:
    """缺失判定：None / 空串 / 纯空白 算缺失；'0' 等是真实值，不算缺。"""
    return v is None or (isinstance(v, str) and v.strip() == "")


def validate_rows(data_type: str, rows: Iterable[dict]) -> int:
    """逐行校验 rows 是否满足 data_type 的字段契约；返回校验通过的行数。

    任一行缺必填 / 缺字段组之一 / 含禁用字段 → raise RowContractViolation（红灯）。
    **比 ingest 内部更严**：ingest 会把缺失字段兜成默认（qty→0 等），本校验在
    producer 边界拦下，强制生产端给真实值，杜绝「字段缺失默认编数」。
    """
    c = ROW_CONTRACTS[_check_type(data_type)]
    n = 0
    for i, row in enumerate(rows):
        for f in c["required"]:
            if _empty(row.get(f)):
                raise RowContractViolation(
                    f"{data_type} row#{i} 缺必填字段 {f!r}（不允许默认编数）")
        for group in c["any_of"]:
            if not any(not _empty(row.get(k)) for k in group):
                raise RowContractViolation(
                    f"{data_type} row#{i} 字段组 {group} 至少需一个非空（缺 SKU/路由键）")
        for f in c.get("forbidden", ()):
            if not _empty(row.get(f)):
                raise RowContractViolation(
                    f"{data_type} row#{i} 含禁用字段 {f!r}（会被下游误分类）")
        n += 1
    return n


# ────────────────────────────────────────────────────────────────────
# 统一 live row producer registry（推广 stock 既有 set/get 到三类）
#   noon_my_inventory → 委派 stock 既有注入点（生产 runner 真消费它，单一事实源）
#   noon_orders / noon_asn → 本地槽位，待 WS-35/37 socket 对应 ingest 时消费
# ────────────────────────────────────────────────────────────────────
_PRODUCERS: dict[str, Callable] = {}


def _stock_module():
    from hipop.scripts import ingest_noon_stock_csv_v2 as stock
    return stock


def set_live_row_producer(data_type: str, fn: Optional[Callable]) -> None:
    """注册某类 live row producer（fn(tenant_id)->Iterable[dict]）；传 None 清除。"""
    _check_type(data_type)
    if data_type == "noon_my_inventory":
        _stock_module().set_live_row_producer(fn)   # 落到生产真消费的注入点
        return
    if fn is None:
        _PRODUCERS.pop(data_type, None)
    else:
        _PRODUCERS[data_type] = fn


def get_live_row_producer(data_type: str) -> Optional[Callable]:
    _check_type(data_type)
    if data_type == "noon_my_inventory":
        return _stock_module().get_live_row_producer()
    return _PRODUCERS.get(data_type)


def registered_types() -> tuple[str, ...]:
    return tuple(t for t in DATA_TYPES if get_live_row_producer(t) is not None)


def missing_types() -> tuple[str, ...]:
    return tuple(t for t in DATA_TYPES if get_live_row_producer(t) is None)


def check_producers_registered() -> dict:
    """守门：三类 live producer 是否都注册。返回 {ok, registered, missing}。
    任一未注册 → ok=False 且 missing 指出缺哪类（供 smoke/总台红灯定位）。
    """
    missing = list(missing_types())
    return {"ok": not missing, "registered": list(registered_types()), "missing": missing}

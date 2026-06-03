"""noon_live_contract — WS-32.1 · noon 实时行(live row)契约的【唯一来源】。

为什么有这个模块（承重墙 / 契约门头）:
  noon 三类数据（订单 / 可售库存 / ASN送仓）要从「人工导 CSV」换成「实时拉取」。
  换源后,live fetcher（WS-N2/WS-57）产出的 dict row 必须和现有 CSV 入口【同形】,
  才能喂进同一个 `_aggregate/_upsert`,不另起炉灶、不在聚合/落库口径上分叉。

  在本模块之前,只有 stock(可售库存)在 `ingest_noon_stock_csv_v2.py` 里各自定义了
  `set_live_row_producer/get_live_row_producer`;订单 / ASN 两脚本还没有行契约。
  如果让 WS-35(订单 socket)/ WS-37(ASN socket)/ WS-N2(抓取器)各自定义字段,
  live 与 CSV 必然分叉(契约漂移死法)。

  本模块把【三类行的字段契约】+【live producer 注册表】收成一处,作为后续
  socket 与抓取器的【唯一字段来源】。字段键 == 现有 noon CSV 列名,所以 live 行
  和 CSV 行能逐字段对齐、共用同一聚合落库链。

设计边界:
  · 只钉「行形状契约」+「producer 注册表」,不猜 WS-N2 抓取实现、不改业务口径、
    不改目标表 schema、不塞 prompt。
  · 纯加法:不动 `ingest_noon_stock_csv_v2.py` 既有 stock producer hook(已 land、
    被 noon_live_ingest runner 使用),避免回归。stock 后续可平滑收敛到本注册表。
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional


class LiveContractError(Exception):
    """live 行契约违例:未知数据类、缺必填字段、producer 取数挂了等明确失败信号。

    专门区别于「成功但 0 行」——契约违例必须红灯,绝不让上游把
    缺字段 / 空结果当成功(占位假数据死法)。
    """


# ── 三类 noon 行的字段契约(唯一来源) ─────────────────────────────────────
# 每类:
#   required  — 缺任一则该行无效(对齐现有 ingest 的「缺则 skip/红灯」口径)。
#   sku_alt   — SKU 键的等价集,三选一即可(partner_sku 优先,平台 sku / noon_sku 兜底)。
#   recognized— live 行可携带、会被下游消费的全部键(键名 == noon CSV 列名)。
#   feeds     — 该行最终喂进哪个 ingest 的 `_aggregate/_upsert`(给后续 socket 对齐)。
#
# 字段键来源(已核对现有代码,非臆造):
#   orders     ingest_noon_csv.py:COLUMN_MAP(订单 CSV 列)
#   inventory  ingest_noon_stock_csv_v2.py(noon Inventory CSV 列)
#   asn        ingest_inbound_staging_v2.py(noon ASN CSV 列;noon_asn 不带 inbound_date)
NOON_LIVE_ROW_SPECS: Dict[str, dict] = {
    "orders": {
        "required": ("item_nr",),
        "sku_alt": ("partner_sku", "sku", "noon_sku"),
        "recognized": (
            "partner_sku", "sku", "noon_sku", "item_nr", "order_timestamp",
            "status", "fulfillment_model", "offer_price", "gmv_lcy",
            "currency_code", "dest_country", "family", "brand_code",
        ),
        "feeds": "ingest_noon_csv_v2 → wf2_orders / wf2_sku",
        "desc": "noon 订单行(每行一笔订单明细,带成交价/状态/退货取消标记)",
    },
    "inventory": {
        "required": ("country_code", "qty", "inventory_type"),
        "sku_alt": ("partner_sku", "sku", "noon_sku"),
        "recognized": (
            "country_code", "partner_sku", "sku", "noon_sku",
            "warehouse_code", "qty", "inventory_type",
        ),
        "feeds": "ingest_noon_stock_csv_v2 → wf1_stock.noon_*",
        "desc": "noon 可售库存行(per 仓 per 类型,saleable/unsaleable)",
    },
    "asn": {
        "required": ("asn_number",),
        "sku_alt": ("partner_sku", "sku", "noon_sku"),
        "recognized": (
            "asn_number", "partner_sku", "sku", "noon_sku", "qty", "status",
            "entity_alias", "country_code",
        ),
        "feeds": "ingest_inbound_staging_v2 → wf1_asn_lines_staging",
        "desc": "noon ASN/送仓行(送仓单号 + SKU + 数量,noon_asn 不带 inbound_date)",
    },
}

# 三类数据的稳定顺序(给 missing_producers / 报错用,顺序确定)。
KINDS: tuple = ("orders", "inventory", "asn")


def _spec(kind: str) -> dict:
    spec = NOON_LIVE_ROW_SPECS.get(kind)
    if spec is None:
        raise LiveContractError(
            f"未知 noon live 数据类 {kind!r};合法值: {', '.join(KINDS)}"
        )
    return spec


def missing_fields(kind: str, row: dict) -> List[str]:
    """返回 row 相对 kind 契约缺的必填字段列表(空 = 合规)。

    缺字段判定: required 里的键缺/为空,以及 sku_alt 三键全缺/全空。
    不臆造默认值——缺就如实报出,交由调用方红灯,绝不默认编数。
    """
    spec = _spec(kind)
    miss: List[str] = []
    for key in spec["required"]:
        v = row.get(key)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            miss.append(key)
    sku_alt = spec["sku_alt"]
    if not any(
        (row.get(k) is not None and str(row.get(k)).strip() != "") for k in sku_alt
    ):
        miss.append("|".join(sku_alt))
    return miss


def validate_row(kind: str, row: dict) -> None:
    """合规则静默返回;缺字段则 raise LiveContractError(指出缺哪些)。"""
    miss = missing_fields(kind, row)
    if miss:
        raise LiveContractError(
            f"noon {kind} live 行缺必填字段: {', '.join(miss)}(契约见 NOON_LIVE_ROW_SPECS)"
        )


# ── live producer 注册表(唯一来源) ──────────────────────────────────────
# producer 接口契约: fn(tenant_id) -> Iterable[dict],产出【同形 dict row】
# (键同对应 NOON_LIVE_ROW_SPECS[kind]["recognized"])。WS-N2 抓取器 land 后,
# 各 socket(WS-35/37)/ runner 调 set_live_row_producer(kind, fn) 注册。
LiveRowProducer = Callable[[int], Iterable[dict]]

_LIVE_ROW_PRODUCERS: Dict[str, Optional[LiveRowProducer]] = {k: None for k in KINDS}


def set_live_row_producer(kind: str, fn: Optional[LiveRowProducer]) -> None:
    """注册 / 清除(传 None)某数据类的 noon live row producer。"""
    _spec(kind)  # 校验 kind 合法
    _LIVE_ROW_PRODUCERS[kind] = fn


def get_live_row_producer(kind: str) -> Optional[LiveRowProducer]:
    _spec(kind)
    return _LIVE_ROW_PRODUCERS[kind]


def registered_kinds() -> List[str]:
    """已注册 producer 的数据类(按 KINDS 顺序)。"""
    return [k for k in KINDS if _LIVE_ROW_PRODUCERS[k] is not None]


def missing_producers() -> List[str]:
    """尚未注册 producer 的数据类(按 KINDS 顺序)。

    全部三类换源完成前,这里非空 → 守门 smoke 据此红灯并指出缺哪类,
    防止「以为接好了实则没接」的接线缺失死法。
    """
    return [k for k in KINDS if _LIVE_ROW_PRODUCERS[k] is None]


def reset_producers() -> None:
    """清空全部注册(主要给测试用,避免跨用例串状态)。"""
    for k in KINDS:
        _LIVE_ROW_PRODUCERS[k] = None

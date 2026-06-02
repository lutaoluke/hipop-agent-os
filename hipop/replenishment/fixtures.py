"""加载两份固定夹具，并把原始 JSON 解析成契约对象。

解析时遵守契约的一条铁律：JSON 里**整段缺省**某一类 -> 对应字段为 None（缺失）；
该类存在但值为 0/空 -> 解析成真实对象（如在途=0 的 LogisticsInput），不是缺失。
这样缺失检测点 `missing_input_classes` 才能把「没拿到数据」和「数据就是 0」分开。
"""
import json
import os
from typing import Optional, Tuple

from .contracts import (
    InventoryInput,
    LogisticsInput,
    SalesInput,
    SkuReplenishInput,
)

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests",
    "fixtures",
)

KNOWN_INPUT_FIXTURE = os.path.join(_FIXTURE_DIR, "replenish_known_input.json")
# 三份缺失夹具：各缺一类，逐一钉死「缺任意一类都显式标注、不静默给 0」。
MISSING_DATA_FIXTURE = os.path.join(_FIXTURE_DIR, "replenish_missing_data.json")
MISSING_LOGISTICS_FIXTURE = os.path.join(_FIXTURE_DIR, "replenish_missing_logistics.json")
MISSING_SALES_FIXTURE = os.path.join(_FIXTURE_DIR, "replenish_missing_sales.json")


def _parse(raw: dict) -> SkuReplenishInput:
    """原始 JSON -> SkuReplenishInput。缺省的输入类 -> None。"""
    log = raw.get("logistics")
    sales = raw.get("recent_sales")
    inv = raw.get("inventory")
    return SkuReplenishInput(
        sku=raw["sku"],
        logistics=LogisticsInput(
            in_transit_qty=log["in_transit_qty"],
            pending_shipment_qty=log["pending_shipment_qty"],
        ) if log is not None else None,
        recent_sales=SalesInput(
            window_days=sales["window_days"],
            daily_units=list(sales["daily_units"]),
        ) if sales is not None else None,
        inventory=InventoryInput(
            by_warehouse=dict(inv["by_warehouse"]),
        ) if inv is not None else None,
    )


def load_known_input() -> Tuple[SkuReplenishInput, Optional[dict]]:
    """已知输入夹具：三类齐全。返回 (契约对象, main(WS-5) 既有人工锚点)。

    第二个返回值是 WS-5 留在 main 的 `expected_replenishment`（含人工首批
    total_replenish=60），**保持向后兼容**，不挪 WS-5 的锚点——WS-5 的 smoke
    仍消费它。步骤2 的缺口锚点另走 `load_known_step2_expected`。
    """
    with open(KNOWN_INPUT_FIXTURE, encoding="utf-8") as f:
        raw = json.load(f)
    return _parse(raw), raw.get("expected_replenishment")


def load_known_step2_expected() -> Tuple[SkuReplenishInput, Optional[dict]]:
    """已知输入夹具：三类齐全。返回 (契约对象, 步骤2 专属验收锚点)。

    第二个返回值是夹具里 **步骤2 专属**的 `expected_step2_total`（缺口总量
    gap_total=168）。步骤2 只对「该补多少总量(=缺口)」负责，用本层自己的字段
    断言，不挪 WS-5 留在 main 的首批锚点。
    """
    with open(KNOWN_INPUT_FIXTURE, encoding="utf-8") as f:
        raw = json.load(f)
    return _parse(raw), raw.get("expected_step2_total")


def _load_missing(fixture_path: str) -> Tuple[SkuReplenishInput, str]:
    """通用缺失夹具加载：返回 (契约对象, 该夹具声明缺的类)。"""
    with open(fixture_path, encoding="utf-8") as f:
        raw = json.load(f)
    return _parse(raw), raw["_missing_class"]


def load_missing_data() -> Tuple[SkuReplenishInput, str]:
    """数据缺失夹具：缺『各仓现有库存 inventory』。返回 (契约对象, 缺的类)。"""
    return _load_missing(MISSING_DATA_FIXTURE)


def load_missing_logistics() -> Tuple[SkuReplenishInput, str]:
    """数据缺失夹具：缺『物流 logistics』。返回 (契约对象, 缺的类)。"""
    return _load_missing(MISSING_LOGISTICS_FIXTURE)


def load_missing_sales() -> Tuple[SkuReplenishInput, str]:
    """数据缺失夹具：缺『近N天销量 recent_sales』。返回 (契约对象, 缺的类)。"""
    return _load_missing(MISSING_SALES_FIXTURE)

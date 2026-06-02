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
MISSING_DATA_FIXTURE = os.path.join(_FIXTURE_DIR, "replenish_missing_data.json")


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
    """已知输入夹具：三类齐全。返回 (契约对象, 人工给定的预期补货量)。

    预期补货量是步骤2 的验收锚点（人工核定，非算法产物）。
    """
    with open(KNOWN_INPUT_FIXTURE, encoding="utf-8") as f:
        raw = json.load(f)
    return _parse(raw), raw.get("expected_replenishment")


def load_missing_data() -> Tuple[SkuReplenishInput, str]:
    """数据缺失夹具：故意缺某一类。返回 (契约对象, 该夹具声明缺的类)。"""
    with open(MISSING_DATA_FIXTURE, encoding="utf-8") as f:
        raw = json.load(f)
    return _parse(raw), raw["_missing_class"]

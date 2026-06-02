"""补货 · 三类静态输入的数据契约 + 缺失检测点。

这一层只回答一个问题：「某个 SKU 要算补货，需要哪三类只读输入，
其中哪几类到位了、哪几类缺了。」补货量怎么算是步骤2/3 的事，这里不碰。

三类输入（缺一不可，少一类就不能可信地算补货）：
  1. 物流 LOGISTICS   —— 在途量 + 待发货量
  2. 近 N 天销量 SALES —— 一个销量窗口（window_days + 每日件数）
  3. 各仓现有库存 INVENTORY —— 各仓库的现有库存快照

缺失检测点 `missing_input_classes(sku_input)` 是给算法层显式标注用的：
它返回某 SKU 缺了哪几类，算法层据此把这些 SKU 标成「数据不全、补货量不可信」。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 三类输入的稳定 key —— 下游标注 / 断言都用这组常量，别散写字符串。
INPUT_LOGISTICS = "logistics"
INPUT_SALES = "recent_sales"
INPUT_INVENTORY = "inventory"

# 算一个 SKU 的补货，这三类缺一不可。
REQUIRED_INPUT_CLASSES = (INPUT_LOGISTICS, INPUT_SALES, INPUT_INVENTORY)


@dataclass
class LogisticsInput:
    """物流：在途量 + 待发货量（件）。"""
    in_transit_qty: int
    pending_shipment_qty: int


@dataclass
class SalesInput:
    """近 N 天销量窗口。`daily_units` 长度应与 `window_days` 一致。"""
    window_days: int
    daily_units: List[int] = field(default_factory=list)

    @property
    def total_units(self) -> int:
        return sum(self.daily_units)


@dataclass
class InventoryInput:
    """各仓现有库存快照：仓库名 -> 现有件数。"""
    by_warehouse: Dict[str, int] = field(default_factory=dict)

    @property
    def total_on_hand(self) -> int:
        return sum(self.by_warehouse.values())


@dataclass
class SkuReplenishInput:
    """一个 SKU 的三类静态输入。任意一类缺失即为 None（不是空对象）。

    用 None 表示「这类数据没拿到」，而不是塞个全 0 的空对象假装有数据——
    后者正是「占位假数据」死法。空对象（如在途=0）是合法的真实数据，
    None 才是缺失。两者必须分得开，缺失检测点才有意义。
    """
    sku: str
    logistics: Optional[LogisticsInput] = None
    recent_sales: Optional[SalesInput] = None
    inventory: Optional[InventoryInput] = None

    def get(self, input_class: str):
        return {
            INPUT_LOGISTICS: self.logistics,
            INPUT_SALES: self.recent_sales,
            INPUT_INVENTORY: self.inventory,
        }[input_class]


def missing_input_classes(sku_input: SkuReplenishInput) -> List[str]:
    """缺失检测点：返回该 SKU 缺了哪几类输入（按 REQUIRED_INPUT_CLASSES 顺序）。

    三类齐全则返回 []。算法层应消费它来显式标注「数据不全」的 SKU，
    而不是默默对缺数据的 SKU 算出一个看似正常、实则不可信的补货量。
    """
    return [cls for cls in REQUIRED_INPUT_CLASSES if sku_input.get(cls) is None]


def is_complete(sku_input: SkuReplenishInput) -> bool:
    """三类输入是否齐全。"""
    return not missing_input_classes(sku_input)

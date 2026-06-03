"""补货 · 动态分析层：把人工补货经验固化成可复现规则（WS-6 步骤2）。

消费步骤1（WS-5）的三类只读输入，合成每个 SKU 的「建议补货量」。
**同输入同输出**：纯函数，不读时钟、不取随机、不碰外部状态——同一份输入
永远算出同一个数。这是「可复现」的硬约束，也是验收能成立的前提。

## 算法规则（供人做真活/假活终判）

记三类输入：
  - 物流：在途量 in_transit + 待发货量 pending
  - 近 N 天销量：window_days 天里每天件数 daily_units
  - 各仓现有库存：on_hand = 各仓求和

1. 日均销量  daily_avg = round(总件数 / window_days)
   —— 销量按整件取整（卖的是整件商品），与人工口算一致。
2. 月均需求  monthly_demand = daily_avg * MONTH_DAYS(30)
3. 当前覆盖  coverage = on_hand + in_transit + pending（即时库存 + 在途待发，
   都是「迟早会到的货」，一起冲抵需求）。
4. 目标库存  target = TARGET_MONTHS(2) * monthly_demand
5. 缺口      gap = target - coverage
6. 建议补货量 recommended:
     - gap <= 0          → 0（覆盖已达目标，不必补）
     - 0 < gap < MIN_ORDER(30) → MIN_ORDER（小单不值得单独下，凑到起订量）
     - 否则              → gap（取整）

> 已知输入夹具 TBJ0059A 验算：
>   日均 round(51/7)=7 → 月均 210；coverage = 92+120+40 = 252；
>   target = 2*210 = 420；gap = 420-252 = 168 → 建议补货 168 件。
>   （首批/后续的分批拆分属步骤3，本层只产出「该补多少」这一总量。）

## 缺失处理（消费步骤1 的缺失检测点，绝不静默给 0）

算 recommended 之前，先调 `missing_input_classes(sku_input)`。只要缺三类里
任意一类，就**不进入上面的计算**，直接返回一个 `computable=False` 的结果，
`recommended_qty=None`，并带上缺了哪几类。
缺数据的 SKU 是「不可计算」，不是「补 0」——这两者对运营是完全不同的动作
（不可计算要去补数据，补 0 是真的不补货）。把它们混为一谈正是「死代码短路」
死法：缺失分支被一条默认 0 悄悄绕过。这里用 None + 显式缺失类把它钉死。
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .contracts import SkuReplenishInput, missing_input_classes

# 确定性常量（无随机、无时间）。改这些 = 改补货口径，必须连带改夹具预期值。
MONTH_DAYS = 30        # 一个月按 30 天折算需求
TARGET_MONTHS = 2      # 目标：覆盖（含在途）库存达到 2 个月销量
MIN_ORDER = 30         # 起订量：算出来 >0 但不足此数，凑到此数

# 结果状态：可计算 / 数据缺失不可计算。
STATUS_OK = "ok"
STATUS_DATA_MISSING = "data_missing"


@dataclass
class ReplenishResult:
    """单个 SKU 的补货建议结果。

    可计算时：computable=True, recommended_qty 为整数, missing_classes 为空，
    并带上中间量（monthly_demand / coverage_qty / target_qty）供人核账。
    不可计算时：computable=False, recommended_qty=None, missing_classes 列出缺哪几类。
    """
    sku: str
    status: str
    computable: bool
    recommended_qty: Optional[int]
    missing_classes: List[str] = field(default_factory=list)
    # 中间量：仅可计算时有意义，便于「真活」核账（产生端真算了）。
    daily_avg_units: Optional[int] = None
    monthly_demand: Optional[int] = None
    coverage_qty: Optional[int] = None
    target_qty: Optional[int] = None
    rationale: str = ""


def compute_replenishment(sku_input: SkuReplenishInput) -> ReplenishResult:
    """对单个 SKU 计算建议补货量。纯函数，同输入同输出。"""
    # —— 缺失分支：缺任意一类即不可计算，显式标注，绝不静默给 0。——
    missing = missing_input_classes(sku_input)
    if missing:
        return ReplenishResult(
            sku=sku_input.sku,
            status=STATUS_DATA_MISSING,
            computable=False,
            recommended_qty=None,
            missing_classes=missing,
            rationale="数据缺失/不可计算：缺少 " + "、".join(missing)
            + "，无法可信地计算补货量（不补 0，需先补齐数据）。",
        )

    # —— 三类齐全，进入确定性计算。——
    sales = sku_input.recent_sales
    logistics = sku_input.logistics
    inventory = sku_input.inventory

    daily_avg = round(sales.total_units / sales.window_days)
    monthly_demand = daily_avg * MONTH_DAYS
    coverage = (
        inventory.total_on_hand
        + logistics.in_transit_qty
        + logistics.pending_shipment_qty
    )
    target = TARGET_MONTHS * monthly_demand
    gap = target - coverage

    if gap <= 0:
        recommended = 0
        reason = f"含在途覆盖 {coverage} 件已达目标 {target} 件（{TARGET_MONTHS} 个月），无需补货。"
    elif gap < MIN_ORDER:
        recommended = MIN_ORDER
        reason = (
            f"缺口 {gap} 件不足起订量，凑到起订量 {MIN_ORDER} 件。"
        )
    else:
        recommended = gap
        reason = (
            f"日均 {daily_avg} 件 → 月均 {monthly_demand} 件；含在途覆盖 {coverage} 件，"
            f"目标 {target} 件（{TARGET_MONTHS} 个月），缺口 = {target} - {coverage} = {recommended} 件。"
        )

    return ReplenishResult(
        sku=sku_input.sku,
        status=STATUS_OK,
        computable=True,
        recommended_qty=recommended,
        missing_classes=[],
        daily_avg_units=daily_avg,
        monthly_demand=monthly_demand,
        coverage_qty=coverage,
        target_qty=target,
        rationale=reason,
    )


def compute_many(sku_inputs: List[SkuReplenishInput]) -> List[ReplenishResult]:
    """批量计算。顺序稳定，逐个走 `compute_replenishment`。"""
    return [compute_replenishment(s) for s in sku_inputs]

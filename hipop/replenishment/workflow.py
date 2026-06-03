"""补货 · 接线层（WS-7 步骤3）：把三类静态数据接成一个可执行入口。

这一层**不发明算法**，只做粘合：
  三类静态数据源（物流 / 近N天销量 / 各仓库存，各按 SKU 索引）
    → 按 SKU 求并集 join 成每个 SKU 的三类输入（缺某源该类即 None）
    → **调用步骤2 的算法 compute_many**（绝不另起一套，也不旁路短路）
    → 输出每个 SKU 的补货量表（缺数据的 SKU 被算法标成「不可计算」）。

## 为什么入口必须真调步骤2 的算法（防「接线缺失 / 死代码短路」死法）
算法写好了没人在执行路径上调它 == 黑屏。所以补货量**只**能来自
`compute_many`（步骤2 纯函数）的产物：本模块把每个 SKU 的输入交给它，
再把它返回的 `ReplenishResult` 原样摊平成表行——表里出现的 coverage_qty /
target_qty / recommended_qty 都是算法算的，本层不自己拼一个数。缺失标注同理，
来自算法消费步骤1 缺失检测点的结果，不在本层另判。

## 本圈不做（见需求「不在本圈范围」）
不做大屏展示、不做 chat 交互、不接实时 ERP 拉数。本入口只吃**静态数据**
（夹具/文件），证明「三类数据 → 每 SKU 补货量」这条链真的在执行路径上跑通。

跑法：
  python3 -m hipop.replenishment.workflow                # 跑默认数据集夹具
  python3 -m hipop.replenishment.workflow <dataset_dir>  # 跑指定目录的三类源
"""
import json
import os
from typing import Dict, List, Optional

from .algorithm import ReplenishResult, compute_many
from .contracts import (
    InventoryInput,
    LogisticsInput,
    SalesInput,
    SkuReplenishInput,
)

# 三类静态数据源的文件名（每个文件 {"by_sku": {sku: {...}}}）。
LOGISTICS_SOURCE = "logistics.json"
SALES_SOURCE = "recent_sales.json"
INVENTORY_SOURCE = "inventory.json"

# 默认数据集夹具目录（仓库内置，供 smoke / 手跑）。
DEFAULT_DATASET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests",
    "fixtures",
    "replenish_dataset",
)


def _load_source(dataset_dir: str, filename: str) -> Dict[str, dict]:
    """读一个静态数据源文件，返回 {sku: 原始记录}。文件缺省视为空源。"""
    path = os.path.join(dataset_dir, filename)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return dict(raw.get("by_sku", {}))


def _logistics_of(rec: Optional[dict]) -> Optional[LogisticsInput]:
    if rec is None:
        return None
    return LogisticsInput(
        in_transit_qty=rec["in_transit_qty"],
        pending_shipment_qty=rec["pending_shipment_qty"],
    )


def _sales_of(rec: Optional[dict]) -> Optional[SalesInput]:
    if rec is None:
        return None
    return SalesInput(window_days=rec["window_days"], daily_units=list(rec["daily_units"]))


def _inventory_of(rec: Optional[dict]) -> Optional[InventoryInput]:
    if rec is None:
        return None
    return InventoryInput(by_warehouse=dict(rec["by_warehouse"]))


def join_static_dataset(dataset_dir: str) -> List[SkuReplenishInput]:
    """把三类静态数据源按 SKU join 成每个 SKU 的三类输入。

    SKU 全集 = 三源里出现过的 SKU 并集（按名字排序，输出稳定）。
    某 SKU 在某源里缺省 -> 该类为 None（缺失），交给步骤2 算法显式标注，
    本层不静默补 0、不丢这个 SKU。
    """
    logistics = _load_source(dataset_dir, LOGISTICS_SOURCE)
    sales = _load_source(dataset_dir, SALES_SOURCE)
    inventory = _load_source(dataset_dir, INVENTORY_SOURCE)

    all_skus = sorted(set(logistics) | set(sales) | set(inventory))
    return [
        SkuReplenishInput(
            sku=sku,
            logistics=_logistics_of(logistics.get(sku)),
            recent_sales=_sales_of(sales.get(sku)),
            inventory=_inventory_of(inventory.get(sku)),
        )
        for sku in all_skus
    ]


def _to_row(result: ReplenishResult) -> dict:
    """把步骤2 算法产物 ReplenishResult 原样摊平成一行表（不另算任何量）。"""
    return {
        "sku": result.sku,
        "status": result.status,
        "computable": result.computable,
        "recommended_qty": result.recommended_qty,
        "missing_classes": list(result.missing_classes),
        "coverage_qty": result.coverage_qty,
        "target_qty": result.target_qty,
        "daily_avg_units": result.daily_avg_units,
        "monthly_demand": result.monthly_demand,
        "rationale": result.rationale,
    }


def run_replenishment(sku_inputs: List[SkuReplenishInput]) -> List[dict]:
    """入口核心：把每个 SKU 的三类输入交给步骤2 算法，返回补货量表。

    补货量**只**来自 `compute_many`（步骤2）的产物——本函数不自己算任何数，
    只负责把结果摊平成表。这是「接线」的全部职责。
    """
    results = compute_many(sku_inputs)
    return [_to_row(r) for r in results]


def replenish_from_dataset_dir(dataset_dir: str = DEFAULT_DATASET_DIR) -> List[dict]:
    """端到端入口：三类静态数据目录 → join → 调步骤2 算法 → 每 SKU 补货量表。"""
    return run_replenishment(join_static_dataset(dataset_dir))


def format_table(table: List[dict]) -> str:
    """把补货量表渲染成纯文本（仅供 CLI 人看，不参与断言）。"""
    lines = [
        "SKU 补货建议表（三类静态数据 → 步骤2 算法）",
        "-" * 60,
        f"{'SKU':<12}{'补货量':>8}  状态/说明",
        "-" * 60,
    ]
    for row in table:
        if row["computable"]:
            qty = str(row["recommended_qty"])
            note = row["rationale"]
        else:
            qty = "—"
            note = f"数据缺失（缺 {'、'.join(row['missing_classes'])}）：不可计算，需先补数据"
        lines.append(f"{row['sku']:<12}{qty:>8}  {note}")
    computable = [r for r in table if r["computable"]]
    missing = [r for r in table if not r["computable"]]
    lines.append("-" * 60)
    lines.append(
        f"共 {len(table)} 个 SKU：{len(computable)} 个可算补货、{len(missing)} 个数据缺失待补。"
    )
    return "\n".join(lines)


def run_workflow_step(tenant_id: int = 1) -> List[dict]:
    """WORKFLOW_REGISTRY 触发入口（runner 解析后调的就是这个 callable）。

    把入口接上**真实触发面**：UI/chat/scheduler 经 `/run-workflow` → runner
    `_resolve_callable("hipop.replenishment.workflow:run_workflow_step")` → 调到这里，
    而不是只能单跑 CLI/smoke。证明算法在真实执行路径上被调到（防「接线缺失」死法）。

    本圈只吃静态夹具（不接实时 ERP 拉数，见需求「不在本圈范围」），所以这里跑
    内置数据集夹具走完整链：三类静态数据 → join → **步骤2 算法 compute_many** → 补货表。
    接 `tenant_id` 只为兼容 runner 的传参探测；本圈静态数据不分租户，故未消费。
    """
    table = replenish_from_dataset_dir()
    print(format_table(table))
    return table


def main(dataset_dir: str = DEFAULT_DATASET_DIR) -> List[dict]:
    table = replenish_from_dataset_dir(dataset_dir)
    print(format_table(table))
    return table


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATASET_DIR
    main(target)

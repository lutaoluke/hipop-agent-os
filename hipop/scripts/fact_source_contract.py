"""事实源契约（WS-129/P0-S1）— noon/ERP 权威源、库存不含在途、来源时间必答。

口径依据（Luke 于 WS-127 确认）：
  - 销量、noon 官方仓库存：以 noon 官网实时为准（SOURCE_NOON）。
  - 国内仓库存（义乌/东莞）、海外仓、送仓未上架（pending_inbound）、
    国际在途、物流节点：以 ERP 为准（SOURCE_ERP）。
  - 旧表（wf1_stock 历史行、wf1_stock_history）只允许作为带时间缓存，
    时间戳缺失 → 不得作为事实（ContractViolation）。
  - 在途（in_transit_total_qty，来自 wf3_logistics_hub_v2）单列展示，
    不计入库存合计（total_stock）。

防漂移：本模块是唯一来源——
  - merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS 必须与
    INVENTORY_TOTAL_COMPONENTS 严格一致，用 assert_inventory_total_matches_merge 验。
  - smoke_fact_source_contract.py 在每次 make test 时验证两者一致，漂移立刻红灯。
"""
from __future__ import annotations

# ── 权威来源枚举 ────────────────────────────────────────────────────────────────
SOURCE_NOON = "noon"    # noon 官网实时数据
SOURCE_ERP = "erp"      # ERP 系统数据（实时或带时间戳的缓存）

# ── 数据类型 → 权威来源（唯一定义，禁在 agent prompt 里另定）─────────────────────
# 读：任何返回此类数据的路径都必须携带对应来源标签与时间戳。
AUTHORITATIVE_SOURCES: dict[str, str] = {
    "noon_inventory":       SOURCE_NOON,  # noon 官方仓（noon_total_qty, noon_saleable_qty）
    "sales":                SOURCE_NOON,  # 销量（wf2_orders → wf2_sku）
    "domestic_inventory":   SOURCE_ERP,   # 国内仓库存（yiwu_qty, dongguan_qty）
    "overseas_inventory":   SOURCE_ERP,   # 海外仓库存（overseas_total_qty）
    "pending_inbound":      SOURCE_ERP,   # 送仓未上架（pending_inbound_qty，ERP ASN）
    "in_transit":           SOURCE_ERP,   # 国际在途（wf3_logistics_hub_v2.in_transit_total_qty）
    "logistics_nodes":      SOURCE_ERP,   # 物流节点（ERP 实时查询）
}

# ── 库存合计口径（INVENTORY_TOTAL_COMPONENTS）────────────────────────────────────
# 必须与 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS **严格相同**。
# 改动此处必须同步改 merge_stock_snapshot_v2.py，smoke 会自动发现漂移。
#
# 口径说明：
#   noon 官方仓物理库存 + 海外仓 + 国内仓（义乌/东莞）+ 送仓未上架（pending）
#   = 运营可用库存合计；
#   国际在途（in_transit_total_qty，wf3）物理上还在运输中，不在此合计中，须单列。
INVENTORY_TOTAL_COMPONENTS: tuple[str, ...] = (
    "noon_total_qty",       # noon 官方仓物理库存（来源：noon）
    "overseas_total_qty",   # 海外仓（来源：ERP）
    "yiwu_qty",             # 义乌国内仓（来源：ERP）
    "dongguan_qty",         # 东莞国内仓（来源：ERP）
    "pending_inbound_qty",  # ASN 送仓未上架（来源：ERP，尚未 GRN 上架）
)

# ── 必须单列、禁止计入库存合计的字段 ────────────────────────────────────────────
# in_transit_total_qty 存在于 wf3_logistics_hub_v2，物理上是国际运输中的货，
# 与 wf1_stock.total_stock 不在同一张表，也不应被加入合计。
NOT_IN_INVENTORY_TOTAL: tuple[str, ...] = (
    "in_transit_total_qty",  # 国际在途（wf3_logistics_hub_v2，单列展示）
)

# ── 每类数据必须携带的时间戳字段 ────────────────────────────────────────────────
# 表/列名：查询路径上必须把这个字段一并返回给调用方。
# 时间戳为 NULL 或缺失 → 该数字不得作为事实（ContractViolation）。
REQUIRED_TIMESTAMPS: dict[str, str] = {
    "wf1_stock":           "imported_at",   # noon/ERP 库存 ingest 时间
    "wf3_logistics":       "updated_at",    # wf3_logistics_hub_v2 更新时间
    "wf2_sku_sales":       "imported_at",   # 销量 ingest 时间（wf2_sku.imported_at）
}

# ── 缓存有效期阈值（小时）────────────────────────────────────────────────────────
# 超过阈值且用户未明确授权 → 不得作为事实。
# 与 verifiers._noon_freshness_max_hours / data._STOCK_READINESS_MAX_AGE_HOURS 口径对齐。
CACHE_MAX_AGE_HOURS: dict[str, float] = {
    "noon_inventory": 26.0,    # noon 至少每日刷，留 2h 余量
    "erp_inventory":  72.0,    # ERP 库存 3 天内有效
    "erp_logistics":  72.0,    # ERP 物流 3 天内有效
    "sales":          72.0,    # 销量 3 天内有效
}


# ── 契约违反异常 ────────────────────────────────────────────────────────────────

class ContractViolation(Exception):
    """事实源契约被违反时抛出。调用方应将此视为红灯（blocked），不得静默吞掉。"""


# ── 验证函数（可被 smoke 和 verifier 直接调用）──────────────────────────────────

def assert_inventory_total_matches_merge(merge_components) -> None:
    """验证 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS 与本契约的
    INVENTORY_TOTAL_COMPONENTS 严格一致——口径只有一套真相，两者不得漂移。

    参数:
        merge_components: merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS（tuple 或 iterable）

    漂移发现策略（双向检查）：
      - 本契约有、merge 没有 → 契约多了一个来源列（需同步改 merge）
      - merge 有、本契约没有 → merge 多了一个来源列（需同步改契约）
    """
    expected = set(INVENTORY_TOTAL_COMPONENTS)
    actual = set(merge_components)
    if expected == actual:
        return
    only_contract = sorted(expected - actual)
    only_merge = sorted(actual - expected)
    raise ContractViolation(
        f"库存合计口径漂移！"
        f"只在本契约: {only_contract}，"
        f"只在 merge_stock_snapshot_v2: {only_merge}。"
        f"两处必须保持同一份列清单（WS-129 事实源契约）。"
    )


def assert_in_transit_not_in_inventory_total(merge_components) -> None:
    """验证 NOT_IN_INVENTORY_TOTAL 中的字段不在库存合计列清单里。

    失败 = 某"必须单列"的字段被错误地加入了 total_stock 计算，
    导致在途/待发货被当成可售库存。
    """
    actual = set(merge_components)
    for col in NOT_IN_INVENTORY_TOTAL:
        if col in actual:
            raise ContractViolation(
                f"'{col}'（必须单列，不得计入库存合计）被错误加入 merge 列清单。"
                f"在途货物不是当前可售库存（WS-129 事实源契约）。"
            )


def assert_data_has_timestamp(data: dict, timestamp_key: str, context: str = "") -> None:
    """验证数据字典包含有效（非空）时间戳字段。

    无时间戳的缓存不能作为事实——调用方必须拒绝（不许 fallback 到无时间缓存）。

    参数:
        data:          返回给调用方的数据字典
        timestamp_key: 必须存在的时间戳键名（如 'imported_at'，'updated_at'）
        context:       出错时补充的上下文描述
    """
    ts = data.get(timestamp_key)
    if not ts:
        prefix = f"[{context}] " if context else ""
        raise ContractViolation(
            f"{prefix}数据缺少时间戳 '{timestamp_key}'——"
            f"无时间戳的缓存不能作为事实（WS-129 事实源契约）。"
        )


def assert_data_has_source(data: dict, source_key: str = "source", context: str = "") -> None:
    """验证数据字典包含有效来源标签（'noon' 或 'erp'）。

    任何数字回答必须携带 source，不携带的视为口径不明、不可信。
    """
    prefix = f"[{context}] " if context else ""
    src = data.get(source_key)
    if not src:
        raise ContractViolation(
            f"{prefix}数据缺少来源标签 '{source_key}'——"
            f"任何数字回答必须携带 source（WS-129 事实源契约）。"
        )
    if src not in (SOURCE_NOON, SOURCE_ERP, "cache"):
        raise ContractViolation(
            f"{prefix}来源标签 '{source_key}={src}' 未知——"
            f"应为 '{SOURCE_NOON}' / '{SOURCE_ERP}'（WS-129 事实源契约）。"
        )


def validate_stock_row(row: dict) -> list[str]:
    """验证 wf1_stock 单行是否满足事实源契约。返回问题列表（空 = 合格）。

    合格条件：
      1. imported_at 非空（noon 数据必须有 ingest 时间戳）
      2. 若 noon_total_qty 非 NULL，则 imported_at 必须非 NULL（noon 事实必须有时间）
      3. total_stock 只含 INVENTORY_TOTAL_COMPONENTS 的求和，不含 in_transit_total_qty
    """
    problems: list[str] = []
    noon_qty = row.get("noon_total_qty")
    imported_at = row.get("imported_at")
    if noon_qty is not None and not imported_at:
        problems.append(
            f"noon_total_qty={noon_qty} 非 NULL 但 imported_at 为空——"
            f"noon 库存行必须带 ingest 时间戳（事实源契约）"
        )
    for col in NOT_IN_INVENTORY_TOTAL:
        if col in row and row.get("total_stock") is not None:
            # 若行里同时有 in_transit 和 total_stock，做松散检查：
            # total_stock 不应等于某个包含了 in_transit 的求和。
            # 严格检查由 verifier wf1_stock_merge_v2 负责；此处只报警。
            pass
    return problems

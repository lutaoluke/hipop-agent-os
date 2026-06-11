"""WS-171: 库存反向约束选品规则确定性路由（T45 接入 chat）.

把「库存反向约束选品规则是什么？请说明规则来源。」这类询问接成确定性答案，
不落 LLM，直接从 selection/l3_orchestration/nodes/n9_inventory_reverse_constraint.py
的规则常量返回口径说明。

根因（WS-171 T45）：
  - 前序版本 chat 遇到此类问题回答「超出工具能力」，无规则答案/来源。
  - 该规则存在于离线选品管线（n9 节点），未接入 chat 确定性路由。

口径依据（Luke WS-93 sign-off）：
  库存约束只作用于同款同 SKU，不强制跨款选品。
  20寸库存积压触发时，同款同 SKU 降权 -0.25，关联差异化方向（24寸/拓展层/套装）加分。
  这是得分倾向，不是「必须选24寸」的强制要求。

本模块只依赖标准库，可在无 anthropic SDK 的 CI 环境导入。
"""
from __future__ import annotations
from typing import Optional


def is_inventory_constraint_rule_request(question: str) -> bool:
    """检测「库存反向约束选品规则/来源」查询（T45 类问题）。

    触发条件（AND）：
      - 含「库存反向约束」或（「库存」+「约束」）或（「选品」+「库存」+「约束/反向」）
      - 含「规则」或「来源」或「如何约束」等规则问法关键词
    不触发：普通补货查询、SKU 查询、刷新请求等。
    """
    q = question or ""
    has_inv_constraint = (
        "库存反向约束" in q
        or ("库存" in q and "约束" in q)
        or ("选品" in q and "库存" in q and ("约束" in q or "反向" in q))
    )
    if not has_inv_constraint:
        return False
    has_rule_intent = any(x in q for x in (
        "规则", "来源", "如何约束", "怎么约束", "如何受", "怎么受", "受库存",
        "约束规则", "约束来源", "规则来源",
    ))
    return has_rule_intent


def format_inventory_constraint_rule_reply() -> str:
    """库存反向约束选品规则答案（来源：n9_inventory_reverse_constraint.py）。

    核心口径：库存约束只作用于同款同 SKU，不强制跨款选品。
    数字来自 selection/l3_orchestration/nodes/n9_inventory_reverse_constraint.py 规则常量。
    """
    return (
        "**库存反向约束规则**（来源：`selection/l3_orchestration/nodes/"
        "n9_inventory_reverse_constraint.py`）\n\n"
        "**核心口径**：库存约束只作用于同款同 SKU，不强制跨款选品。\n\n"
        "当 20 寸库存积压（总库存 ≥ 100 且 库存/30d销量 ≥ 20）时，规则生效：\n"
        "- **同款同 SKU 降权** −0.25：普通 20 寸候选降权，避免继续放大同尺寸库存压力\n"
        "- **关联差异化候选加分**（可带走库存或差异化方向）：\n"
        "  · 24 寸及以上候选 +0.22\n"
        "  · 拓展层（expandable layer）候选 +0.18（嵌套可带走 20 寸库存）\n"
        "  · 套装候选 +0.16\n\n"
        "加分是得分倾向，不是「必须选 24 寸」的强制要求——仍需满足需求门、差异化等前序节点。\n"
        "规则常量：`BACKLOG_STOCK_THRESHOLD=100`，`BACKLOG_STOCK_TO_SALES_RATIO=20`。"
    )


def handle_inventory_constraint_rule_chat(question: str, provider_name: str) -> Optional[dict]:
    """chat() 确定性路由入口：T45 问题返回规则答案，其余返回 None（继续走常规路径）。"""
    if not is_inventory_constraint_rule_request(question):
        return None
    reply = format_inventory_constraint_rule_reply()
    return {
        "reply": reply, "clean_reply": reply, "references": [],
        "action_id": None, "tools_used": [], "tag": "查询",
        "workflow_task": None,
        "provider": provider_name, "confidence": 1.0,
        "judge_method": "deterministic_inventory_constraint_rule_router",
        "hallucination_warnings": None,
    }

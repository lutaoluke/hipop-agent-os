"""
WS-128 P0-S0: Chat boundary contract — 3 evidence paths.

三条热点路径的边界契约（不靠 SYSTEM_PROMPT，改写为可测代码）:

  QUERY           — 读工具 (query_sku / query_order / list_products 等) 返回真实数据
  WORKFLOW_TRIGGER — run_workflow 工具 → tasks 表落行 → task_id
  TASK_READBACK   — get_task_with_events(task_id) 从 tasks 表读真实状态

每条路径有且只有一种合法证据来源；chat() 的安全层验证 reply 和证据路径一致。
无真实证据时，任何声称数据已刷新 / 任务已完成 / 工作流已启动 的回复都是 bypass。

可在无 anthropic SDK 的 CI 环境中导入（仅标准库 + enum）。
"""
from __future__ import annotations

from enum import Enum
from typing import List


class EvidenceClass(Enum):
    QUERY = "query"                       # read-only data tool returned real rows
    WORKFLOW_TRIGGER = "workflow_trigger" # run_workflow → task row in tasks table
    TASK_READBACK = "task_readback"       # get_task_with_events read from tasks table
    NONE = "none"                         # no tool evidence — LLM reply is unsupported


# Read-only query tools: evidence comes from DB/API data, not LLM invention.
QUERY_TOOLS: frozenset = frozenset({
    "query_sku",
    "query_sku_live",
    "query_order",
    "query_order_live",
    "list_products",
    "scope_overview",
    "compute_replenishment",
    "data_health_check",
    "compute_air_freight_roi",
    "explain_status_enum",
    "query_1688_similar",
})

# Workflow trigger tools: evidence comes from tasks table row.
WORKFLOW_TOOLS: frozenset = frozenset({"run_workflow"})


def classify_evidence(tool_log: list) -> EvidenceClass:
    """Return the primary evidence class for a chat response.

    Priority: WORKFLOW_TRIGGER > QUERY > NONE.
    TASK_READBACK is set externally (by _workflow_receipt_reply after reading tasks table).
    """
    names = {t.get("name") for t in (tool_log or [])}
    if names & WORKFLOW_TOOLS:
        return EvidenceClass.WORKFLOW_TRIGGER
    if names & QUERY_TOOLS:
        return EvidenceClass.QUERY
    return EvidenceClass.NONE


def check_task_completion_bypass(reply: str, tool_log: list) -> List[str]:
    """Detect '已完成/已刷新' task completion claims without run_workflow evidence.

    This closes the gap not covered by the existing promise_workflow check in
    _safety.py, which only covers '已触发/已启动' style. Patterns like
    '数据已刷新/任务已跑完/已重算完成' are the bypass channel this gate closes.

    Returns a list of warning strings (empty = no bypass detected).
    """
    import re
    _BYPASS_RE = re.compile(
        r"(数据.{0,5}已刷新"
        r"|库存.{0,5}已刷新"
        r"|销量.{0,5}已刷新"
        r"|已刷新.{0,5}(完成|成功|好了)"
        r"|工作流.{0,10}已完成"
        r"|后台任务.{0,10}已完成"
        r"|任务已(跑完|完成|成功)"
        r"|已重算.{0,5}完(成)?"
        r"|已跑完"
        r"|已同步.{0,5}(完成|好了|成功))"
    )
    if not _BYPASS_RE.search(reply):
        return []
    if any(t.get("name") == "run_workflow" for t in (tool_log or [])):
        return []
    return [
        "⚠️ Agent 宣称任务/数据已完成或已刷新，但本轮没真调 run_workflow — "
        "这是 hallucinate（禁旁路生成已完成/已刷新声明，请重发刷新指令让系统真跑）"
    ]

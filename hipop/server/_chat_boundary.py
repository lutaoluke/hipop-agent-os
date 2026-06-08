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

import re
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

# Task-completion evidence tools: prove a task actually finished.
# run_workflow is NOT in this set — it only proves task creation, not completion.
TASK_DONE_TOOLS: frozenset = frozenset({
    "task_result",
    "task_status_readback",
    "check_workflow_status",
    "get_task_status",
})

_TASK_DONE_STATUSES: frozenset = frozenset({
    "done", "done_unverified", "success", "complete", "completed",
})

# Completion/refresh claim patterns that require task-done evidence.
# Covers 已刷新 and 已经刷新 (optional 经); both 已刷新 and 已更新.
_COMPLETION_BYPASS_RE = re.compile(
    r"(数据.{0,8}已经?(?:刷新|更新)"        # 数据已刷新/已更新/已经刷新/已经更新
    r"|库存.{0,8}已经?(?:刷新|更新)"        # 库存已刷新/已更新/已经刷新/已经更新
    r"|销量.{0,8}已经?(?:刷新|更新)"        # 销量已刷新/已更新
    r"|已经?(?:刷新|更新|同步).{0,8}(?:完成|成功|好了)"  # 已(经)刷新/更新/同步…完成
    r"|(?:刷新|更新|同步).{0,5}已完成"      # 刷新/更新/同步已完成
    r"|工作流.{0,10}已完成"
    r"|后台任务.{0,10}已完成"
    r"|任务已(?:跑完|完成|成功)"
    r"|已重算.{0,5}完(?:成)?"
    r"|已跑完"
    r")"
)


def classify_evidence(tool_log: list) -> EvidenceClass:
    """Return the primary evidence class for a chat response.

    Priority: TASK_READBACK > WORKFLOW_TRIGGER > QUERY > NONE.
    TASK_READBACK requires explicit done/success status in a task-done tool entry.
    """
    # Task completion evidence has highest specificity
    if _has_task_done_evidence(tool_log):
        return EvidenceClass.TASK_READBACK
    names = {t.get("name") for t in (tool_log or [])}
    if names & WORKFLOW_TOOLS:
        return EvidenceClass.WORKFLOW_TRIGGER
    if names & QUERY_TOOLS:
        return EvidenceClass.QUERY
    return EvidenceClass.NONE


def _has_task_done_evidence(tool_log: list) -> bool:
    """Return True if tool_log contains explicit task-completion evidence.

    Looks for task_result / task_status_readback / check_workflow_status /
    get_task_status entries whose result.status or top-level status is done/success.

    run_workflow alone is NOT sufficient — it only proves task creation/trigger.
    """
    for t in (tool_log or []):
        if t.get("name") not in TASK_DONE_TOOLS:
            continue
        result = t.get("result") or {}
        status = (result.get("status") or t.get("status") or "").lower()
        if status in _TASK_DONE_STATUSES:
            return True
    return False


def check_task_completion_bypass(reply: str, tool_log: list) -> List[str]:
    """Two-phase gate: block '已完成/已刷新' claims without task-done evidence.

    Phase 1 — no run_workflow at all → hallucinate banner (no task was even triggered).
    Phase 2 — run_workflow present but no task-done evidence → "已触发 ≠ 已完成" banner.
               run_workflow only proves the task was CREATED/TRIGGERED, not that it
               finished or that data was refreshed.
    Allowed  — task_result / task_status_readback with done/success status present,
               OR the reply contains no completion/refresh claims.

    This closes the red-team gap found in PR #74 round-1: the previous version
    exempted any reply that had run_workflow in tool_log, which meant a reply like
    '数据已刷新完成，任务已完成' would pass with only run_workflow evidence.
    """
    if not _COMPLETION_BYPASS_RE.search(reply):
        return []

    # Explicit task-done evidence → allowed
    if _has_task_done_evidence(tool_log):
        return []

    has_run_workflow = any(t.get("name") == "run_workflow" for t in (tool_log or []))
    if has_run_workflow:
        return [
            "⚠️ Agent 宣称任务/数据已完成或已刷新，但只有 run_workflow（任务创建）证据，"
            "没有任务完成（task_result/status=done）的工具证据 — "
            "已触发 ≠ 已完成，禁旁路生成已完成/已刷新声明"
        ]
    return [
        "⚠️ Agent 宣称任务/数据已完成或已刷新，但本轮没真调 run_workflow — "
        "这是 hallucinate（禁旁路生成已完成/已刷新声明，请重发刷新指令让系统真跑）"
    ]

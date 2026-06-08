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
    "get_task_with_events",
    "api_task_status",
})

_TASK_DONE_STATUSES: frozenset = frozenset({
    "done", "done_unverified", "success", "complete", "completed",
})

# Completion/refresh claim patterns that require task-done evidence.
# Covers 已刷新/已经刷新/已更新; colloquial 完成了/结束了/完毕/处理完 variants.
_RESULT_TAIL_RE = (
    r"(?:完成了?|成功了?|好了|完(?:了)?|完毕|结束了|弄好了|搞定了|"
    r"搞好了|处理好了|处理完(?:了)?|做好了|做完(?:了)?|跑好了|"
    r"跑完(?:了)?|到最新了?|行了)"
)
_TASK_RESULT_TAIL_RE = (
    r"(?:已?(?:跑完|完成|成功|结束)(?:了)?|跑好了|跑完(?:了)?|处理好了|"
    r"处理完(?:了)?|弄好了|搞定了|搞好了|做好了|做完(?:了)?|"
    r"完(?:了)?|完毕|结束了|行了)"
)
_COMPLETION_BYPASS_RE = re.compile(
    r"(数据.{0,8}已经?(?:刷新|更新)"        # 数据已刷新/已更新/已经刷新/已经更新
    r"|库存.{0,8}已经?(?:刷新|更新)"        # 库存已刷新/已更新/已经刷新/已经更新
    r"|销量.{0,8}已经?(?:刷新|更新)"        # 销量已刷新/已更新
    r"|已经?(?:刷新|更新|同步).{0,8}(?:完成|成功|好了|完了)"  # 已(经)刷新/更新/同步…完成/好了
    r"|(?:刷新|更新|同步).{0,5}(?:已完成|好了|完了|弄好了|搞定了)"  # 刷新/更新/同步已完成/好了
    r"|(?:数据|库存|销量).{0,10}(?:刷|刷新|更新|同步).{0,5}(?:好了|完了|弄好了|搞定了)"  # 数据刷新/刷好了
    rf"|(?:数据|库存|销量).{{0,10}}(?:刷|刷新|更新|同步|重算|处理).{{0,8}}{_RESULT_TAIL_RE}"
    rf"|(?:刷|刷新|更新|同步|重算|处理).{{0,8}}{_RESULT_TAIL_RE}"
    rf"|(?:任务|后台任务|工作流).{{0,10}}{_TASK_RESULT_TAIL_RE}"
    r"|(?:处理好了|处理完(?:了)?|搞定了)"  # bare "处理好了/处理完/搞定了" is still a completion claim
    r"|工作流.{0,10}已完成"
    r"|后台流程.{0,10}已经?(?:结束|完成)"
    r"|后台任务.{0,10}已完成"
    r"|任务已(?:跑完|完成|成功)"
    r"|(?:数据|库存|销量).{0,10}已(?:重新)?(?:计算|重算)"
    r"|已(?:导入|同步|更新).{0,8}最新(?:数据|库存|销量)"
    r"|最新(?:数据|库存|销量).{0,8}已(?:导入|同步|更新)"
    r"|已重算.{0,5}完(?:成)?"
    r"|已跑完"
    r")"
)

# Short completion claims that are too generic to put in the broad regex above.
# These still need task-done evidence when no tool backs them, when the only
# action evidence is run_workflow (task creation), or when query evidence would
# otherwise wash out a bare "done/update/sync" claim.
_SHORT_STATUS_BYPASS_RE = re.compile(
    r"(?:"
    r"操作.{0,4}(?:完毕|完(?:了)?|已完成|完成了?|成功了?)"
    r"|已(?:完成处理|刷新|完成|更新|同步)(?=$|[。！？!?,，、\s])"
    r"|完成了(?=$|[。！？!?,，、\s])"
    r"|已经(?:完成|更新|刷新)(?=$|[。！？!?,，、\s])"
    r"|(?:刷新|更新|同步|处理)了(?=$|[。！？!?,，、\s])"
    r"|完成(?=$|[。！？!?,，、\s])"
    r"|已经?处理(?=$|[。！？!?,，、\s])"
    r"|已经同步(?=$|[。！？!?,，、\s])"
    r")"
)

# Query evidence can support a generic status judgment, but not a bare
# completion/update/sync claim.
_QUERY_SAFE_SHORT_STATUS_RE = re.compile(r"一切正常")


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


def _iter_status_values(entry: dict):
    """Yield explicit status/state values from supported task readback payloads."""
    candidates = [
        entry.get("status"),
        entry.get("state"),
    ]

    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    candidates.extend([result.get("status"), result.get("state")])

    for source in (entry, result):
        task = source.get("task") if isinstance(source.get("task"), dict) else {}
        candidates.extend([task.get("status"), task.get("state")])
        events = source.get("events") if isinstance(source.get("events"), list) else []
        for event in events:
            if isinstance(event, dict):
                candidates.extend([event.get("status"), event.get("state")])

    for value in candidates:
        if value is None:
            continue
        text = str(value).strip().lower()
        if text:
            yield text


def _has_task_done_evidence(tool_log: list) -> bool:
    """Return True if tool_log contains explicit task-completion evidence.

    Looks for task_result / task_status_readback / check_workflow_status /
    get_task_status / get_task_with_events entries whose task state, event status,
    result.status, or top-level status is done/success.

    run_workflow alone is NOT sufficient — it only proves task creation/trigger.
    Provider summaries that only contain result_keys are also NOT sufficient.
    """
    for t in (tool_log or []):
        if t.get("name") not in TASK_DONE_TOOLS:
            continue
        if any(status in _TASK_DONE_STATUSES for status in _iter_status_values(t)):
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
    completion_claim = _COMPLETION_BYPASS_RE.search(reply)
    short_status_claim = _SHORT_STATUS_BYPASS_RE.search(reply)
    query_safe_status_claim = _QUERY_SAFE_SHORT_STATUS_RE.search(reply)
    if not completion_claim and not short_status_claim and not query_safe_status_claim:
        return []

    # Explicit task-done evidence → allowed
    if _has_task_done_evidence(tool_log):
        return []

    names = {t.get("name") for t in (tool_log or [])}
    has_run_workflow = bool(names & WORKFLOW_TOOLS)
    has_query_evidence = bool(names & QUERY_TOOLS)
    # "一切正常" can be a normal query-backed status judgment. Do not force task
    # readback unless the reply also contains a real completion/refresh claim,
    # a bare completion/update/sync claim, or the evidence is only a workflow
    # trigger.
    if query_safe_status_claim and not completion_claim and not short_status_claim and has_query_evidence and not has_run_workflow:
        return []

    if has_run_workflow:
        return [
            "⚠️ Agent 宣称任务/数据已完成、已刷新、已更新、已同步或已处理，但只有 run_workflow（任务创建）证据，"
            "没有任务完成（task_result/status=done）的工具证据 — "
            "已触发 ≠ 已完成，禁旁路生成已完成/已刷新/已更新/已同步/已处理声明"
        ]
    return [
        "⚠️ Agent 宣称任务/数据已完成、已刷新、已更新、已同步或已处理，但本轮没真调 run_workflow — "
        "这是 hallucinate（禁旁路生成已完成/已刷新/已更新/已同步/已处理声明，请重发刷新指令让系统真跑）"
    ]

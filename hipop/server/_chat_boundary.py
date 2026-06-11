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

# Structural result/completion claims that require task-done evidence.
#
# This intentionally models sentence shape instead of action synonyms:
#   - result-bearing subjects (task/workflow/data/inventory/system/etc.)
#   - Chinese completion/result morphology ("已/已经 ...", "...完/好/完成/生效")
#   - narrow exclusions for trigger-only and query-only states.
#
# run_workflow may justify "已创建/已排队/已开始", but never "已完成/已生效".
_CLAUSE_SEPARATORS = "，。！？!?；;\n"
_CLAUSE_CHUNK = rf"[^{_CLAUSE_SEPARATORS}]{{0,24}}"
_CLAUSE_TOKEN = rf"[^{_CLAUSE_SEPARATORS}\s]{{1,18}}"
_CLAIM_BOUNDARY = rf"(?=$|[{_CLAUSE_SEPARATORS}\s,、])"
_RESULT_SUBJECT_RE = (
    r"(?:任务|后台任务|工作流|流程|后台流程|操作|系统|数据|库存|销量|报表|"
    r"最新数据|最新库存|最新销量)"
)
_ALREADY_RE = r"(?:已|已经)"
_NON_RESULT_ALREADY_RE = (
    r"(?:触发|启动|开始|排队|创建|提交|受理|待执行|进入|安排|"
    r"在|正在|查|查询|拉取|拉|看|为|给)"
)
_NON_RESULT_BARE_LE_RE = (
    r"(?:(?:[^\s，。！？!?；;\n]{0,4})(?:查|看|拉|知道|明白|了解|可以|辛苦|感谢|谢谢)|"
    r"触发|启动|开始|排队|创建|提交|受理|导出|生成|通知)"
)
_COMPLETION_STATE_RE = (
    r"(?:完成了?|成功(?:完成|了)?|好了?|完(?:了)?|完毕|结束了?|"
    r"收尾了?|收工了?|办妥了?|搞定了?|搞好了?|闭环了?|"
    r"跑通了?|走通了?|跑顺了?|到最新了?|生效了?|落地了?|"
    r"收口了?|封存了?)"
)

_STRUCTURAL_RESULT_CLAIM_RES = (
    # 数据已经推送到系统 / 库存已覆盖线上系统 / 系统已经生效
    re.compile(
        rf"(?P<claim>{_RESULT_SUBJECT_RE}{_CLAUSE_CHUNK}"
        rf"{_ALREADY_RE}(?!{_NON_RESULT_ALREADY_RE}){_CLAUSE_CHUNK})"
    ),
    # 已成功导入最新数据 / 已处理完库存
    re.compile(
        rf"(?P<claim>{_ALREADY_RE}(?!{_NON_RESULT_ALREADY_RE})"
        rf"{_CLAUSE_CHUNK}{_RESULT_SUBJECT_RE})"
    ),
    # 流程跑通了 / 数据刷新好了 / 操作完毕
    re.compile(
        rf"(?P<claim>{_RESULT_SUBJECT_RE}{_CLAUSE_CHUNK}{_COMPLETION_STATE_RE})"
    ),
    # 已跑完 / 已重算完 / 已生效
    re.compile(
        rf"(?P<claim>{_ALREADY_RE}(?!{_NON_RESULT_ALREADY_RE})"
        rf"{_CLAUSE_TOKEN}{_COMPLETION_STATE_RE}){_CLAIM_BOUNDARY}"
    ),
    # 处理好了 / 刷好了 / 完成了 / 流程落地了
    re.compile(
        rf"(?P<claim>{_CLAUSE_TOKEN}{_COMPLETION_STATE_RE}){_CLAIM_BOUNDARY}"
    ),
    # 任意短语 + 了：更新了 / 同步了 / 处理了 / 完成了。
    # Requires phrase-start boundary to avoid matching tails (e.g. 道了 from 知道了).
    # Cognitive/ack/courtesy verbs (知道/明白/了解/可以/辛苦/感谢/谢谢) are excluded
    # via _NON_RESULT_BARE_LE_RE — so 好的知道了/明白了/辛苦了 do not trigger.
    re.compile(
        rf"(?:^|(?<=[{_CLAUSE_SEPARATORS}\s]))"
        rf"(?P<claim>(?!{_NON_RESULT_BARE_LE_RE}){_CLAUSE_TOKEN}了)"
        rf"{_CLAIM_BOUNDARY}"
    ),
    # Bare short "已更新/已同步/已刷新/已处理/完成" style status claims.
    re.compile(
        rf"(?P<claim>{_ALREADY_RE}(?!{_NON_RESULT_ALREADY_RE})"
        rf"{_CLAUSE_TOKEN}){_CLAIM_BOUNDARY}"
        rf"|(?P<bare>完成){_CLAIM_BOUNDARY}"
    ),
)

# Query-action phrases are read-only claims, not task/data completion claims.
# They are excluded from this gate; other fake-query gates validate evidence.
_QUERY_ACTION_SAFE_RE = re.compile(
    r"(?P<claim>"
    r"已处理查询结果"
    r"|(?:数据|库存).{0,4}(?:已查到|查好了|看好了|拉好了)"
    r"|(?:我)?(?:查|看|拉).{0,4}了"
    r"|(?:查询|查).{0,6}(?:已完成|完成了?|完(?:了)?|好了|结束(?:了)?)"
    r")"
)

# Query evidence can support generic status judgments, but a safe status phrase
# only exempts its own span; it cannot wash out a later result claim.
_QUERY_STATUS_SAFE_RE = re.compile(r"(?P<claim>一切正常)")

# WS-146：客观时效事实（freshness fact），不是「本轮执行了某动作」的完成声明：
#   「数据已更新到 2026-06-09」「库存同步至 2026-05-31」「更新日期：2026-06-09」。
# 判别锚点 =（更新/刷新/同步/截至）+（到/至/于）+ **具体日期**，且不带完成态「了/完成/完毕/
# 生效」。这样既放过 freshness 客观陈述（熔断 round-1 误删的根因点），又不放过「数据更新到
# 最新了 / 库存同步到最新了」这类带「了」无具体日期的完成声明（仍被结构门拦）。
_FRESHNESS_DATE_RE = r"(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}[日号])"
_FRESHNESS_FACT_SAFE_RE = re.compile(
    r"(?P<claim>"
    + r"[^\s，。！？!?；;\n]{0,8}?"                       # 可选主语前缀（库存数据/最新销量…）
    + r"(?:已|已经)?(?:更新|刷新|同步|截至)"
    + r"[^，。！？!?；;\n]{0,4}?(?:到|至|于|为|是)?\s*" + _FRESHNESS_DATE_RE
    + r"|(?:更新|数据|库存|销量|订单)(?:日期|时间)[:：]?\s*" + _FRESHNESS_DATE_RE
    + r")"
)

# WS-146 B方案：fake-friend —— 完成/启动体黏在**非任务词**上（趋势词 / 非任务技术名词），不是
# 「本轮执行了后台任务」的完成声明（验门人 route-b 点名：完成度已开始改善 / 拉取中文字段已完成映射）。
# 用**闭集语义类**（趋势词闭集 + 非任务技术名词闭集，领域建模而非穷举措辞）做安全 span，避免真实
# 路径上这些句子被结构完成门误挂 banner。趋势词闭集与 _exec_slot_contract 的执行动作闭集互斥，
# 真实「数据已刷新/任务已完成」不在此集、仍被拦。
_TREND_WORDS = r"向好|好转|改善|回升|回暖|提升|走高|向上|提高|增长|下滑|走低|下降"
_NONTASK_TECH = r"映射|字段|结构|索引|分析|统计|建模|分类|聚类|解析|归一|画像"
_FAKE_FRIEND_SAFE_RE = re.compile(
    r"(?P<claim>"
    + r"(?:趋势|环比|同比|整体|持续|逐渐)?(?:" + _TREND_WORDS + r")"
    + r"|(?:已经?|刚刚?)?(?:开始|完成|完毕)(?:" + _TREND_WORDS + r"|" + _NONTASK_TECH + r")"
    + r")"
)


def _span_within(span: tuple, candidates: list) -> bool:
    start, end = span
    return any(start >= safe_start and end <= safe_end for safe_start, safe_end in candidates)


def _match_span(match) -> tuple:
    for name in ("claim", "bare"):
        try:
            span = match.span(name)
        except IndexError:
            continue
        if span != (-1, -1):
            return span
    return match.span()


def _query_safe_spans(reply: str, include_status: bool) -> list:
    spans = [_match_span(m) for m in _QUERY_ACTION_SAFE_RE.finditer(reply)]
    # WS-146：时效客观事实（更新/同步 到 <具体日期>）任何时候都豁免（不依赖查询证据）。
    spans.extend(_match_span(m) for m in _FRESHNESS_FACT_SAFE_RE.finditer(reply))
    # WS-146 B方案：fake-friend（完成/启动体黏在趋势词/非任务技术名词上）也豁免。
    spans.extend(_match_span(m) for m in _FAKE_FRIEND_SAFE_RE.finditer(reply))
    if include_status:
        spans.extend(_match_span(m) for m in _QUERY_STATUS_SAFE_RE.finditer(reply))
    return spans


def _has_structural_result_claim(reply: str, safe_spans: list) -> bool:
    """Return True when reply contains a non-query-safe result/completion claim."""
    for pattern in _STRUCTURAL_RESULT_CLAIM_RES:
        for match in pattern.finditer(reply):
            if not _span_within(_match_span(match), safe_spans):
                return True
    return False


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
    """Two-phase gate: block structural result claims without task-done evidence.

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
    names = {t.get("name") for t in (tool_log or [])}
    has_run_workflow = bool(names & WORKFLOW_TOOLS)
    has_query_evidence = bool(names & QUERY_TOOLS)

    query_action_claim = _QUERY_ACTION_SAFE_RE.search(reply)
    query_safe_status_claim = _QUERY_STATUS_SAFE_RE.search(reply)
    safe_spans = _query_safe_spans(reply, include_status=has_query_evidence)
    completion_claim = _has_structural_result_claim(reply, safe_spans)

    if not completion_claim and not query_safe_status_claim:
        return []
    if query_action_claim and not completion_claim and not query_safe_status_claim:
        return []

    # Explicit task-done evidence → allowed.
    if _has_task_done_evidence(tool_log):
        return []

    # "一切正常" / "查询完成" can be normal query-backed status judgments. A
    # query-safe phrase does not exempt a separate result claim in the same reply
    # because _has_structural_result_claim only ignores the safe phrase's span.
    if (
        query_safe_status_claim
        and not completion_claim
        and has_query_evidence
        and not has_run_workflow
    ):
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

"""统一证据 / 执行记录契约（WS-144/E1.1）— 工作台数字与执行的可追溯承重墙。

建立在事实源契约（WS-129, fact_source_contract.py）之上，把两类东西统一成
**可追溯、可校验、缺则 fail-closed** 的结构：

  1. 查询证据 Evidence —— 运营看到的每个业务数字必须带三要素：
       来源(source) / 取数时间(fetched_at) / 覆盖口径(coverage)。
     缺任一 → ContractViolation，回答层不得出数（防"占位假数据"+"无来源裸数"）。

  2. 执行记录 ExecutionRecord —— 运营看到的每次刷新/重算必须有真实任务记录：
       task_id(真实) / workflow / 步骤数(≥1) / 终态或运行态 + 失败原因。
     不得只说"已启动"而没有可查的 task_id 与步骤（防"接线缺失"+"假完成"）。

设计原则（呼应三种死法）：
  - 接线缺失：消费端（回答层 formatter / hint）必须真的调用本契约的 assert_* /
    render_*，不许旁路旧字段直接渲染。smoke 钉死消费端真读证据。
  - 死代码短路：新契约与旧自由出数不得并存——migrate 的样板工具，回答层在出数前
    强制过 assert_query_evidence，无证据直接 fail-closed。
  - 占位假数据：source/fetched_at/coverage 三要素任一为空/写死占位 → 立即 raise，
    不允许空字段冒充实时事实。

本模块**纯函数、无 DB、无 LLM**，可被 tool / formatter / verifier / smoke 直接调用。
确定性规则只落在这里，**禁止**塞进 agent SYSTEM_PROMPT。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 复用事实源契约的权威来源枚举与异常——只有一套真相，不在此另定。
from .fact_source_contract import (  # noqa: F401  (re-export for consumers)
    SOURCE_NOON,
    SOURCE_ERP,
    ContractViolation,
)

# ── 证据级来源枚举 ───────────────────────────────────────────────────────────────
# 在事实源契约的 noon/erp 之上，额外允许：
#   - "cache"：带时间戳的缓存（仍须 fetched_at，且消费端按时效门判定是否可用）
#   - "merged"：跨源聚合数（如 total_stock = noon 官方仓 + ERP 各仓 + pending），
#               其 coverage 必须显式列出构成的各源，不得用 merged 掩盖口径。
SOURCE_CACHE = "cache"
SOURCE_MERGED = "merged"

_VALID_SOURCES = (SOURCE_NOON, SOURCE_ERP, SOURCE_CACHE, SOURCE_MERGED)

# 证据对象在 tool_result 里的标准键名（消费端按此键读取）。
EVIDENCE_KEY = "evidence"
# 执行记录在 tool_result 里的标准键名。
EXECUTION_KEY = "execution_record"

# 执行记录的合法状态机。
EXEC_QUEUED = "queued"
EXEC_RUNNING = "running"
EXEC_DONE = "done"
EXEC_ERROR = "error"
EXEC_CREATE_FAILED = "create_failed"

_EXEC_TERMINAL = (EXEC_DONE, EXEC_ERROR, EXEC_CREATE_FAILED)
_EXEC_LIVE = (EXEC_QUEUED, EXEC_RUNNING)
_EXEC_ALL = _EXEC_LIVE + _EXEC_TERMINAL
# 失败态必须带 reason（不许静默失败、不许只报状态没原因）。
_EXEC_NEEDS_REASON = (EXEC_ERROR, EXEC_CREATE_FAILED)
# 这些态必须有真实 task_id + 至少 1 个步骤（真任务记录）。
_EXEC_NEEDS_REAL_TASK = (EXEC_QUEUED, EXEC_RUNNING, EXEC_DONE, EXEC_ERROR)


# ════════════════════════════════════════════════════════════════════════════════
# 1) 查询证据 Evidence
# ════════════════════════════════════════════════════════════════════════════════

def build_query_evidence(
    *,
    source: str,
    fetched_at: Any,
    coverage: str,
    sub_sources: Optional[List[str]] = None,
    context: str = "",
) -> Dict[str, Any]:
    """构造一个**完整**的查询证据对象。三要素任一缺失/占位即 raise ContractViolation。

    参数:
        source:     权威来源标签，必须是 noon/erp/cache/merged 之一。
        fetched_at: 取数时间（字符串或可 str 化的时间），不得为空。
        coverage:   覆盖口径的人话描述（如"KSA total_stock=noon官方仓+海外+国内+pending；
                    Top10"），不得为空。merged 来源必须在 coverage 或 sub_sources 里
                    列出构成的各源，否则视为口径不明。
        sub_sources: merged 时构成的各权威源列表（如 [SOURCE_NOON, SOURCE_ERP]）。
        context:    出错信息前缀。

    返回:
        {"source":..., "fetched_at":..., "coverage":..., "sub_sources":[...]}
    """
    prefix = f"[{context}] " if context else ""

    if not source or source not in _VALID_SOURCES:
        raise ContractViolation(
            f"{prefix}证据缺少/非法来源 source={source!r}——"
            f"必须是 {_VALID_SOURCES} 之一（WS-144 证据契约）。"
        )
    ts = "" if fetched_at is None else str(fetched_at).strip()
    if not ts:
        raise ContractViolation(
            f"{prefix}证据缺少取数时间 fetched_at——"
            f"无时间的数字不得作为事实（WS-144 证据契约）。"
        )
    cov = (coverage or "").strip()
    if not cov:
        raise ContractViolation(
            f"{prefix}证据缺少覆盖口径 coverage——"
            f"运营必须知道这个数字覆盖了什么（WS-144 证据契约）。"
        )

    subs = list(sub_sources or [])
    if source == SOURCE_MERGED and not subs:
        raise ContractViolation(
            f"{prefix}merged 聚合数必须在 sub_sources 列出构成的各权威源，"
            f"不得用 merged 掩盖口径（WS-144 证据契约）。"
        )
    for s in subs:
        if s not in _VALID_SOURCES:
            raise ContractViolation(
                f"{prefix}sub_sources 含非法来源 {s!r}（WS-144 证据契约）。"
            )

    return {
        "source": source,
        "fetched_at": ts,
        "coverage": cov,
        "sub_sources": subs,
    }


def assert_query_evidence(evidence: Optional[Dict[str, Any]], context: str = "") -> Dict[str, Any]:
    """校验一个证据对象三要素完整；不完整即 raise。消费端出数前必须调它。

    回答层（formatter / hint）在渲染任何业务数字前调用本函数；它失败就 fail-closed
    不出数。这是"无证据不出数"的承重点。
    """
    prefix = f"[{context}] " if context else ""
    if not isinstance(evidence, dict):
        raise ContractViolation(
            f"{prefix}缺少证据对象（{EVIDENCE_KEY}）——无证据不出数（WS-144 证据契约）。"
        )
    # 复用 build 的同一套校验，避免两套口径漂移。
    return build_query_evidence(
        source=evidence.get("source"),
        fetched_at=evidence.get("fetched_at"),
        coverage=evidence.get("coverage"),
        sub_sources=evidence.get("sub_sources"),
        context=context,
    )


_SOURCE_LABEL = {
    SOURCE_NOON: "noon 官网实时",
    SOURCE_ERP: "ERP",
    SOURCE_CACHE: "带时间缓存",
    SOURCE_MERGED: "多源聚合",
}


def render_evidence_suffix(evidence: Dict[str, Any]) -> str:
    """把证据对象渲染成确定性的一行可读后缀（来源/取数时间/口径）。

    回答层把这行附在数字回答末尾，运营据此可追溯。**先校验后渲染**——证据不全
    直接抛，绝不渲染半截证据冒充完整。
    """
    ev = assert_query_evidence(evidence, context="render")
    label = _SOURCE_LABEL.get(ev["source"], ev["source"])
    if ev["source"] == SOURCE_MERGED and ev["sub_sources"]:
        sub = "+".join(_SOURCE_LABEL.get(s, s) for s in ev["sub_sources"])
        label = f"{label}（{sub}）"
    return f"（来源：{label}｜取数时间：{ev['fetched_at']}｜口径：{ev['coverage']}）"


# ════════════════════════════════════════════════════════════════════════════════
# 2) 执行记录 ExecutionRecord
# ════════════════════════════════════════════════════════════════════════════════

def build_execution_record(
    *,
    status: str,
    task_id: Optional[str] = None,
    workflow: str = "",
    steps: Optional[List[Dict[str, Any]]] = None,
    step_count: Optional[int] = None,
    reason: str = "",
    context: str = "",
) -> Dict[str, Any]:
    """构造一个**可信**的执行记录。

    规则（fail-closed）：
      - status 必须是合法状态机之一。
      - queued/running/done/error 态：必须有真实 task_id（非空），且步骤数 ≥1
        （steps 列表或 step_count 任一证明任务真的产生了记录）。
      - error/create_failed 态：必须带 reason（失败必须有原因，不许静默）。
      - create_failed 态：允许无 task_id（任务压根没建起来），但必须有 reason。

    这堵死"只说已启动、没有可查的真实任务"——没有真任务记录的"已启动"会被拒。
    """
    prefix = f"[{context}] " if context else ""
    if status not in _EXEC_ALL:
        raise ContractViolation(
            f"{prefix}执行记录状态非法 status={status!r}——"
            f"必须是 {_EXEC_ALL} 之一（WS-144 执行契约）。"
        )

    tid = (str(task_id).strip() if task_id is not None else "")
    n_steps = int(step_count) if step_count is not None else len(steps or [])

    if status in _EXEC_NEEDS_REAL_TASK:
        if not tid:
            raise ContractViolation(
                f"{prefix}status={status} 必须有真实 task_id——"
                f"没有可查的任务记录不得当成已执行（WS-144 执行契约）。"
            )
        if n_steps < 1:
            raise ContractViolation(
                f"{prefix}status={status} 必须有 ≥1 个步骤记录（step_count={n_steps}）——"
                f"只说'已启动'而无真实步骤即接线缺失（WS-144 执行契约）。"
            )

    rsn = (reason or "").strip()
    if status in _EXEC_NEEDS_REASON and not rsn:
        raise ContractViolation(
            f"{prefix}status={status} 必须带失败原因 reason——"
            f"失败不许静默（WS-144 执行契约）。"
        )

    return {
        "status": status,
        "task_id": tid or None,
        "workflow": workflow or None,
        "step_count": n_steps,
        "steps": list(steps or []),
        "reason": rsn or None,
    }


def assert_execution_real(record: Optional[Dict[str, Any]], context: str = "") -> Dict[str, Any]:
    """校验一个执行记录可信；不可信即 raise。消费端报告执行结果前必须调它。"""
    prefix = f"[{context}] " if context else ""
    if not isinstance(record, dict):
        raise ContractViolation(
            f"{prefix}缺少执行记录（{EXECUTION_KEY}）——"
            f"不得无记录就报'已执行/已启动'（WS-144 执行契约）。"
        )
    return build_execution_record(
        status=record.get("status"),
        task_id=record.get("task_id"),
        workflow=record.get("workflow") or "",
        steps=record.get("steps"),
        step_count=record.get("step_count"),
        reason=record.get("reason") or "",
        context=context,
    )


_EXEC_STATUS_LABEL = {
    EXEC_QUEUED: "已排队",
    EXEC_RUNNING: "执行中",
    EXEC_DONE: "已完成",
    EXEC_ERROR: "执行失败",
    EXEC_CREATE_FAILED: "创建失败",
}


def render_execution_suffix(record: Dict[str, Any]) -> str:
    """把执行记录渲染成确定性的一行可读结果。**绝不**输出裸"已启动"。

    成功创建：含真实 task_id + 步骤数 + 状态；失败：含状态 + 原因。
    """
    rec = assert_execution_real(record, context="render")
    status_label = _EXEC_STATUS_LABEL.get(rec["status"], rec["status"])
    wf = rec["workflow"] or "工作流"
    if rec["status"] == EXEC_CREATE_FAILED:
        return f"{wf} 任务{status_label}：{rec['reason']}（未产生任务记录）。"
    base = f"已创建任务 {rec['task_id']}（{wf}），状态：{status_label}，已记录步骤数：{rec['step_count']}"
    if rec["reason"]:
        base += f"，原因：{rec['reason']}"
    return base + "。"

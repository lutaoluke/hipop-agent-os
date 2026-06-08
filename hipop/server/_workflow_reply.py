"""_workflow_reply.py — 工作流回复三态化（T21-SUB-2, WS-100）

与 agent.py 分离，目的是让 smoke 测试在无 anthropic SDK 的 CI 环境中也能导入并验证：
  - _workflow_receipt_reply: 三态受理回执（已排队/已开始/已完成·失败）
"""
from __future__ import annotations

from typing import Optional

from . import data as _data


def _workflow_receipt_reply(task_id: str, workflow: str, label: str) -> str:
    """三态受理回执（T21-SUB-2）：基于 SUB-1 回读接口确定性映射任务状态。

    - 直接回答任务是否已创建（task row 存在 → 是；不存在 → 未确认）
    - 附 task_id / workflow / 当前状态（三态之一）
    - 无 done/error 事件时措辞为「已排队/待执行」，不得暗示已跑完
    """
    try:
        task_data = _data.get_task_with_events(task_id)
    except Exception:
        task_data = None

    if task_data is None:
        return (
            f"⚠️ 未确认{label}（{workflow}）的后台任务是否创建成功，"
            f"任务 ID={task_id} 在数据库中暂时查不到。"
            "请在工作台任务面板确认，或重试。"
        )

    task = task_data.get("task") or {}
    events = task_data.get("events") or []
    state = task.get("state") or "queued"
    event_statuses = {e.get("status") for e in events}

    # 三态映射（优先级：error > final done > running/started > queued）
    if "error" in event_statuses:
        state_label = "执行失败"
        note = "请查看工作台任务面板了解详情，或重试。"
    elif "done" in event_statuses and state in ("done", "done_unverified"):
        state_label = "已完成"
        note = "任务已完成，我会继续回答你的原问题。"
    elif state == "running" or "started" in event_statuses:
        state_label = "已开始执行"
        note = "完成后我会继续回答你的原问题。"
    else:
        state_label = "已排队/待执行"
        note = "完成后我会继续回答你的原问题。"

    return (
        f"已受理{label}（{workflow}），后台任务已创建。\n"
        f"任务 ID：{task_id}｜当前状态：{state_label}\n"
        f"{note}"
    )

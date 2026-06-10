"""smoke_t38_sales_cycle_recompute.py — T38 fail-then-pass smoke

WS-123: 修复 T38 — "重算销售周期和补货建议" 必须真正路由到 wf5_sales_cycle_v2,
不能落入 LLM 自由回复路径编造任务号/accepted/SSE 进度。

FAIL 条件（修前）：
  - _deterministic_workflow_request("请重算销售周期和补货建议，并返回任务进度证据") 返回 None
    → 意味着走到 LLM 路径，可能编假任务证据
  - "刷新补货建议" / "重新计算销售周期" 等宽口径同样无法路由到 wf5

PASS 条件（修后）：
  - 上述三类口语触发均路由到 wf5_sales_cycle_v2
  - wf3/wf1 原有路由不受影响（回归）
  - 无触发词时返回 None（不误触发）
  - _safety 仍然正确拦截 LLM 路径的假"已触发"宣称（回归）
  - 两态失败：tool 失败返回的回复不含假任务证据（task_id/accepted/SSE 词）
"""

import os
import sys
import re
import traceback
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server.agent import _deterministic_workflow_request
from hipop.server import _safety
from hipop.server import agent as _agent
from hipop.server import _provider as _prov


# ────────────────────────────────────────────────────────────────────────────
# T38 路由失败组（修前 FAIL，修后 PASS）
# ────────────────────────────────────────────────────────────────────────────

def test_t38_original_prompt_routes_to_wf5():
    """T38 原话"请重算销售周期和补货建议，并返回任务进度证据"必须路由到 wf5_sales_cycle_v2。

    FAIL (before fix): 返回 None，落入 LLM 自由回复路径。
    PASS (after fix):  返回 {"workflow": "wf5_sales_cycle_v2", ...}。
    """
    result = _deterministic_workflow_request(
        "请重算销售周期和补货建议，并返回任务进度证据"
    )
    assert result is not None, (
        "T38 原话未能路由到 wf5_sales_cycle_v2：返回 None"
    )
    assert result.get("workflow") == "wf5_sales_cycle_v2", (
        f"T38 路由到错误 workflow: {result.get('workflow')!r}，期望 wf5_sales_cycle_v2"
    )
    print(f"    T38 原话路由到 {result['workflow']} ({result.get('label')})")


def test_t38_refresh_replenishment_routes_to_wf5():
    """宽口径：'刷新补货建议' 必须路由到 wf5_sales_cycle_v2。

    FAIL (before fix): 返回 None。
    PASS (after fix):  返回 wf5_sales_cycle_v2。
    """
    result = _deterministic_workflow_request("帮我刷新补货建议")
    assert result is not None, "'刷新补货建议' 未路由，返回 None"
    assert result.get("workflow") == "wf5_sales_cycle_v2", (
        f"路由到错误 workflow: {result.get('workflow')!r}"
    )
    print(f"    '刷新补货建议' 路由到 {result['workflow']}")


def test_t38_recalc_sales_cycle_routes_to_wf5():
    """宽口径：'重新计算销售周期' 必须路由到 wf5_sales_cycle_v2。

    FAIL (before fix): "重新计算" 不在触发词列表，返回 None。
    PASS (after fix):  返回 wf5_sales_cycle_v2。
    """
    result = _deterministic_workflow_request("重新计算销售周期")
    assert result is not None, "'重新计算销售周期' 未路由，返回 None"
    assert result.get("workflow") == "wf5_sales_cycle_v2", (
        f"路由到错误 workflow: {result.get('workflow')!r}"
    )
    print(f"    '重新计算销售周期' 路由到 {result['workflow']}")


def test_t38_rerun_replenishment_routes_to_wf5():
    """宽口径：'重跑补货建议' 必须路由到 wf5_sales_cycle_v2。

    FAIL (before fix): "重跑" 不在触发词列表，返回 None。
    PASS (after fix):  返回 wf5_sales_cycle_v2。
    """
    result = _deterministic_workflow_request("重跑补货建议")
    assert result is not None, "'重跑补货建议' 未路由，返回 None"
    assert result.get("workflow") == "wf5_sales_cycle_v2", (
        f"路由到错误 workflow: {result.get('workflow')!r}"
    )
    print(f"    '重跑补货建议' 路由到 {result['workflow']}")


# ────────────────────────────────────────────────────────────────────────────
# 回归组（修前修后均应 PASS）
# ────────────────────────────────────────────────────────────────────────────

def test_t38_logistics_still_routes_to_wf3():
    """回归：'刷新物流' 仍然路由到 wf3_logistics_v2，不被 wf5 误抢。"""
    result = _deterministic_workflow_request("帮我刷新物流")
    assert result is not None, "'刷新物流' 返回 None（回归失败）"
    assert result.get("workflow") == "wf3_logistics_v2", (
        f"物流路由被改变: {result.get('workflow')!r}"
    )
    print(f"    物流路由正常: {result['workflow']}")


def test_t38_stock_still_routes_to_wf1():
    """回归：'刷新库存' 仍然路由到 wf1_stock_v2，不被 wf5 误抢。"""
    result = _deterministic_workflow_request("帮我刷新库存")
    assert result is not None, "'刷新库存' 返回 None（回归失败）"
    assert result.get("workflow") == "wf1_stock_v2", (
        f"库存路由被改变: {result.get('workflow')!r}"
    )
    print(f"    库存路由正常: {result['workflow']}")


def test_t38_no_trigger_word_returns_none():
    """无触发词时不得误触发：'销售周期分析' 返回 None。"""
    result = _deterministic_workflow_request("给我看一下销售周期分析")
    assert result is None, (
        f"无触发词的问题误触发了路由: {result!r}"
    )
    print("    无触发词正确返回 None")


def test_t38_negation_guard_still_works():
    """'不用刷新' 否定词保护仍有效。"""
    result = _deterministic_workflow_request("不用刷新补货建议了")
    assert result is None, (
        f"否定词保护失效，误触发了路由: {result!r}"
    )
    print("    否定词保护正常")


# ────────────────────────────────────────────────────────────────────────────
# Safety 组（回归：假启动宣称仍被拦截）
# ────────────────────────────────────────────────────────────────────────────

def test_t38_safety_fake_triggered_caught():
    """_safety 必须拦截：回复宣称'已触发/已启动工作流'但本轮没调 run_workflow。

    这是 T36/T38 共用的假启动守门（回归检查）。
    """
    _, warns = _safety.sanitize_reply(
        "销售周期重算任务已触发，任务 ID 为 38377c42，当前状态 accepted，"
        "预计 30 分钟后完成。",
        tools_used=[],   # 没有调任何工具
        tool_log=[],
    )
    assert any("触发" in w or "run_workflow" in w or "hallucinate" in w for w in warns), (
        f"假'已触发'宣称未被 _safety 拦截: {warns}"
    )
    print(f"    假触发宣称已被拦截: {warns[0][:60]}…")


def test_t38_safety_fake_task_id_caught():
    """_safety 必须拦截：回复中出现 8 位十六进制任务号但无 run_workflow 工具调用。"""
    _, warns = _safety.sanitize_reply(
        "你的补货重算任务号是 38377c42，状态为 accepted。",
        tools_used=[],
        tool_log=[],
    )
    assert any("38377c42" in w or "task_id" in w or "编造" in w for w in warns), (
        f"假 task_id 未被 _safety 拦截: {warns}"
    )
    print(f"    假 task_id 已被拦截: {warns[0][:60]}…")


def test_t38_real_run_workflow_task_id_passes_safety():
    """_safety 放行：run_workflow 工具确实返回了 task_id，回复中引用它是合法的。"""
    fake_task_id = "ab123456"
    tool_log = [{"name": "run_workflow", "task_id": fake_task_id}]
    _, warns = _safety.sanitize_reply(
        f"销售周期重算任务已创建，任务号 {fake_task_id}，当前状态已排队。",
        tools_used=["run_workflow"],
        tool_log=tool_log,
    )
    fake_id_warns = [w for w in warns if fake_task_id in w or "编造" in w or "task_id" in w]
    assert not fake_id_warns, (
        f"合法 run_workflow task_id 被误报为假: {fake_id_warns}"
    )
    print("    run_workflow 真实 task_id 被正确放行")


def test_t38_safety_uppercase_task_id_caught():
    """验门人 round-2 gap: 大写 task_id 38377C42 必须被 _safety 拦截（修前漏检）。

    FAIL (before fix): _TASK_ID_MENTION_RE 只匹配 [0-9a-f]，38377C42 逃过检测。
    PASS (after fix):  大小写归一后与 real_ids 比对，38377C42 → 38377c42 → 无 real_ids → 拦截。
    """
    _, warns = _safety.sanitize_reply(
        "销售周期重算任务号是 38377C42，当前状态 accepted。",
        tools_used=[],
        tool_log=[],
    )
    assert any("38377c42" in w.lower() or "task_id" in w or "编造" in w for w in warns), (
        f"大写 task_id 38377C42 未被 _safety 拦截: {warns}"
    )
    print(f"    大写 task_id 已被拦截: {warns[0][:60]}…")


def test_t38_safety_uppercase_real_task_id_passes():
    """大写真实 task_id（run_workflow 返回的）不应被误报为假。"""
    real_task_id_upper = "AB123456"
    real_task_id_lower = "ab123456"
    tool_log = [{"name": "run_workflow", "task_id": real_task_id_lower}]
    _, warns = _safety.sanitize_reply(
        f"销售周期重算任务已创建，任务号 {real_task_id_upper}，当前状态已排队。",
        tools_used=["run_workflow"],
        tool_log=tool_log,
    )
    fake_id_warns = [w for w in warns if "ab123456" in w.lower() or "编造" in w]
    assert not fake_id_warns, (
        f"大写真实 task_id 被误报为假: {fake_id_warns}"
    )
    print("    大写真实 task_id 正确放行（大小写归一）")


def test_t38_safety_accepted_status_caught():
    """验门人 round-2 gap: 'accepted' 状态单独出现且无 run_workflow → 必须拦截。

    FAIL (before fix): 'accepted' 未在 promise_workflow 或 _check_fake_task_ids 中捕获。
    PASS (after fix):  fake_task_evidence 正则捕获 '状态.*accepted'，无 run_workflow → 警告。
    """
    _, warns = _safety.sanitize_reply(
        "你的重算任务状态为 accepted，系统正在处理中。",
        tools_used=[],
        tool_log=[],
    )
    assert any("accepted" in w.lower() or "T38" in w or "假任务" in w for w in warns), (
        f"'accepted' 状态未被 _safety 拦截: {warns}"
    )
    print(f"    'accepted' 状态已被拦截: {warns[0][:60]}…")


def test_t38_safety_current_status_accepted_caught():
    """验门人 round-3 gap: '当前状态 accepted' 自然说法也必须被拦截。

    FAIL (before fix): 只匹配 '状态为 accepted' / '状态: accepted'，中间无连接词时漏检。
    PASS (after fix):  状态/status 与 accepted 之间允许少量自然语言间隔。
    """
    _, warns = _safety.sanitize_reply(
        "当前状态 accepted，系统正在处理中。",
        tools_used=[],
        tool_log=[],
    )
    assert any("accepted" in w.lower() or "T38" in w or "假任务" in w for w in warns), (
        f"'当前状态 accepted' 未被 _safety 拦截: {warns}"
    )
    print(f"    '当前状态 accepted' 已被拦截: {warns[0][:60]}…")


def test_t38_safety_sse_progress_caught():
    """验门人 round-2 gap: SSE 进度声明且无 run_workflow → 必须拦截。

    FAIL (before fix): SSE 推送进度未在任何检测规则中捕获。
    PASS (after fix):  fake_task_evidence 正则捕获 'SSE.*进度'，无 run_workflow → 警告。
    """
    _, warns = _safety.sanitize_reply(
        "任务已创建，前端将通过 SSE 推送进度，完成后自动刷新。",
        tools_used=[],
        tool_log=[],
    )
    assert any("SSE" in w or "T38" in w or "假任务" in w for w in warns), (
        f"SSE 进度声明未被 _safety 拦截: {warns}"
    )
    print(f"    SSE 进度已被拦截: {warns[0][:60]}…")


def test_t38_safety_sse_will_push_progress_caught():
    """验门人 round-3 gap: 'SSE 将推送进度' 自然说法也必须被拦截。

    FAIL (before fix): 只匹配 'SSE推送' / 'SSE进度' 紧邻词，'将' 插入后漏检。
    PASS (after fix):  SSE 与进度/推送/订阅之间允许少量自然语言间隔。
    """
    _, warns = _safety.sanitize_reply(
        "SSE 将推送进度，完成后自动刷新。",
        tools_used=[],
        tool_log=[],
    )
    assert any("SSE" in w or "T38" in w or "假任务" in w for w in warns), (
        f"'SSE 将推送进度' 未被 _safety 拦截: {warns}"
    )
    print(f"    'SSE 将推送进度' 已被拦截: {warns[0][:60]}…")


def test_t38_safety_auto_callback_promise_caught():
    """验门人 round-4 gap: chat 不得承诺工作流跑完后自动回来通知/答复。

    FAIL (before fix): 有真实 run_workflow 时，"跑完后会自动回来告诉你" 不报警。
    PASS (after fix):  自动回报承诺独立报警；任务进度只能让用户看任务面板/重试。
    """
    _, warns = _safety.sanitize_reply(
        "已启动 wf6_alerts_v2，跑完后会自动回来告诉你结果。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow", "task_id": "ab123456"}],
    )
    assert any("自动" in w and ("回" in w or "通知" in w or "答复" in w) for w in warns), (
        f"自动回报承诺未被 _safety 拦截: {warns}"
    )
    print(f"    自动回报承诺已被拦截: {warns[0][:60]}…")


def test_t38_safety_accepted_real_workflow_passes():
    """'accepted' + 真实 run_workflow 不被误拦。"""
    _, warns = _safety.sanitize_reply(
        "已受理销售周期重算（wf5_sales_cycle_v2），任务 ID：ab123456，当前状态：已排队。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow", "task_id": "ab123456"}],
    )
    accepted_warns = [w for w in warns if "accepted" in w.lower() or "SSE" in w]
    assert not accepted_warns, (
        f"真实 run_workflow 后的回复被误拦: {accepted_warns}"
    )
    print("    真实 run_workflow 回复放行（无误报）")


# ────────────────────────────────────────────────────────────────────────────
# 两态失败表达测试（创建前失败不能给 task_id）
# ────────────────────────────────────────────────────────────────────────────

def test_t38_failure_reply_has_no_fake_task_id():
    """两态失败：task 创建失败时的回复不能含假 task_id 或 'accepted'。

    模拟 tool_run_workflow 返回 ok=False 时的 failure reply 路径：
    chat() 使用 `(tool_result or {}).get("error") or "工作流触发失败。"` 作为回复。
    该回复不应包含任何任务号或 accepted。
    """
    # 模拟 tool_run_workflow 失败返回
    failure_result = {"ok": False, "error": "本轮没有创建重算任务：wf5 正在运行中，请稍后重试。"}
    failure_reply = failure_result.get("message") or failure_result.get("error") or "工作流触发失败。"

    # 失败回复不得含假 task_id 或 accepted
    fake_id_re = re.compile(r'[0-9a-f]{8}\b')
    assert not fake_id_re.search(failure_reply), (
        f"失败回复中出现了疑似 task_id: {failure_reply!r}"
    )
    assert "accepted" not in failure_reply.lower(), (
        f"失败回复中出现了 accepted: {failure_reply!r}"
    )
    assert "已触发" not in failure_reply and "已启动" not in failure_reply, (
        f"失败回复中出现假启动宣称: {failure_reply!r}"
    )
    print(f"    两态失败回复无假证据: {failure_reply!r}")


# ────────────────────────────────────────────────────────────────────────────
# End-to-end: chat() → _exec_tool("run_workflow") → workflow_task 证据链
# ────────────────────────────────────────────────────────────────────────────

_FAKE_TASK_ID = "ab123456"
_SCOPE = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}


def _chat_with_fake_exec(question: str, exec_result: dict) -> dict:
    """Call chat() with _exec_tool mocked to return exec_result."""
    with patch.object(_agent, "_exec_tool", return_value=exec_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        return _agent.chat([{"role": "user", "content": question}], _SCOPE)


def test_t38_chat_e2e_workflow_task_has_real_task_id():
    """E2E: chat() with T38 trigger creates workflow_task with the run_workflow task_id.

    FAIL (before fix): workflow_task is None or task_id doesn't match — falls to LLM path.
    PASS (after fix):  workflow_task.task_id == the task_id returned by _exec_tool.
    """
    fake_ok = {
        "ok": True, "task_id": _FAKE_TASK_ID,
        "workflow": "wf5_sales_cycle_v2", "label": "销售周期与补货重算",
        "total_steps": 3, "affected_modules": ["sales_cycle", "replenishment"],
        "followup_prompt": "请重算销售周期和补货建议",
    }
    result = _chat_with_fake_exec("请重算销售周期和补货建议，并返回任务进度证据", fake_ok)
    # T36-S3: backend now returns workflow_tasks (list); backward-compat old dict
    wt_list = result.get("workflow_tasks") or []
    if not wt_list and result.get("workflow_task"):
        wt_list = [result.get("workflow_task")]
    wt = wt_list[0] if wt_list else None
    assert wt is not None, (
        f"workflow_task is None — T38 trigger did not create real task (result keys: {list(result)})"
    )
    assert wt["task_id"] == _FAKE_TASK_ID, (
        f"workflow_task.task_id {wt['task_id']!r} != expected {_FAKE_TASK_ID!r}"
    )
    assert wt["workflow"] == "wf5_sales_cycle_v2", (
        f"workflow_task.workflow {wt['workflow']!r} != wf5_sales_cycle_v2"
    )
    assert "run_workflow" in result.get("tools_used", []), (
        f"run_workflow not in tools_used: {result.get('tools_used')}"
    )
    reply = result.get("reply", "")
    assert "accepted" not in reply.lower(), f"reply contains 'accepted': {reply!r}"
    print(f"    E2E OK: workflow_task.task_id={wt['task_id']}, workflow={wt['workflow']}")


def test_t38_chat_e2e_failure_has_no_fake_task_id():
    """E2E failure path: when run_workflow fails, chat() reply must NOT contain fake task_id.

    FAIL (before fix): could return a hallucinated task_id or 'accepted'.
    PASS (after fix):  workflow_task is None; reply contains error message, no fake evidence.
    """
    fake_fail = {
        "ok": False,
        "error": "wf5 正在运行中，请稍后重试。",
        "message": "工作流 wf5_sales_cycle_v2 正在运行中，请稍后重试。",
    }
    result = _chat_with_fake_exec("重算销售周期", fake_fail)
    wt = result.get("workflow_task")
    assert wt is None, f"workflow_task should be None on failure, got: {wt!r}"
    reply = result.get("reply", "")
    fake_id_re = re.compile(r'[0-9a-f]{8}\b')
    assert not fake_id_re.search(reply), f"failure reply contains fake task_id: {reply!r}"
    assert "accepted" not in reply.lower(), f"failure reply contains 'accepted': {reply!r}"
    assert "已触发" not in reply and "已启动" not in reply, (
        f"failure reply contains fake trigger claim: {reply!r}"
    )
    print(f"    E2E failure: no fake evidence in reply: {reply!r}")


def test_t38_chat_e2e_duplicate_running_returns_real_existing_task():
    """E2E duplicate path: governance denial with an existing task id is still real evidence.

    FAIL (before fix): chat() collapses the denial to "工作流触发失败。" and workflow_task=None.
    PASS (after fix):  workflow_task carries the existing running task_id/workflow, while reply says
    no duplicate task was created.
    """
    existing_task_id = "c37cf4e9"
    duplicate_denial = {
        "action_type": "denied",
        "tool": "run_workflow",
        "reason": f"该 workflow 已有运行中实例: ['{existing_task_id}']（防并发抢资源）",
        "proposal_id": "duplicate123",
    }
    result = _chat_with_fake_exec("重算销售周期", duplicate_denial)
    # T36-S3: backend now returns workflow_tasks (list); backward-compat old dict
    wt_list = result.get("workflow_tasks") or []
    if not wt_list and result.get("workflow_task"):
        wt_list = [result.get("workflow_task")]
    wt = wt_list[0] if wt_list else None
    assert wt is not None, "duplicate running path should return the existing real workflow_task"
    assert wt["task_id"] == existing_task_id, (
        f"workflow_task.task_id {wt['task_id']!r} != existing {existing_task_id!r}"
    )
    assert wt["workflow"] == "wf5_sales_cycle_v2", (
        f"workflow_task.workflow {wt['workflow']!r} != wf5_sales_cycle_v2"
    )
    reply = result.get("reply", "")
    assert "未新建重复任务" in reply, f"duplicate reply should be explicit, got: {reply!r}"
    assert "accepted" not in reply.lower(), f"duplicate reply contains accepted: {reply!r}"
    print(f"    E2E duplicate: existing workflow_task.task_id={wt['task_id']}")


def test_t38_alert_count_query_uses_scope_overview_not_workflow():
    """Round-5: '红色告警有几个' 是纯查询，必须只读 scope_overview，禁止 run_workflow。

    FAIL (before fix): 问题落到 LLM；红队 stub 模拟它选择 data_health_check + run_workflow，
    并承诺"跑完后会自动回来告诉你"。
    PASS (after fix):  chat() 在 provider 前确定性路由到 scope_overview，返回真实红色告警数。
    """
    def fake_exec(name: str, args: dict, user: dict = None):
        if name == "scope_overview":
            return {
                "store": args.get("store", "KSA"),
                "sku_count": 2091,
                "alerts_red": 2,
                "alerts_pending": 5,
                "references": [{"table": "wf6_logistics_alerts_v2", "where": "tenant_id=1 AND ops_status='待处理'"}],
            }
        return {"ok": True, "task_id": "badc0ffe", "workflow": args.get("workflow", name)}

    provider_bad = {
        "reply": "红色告警数据较旧，我已启动 wf6_alerts_v2，跑完后会自动回来告诉你。",
        "tool_log": [
            {"name": "data_health_check", "args": {"store": "KSA"}},
            {"name": "run_workflow", "args": {"workflow": "wf6_alerts_v2"}, "task_id": "badc0ffe"},
        ],
        "refs_collected": [],
        "workflow_task": {
            "task_id": "badc0ffe",
            "workflow": "wf6_alerts_v2",
            "label": "物流告警生成",
            "total_steps": 1,
            "affected_modules": ["logistics"],
            "followup_prompt": "红色告警有几个",
        },
    }

    with patch.object(_agent, "_exec_tool", side_effect=fake_exec), \
         patch.object(_prov, "chat_with_tools", return_value=provider_bad), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "红色告警有几个"}], _SCOPE)

    tools = result.get("tools_used") or []
    reply = result.get("reply") or ""
    assert tools == ["scope_overview"], f"纯告警数量查询必须只用 scope_overview，实际 tools={tools}"
    assert result.get("workflow_task") is None, f"纯查询不应创建 workflow_task: {result.get('workflow_task')!r}"
    assert "run_workflow" not in tools, f"纯查询不应触发 run_workflow: {tools}"
    assert re.search(r"\b2\b", reply), f"回复必须含真实红色告警数 2: {reply!r}"
    assert not re.search(r"自动.{0,8}(回来|通知|答复|回报)|跑完后.{0,12}(告诉|通知|答复)", reply), (
        f"回复不应承诺自动回报: {reply!r}"
    )
    print(f"    红色告警数量查询只读 scope_overview: {reply!r}")


# ────────────────────────────────────────────────────────────────────────────
# Safety 完成态假证据组（验门人 round-5 gaps）
# ────────────────────────────────────────────────────────────────────────────

def test_t38_safety_done_claim_without_run_workflow_caught():
    """T38 Gap-1: "已重新计算并跑完了" 无 run_workflow 证据 → sanitize_reply 必须产生 warning。

    FAIL (before fix): warnings == []，假完成声明未被拦截。
    PASS (after fix):  warnings 非空，含 T38 假完成证据提示。
    """
    _, warnings = _safety.sanitize_reply(
        "销售周期和补货建议已重新计算并跑完了。",
        tools_used=[],
        tool_log=[],
    )
    assert warnings, (
        "T38 Gap-1: '已重新计算并跑完了'(无 run_workflow) 未被 sanitize_reply 拦截，"
        f"warnings={warnings!r}"
    )
    assert any("T38" in w or "完成" in w or "run_workflow" in w for w in warnings), (
        f"warning 内容未提及 T38 假完成证据: {warnings!r}"
    )
    print(f"    Gap-1 已重新计算无 run_workflow 被拦截: {warnings[0]!r}")


def test_t38_safety_task_completed_with_run_workflow_only_caught():
    """T38 Gap-2: "任务已完成，任务 ID：ab123456" 仅有 run_workflow 创建证据（无 done 回读）
    → sanitize_reply 必须产生 warning。

    FAIL (before fix): warnings == []，LLM 编造的完成声明未被拦截。
    PASS (after fix):  warnings 非空，含 T38 假完成声明提示。

    原理：sanitize_reply() 仅在 LLM 路径调用；真实完成由 _workflow_receipt_reply()
    在确定性路径返回，不经过 sanitize_reply()。因此任何到达此处的"已完成"均是编造。
    """
    _, warnings = _safety.sanitize_reply(
        "任务已完成，任务 ID：ab123456。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow", "task_id": "ab123456"}],
    )
    assert warnings, (
        "T38 Gap-2: '任务已完成'(仅 run_workflow 创建，无 done 回读) 未被拦截，"
        f"warnings={warnings!r}"
    )
    assert any("T38" in w or "完成" in w or "回读" in w for w in warnings), (
        f"warning 内容未提及 T38 假完成声明: {warnings!r}"
    )
    print(f"    Gap-2 任务已完成仅创建证据被拦截: {warnings[0]!r}")


# ────────────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # T38 路由失败组（修前 FAIL，修后 PASS）
        test_t38_original_prompt_routes_to_wf5,
        test_t38_refresh_replenishment_routes_to_wf5,
        test_t38_recalc_sales_cycle_routes_to_wf5,
        test_t38_rerun_replenishment_routes_to_wf5,
        # 回归组（修前修后均 PASS）
        test_t38_logistics_still_routes_to_wf3,
        test_t38_stock_still_routes_to_wf1,
        test_t38_no_trigger_word_returns_none,
        test_t38_negation_guard_still_works,
        # Safety 基础组
        test_t38_safety_fake_triggered_caught,
        test_t38_safety_fake_task_id_caught,
        test_t38_real_run_workflow_task_id_passes_safety,
        # Safety 扩展组（验门人 round-2 gaps）
        test_t38_safety_uppercase_task_id_caught,
        test_t38_safety_uppercase_real_task_id_passes,
        test_t38_safety_accepted_status_caught,
        test_t38_safety_current_status_accepted_caught,
        test_t38_safety_sse_progress_caught,
        test_t38_safety_sse_will_push_progress_caught,
        test_t38_safety_auto_callback_promise_caught,
        test_t38_safety_accepted_real_workflow_passes,
        # 两态失败表达
        test_t38_failure_reply_has_no_fake_task_id,
        # E2E chat() → run_workflow 证据链
        test_t38_chat_e2e_workflow_task_has_real_task_id,
        test_t38_chat_e2e_failure_has_no_fake_task_id,
        test_t38_chat_e2e_duplicate_running_returns_real_existing_task,
        test_t38_alert_count_query_uses_scope_overview_not_workflow,
        # Safety 完成态假证据组（验门人 round-5 gaps）
        test_t38_safety_done_claim_without_run_workflow_caught,
        test_t38_safety_task_completed_with_run_workflow_only_caught,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

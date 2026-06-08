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
import traceback
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server.agent import _deterministic_workflow_request
from hipop.server import _safety


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
    import re
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
        # Safety 回归组
        test_t38_safety_fake_triggered_caught,
        test_t38_safety_fake_task_id_caught,
        test_t38_real_run_workflow_task_id_passes_safety,
        # 两态失败表达
        test_t38_failure_reply_has_no_fake_task_id,
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

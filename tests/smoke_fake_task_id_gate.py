"""smoke_fake_task_id_gate.py — T36-S2 fail-then-pass smoke

验收：_safety._check_fake_task_ids() 只信 run_workflow 工具返回的 task_id；
其他工具（query_sku_live 等）携带的 task_id 无法洗白虚假任务号声明。

FAIL（修前）：real_ids 收录所有 tool_log.task_id，query_sku 返回的 task_id
             也能通过校验 → 假任务号未被拦截。
PASS（修后）：只有 name=="run_workflow" 条目的 task_id 才算真实 → 假任务号被拦截。
"""

import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server._safety import _check_fake_task_ids, sanitize_reply


# ── 红队核心回归 ──────────────────────────────────────────────────────────────

def test_redteam_non_run_workflow_task_id_is_fake():
    """红队: tool_log 含 run_workflow(AAA) + query_sku(BBB)，reply 声称"任务 bbbbbbbb"。

    修前: BBB 被收入 real_ids → 不报警（假任务号通过）。
    修后: 只有 run_workflow 才信 → BBB 被判 fake → 报警拦截。
    """
    tool_log = [
        {"name": "run_workflow", "task_id": "aabbccdd"},
        {"name": "query_sku_live", "task_id": "11223344"},  # 非 run_workflow
    ]
    reply = "好的，任务 11223344 已经在后台跑了，稍后查看结果。"
    warns = _check_fake_task_ids(reply, tool_log)
    assert warns, (
        "红队失败：reply 声称'任务 11223344'但该 task_id 来自 query_sku_live "
        "而非 run_workflow，应被拦截为假任务号，但没有报警"
    )
    assert "11223344" in warns[0], f"报警内容未包含假 task_id: {warns}"


def test_redteam_run_workflow_task_id_is_real():
    """正路: reply 声称"任务 aabbccdd"，该 task_id 确实来自 run_workflow → 不报警。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabbccdd"},
        {"name": "query_sku_live", "task_id": "11223344"},
    ]
    reply = "好的，任务 aabbccdd 已提交，请等待结果。"
    warns = _check_fake_task_ids(reply, tool_log)
    assert not warns, f"误报：run_workflow 返回的真实 task_id 被误判为假: {warns}"


# ── 边界与基础合约 ─────────────────────────────────────────────────────────────

def test_no_task_id_in_reply_no_warning():
    """reply 不含任务号提及 → 不论 tool_log 如何都不报警。"""
    tool_log = [{"name": "query_sku_live", "task_id": "11223344"}]
    warns = _check_fake_task_ids("补货建议已生成，请查看。", tool_log)
    assert not warns, f"误报：reply 无任务号提及却报警: {warns}"


def test_empty_tool_log_triggers_warning():
    """tool_log 为空时，任何任务号声明都视为假。"""
    warns = _check_fake_task_ids("任务 aabbccdd 已经提交了。", [])
    assert warns, "tool_log 为空但 reply 声称任务号，应报警"
    assert "aabbccdd" in warns[0], f"报警未包含 task_id: {warns}"


def test_multiple_fake_ids_all_reported():
    """reply 同时声称两个假任务号 → 两个都报警。"""
    tool_log = [{"name": "run_workflow", "task_id": "aabbccdd"}]
    reply = "任务 11223344 和任务 55667788 都在跑。"
    warns = _check_fake_task_ids(reply, tool_log)
    assert warns, "两个假 task_id 应被拦截"
    assert "11223344" in warns[0] or "55667788" in warns[0], f"报警内容: {warns}"


def test_sanitize_reply_accepts_tool_log_kwarg():
    """sanitize_reply 必须接受 tool_log= 关键字参数（向后兼容，无 tool_log 时也能调）。"""
    # 无 tool_log（旧式调用）
    result, warns = sanitize_reply("正常回复", [])
    assert isinstance(warns, list)

    # 有 tool_log
    result2, warns2 = sanitize_reply("正常回复", [], tool_log=[])
    assert isinstance(warns2, list)


def test_sanitize_reply_propagates_fake_task_id_warning():
    """sanitize_reply 通过 tool_log 触发 _check_fake_task_ids banner。"""
    tool_log = [{"name": "query_sku_live", "task_id": "11223344"}]
    _, warns = sanitize_reply(
        "任务 11223344 已经提交了。",
        ["query_sku_live"],
        tool_log=tool_log,
    )
    assert any("11223344" in w for w in warns), (
        f"sanitize_reply 未把假 task_id 报警传递出来: {warns}"
    )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_fake_task_id_gate — T36-S2 _check_fake_task_ids 防伪")

    tests = [
        ("test_redteam_non_run_workflow_task_id_is_fake",
         test_redteam_non_run_workflow_task_id_is_fake),
        ("test_redteam_run_workflow_task_id_is_real",
         test_redteam_run_workflow_task_id_is_real),
        ("test_no_task_id_in_reply_no_warning",
         test_no_task_id_in_reply_no_warning),
        ("test_empty_tool_log_triggers_warning",
         test_empty_tool_log_triggers_warning),
        ("test_multiple_fake_ids_all_reported",
         test_multiple_fake_ids_all_reported),
        ("test_sanitize_reply_accepts_tool_log_kwarg",
         test_sanitize_reply_accepts_tool_log_kwarg),
        ("test_sanitize_reply_propagates_fake_task_id_warning",
         test_sanitize_reply_propagates_fake_task_id_warning),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_fake_task_id_gate all {len(tests)} passed")

"""smoke_inventory_refresh_confirm.py — WS-159 fail-then-pass smoke

库存刷新询问式确认门:问「能不能刷新库存?」→ 提议 + 反问;下一轮确认 → 只执行一次真实
wf1_stock_v2;取消/换题/模糊/无 pending 的裸「好」→ 不执行。高风险不入此门。

FAIL（修前）:
  - hipop/server/_inventory_refresh_gate 不存在 → import 失败
  - chat() 没有跨轮 pending 门:「能不能帮我刷新一下库存?」要么落 LLM 不可控，要么裸「好」
    无法解锁真实刷新，或「好」在无 pending 时被误执行。

PASS（修后）:
  - 轮1 询问 → 不创建任务、tools_used 不含 run_workflow、回复含可执行判断 + 反问确认。
  - 轮2 确认 → 只创建一次 wf1_stock_v2 真实任务，workflow_tasks[0].task_id 真实回填。
  - 取消 / 换题 / 无 pending 裸确认 → 不创建任务。
  - 缺信息（缺店铺范围）→ 不创建任务、说明缺口、不挂可执行 pending（回复无 marker）。
  - 高风险询问「能不能下采购单?」+「好」→ 不被此门解锁。
  - 回归 WS-145:肯定命令「帮我刷库存」仍直接走 wf1_stock_v2;否定/假设/影响面仍不执行。
"""

import os
import sys
import traceback
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server import _inventory_refresh_gate as ig
from hipop.server import agent as _agent
from hipop.server import _provider as _prov
from hipop.server.agent import _deterministic_workflow_request

_SCOPE = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}

_INQUIRY = "能不能帮我刷新一下库存？"
_FAKE_OK = {
    "ok": True, "task_id": "ab123456", "workflow": "wf1_stock_v2",
    "label": "库存刷新", "total_steps": 3, "affected_modules": ["stock"],
    "followup_prompt": _INQUIRY,
}

_BAD_EVIDENCE = ("已触发", "已启动", "已完成", "已刷新")


def _assert_no_task(result):
    assert not (result.get("workflow_tasks") or result.get("workflow_task")), result
    assert "run_workflow" not in (result.get("tools_used") or []), result


def _no_fake_evidence(reply):
    for b in _BAD_EVIDENCE:
        assert b not in reply, f"回复含假证据 {b!r}: {reply!r}"


# ── 1) 模块级结构判别 ───────────────────────────────────────────────────────

def test_inquiry_detection():
    for q in ("能不能帮我刷新一下库存？", "可不可以刷新库存吗？", "能否帮我刷新库存？"):
        assert ig.is_inventory_refresh_inquiry(q), q
    # 肯定命令不是询问
    assert not ig.is_inventory_refresh_inquiry("帮我刷库存")
    # 不涉及库存不是本门询问
    assert not ig.is_inventory_refresh_inquiry("能不能刷新物流？")


def test_confirmation_detection():
    for q in ("好", "可以", "确认", "刷新吧", "麻烦了", "好，麻烦了", "好的", "嗯可以"):
        assert ig.is_confirmation(q), q
    # 换题不是裸确认
    assert not ig.is_confirmation("好的去查一下物流告警")
    # 高风险夹带不是裸确认（防一句「好」解锁高风险）
    assert not ig.is_confirmation("好啊那帮我下采购单")
    # 询问句不是确认
    assert not ig.is_confirmation("可不可以刷新库存？")


def test_cancellation_detection():
    for q in ("不用了", "先别刷", "取消", "算了", "先不要"):
        assert ig.is_cancellation(q), q
    assert not ig.is_cancellation("好")


def test_replies_carry_no_fake_evidence():
    texts = [
        ig.proposal_reply("KSA"),
        ig.infeasible_reply("缺少店铺范围", "先选店铺"),
        ig.bare_confirm_no_pending_reply(),
        ig.cancelled_reply(),
        ig.pending_now_infeasible_reply("已有运行中任务", "等它跑完"),
    ]
    for t in texts:
        _no_fake_evidence(t)
    # 提议回复必含 marker；不可执行回复必不含 marker
    assert ig.PROPOSAL_MARKER in ig.proposal_reply("KSA")
    assert ig.PROPOSAL_MARKER not in ig.infeasible_reply("x", "y")


# ── 2) chat() 轮1：询问 → 提议，不落任务（验收 #1）────────────────────────────

def test_chat_turn1_inquiry_proposes_no_task():
    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": _INQUIRY}], _SCOPE)
    _assert_no_task(result)
    reply = result.get("reply") or ""
    assert ig.PROPOSAL_MARKER in reply, reply  # 反问确认
    assert "wf1_stock_v2" in reply or "库存" in reply, reply  # 说明范围
    _no_fake_evidence(reply)
    assert result.get("judge_method") == "inventory_refresh_proposed", result


# ── 3) chat() 两轮：确认 → 只执行一次真实 wf1_stock_v2（验收 #2）──────────────

def test_chat_turn2_confirm_executes_real_task():
    msgs = [
        {"role": "user", "content": _INQUIRY},
        {"role": "assistant", "content": ig.proposal_reply("KSA")},
        {"role": "user", "content": "好"},
    ]
    with patch.object(_agent, "_exec_tool", return_value=_FAKE_OK) as m, \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(msgs, _SCOPE)
    wt = result.get("workflow_tasks") or []
    assert wt and wt[0]["task_id"] == "ab123456", result
    assert wt[0]["workflow"] == "wf1_stock_v2", result
    assert "run_workflow" in (result.get("tools_used") or []), result
    # 只执行一次 run_workflow
    rw_calls = [c for c in m.call_args_list if c.args and c.args[0] == "run_workflow"]
    assert len(rw_calls) == 1, f"run_workflow 应只调一次: {m.call_args_list}"
    assert result.get("judge_method") == "inventory_refresh_confirm_consumed", result


# ── 4) 取消轮：有 pending 但取消 → 不执行（验收 #3）──────────────────────────

def test_chat_cancel_clears_pending_no_task():
    for cancel in ("不用了", "先别刷", "取消"):
        msgs = [
            {"role": "user", "content": _INQUIRY},
            {"role": "assistant", "content": ig.proposal_reply("KSA")},
            {"role": "user", "content": cancel},
        ]
        with patch.object(_agent, "_exec_tool") as m, \
             patch.object(_prov, "get_provider", return_value="smoke"):
            result = _agent.chat(msgs, _SCOPE)
        _assert_no_task(result)
        assert not any(
            c.args and c.args[0] == "run_workflow" for c in m.call_args_list
        ), f"{cancel}: 不应触发 run_workflow"


# ── 5) 无 pending 裸「好」→ 不执行，要求补充（验收 #4）───────────────────────

def test_chat_bare_confirm_no_pending_no_task():
    with patch.object(_agent, "_exec_tool") as m, \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "好"}], _SCOPE)
    _assert_no_task(result)
    assert not any(c.args and c.args[0] == "run_workflow" for c in m.call_args_list)
    assert result.get("judge_method") == "inventory_refresh_no_pending", result


# ── 6) 换题后 pending 失效：[询问]+[换题]+[好] → 不执行旧刷新（验收 #5）───────

def test_chat_topic_change_invalidates_pending():
    msgs = [
        {"role": "user", "content": _INQUIRY},
        {"role": "assistant", "content": ig.proposal_reply("KSA")},
        {"role": "user", "content": "先看下物流告警有几个"},
        {"role": "assistant", "content": "KSA 当前红色告警 3 个。"},
        {"role": "user", "content": "好"},
    ]
    with patch.object(_agent, "_exec_tool") as m, \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(msgs, _SCOPE)
    _assert_no_task(result)
    assert not any(
        c.args and c.args[0] == "run_workflow" for c in m.call_args_list
    ), "换题后裸「好」不应触发旧库存刷新"


# ── 7) 缺信息场景：缺店铺范围 → 不落任务、说明缺口、不挂 pending（验收 #6）────

def test_chat_infeasible_missing_store_no_pending():
    scope_no_store = {"tenant_id": 1, "current_user": "test", "current_role": "admin"}
    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": _INQUIRY}], scope_no_store)
    _assert_no_task(result)
    reply = result.get("reply") or ""
    assert "店铺" in reply, reply  # 说明缺口
    assert ig.PROPOSAL_MARKER not in reply, "不可执行不应挂可执行 pending（无 marker）"
    assert result.get("judge_method") == "inventory_refresh_infeasible", result
    # 下一轮裸「好」也不应执行（因为上一轮无 marker → 无 pending）
    msgs = [
        {"role": "user", "content": _INQUIRY},
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "好"},
    ]
    with patch.object(_agent, "_exec_tool") as m, \
         patch.object(_prov, "get_provider", return_value="smoke"):
        r2 = _agent.chat(msgs, scope_no_store)
    assert not any(c.args and c.args[0] == "run_workflow" for c in m.call_args_list)


# ── 8) 高风险询问不入此门：「能不能下采购单?」+「好」→ 不解锁（验收 #7）───────

def test_chat_high_risk_inquiry_not_unlocked_by_confirm():
    # 高风险询问本身不被本门当作库存刷新提议
    assert not ig.is_inventory_refresh_inquiry("能不能帮我下采购单？")
    msgs = [
        {"role": "user", "content": "能不能帮我下采购单？"},
        {"role": "assistant", "content": "这步属于高风险动作，必须先确认。" + ig.PROPOSAL_MARKER},
        {"role": "user", "content": "好"},
    ]
    with patch.object(_agent, "_exec_tool") as m, \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(msgs, _SCOPE)
    # 即便上一轮 assistant 文本里混入 marker，因上一轮 user 非「库存刷新询问」→ 无 pending
    assert not any(
        c.args and c.args[0] == "run_workflow" for c in m.call_args_list
    ), "高风险不应被一句「好」解锁刷新"


# ── 9) 回归 WS-145：肯定命令直走 wf1；否定/假设/影响面不执行 ──────────────────

def test_regression_affirmative_command_still_routes():
    r = _deterministic_workflow_request("帮我刷库存，ERP 6 仓")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", r


def test_regression_affirmative_command_chat_creates_task():
    with patch.object(_agent, "_exec_tool", return_value=_FAKE_OK), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存，ERP 6 仓"}], _SCOPE)
    wt = result.get("workflow_tasks") or []
    assert wt and wt[0]["task_id"] == "ab123456", result
    assert wt[0]["workflow"] == "wf1_stock_v2", result


def test_regression_non_executory_still_blocked():
    for q in ("不要刷新库存", "如果刷新库存会影响什么？", "刷新库存有什么影响"):
        assert _deterministic_workflow_request(q) is None, q


if __name__ == "__main__":
    tests = [
        test_inquiry_detection,
        test_confirmation_detection,
        test_cancellation_detection,
        test_replies_carry_no_fake_evidence,
        test_chat_turn1_inquiry_proposes_no_task,
        test_chat_turn2_confirm_executes_real_task,
        test_chat_cancel_clears_pending_no_task,
        test_chat_bare_confirm_no_pending_no_task,
        test_chat_topic_change_invalidates_pending,
        test_chat_infeasible_missing_store_no_pending,
        test_chat_high_risk_inquiry_not_unlocked_by_confirm,
        test_regression_affirmative_command_still_routes,
        test_regression_affirmative_command_chat_creates_task,
        test_regression_non_executory_still_blocked,
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

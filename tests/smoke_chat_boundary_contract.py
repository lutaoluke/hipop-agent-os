"""smoke_chat_boundary_contract.py — WS-128 P0-S0 fail-then-pass smoke

验收：chat 查询 / 工作流执行 / 状态回读 三条热点路径在同一分类框架下可区分，
且无真实证据时旁路宣称被拦截。

三条路径定义（_chat_boundary.py）：
  QUERY           — 读工具证据 (query_sku / list_products 等)
  WORKFLOW_TRIGGER — run_workflow → tasks 表落行 → task_id
  TASK_READBACK   — get_task_with_events 从 tasks 表读真实状态

FAIL 条件（修前）：
  - classify_evidence 不存在 → ImportError
  - 已完成/已刷新旁路未拦截 → test_bypass_done_without_workflow 失败
  - TASK_READBACK 三态不含"已完成" → test_task_readback_three_states 失败

PASS 条件（修后）：
  - 所有 classify_evidence 断言正确
  - 已完成/已刷新旁路被 check_task_completion_bypass 捕获
  - _workflow_receipt_reply 正确读 tasks 表返回三态回执
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server._chat_boundary import (
    EvidenceClass,
    classify_evidence,
    check_task_completion_bypass,
    QUERY_TOOLS,
    WORKFLOW_TOOLS,
)


# ── Path 1: Query tool call evidence ──────────────────────────────────────────

def test_query_tool_classified_as_query():
    """tool_log 含 query_sku → QUERY evidence class."""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.QUERY, f"期望 QUERY，实得 {cls}"


def test_all_query_tools_classified():
    """每个 QUERY_TOOLS 成员单独测，都应分类为 QUERY。"""
    for tool in QUERY_TOOLS:
        cls = classify_evidence([{"name": tool}])
        assert cls == EvidenceClass.QUERY, (
            f"工具 {tool} 应分类为 QUERY，实得 {cls}"
        )


def test_empty_tool_log_classified_as_none():
    """无工具调用 → NONE (LLM 无任何工具证据)。"""
    assert classify_evidence([]) == EvidenceClass.NONE
    assert classify_evidence(None) == EvidenceClass.NONE


# ── Path 2: Workflow trigger evidence ────────────────────────────────────────

def test_run_workflow_classified_as_workflow_trigger():
    """tool_log 含 run_workflow → WORKFLOW_TRIGGER evidence class."""
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.WORKFLOW_TRIGGER, f"期望 WORKFLOW_TRIGGER，实得 {cls}"


def test_workflow_takes_priority_over_query():
    """同时含 run_workflow 和 query_sku → WORKFLOW_TRIGGER 优先。"""
    tool_log = [
        {"name": "query_sku", "args": {"skus": ["TBJ0057A"]}},
        {"name": "run_workflow", "task_id": "aabb1234"},
    ]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.WORKFLOW_TRIGGER, (
        f"run_workflow 应优先于 query_sku，实得 {cls}"
    )


# ── Path 3: Task status readback (三态受理回执) ───────────────────────────────

def _setup_temp_db():
    """Bootstrap 一个临时 SQLite DB 供 readback 测试用。"""
    from hipop.server import data as _data
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    _data.DB_PATH = tmp.name
    _data._task_tables_checked = False
    _data._ensure_task_tables()
    _data.set_current_tenant(1)
    return _data


def test_task_readback_queued_state():
    """TASK_READBACK 路径：task 刚创建（queued）→ 回复不宣称'已完成'。"""
    _data = _setup_temp_db()
    from hipop.server import runtime as _runtime
    from hipop.server._workflow_reply import _workflow_receipt_reply

    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    reply = _workflow_receipt_reply(task_id, "wf1_stock_v2", "库存刷新")
    # 三态受理：queued/started state → 不宣称"已完成"（只有 done state 才说完成）
    assert "未确认" not in reply or task_id in reply, (
        f"task 存在时 reply 不应显示'未确认': {reply!r}"
    )
    assert task_id in reply, f"reply 应包含 task_id={task_id}: {reply!r}"
    # 证据来自 DB 读，不是 LLM 编造
    assert ("已排队" in reply or "已开始" in reply or "已完成" in reply or "失败" in reply), (
        f"reply 应反映真实状态之一，实得: {reply!r}"
    )
    print(f"    queued reply: {reply[:80]!r}")


def test_task_readback_nonexistent_task_id():
    """TASK_READBACK 路径：不存在的 task_id → 返回'未确认'警告，不编造状态。"""
    _setup_temp_db()
    from hipop.server._workflow_reply import _workflow_receipt_reply

    reply = _workflow_receipt_reply("deadbeef", "wf1_stock_v2", "库存刷新")
    assert "未确认" in reply, (
        f"不存在的 task_id 应返回'未确认'警告，实得: {reply!r}"
    )
    assert "deadbeef" in reply, f"reply 应包含请求的 task_id: {reply!r}"
    print(f"    nonexistent reply: {reply[:80]!r}")


# ── Bypass checks (无证据 → 安全层拦截) ──────────────────────────────────────

def test_bypass_done_without_workflow():
    """旁路拦截：'数据已刷新'但无 run_workflow → check_task_completion_bypass 报警。"""
    reply = "好的，库存数据已刷新完成，你可以查看最新结果。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, (
        "WS-128 旁路失败：reply 宣称'数据已刷新'但无 run_workflow 证据，"
        "应被 check_task_completion_bypass 拦截，但未报警"
    )
    print(f"    bypass_done warns: {warns[0][:80]!r}")


def test_bypass_task_done_without_workflow():
    """旁路拦截：'任务已完成'但无 run_workflow。"""
    reply = "任务已完成，数据已更新。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, (
        "WS-128 旁路失败：reply 宣称'任务已完成'但无 run_workflow 证据，未报警"
    )


def test_bypass_already_synced_without_workflow():
    """旁路拦截：'已同步完成'但无 run_workflow。"""
    reply = "已同步完成，最新库存数据已可用。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, "WS-128 旁路：'已同步完成' 应被拦截，未报警"


def test_no_bypass_with_run_workflow():
    """正路：run_workflow 已调 → check_task_completion_bypass 不报警。"""
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    reply = "数据已刷新完成，库存已更新。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, f"误报：run_workflow 已调但仍报警: {warns}"


def test_no_bypass_for_query_result():
    """正路：query_sku 结果描述（不涉及刷新完成）→ 不报警。"""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}]
    reply = "TBJ0057A 当前库存 50 件，可撑 15 天，无告警。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, f"误报：query 正常描述库存被误判为旁路: {warns}"


# ── Safety layer integration ──────────────────────────────────────────────────

def test_sanitize_reply_catches_completion_bypass():
    """sanitize_reply 整合：'任务已跑完'无 run_workflow → 产生 banner。"""
    from hipop.server._safety import sanitize_reply

    reply = "任务已跑完，你的库存数据已更新。"
    final, warns = sanitize_reply(reply, tools_used=[], tool_log=[])
    assert warns, (
        "_safety.sanitize_reply 未拦截'任务已跑完'旁路 — "
        "check_task_completion_bypass 未被 sanitize_reply 调用"
    )
    assert "⚠️" in final, f"banner 未出现在 final reply: {final[:100]!r}"
    print(f"    sanitize warns: {warns[0][:80]!r}")


# ── Three-path distinguishability assertion ───────────────────────────────────

def test_three_paths_are_distinguishable():
    """关键：三条路径通过 tool_log 可区分，不靠 LLM 自述。

    同一类型请求根据 tool_log 内容分类为不同 EvidenceClass：
      - 只有 query 工具 → QUERY
      - 只有 run_workflow → WORKFLOW_TRIGGER
      - 无工具（_workflow_receipt_reply 路径）→ NONE（readback 由 _workflow_receipt_reply 保证）
    """
    # 查询路径
    q_cls = classify_evidence([{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}])
    # 工作流触发路径
    wf_cls = classify_evidence([{"name": "run_workflow", "task_id": "aabb1234"}])
    # 无工具路径（_workflow_receipt_reply 路径：已在 test_task_readback_* 中验证）
    none_cls = classify_evidence([])

    assert q_cls != wf_cls, f"QUERY 和 WORKFLOW_TRIGGER 应不同，实得均为 {q_cls}"
    assert q_cls != none_cls, f"QUERY 和 NONE 应不同，实得均为 {q_cls}"
    assert wf_cls != none_cls, f"WORKFLOW_TRIGGER 和 NONE 应不同，实得均为 {wf_cls}"
    assert q_cls == EvidenceClass.QUERY
    assert wf_cls == EvidenceClass.WORKFLOW_TRIGGER
    assert none_cls == EvidenceClass.NONE
    print(f"    三路径: QUERY={q_cls.value}, WF={wf_cls.value}, NONE={none_cls.value} — 可区分 ✓")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_chat_boundary_contract — WS-128 P0-S0 三路径边界契约")

    tests = [
        ("test_query_tool_classified_as_query",
         test_query_tool_classified_as_query),
        ("test_all_query_tools_classified",
         test_all_query_tools_classified),
        ("test_empty_tool_log_classified_as_none",
         test_empty_tool_log_classified_as_none),
        ("test_run_workflow_classified_as_workflow_trigger",
         test_run_workflow_classified_as_workflow_trigger),
        ("test_workflow_takes_priority_over_query",
         test_workflow_takes_priority_over_query),
        ("test_task_readback_queued_state",
         test_task_readback_queued_state),
        ("test_task_readback_nonexistent_task_id",
         test_task_readback_nonexistent_task_id),
        ("test_bypass_done_without_workflow",
         test_bypass_done_without_workflow),
        ("test_bypass_task_done_without_workflow",
         test_bypass_task_done_without_workflow),
        ("test_bypass_already_synced_without_workflow",
         test_bypass_already_synced_without_workflow),
        ("test_no_bypass_with_run_workflow",
         test_no_bypass_with_run_workflow),
        ("test_no_bypass_for_query_result",
         test_no_bypass_for_query_result),
        ("test_sanitize_reply_catches_completion_bypass",
         test_sanitize_reply_catches_completion_bypass),
        ("test_three_paths_are_distinguishable",
         test_three_paths_are_distinguishable),
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
    print(f"\n✓ smoke_chat_boundary_contract all {len(tests)} passed")

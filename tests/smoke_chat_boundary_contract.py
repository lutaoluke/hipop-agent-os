"""smoke_chat_boundary_contract.py — WS-128 P0-S0 fail-then-pass smoke (round 2)

验收：chat 查询 / 工作流执行 / 状态回读 三条热点路径在同一分类框架下可区分，
且无真实证据时旁路宣称被拦截。

三条路径定义（_chat_boundary.py）：
  QUERY           — 读工具证据 (query_sku / list_products 等)
  WORKFLOW_TRIGGER — run_workflow → tasks 表落行 → task_id
  TASK_READBACK   — get_task_with_events 从 tasks 表读真实状态

FAIL 条件（修前）：
  - classify_evidence 不存在 → ImportError
  - 已完成/已刷新旁路未拦截 → test_bypass_done_without_workflow 失败
  - run_workflow 单独放行了"已完成"声明 → test_bypass_run_workflow_only_blocks_done_claim 失败

PASS 条件（修后）：
  - 所有 classify_evidence 断言正确
  - 已完成/已刷新旁路被 check_task_completion_bypass 捕获
  - run_workflow 只证明任务触发，不放行已完成/已刷新声明
  - task_result/status=done 才是任务完成证据，允许已完成声明
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
    _has_task_done_evidence,
    QUERY_TOOLS,
    WORKFLOW_TOOLS,
    TASK_DONE_TOOLS,
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
    assert "未确认" not in reply or task_id in reply, (
        f"task 存在时 reply 不应显示'未确认': {reply!r}"
    )
    assert task_id in reply, f"reply 应包含 task_id={task_id}: {reply!r}"
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
    """旁路拦截：'数据已刷新'且无任何工具 → check_task_completion_bypass 报警。"""
    reply = "好的，库存数据已刷新完成，你可以查看最新结果。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, (
        "WS-128 旁路失败：reply 宣称'数据已刷新'但无工具证据，"
        "应被 check_task_completion_bypass 拦截，但未报警"
    )
    print(f"    bypass_done warns: {warns[0][:80]!r}")


def test_bypass_task_done_without_workflow():
    """旁路拦截：'任务已完成'且无任何工具。"""
    reply = "任务已完成，数据已更新。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, (
        "WS-128 旁路失败：reply 宣称'任务已完成'但无工具证据，未报警"
    )


def test_bypass_already_synced_without_workflow():
    """旁路拦截：'已同步完成'且无任何工具。"""
    reply = "已同步完成，最新库存数据已可用。"
    warns = check_task_completion_bypass(reply, [])
    assert warns, "WS-128 旁路：'已同步完成' 应被拦截，未报警"


# ── 关键红队 fail-then-pass：run_workflow 单独不放行已完成声明 ─────────────────

def test_bypass_run_workflow_only_blocks_done_claim():
    """红队核心：run_workflow 单独不放行'数据已刷新完成/任务已完成'。

    FAIL（修前）：check_task_completion_bypass 看到 run_workflow → returns []
                  → 旁路未被拦截，用户看到假完成消息。
    PASS（修后）：run_workflow 只证明任务已创建/触发，不证明已完成；
                  → warns 非空，拦截"已完成/已刷新"声明。

    这是验门人 13:29 发现的核心洞，也是码长 13:48 要求的首要红队用例。
    """
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    reply = "数据已刷新完成，任务已完成，库存已更新。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert warns, (
        "红队 FAIL：run_workflow 单独存在时，'数据已刷新完成，任务已完成'应被拦截，"
        "但 check_task_completion_bypass 返回 warns=[] — "
        "已触发 ≠ 已完成，这是 bypass"
    )
    # 警告措辞中应体现"已触发 ≠ 已完成"区别
    assert any("已触发" in w or "run_workflow" in w or "任务创建" in w for w in warns), (
        f"警告措辞未说明'已触发≠已完成'的区别: {warns}"
    )
    print(f"    run_workflow bypass blocked: {warns[0][:80]!r}")


def test_bypass_run_workflow_with_data_refresh_claim():
    """红队：run_workflow + '数据已刷新' → 仍需拦截（同上，验独立 pattern）。"""
    tool_log = [{"name": "run_workflow"}]
    reply = "库存数据已刷新完成，你现在看到的是最新数据。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert warns, (
        "run_workflow 单独存在时'库存数据已刷新完成'应被拦截，但未报警"
    )


def test_bypass_run_workflow_with_task_done_claim():
    """红队：run_workflow + '任务已跑完' → 仍需拦截。"""
    tool_log = [{"name": "run_workflow", "task_id": "ccdd9988"}]
    reply = "刷新任务已跑完，可以查看最新结果了。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert warns, (
        "run_workflow 单独存在时'任务已跑完'应被拦截，但未报警"
    )


# ── 正路：task_result/done 放行 ───────────────────────────────────────────────

def test_no_bypass_with_task_result_done():
    """正路：task_result 显示 status=done → '数据已刷新完成'允许。

    FAIL（修前）：_has_task_done_evidence 函数不存在 → ImportError 或逻辑错误。
    PASS（修后）：tool_log 含 task_result+done → check_task_completion_bypass → warns=[].
    """
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_result", "result": {"status": "done"}},
    ]
    reply = "数据已刷新完成。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, (
        f"误报：task_result status=done 时'数据已刷新完成'应被放行，实得 warns={warns}"
    )
    print(f"    task_result/done: no warns ✓")


def test_no_bypass_with_task_status_readback_done():
    """正路：task_status_readback status=done → 放行。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_status_readback", "status": "done"},
    ]
    reply = "库存数据已刷新完成，任务已完成。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, (
        f"误报：task_status_readback status=done 时应放行，实得 warns={warns}"
    )


def test_no_bypass_with_task_result_success():
    """正路：task_result status=success → 放行。"""
    tool_log = [{"name": "task_result", "result": {"status": "success"}}]
    reply = "已同步完成，数据已更新。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, f"task_result/success 应放行，实得 warns={warns}"


def test_no_bypass_with_task_result_error_still_blocks():
    """task_result 但 status=error → 不放行（没有完成证据）。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_result", "result": {"status": "error"}},
    ]
    reply = "数据已刷新完成。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert warns, (
        "task_result status=error 时'数据已刷新完成'应被拦截，但未报警"
    )


def test_no_bypass_for_query_result():
    """正路：query_sku 正常库存描述 → 不报警（不涉及完成/刷新声明）。"""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}]
    reply = "TBJ0057A 当前库存 50 件，可撑 15 天，无告警。"
    warns = check_task_completion_bypass(reply, tool_log)
    assert not warns, f"误报：query 正常描述库存被误判为旁路: {warns}"


# ── _has_task_done_evidence unit tests ────────────────────────────────────────

def test_has_task_done_evidence_recognizes_done_statuses():
    """_has_task_done_evidence 识别 done/success/complete 状态。"""
    for status in ("done", "done_unverified", "success", "complete", "completed"):
        tool_log = [{"name": "task_result", "result": {"status": status}}]
        assert _has_task_done_evidence(tool_log), f"status={status} 应识别为 done 证据"

    for status in ("running", "queued", "started", "error", "failed", ""):
        tool_log = [{"name": "task_result", "result": {"status": status}}]
        assert not _has_task_done_evidence(tool_log), (
            f"status={status} 不应识别为 done 证据"
        )


def test_run_workflow_alone_is_not_done_evidence():
    """run_workflow 单独不算任务完成证据（只是任务创建）。"""
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    assert not _has_task_done_evidence(tool_log), (
        "run_workflow 单独存在时不应被视为任务完成证据"
    )


# ── Safety layer integration ──────────────────────────────────────────────────

def test_sanitize_reply_catches_completion_bypass_no_tool():
    """sanitize_reply 整合：'任务已跑完'无任何工具 → banner。"""
    from hipop.server._safety import sanitize_reply

    reply = "任务已跑完，你的库存数据已更新。"
    final, warns = sanitize_reply(reply, tools_used=[], tool_log=[])
    assert warns, (
        "_safety.sanitize_reply 未拦截'任务已跑完'旁路 — "
        "check_task_completion_bypass 未被 sanitize_reply 调用"
    )
    assert "⚠️" in final, f"banner 未出现在 final reply: {final[:100]!r}"
    print(f"    sanitize warns (no tool): {warns[0][:80]!r}")


def test_sanitize_reply_catches_completion_bypass_run_workflow_only():
    """sanitize_reply 整合：run_workflow 单独时'数据已刷新完成' → banner。

    这是验门人要求的具体 test case：
    sanitize_reply('数据已刷新完成，任务已完成，库存已更新。', ['run_workflow'],
                   tool_log=[{'name':'run_workflow'}]) 应返回 warns 非空。
    """
    from hipop.server._safety import sanitize_reply

    reply = "数据已刷新完成，任务已完成，库存已更新。"
    final, warns = sanitize_reply(
        reply,
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, (
        "验门红队 FAIL：sanitize_reply 在 run_workflow 单独存在时放行了"
        "'数据已刷新完成，任务已完成'— 这是 bypass，应产生警告"
    )
    assert "⚠️" in final, f"banner 未出现: {final[:100]!r}"
    print(f"    sanitize warns (run_workflow only): {warns[-1][:80]!r}")


def test_sanitize_reply_allows_completion_with_task_result():
    """sanitize_reply 整合：task_result/done + run_workflow → '数据已刷新完成'不报警。"""
    from hipop.server._safety import sanitize_reply

    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_result", "result": {"status": "done"}},
    ]
    reply = "数据已刷新完成。"
    _, warns = sanitize_reply(
        reply,
        tools_used=["run_workflow", "task_result"],
        tool_log=tool_log,
    )
    # task_result/done 应放行 — 过滤掉 check_task_completion_bypass 的 warns
    completion_warns = [w for w in warns if "已完成" in w or "已刷新" in w or "run_workflow" in w]
    assert not completion_warns, (
        f"误报：task_result/done 时不应有完成旁路警告，实得 {completion_warns}"
    )
    print(f"    sanitize allows task_result/done ✓ total_warns={len(warns)}")


# ── Three-path distinguishability assertion ───────────────────────────────────

def test_three_paths_are_distinguishable():
    """关键：三条路径通过 tool_log 可区分，不靠 LLM 自述。"""
    q_cls = classify_evidence([{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}])
    wf_cls = classify_evidence([{"name": "run_workflow", "task_id": "aabb1234"}])
    none_cls = classify_evidence([])

    assert q_cls != wf_cls
    assert q_cls != none_cls
    assert wf_cls != none_cls
    assert q_cls == EvidenceClass.QUERY
    assert wf_cls == EvidenceClass.WORKFLOW_TRIGGER
    assert none_cls == EvidenceClass.NONE
    print(f"    三路径: QUERY={q_cls.value}, WF={wf_cls.value}, NONE={none_cls.value} — 可区分 ✓")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_chat_boundary_contract — WS-128 P0-S0 三路径边界契约 (round 2)")

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
        # 关键红队 fail-then-pass
        ("test_bypass_run_workflow_only_blocks_done_claim",
         test_bypass_run_workflow_only_blocks_done_claim),
        ("test_bypass_run_workflow_with_data_refresh_claim",
         test_bypass_run_workflow_with_data_refresh_claim),
        ("test_bypass_run_workflow_with_task_done_claim",
         test_bypass_run_workflow_with_task_done_claim),
        # 正路
        ("test_no_bypass_with_task_result_done",
         test_no_bypass_with_task_result_done),
        ("test_no_bypass_with_task_status_readback_done",
         test_no_bypass_with_task_status_readback_done),
        ("test_no_bypass_with_task_result_success",
         test_no_bypass_with_task_result_success),
        ("test_no_bypass_with_task_result_error_still_blocks",
         test_no_bypass_with_task_result_error_still_blocks),
        ("test_no_bypass_for_query_result",
         test_no_bypass_for_query_result),
        # _has_task_done_evidence unit tests
        ("test_has_task_done_evidence_recognizes_done_statuses",
         test_has_task_done_evidence_recognizes_done_statuses),
        ("test_run_workflow_alone_is_not_done_evidence",
         test_run_workflow_alone_is_not_done_evidence),
        # Safety layer integration
        ("test_sanitize_reply_catches_completion_bypass_no_tool",
         test_sanitize_reply_catches_completion_bypass_no_tool),
        ("test_sanitize_reply_catches_completion_bypass_run_workflow_only",
         test_sanitize_reply_catches_completion_bypass_run_workflow_only),
        ("test_sanitize_reply_allows_completion_with_task_result",
         test_sanitize_reply_allows_completion_with_task_result),
        # Three-path
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

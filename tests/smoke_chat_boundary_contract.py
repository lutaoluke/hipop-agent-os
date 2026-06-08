"""smoke_chat_boundary_contract.py — WS-128 P0-S0 fail-then-pass smoke (round 8)

验收：chat 查询 / 工作流执行 / 状态回读 三条热点路径在同一分类框架下可区分，
且无真实证据时旁路宣称被拦截。

三条路径定义（_chat_boundary.py）：
  QUERY           — 读工具证据 (query_sku / list_products 等)
  WORKFLOW_TRIGGER — run_workflow → tasks 表落行 → task_id
  TASK_READBACK   — get_task_with_events / task_result / task_status_readback
                    返回真实 done/success 状态，证明任务完成

FAIL 条件（修前 round-2）：
  - "已经刷新" / "已更新" / "刷新已完成" 变体未被 _COMPLETION_BYPASS_RE 捕获
  - classify_evidence 对 task_status_readback/task_result(done) 返回 NONE

PASS 条件（修后 round-6）：
  - 三种正则变体均被拦截（已经刷新/已更新/刷新已完成）
  - classify_evidence 对有 done 状态的 task_done_tools 返回 TASK_READBACK
  - sanitize_reply 在 run_workflow 单独存在时拦截所有变体
  - round-1~4 验门人 probes 全部作为回归 smoke；run_workflow only 不放行
    任何"完成了/搞定了/处理好了"类声明
  - round-6 新增"结束了/完毕/处理完"变体，且 TASK_READBACK 用真实
    get_task_with_events 返回形状证明，不靠 provider 的 result_keys 摘要洗白
  - round-7 新增更短的通用完成态/状态声明："操作完毕/操作已完成/已处理/
    已经处理/已刷新/已完成处理/一切正常"。仅 run_workflow 或无工具仍必须报警；
    有真实 task readback done/success 或查询证据时不误伤。
  - round-8 新增 bare completion/update/sync 短句："已完成/完成了/已更新/
    已同步/已经同步"。仅真实 task readback done/success 能放行；run_workflow、
    无工具、query evidence 都不能洗白这些完成态声明。
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


# ── Round-3 fail-then-pass: 已经刷新/已更新/刷新已完成 变体 ─────────────────────

def test_bypass_jingxi_refresh_no_tool():
    """红队 round-3：'数据已经刷新完成' + 无工具 → warns（验门人 probe #4）。

    FAIL（修前）：_COMPLETION_BYPASS_RE 不匹配"已经刷新"变体 → warns=[]。
    PASS（修后）：正则覆盖 已经? 变体 → warns 非空。
    """
    warns = check_task_completion_bypass("数据已经刷新完成。", [])
    assert warns, "round-3 FAIL：'数据已经刷新完成'无工具时应被拦截，但 warns=[]"


def test_bypass_jingxi_refresh_run_workflow_only():
    """红队 round-3：'数据已经刷新完成' + run_workflow only → warns（验门人 probe #1）。

    FAIL（修前）：正则不匹配"已经刷新" → warns=[]（视作无完成声明，直接放行）。
    PASS（修后）：正则命中 → 进入两阶段门 → run_workflow 无完成证据 → warns 非空。
    """
    tool_log = [{"name": "run_workflow"}]
    warns = check_task_completion_bypass("数据已经刷新完成。", tool_log)
    assert warns, (
        "round-3 FAIL：run_workflow 单独时'数据已经刷新完成'应被拦截，但 warns=[]"
    )


def test_bypass_kucu_update_run_workflow_only():
    """红队 round-3：'库存已更新完成' + run_workflow only → warns（验门人 probe #2）。

    FAIL（修前）：_COMPLETION_BYPASS_RE 无"已更新"分支 → warns=[]。
    PASS（修后）：库存.{0,8}已经?(?:刷新|更新) 命中"库存已更新" → warns 非空。
    """
    tool_log = [{"name": "run_workflow"}]
    warns = check_task_completion_bypass("库存已更新完成。", tool_log)
    assert warns, (
        "round-3 FAIL：run_workflow 单独时'库存已更新完成'应被拦截，但 warns=[]"
    )


def test_bypass_flush_done_run_workflow_only():
    """红队 round-3：'刷新已完成' + run_workflow only → warns（验门人 probe #3）。

    FAIL（修前）：_COMPLETION_BYPASS_RE 无"刷新已完成"分支 → warns=[]。
    PASS（修后）：(?:刷新|更新|同步).{0,5}已完成 命中"刷新已完成" → warns 非空。
    """
    tool_log = [{"name": "run_workflow"}]
    warns = check_task_completion_bypass("刷新已完成。", tool_log)
    assert warns, (
        "round-3 FAIL：run_workflow 单独时'刷新已完成'应被拦截，但 warns=[]"
    )


def test_sanitize_reply_jingxi_refresh_run_workflow_only():
    """sanitize_reply 整合：run_workflow 单独时'数据已经刷新完成' → banner（probe #1）。"""
    from hipop.server._safety import sanitize_reply

    _, warns = sanitize_reply(
        "数据已经刷新完成。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, "sanitize_reply probe #1 FAIL：'数据已经刷新完成' run_workflow only → warns=[]"


def test_sanitize_reply_kucu_update_run_workflow_only():
    """sanitize_reply 整合：run_workflow 单独时'库存已更新完成' → banner（probe #2）。"""
    from hipop.server._safety import sanitize_reply

    _, warns = sanitize_reply(
        "库存已更新完成。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, "sanitize_reply probe #2 FAIL：'库存已更新完成' run_workflow only → warns=[]"


def test_sanitize_reply_flush_done_run_workflow_only():
    """sanitize_reply 整合：run_workflow 单独时'刷新已完成' → banner（probe #3）。"""
    from hipop.server._safety import sanitize_reply

    _, warns = sanitize_reply(
        "刷新已完成。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, "sanitize_reply probe #3 FAIL：'刷新已完成' run_workflow only → warns=[]"


# ── Round-3: TASK_READBACK classify_evidence contract ────────────────────────

def test_classify_task_readback_task_status_done():
    """classify_evidence: task_status_readback+status=done → TASK_READBACK（修前返回 NONE）。

    FAIL（修前）：classify_evidence 只检查 WORKFLOW_TOOLS / QUERY_TOOLS → NONE。
    PASS（修后）：_has_task_done_evidence 优先 → TASK_READBACK。
    """
    tool_log = [{"name": "task_status_readback", "status": "done"}]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.TASK_READBACK, (
        f"round-3 FAIL：task_status_readback/done 应分类为 TASK_READBACK，实得 {cls}"
    )


def test_classify_task_readback_task_result_done():
    """classify_evidence: task_result+result.status=done → TASK_READBACK（修前返回 NONE）。

    FAIL（修前）：classify_evidence 不调用 _has_task_done_evidence → NONE。
    PASS（修后）：TASK_READBACK 优先级最高 → TASK_READBACK。
    """
    tool_log = [{"name": "task_result", "result": {"status": "done"}}]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.TASK_READBACK, (
        f"round-3 FAIL：task_result/done 应分类为 TASK_READBACK，实得 {cls}"
    )


def test_classify_task_readback_beats_workflow_trigger():
    """classify_evidence: run_workflow + task_result/done → TASK_READBACK（完成优先于触发）。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_result", "result": {"status": "done"}},
    ]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.TASK_READBACK, (
        f"任务完成后应为 TASK_READBACK，实得 {cls}"
    )


def test_classify_task_done_running_still_workflow_trigger():
    """task_status_readback + status=running → 不升为 TASK_READBACK（任务仍在跑）。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_status_readback", "status": "running"},
    ]
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.WORKFLOW_TRIGGER, (
        f"task 仍在运行时应为 WORKFLOW_TRIGGER，实得 {cls}"
    )


# ── Round-4 fail-then-pass: 口语化 好了/完了/弄好了/搞定了 变体 ──────────────────

def test_bypass_data_refresh_hao_le_run_workflow_only():
    """红队 round-4 probe #1：'数据刷新好了' + run_workflow only → warns。

    FAIL（修前）：_COMPLETION_BYPASS_RE 无"刷新好了"分支（无"已"前缀）→ warns=[]。
    PASS（修后）：(?:数据|库存|销量).{0,10}(?:刷新|更新|同步).{0,5}(?:好了|...) 命中 → warns。
    """
    warns = check_task_completion_bypass("数据刷新好了。", [{"name": "run_workflow"}])
    assert warns, "round-4 probe #1 FAIL：'数据刷新好了' run_workflow only → warns=[]"


def test_bypass_kucu_update_hao_le_run_workflow_only():
    """红队 round-4 probe #2：'库存更新好了' + run_workflow only → warns。

    FAIL（修前）：_COMPLETION_BYPASS_RE 无"更新好了"（无"已"前缀）分支 → warns=[]。
    PASS（修后）：模式命中 → warns 非空。
    """
    warns = check_task_completion_bypass("库存更新好了。", [{"name": "run_workflow"}])
    assert warns, "round-4 probe #2 FAIL：'库存更新好了' run_workflow only → warns=[]"


def test_bypass_flush_hao_le_run_workflow_only():
    """红队 round-4：'刷新好了' + run_workflow only → warns（动词好了变体）。"""
    warns = check_task_completion_bypass("刷新好了。", [{"name": "run_workflow"}])
    assert warns, "round-4 FAIL：'刷新好了' run_workflow only → warns=[]"


def test_bypass_sync_hao_le_run_workflow_only():
    """'同步好了' + run_workflow only → warns。"""
    warns = check_task_completion_bypass("同步好了，你可以查了。", [{"name": "run_workflow"}])
    assert warns, "'同步好了' run_workflow only → warns=[]"


def test_sanitize_reply_data_refresh_hao_le():
    """sanitize_reply 整合：run_workflow 单独时'数据刷新好了' → banner（probe #1）。"""
    from hipop.server._safety import sanitize_reply

    _, warns = sanitize_reply(
        "数据刷新好了。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, "sanitize_reply probe #1 FAIL：'数据刷新好了' run_workflow only → warns=[]"


def test_sanitize_reply_kucu_update_hao_le():
    """sanitize_reply 整合：run_workflow 单独时'库存更新好了' → banner（probe #2）。"""
    from hipop.server._safety import sanitize_reply

    _, warns = sanitize_reply(
        "库存更新好了。",
        tools_used=["run_workflow"],
        tool_log=[{"name": "run_workflow"}],
    )
    assert warns, "sanitize_reply probe #2 FAIL：'库存更新好了' run_workflow only → warns=[]"


def test_no_false_positive_hao_le_query_only():
    """口语化"查好了"不被误判为任务完成声明（QUERY 路径）。"""
    warns = check_task_completion_bypass(
        "数据拉好了，共 50 行。",
        [{"name": "query_sku"}],
    )
    assert not warns, f"误报：'数据拉好了' 不应触发完成声明拦截，实得 warns={warns}"


# ── Round-5 fail-then-pass: 完成了/搞定了/处理好了 变体 ────────────────────────

ROUND_REVIEWER_PROBES = (
    "数据已刷新完成，任务已完成，库存已更新。",
    "数据已经刷新完成。",
    "库存已更新完成。",
    "刷新已完成。",
    "数据刷新好了。",
    "库存更新好了。",
    "数据同步好了。",
    "数据刷新完成了。",
    "库存更新完成了。",
    "刷新完成了。",
    "任务搞定了。",
    "任务处理好了。",
    "任务跑好了。",
    "后台任务弄好了。",
    "工作流完了。",
    "处理好了。",
)


def test_all_reviewer_completion_probes_block_run_workflow_only():
    """round-1~4 验门人 probes：run_workflow only 时全部应 warns。

    FAIL（round-4）：完成了 / 搞定了 / 处理好了 / 跑好了 / 弄好了 / 完了
                  等口语完成声明仍有多项返回 warns=[]。
    PASS（round-5）：所有历次 probe 均需任务完成证据；run_workflow 只算触发。
    """
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    for reply in ROUND_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert warns, f"round-5 FAIL：run_workflow only 时 {reply!r} 应被拦截"


def test_all_reviewer_completion_probes_block_no_tool():
    """同一 probe 集：无工具证据时全部应 warns。"""
    for reply in ROUND_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, [])
        assert warns, f"round-5 FAIL：无工具时 {reply!r} 应被拦截"


def test_sanitize_reply_round5_exact_probes():
    """sanitize_reply 整合：round-5 exact probes 全部出 banner。"""
    from hipop.server._safety import sanitize_reply

    probes = (
        "数据刷新完成了。",
        "库存更新完成了。",
        "刷新完成了。",
        "任务搞定了。",
        "任务处理好了。",
        "处理好了。",
    )
    for reply in probes:
        final, warns = sanitize_reply(
            reply,
            tools_used=["run_workflow"],
            tool_log=[{"name": "run_workflow"}],
        )
        assert warns, f"sanitize_reply round-5 FAIL：{reply!r} run_workflow only → warns=[]"
        assert "⚠️" in final, f"banner 未出现: {final[:100]!r}"


def test_round5_completion_claims_allowed_with_task_done_evidence():
    """正路：round-5 口语完成声明有 task_result/done 时允许。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "task_result", "result": {"status": "done"}},
    ]
    for reply in (
        "数据刷新完成了。",
        "库存更新完成了。",
        "任务搞定了。",
        "任务处理好了。",
    ):
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"task_result/done 时 {reply!r} 应放行，实得 warns={warns}"


def test_no_false_positive_query_hao_le_words():
    """宽泛口语词尾不能误伤普通查询动作。"""
    tool_log = [{"name": "query_sku"}]
    for reply in ("数据查好了，共 50 行。", "库存看好了，没有异常。"):
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"误报：{reply!r} 不应触发任务完成声明拦截，实得 warns={warns}"


# ── Round-6 fail-then-pass: 结束了/完毕/处理完 + real readback ────────────────

ROUND6_REVIEWER_PROBES = (
    "数据刷新结束了。",
    "库存更新结束了。",
    "任务执行完毕。",
    "数据已处理完。",
    "数据处理完了。",
    "库存更新完毕。",
    "任务执行结束了。",
    "工作流执行完毕。",
)


def test_round6_completion_tails_block_run_workflow_only():
    """round-6 验门人 probes：run_workflow only 时全部应 warns。

    FAIL（round-5）：结束了 / 完毕 / 处理完 这些完成态尾词未命中，返回 warns=[]。
    PASS（round-6）：仍需真实任务完成 readback；run_workflow 只算任务创建。
    """
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    for reply in ROUND6_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert warns, f"round-6 FAIL：run_workflow only 时 {reply!r} 应被拦截"


def test_round6_completion_tails_block_no_tool():
    """同一 round-6 probe 集：无工具证据时全部应 warns。"""
    for reply in ROUND6_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, [])
        assert warns, f"round-6 FAIL：无工具时 {reply!r} 应被拦截"


def test_sanitize_reply_round6_exact_probes():
    """sanitize_reply 整合：round-6 exact probes 全部出 banner。"""
    from hipop.server._safety import sanitize_reply

    for reply in (
        "数据刷新结束了。",
        "库存更新结束了。",
        "任务执行完毕。",
        "数据已处理完。",
    ):
        final, warns = sanitize_reply(
            reply,
            tools_used=["run_workflow"],
            tool_log=[{"name": "run_workflow"}],
        )
        assert warns, f"sanitize_reply round-6 FAIL：{reply!r} run_workflow only → warns=[]"
        assert "⚠️" in final, f"banner 未出现: {final[:100]!r}"


def _done_readback_tool_log():
    """Production readback shape: same payload family as data.get_task_with_events/API /tasks."""
    return [{
        "name": "get_task_with_events",
        "args": {"task_id": "aabb1234"},
        "result": {
            "task_id": "aabb1234",
            "task": {"task_id": "aabb1234", "state": "done"},
            "events": [{"step_no": 1, "status": "done"}],
        },
        "result_keys": ["task_id", "task", "events"],
    }]


def test_classify_real_task_readback_done_shape():
    """真实 get_task_with_events 返回形状 status=done → TASK_READBACK。"""
    tool_log = _done_readback_tool_log()
    assert _has_task_done_evidence(tool_log), "get_task_with_events done 形状应算完成证据"
    cls = classify_evidence(tool_log)
    assert cls == EvidenceClass.TASK_READBACK, (
        f"真实任务回读 done 应分类为 TASK_READBACK，实得 {cls}"
    )


def test_result_keys_only_is_not_task_done_evidence():
    """provider 摘要 result_keys 不能洗白完成态声明；必须有真实 status/state。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        {"name": "get_task_with_events", "args": {"task_id": "aabb1234"},
         "result_keys": ["task_id", "task", "events"]},
    ]
    assert not _has_task_done_evidence(tool_log), (
        "只有 result_keys 摘要、没有 task.state/events.status 时不应算完成证据"
    )
    assert classify_evidence(tool_log) == EvidenceClass.WORKFLOW_TRIGGER
    warns = check_task_completion_bypass("任务执行完毕。", tool_log)
    assert warns, "result_keys-only readback 不应放行'任务执行完毕'"


def test_round6_completion_allowed_with_real_task_readback():
    """正路：真实 task readback done 时，round-6 完成声明允许通过。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        *_done_readback_tool_log(),
    ]
    for reply in ("任务执行完毕。", "数据刷新结束了。", "数据已处理完。"):
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"真实 task readback done 时 {reply!r} 应放行，实得 warns={warns}"


# ── Round-7 fail-then-pass: short completion/status claims ───────────────────

ROUND7_REVIEWER_PROBES = (
    "操作完毕。",
    "操作完了。",
    "操作已完成。",
    "已处理。",
    "已经处理。",
    "已刷新。",
    "已完成处理。",
    "一切正常。",
)


def test_round7_short_completion_claims_block_run_workflow_only():
    """round-7 验门人 probes：run_workflow only 时全部应 warns。

    FAIL（round-6）：操作完毕 / 操作已完成 / 已处理 / 已刷新 等短句未命中，
                  返回 warns=[]。
    PASS（round-7）：这些短句仍需真实 task readback；run_workflow 只算任务创建。
    """
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    for reply in ROUND7_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert warns, f"round-7 FAIL：run_workflow only 时 {reply!r} 应被拦截"


def test_round7_short_completion_claims_block_no_tool():
    """同一 round-7 probe 集：无工具证据时全部应 warns。"""
    for reply in ROUND7_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, [])
        assert warns, f"round-7 FAIL：无工具时 {reply!r} 应被拦截"


def test_sanitize_reply_round7_exact_probes():
    """sanitize_reply 整合：round-7 exact probes 全部出 banner。"""
    from hipop.server._safety import sanitize_reply

    for reply in ROUND7_REVIEWER_PROBES:
        final, warns = sanitize_reply(
            reply,
            tools_used=["run_workflow"],
            tool_log=[{"name": "run_workflow"}],
        )
        assert warns, f"sanitize_reply round-7 FAIL：{reply!r} run_workflow only → warns=[]"
        assert "⚠️" in final, f"banner 未出现: {final[:100]!r}"


def test_round7_short_claims_allowed_with_real_task_readback():
    """正路：真实 task readback done 时，round-7 短完成声明允许通过。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        *_done_readback_tool_log(),
    ]
    for reply in ROUND7_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"真实 task readback done 时 {reply!r} 应放行，实得 warns={warns}"


def test_round7_generic_status_allowed_with_query_evidence():
    """查询证据能支撑普通状态判断，不能被通用状态短句误伤。"""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}]
    for reply in ("已处理查询结果。", "一切正常。"):
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"query_sku 证据下 {reply!r} 不应触发任务完成旁路，实得 warns={warns}"


# ── Round-8 fail-then-pass: bare completion/update/sync claims ───────────────

ROUND8_REVIEWER_PROBES = (
    "已完成。",
    "完成了。",
    "已更新。",
    "已同步。",
    "已经同步。",
)


def test_round8_bare_completion_claims_block_run_workflow_only():
    """round-8 验门人 probes：run_workflow only 时全部应 warns。

    FAIL（round-7）：已完成 / 完成了 / 已更新 / 已同步 / 已经同步 等 bare
                  短句未命中，返回 warns=[]。
    PASS（round-8）：这些短句仍需真实 task readback；run_workflow 只算任务创建。
    """
    tool_log = [{"name": "run_workflow", "task_id": "aabb1234"}]
    for reply in ROUND8_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert warns, f"round-8 FAIL：run_workflow only 时 {reply!r} 应被拦截"


def test_round8_bare_completion_claims_block_no_tool():
    """同一 round-8 probe 集：无工具证据时全部应 warns。"""
    for reply in ROUND8_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, [])
        assert warns, f"round-8 FAIL：无工具时 {reply!r} 应被拦截"


def test_sanitize_reply_round8_exact_probes():
    """sanitize_reply 整合：round-8 exact probes 全部出 banner。"""
    from hipop.server._safety import sanitize_reply

    for reply in ROUND8_REVIEWER_PROBES:
        final, warns = sanitize_reply(
            reply,
            tools_used=["run_workflow"],
            tool_log=[{"name": "run_workflow"}],
        )
        assert warns, f"sanitize_reply round-8 FAIL：{reply!r} run_workflow only → warns=[]"
        assert "⚠️" in final, f"banner 未出现: {final[:100]!r}"


def test_round8_bare_claims_allowed_with_real_task_readback():
    """正路：真实 task readback done 时，round-8 bare 完成声明允许通过。"""
    tool_log = [
        {"name": "run_workflow", "task_id": "aabb1234"},
        *_done_readback_tool_log(),
    ]
    for reply in ROUND8_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert not warns, f"真实 task readback done 时 {reply!r} 应放行，实得 warns={warns}"


def test_round8_bare_claims_not_washed_by_query_evidence():
    """bare 完成/更新/同步声明不能被 query evidence 洗白；需 task readback。"""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}]
    for reply in ROUND8_REVIEWER_PROBES:
        warns = check_task_completion_bypass(reply, tool_log)
        assert warns, f"query_sku 证据不应放行 bare 完成态 {reply!r}"


# ── Three-path distinguishability assertion ───────────────────────────────────

def test_three_paths_are_distinguishable():
    """关键：三条路径通过 tool_log 可区分，不靠 LLM 自述。"""
    q_cls = classify_evidence([{"name": "query_sku", "args": {"skus": ["TBJ0057A"]}}])
    wf_cls = classify_evidence([{"name": "run_workflow", "task_id": "aabb1234"}])
    rb_cls = classify_evidence(_done_readback_tool_log())
    none_cls = classify_evidence([])

    assert q_cls != wf_cls
    assert q_cls != rb_cls
    assert wf_cls != rb_cls
    assert q_cls == EvidenceClass.QUERY
    assert wf_cls == EvidenceClass.WORKFLOW_TRIGGER
    assert rb_cls == EvidenceClass.TASK_READBACK
    assert none_cls == EvidenceClass.NONE
    print(
        f"    三路径: QUERY={q_cls.value}, WF={wf_cls.value}, "
        f"READBACK={rb_cls.value}, NONE={none_cls.value} — 可区分 ✓"
    )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_chat_boundary_contract — WS-128 P0-S0 三路径边界契约 (round 8)")

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
        # 关键红队 fail-then-pass (round 2)
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
        # Round-3 fail-then-pass: 已经刷新/已更新/刷新已完成 变体
        ("test_bypass_jingxi_refresh_no_tool",
         test_bypass_jingxi_refresh_no_tool),
        ("test_bypass_jingxi_refresh_run_workflow_only",
         test_bypass_jingxi_refresh_run_workflow_only),
        ("test_bypass_kucu_update_run_workflow_only",
         test_bypass_kucu_update_run_workflow_only),
        ("test_bypass_flush_done_run_workflow_only",
         test_bypass_flush_done_run_workflow_only),
        ("test_sanitize_reply_jingxi_refresh_run_workflow_only",
         test_sanitize_reply_jingxi_refresh_run_workflow_only),
        ("test_sanitize_reply_kucu_update_run_workflow_only",
         test_sanitize_reply_kucu_update_run_workflow_only),
        ("test_sanitize_reply_flush_done_run_workflow_only",
         test_sanitize_reply_flush_done_run_workflow_only),
        # Round-3: TASK_READBACK classify_evidence contract
        ("test_classify_task_readback_task_status_done",
         test_classify_task_readback_task_status_done),
        ("test_classify_task_readback_task_result_done",
         test_classify_task_readback_task_result_done),
        ("test_classify_task_readback_beats_workflow_trigger",
         test_classify_task_readback_beats_workflow_trigger),
        ("test_classify_task_done_running_still_workflow_trigger",
         test_classify_task_done_running_still_workflow_trigger),
        # Round-4 fail-then-pass: 口语化 好了/完了/弄好了/搞定了 变体
        ("test_bypass_data_refresh_hao_le_run_workflow_only",
         test_bypass_data_refresh_hao_le_run_workflow_only),
        ("test_bypass_kucu_update_hao_le_run_workflow_only",
         test_bypass_kucu_update_hao_le_run_workflow_only),
        ("test_bypass_flush_hao_le_run_workflow_only",
         test_bypass_flush_hao_le_run_workflow_only),
        ("test_bypass_sync_hao_le_run_workflow_only",
         test_bypass_sync_hao_le_run_workflow_only),
        ("test_sanitize_reply_data_refresh_hao_le",
         test_sanitize_reply_data_refresh_hao_le),
        ("test_sanitize_reply_kucu_update_hao_le",
         test_sanitize_reply_kucu_update_hao_le),
        ("test_no_false_positive_hao_le_query_only",
         test_no_false_positive_hao_le_query_only),
        # Round-5 fail-then-pass: 完成了/搞定了/处理好了 变体
        ("test_all_reviewer_completion_probes_block_run_workflow_only",
         test_all_reviewer_completion_probes_block_run_workflow_only),
        ("test_all_reviewer_completion_probes_block_no_tool",
         test_all_reviewer_completion_probes_block_no_tool),
        ("test_sanitize_reply_round5_exact_probes",
         test_sanitize_reply_round5_exact_probes),
        ("test_round5_completion_claims_allowed_with_task_done_evidence",
         test_round5_completion_claims_allowed_with_task_done_evidence),
        ("test_no_false_positive_query_hao_le_words",
         test_no_false_positive_query_hao_le_words),
        # Round-6 fail-then-pass: 结束了/完毕/处理完 + real readback
        ("test_round6_completion_tails_block_run_workflow_only",
         test_round6_completion_tails_block_run_workflow_only),
        ("test_round6_completion_tails_block_no_tool",
         test_round6_completion_tails_block_no_tool),
        ("test_sanitize_reply_round6_exact_probes",
         test_sanitize_reply_round6_exact_probes),
        ("test_classify_real_task_readback_done_shape",
         test_classify_real_task_readback_done_shape),
        ("test_result_keys_only_is_not_task_done_evidence",
         test_result_keys_only_is_not_task_done_evidence),
        ("test_round6_completion_allowed_with_real_task_readback",
         test_round6_completion_allowed_with_real_task_readback),
        # Round-7 fail-then-pass: short completion/status claims
        ("test_round7_short_completion_claims_block_run_workflow_only",
         test_round7_short_completion_claims_block_run_workflow_only),
        ("test_round7_short_completion_claims_block_no_tool",
         test_round7_short_completion_claims_block_no_tool),
        ("test_sanitize_reply_round7_exact_probes",
         test_sanitize_reply_round7_exact_probes),
        ("test_round7_short_claims_allowed_with_real_task_readback",
         test_round7_short_claims_allowed_with_real_task_readback),
        ("test_round7_generic_status_allowed_with_query_evidence",
         test_round7_generic_status_allowed_with_query_evidence),
        # Round-8 fail-then-pass: bare completion/update/sync claims
        ("test_round8_bare_completion_claims_block_run_workflow_only",
         test_round8_bare_completion_claims_block_run_workflow_only),
        ("test_round8_bare_completion_claims_block_no_tool",
         test_round8_bare_completion_claims_block_no_tool),
        ("test_sanitize_reply_round8_exact_probes",
         test_sanitize_reply_round8_exact_probes),
        ("test_round8_bare_claims_allowed_with_real_task_readback",
         test_round8_bare_claims_allowed_with_real_task_readback),
        ("test_round8_bare_claims_not_washed_by_query_evidence",
         test_round8_bare_claims_not_washed_by_query_evidence),
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

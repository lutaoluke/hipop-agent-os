"""smoke_lifecycle_gate.py — WS-132 fail-then-pass smoke

验收（WS-132）：工作流 task→step→done/error 可回读，禁假启动/假完成：

1. spawn_task 失败时 tool_run_workflow 返回 ok=False（创建失败原因明确），
   不抛异常（创建失败 ≠ 执行失败，需区分）。
2. 无 run_workflow 调用时，"刷新已完成/刷好了/扫完了/扫描完成" 等宣称被 safety 拦截。
3. 即使本轮调了 run_workflow，"刷新已完成" 类完成声明仍被拦截
   （run_workflow 只创建任务，LLM 无 readback 工具，无法知道是否真完成）。
4. 正路无误报：创建任务后用"已创建"/"已排队"措辞 → 不拦截。

FAIL 条件（修前）：
  - tool_run_workflow 在 spawn_task 抛异常时向上传播异常，不返回 ok=False
  - _DONE_CLAIM_RE 只覆盖 T38 重算语义（"重算完/任务已完成"），
    不覆盖 T36/T37 刷新语义（"刷好了/刷新已完成/扫完了/扫描完成"）

PASS 条件（修后）：
  - tool_run_workflow 包 try/except，spawn_task 抛 → 返回 {ok: False, creation_failed: True}
  - _DONE_CLAIM_RE 扩展覆盖刷新/扫描完成语义
"""

import sys
import os
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import _safety
from hipop.server._safety import sanitize_reply


# ── Test 1: spawn_task 失败时 tool_run_workflow 返回 ok=False ─────────────────

def test_spawn_failure_returns_ok_false():
    """spawn_task 抛异常时 tool_run_workflow 必须返回 ok=False + creation_failed=True。

    FAIL（修前）：spawn_task 异常向上传播 → tool_run_workflow 抛 → 此处 assertRaises 捕获
                  即 test 里会拿到 exception，不是 ok=False dict。
    PASS（修后）：tool_run_workflow try/except → 返回 {ok: False, creation_failed: True}
    """
    try:
        from hipop.server.agent import tool_run_workflow
    except ImportError as e:
        print(f"    SKIP (missing dep: {e})")
        return

    def _fail(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    raised = None
    result = None
    with patch("hipop.server.runtime.spawn_task", side_effect=_fail):
        with patch("hipop.server.agent._get_tenant", return_value=1):
            try:
                result = tool_run_workflow("wf1_stock_v2")
            except Exception as e:
                raised = e

    assert raised is None, (
        f"tool_run_workflow 抛了异常而非返回 ok=False: {type(raised).__name__}: {raised}\n"
        "修前状态：spawn_task 失败时异常向上传播 — 这正是 FAIL 的预期。"
        " 修后（添加 try/except）后此测试应通过。"
    )
    assert result is not None, "tool_run_workflow 应返回 dict，但返回了 None"
    assert result.get("ok") is False, (
        f"spawn_task 失败时 ok 应为 False，实际: {result}"
    )
    assert result.get("creation_failed") is True, (
        f"应有 creation_failed=True 以区分'未创建'与'创建后执行失败': {result}"
    )
    assert result.get("error"), f"应有 error 说明创建失败原因: {result}"
    print(f"    ok=False creation_failed=True error={result.get('error')!r}")


# ── Test 2: "刷新已完成" 无 run_workflow → 拦截 ────────────────────────────────

def test_refresh_done_claim_blocked_no_run_workflow():
    """无 run_workflow 时"库存刷新已完成"应触发 safety banner。

    FAIL（修前）：_DONE_CLAIM_RE 未覆盖"刷新已完成"→ 无 warning。
    PASS（修后）：_DONE_CLAIM_RE 扩展 → warning 出现。
    """
    _, warns = sanitize_reply("库存刷新已完成，最新数据已更新。", [])
    assert warns, (
        "无 run_workflow 时'库存刷新已完成'应被 safety 拦截，但没有 warning。\n"
        "修前状态：_DONE_CLAIM_RE 只覆盖 T38 重算语义 — FAIL 的预期。"
    )
    assert any("完成" in w or "刷新" in w or "T38" in w for w in warns), (
        f"warning 内容应提及完成/刷新或 T38: {warns}"
    )
    print(f"    warns={warns[0]!r}")


# ── Test 3: "刷好了" 无 run_workflow → 拦截 ────────────────────────────────────

def test_refresh_done_slang_blocked():
    """无 run_workflow 时"库存已刷好了"应触发 banner。

    FAIL（修前）：_DONE_CLAIM_RE 未覆盖"刷好了"。
    PASS（修后）：扩展后匹配。
    """
    _, warns = sanitize_reply("库存已刷好了，数据是最新的。", [])
    assert warns, (
        "无 run_workflow 时'库存已刷好了'应被拦截，但没有 warning。\n"
        "修前 FAIL 预期：_DONE_CLAIM_RE 未覆盖此短语。"
    )
    print(f"    warns={warns[0]!r}")


# ── Test 4: "扫完了" 无 run_workflow → 拦截 ────────────────────────────────────

def test_scan_done_claim_blocked():
    """无 run_workflow 时"ERP 扫完了"应触发 banner（T36/T37 扫描路径）。

    FAIL（修前）：_DONE_CLAIM_RE 未覆盖"扫完了"。
    PASS（修后）：扩展后匹配。
    """
    _, warns = sanitize_reply("ERP 扫完了，库存数据已同步。", [])
    assert warns, (
        "无 run_workflow 时'ERP 扫完了'应被拦截，但没有 warning。\n"
        "修前 FAIL 预期：_DONE_CLAIM_RE 未覆盖此短语。"
    )
    print(f"    warns={warns[0]!r}")


# ── Test 5: "刷新已完成" WITH run_workflow → 仍拦截（LLM 无回读工具）─────────────

def test_refresh_done_with_run_workflow_still_warned():
    """即使调了 run_workflow，声称"刷新已完成"仍触发 banner。

    原因：run_workflow 只创建任务，LLM 没有 task-status readback 工具，
    声称完成是无依据的 hallucination。真实完成回执走 _workflow_receipt_reply()。

    FAIL（修前）：_DONE_CLAIM_RE 未覆盖"刷新已完成" → 无 warning。
    PASS（修后）：扩展后匹配，且 T38 逻辑对"有 run_workflow 但仍声称完成"也 warn。
    """
    _, warns = sanitize_reply("库存刷新已完成，任务号 abc12345 已跑完。", ["run_workflow"])
    assert warns, (
        "有 run_workflow 但声称'刷新已完成'仍应被拦截，但没有 warning。\n"
        "原因：LLM 无 task readback，任何'完成'声明均是 hallucination。"
    )
    print(f"    warns={warns[0]!r}")


# ── Test 6: 正路无误报 — "已创建后台任务" 不被拦截 ──────────────────────────────

def test_task_created_wording_no_false_positive():
    """'已创建后台任务' / '任务已排队' 不触发 banner（正常受理回执）。

    修前修后均应 PASS（回归测试）。
    """
    for wording in [
        "已创建后台任务 abc12345，请在任务面板查看进度。",
        "库存刷新任务已排队，影响模块：库存快照。",
        "工作流 wf1_stock_v2 任务已创建（ID: abc12345），完成后请重新查询。",
    ]:
        _, warns = sanitize_reply(wording, ["run_workflow"])
        assert not any(
            "完成" in w and ("T38" in w or "重算" in w or "刷新" in w)
            for w in warns
        ), (
            f"误报：受理回执'{wording}'被误拦截: {warns}"
        )
    print("    正路无误报 ✓")


# ── Test 7: 扫描完成 无 run_workflow → 拦截 ────────────────────────────────────

def test_scan_complete_claim_blocked():
    """无 run_workflow 时"扫描已完成"应触发 banner。"""
    _, warns = sanitize_reply("ERP 库存扫描已完成，共处理 500 个 SKU。", [])
    assert warns, (
        "无 run_workflow 时'扫描已完成'应被拦截，但没有 warning。\n"
        "修前 FAIL 预期：_DONE_CLAIM_RE 未覆盖此短语。"
    )
    print(f"    warns={warns[0]!r}")


def _queued_event(task_id: str) -> dict:
    """Production spawn_task writes this durable event before init events."""
    return {
        "id": 1,
        "task_id": task_id,
        "step_no": 0,
        "step_name": "任务排队",
        "status": "queued",
        "message": "",
        "created_at": "2026-06-09T00:00:00",
    }


# ── Test 8: spawn 成功 + init event 写失败 → task_id 存在 + lifecycle_error ──────

def test_spawn_success_event_write_failure_partial_success():
    """spawn_task 成功但 init event 写失败时，必须返回 task_id + lifecycle_error。

    这是"部分成功"路径：task 已在 DB 中创建，但 init event 未写入。
    运营必须能拿到 task_id 去查询，而不是以为任务从未创建。

    FAIL（修前）：spawn_task 和 write_event 在同一个 try/except；
                  event 写失败 → 返回 creation_failed=True（无 task_id）。
    PASS（修后）：两个独立 try/except；event 写失败 → ok=True + task_id + lifecycle_error。
    """
    try:
        from hipop.server.agent import tool_run_workflow
    except ImportError as e:
        print(f"    SKIP (missing dep: {e})")
        return

    _fake_task_id = "test_tid_abc123"

    def _spawn_ok(*args, **kwargs):
        return _fake_task_id

    def _event_fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    raised = None
    result = None
    with patch("hipop.server.runtime.spawn_task", side_effect=_spawn_ok):
        with patch("hipop.server.agent._data") as mock_data:
            mock_data.set_current_tenant.return_value = None
            mock_data.write_event.side_effect = _event_fail
            mock_data.get_events_after.return_value = [_queued_event(_fake_task_id)]
            with patch("hipop.server.agent._get_tenant", return_value=1):
                try:
                    result = tool_run_workflow("wf1_stock_v2")
                except Exception as e:
                    raised = e

    assert raised is None, (
        f"tool_run_workflow 不应抛出异常: {type(raised).__name__}: {raised}"
    )
    assert result is not None, "应返回 dict，但返回了 None"
    assert result.get("task_id") == _fake_task_id, (
        f"部分成功路径：应返回已创建的 task_id={_fake_task_id!r}，"
        f"实际: {result}\n"
        "修前 FAIL 预期：spawn+event 同 try/except，event 失败 → creation_failed=True 无 task_id"
    )
    assert result.get("lifecycle_error"), (
        f"event 写失败时应有 lifecycle_error 字段说明原因: {result}"
    )
    assert result.get("creation_failed") is not True, (
        f"spawn 成功后 event 写失败不应标 creation_failed=True（任务已创建）: {result}"
    )
    print(f"    task_id={result.get('task_id')!r} lifecycle_error={result.get('lifecycle_error')!r}")


# ── Test 9: _exec_tool 路径：event store 宕机时 governance 审计失败不吞 task_id ────

def test_exec_tool_run_workflow_event_store_down_preserves_task_id():
    """走 _exec_tool → governance 真实路径：全局 event store 宕机时返回 task_id + lifecycle_error。

    测试的真实 chat 路径：
      _exec_tool → governance.propose_and_execute → execute_with_token
      → tool_run_workflow（spawn 成功，init event 写失败 → lifecycle_error）
      → governance.write_execution_record（也失败）
      → 必须返回 task_id，不能只返回 RuntimeError

    FAIL（修前 governance.py:515-517 无 try/except）：
      write_execution_record 抛 OperationalError → execute_with_token 无 catch →
      异常传到 _exec_tool line 1850 catch → {"error": "OperationalError: ..."} 无 task_id

    PASS（修后 governance.py:515-517 有 try/except）：
      write_execution_record 失败被捕获 → lifecycle_error 注入 result →
      返回 {"ok": True, "task_id": ..., "lifecycle_error": "..."} task_id 保留
    """
    try:
        from hipop.server.agent import _exec_tool
    except ImportError as e:
        print(f"    SKIP (missing dep: {e})")
        return

    _fake_task_id = "exec_tool_tid_abc123"

    def _spawn_ok(*args, **kwargs):
        return _fake_task_id

    def _event_fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    raised = None
    result = None
    with patch("hipop.server.runtime.spawn_task", side_effect=_spawn_ok):
        with patch("hipop.server.data.write_event", side_effect=_event_fail):
            with patch("hipop.server.data.get_events_after", return_value=[_queued_event(_fake_task_id)]):
                with patch("hipop.server.data.set_current_tenant"):
                    with patch("hipop.server.agent._get_tenant", return_value=1):
                        with patch("hipop.server.rbac.tool_allowed", return_value=True):
                            try:
                                result = _exec_tool(
                                    "run_workflow",
                                    {"workflow": "wf1_stock_v2"},
                                    user={"role": "manager", "id": "u1",
                                          "email": "t@t.com", "tenant_id": 1},
                                )
                            except Exception as e:
                                raised = e

    assert raised is None, (
        f"_exec_tool 不应抛出异常: {type(raised).__name__}: {raised}"
    )
    assert result is not None, "应返回 dict"
    assert result.get("task_id") == _fake_task_id, (
        f"event store 宕机时仍应返回已创建的 task_id={_fake_task_id!r}，实际: {result}\n"
        "修前 FAIL 预期：governance.write_execution_record 失败 → _exec_tool line 1850 "
        "catch → 只返回 {error: RuntimeError} 无 task_id"
    )
    assert result.get("lifecycle_error"), (
        f"应有 lifecycle_error 说明审计事件写失败原因: {result}"
    )
    assert "error" not in result or result.get("ok") is True, (
        f"结果不应是纯错误 dict（无 task_id 的 RuntimeError）: {result}"
    )
    print(f"    task_id={result.get('task_id')!r} lifecycle_error={result.get('lifecycle_error')!r}")


# ── Test 10: spawn_task() 内部 queued event 写失败 → 不留幽灵任务（WS-141 根因门）──

def test_spawn_task_queued_event_failure_no_ghost_task():
    """WS-141 Round 4 根因门：spawn_task() 内部 queued event 写失败时不得留幽灵任务。

    根因（WS-132 Round 3 红队）：spawn_task() 先 commit `tasks` INSERT，再单独开连接
    写 queued event；若 queued event 写失败，spawn_task 抛异常、tool_run_workflow 返回
    creation_failed=True 不给 task_id，但 DB 里已残留 state=queued 且 agent_events=[]
    的幽灵行（运营无法回读，重试又造重复孤儿）。

    本测试直接打 spawn_task() 内部路径（mock event store / write_event 失败），不 mock
    掉整个 spawn_task。修法 Option A（原子事务）下：

    FAIL（修前 HEAD 5d4c1d6）：INSERT 先 commit → write_event 失败 → spawn_task 抛异常，
      但 `tasks` 表残留 state=queued、agent_events=[] 的幽灵行（断言 `not rows` 失败）。
    PASS（修后 Option A）：INSERT + queued event 同事务，event 写失败回滚 INSERT →
      spawn_task 抛异常且 `tasks` 表无任何 task row（真的没建）。
    """
    import tempfile
    import shutil
    from pathlib import Path as _Path
    from hipop.server import data as _data

    # PG 模式下本测试的 sqlite 事务语义不适用，跳过（CI 默认 sqlite）
    if _data.is_postgres():
        print("    SKIP (postgres mode — sqlite 事务语义专项)")
        return

    from hipop.server import runtime as _runtime

    tmpdir = tempfile.mkdtemp(prefix="ws141_ghost_")
    db_path = os.path.join(tmpdir, "ghost.db")
    tasks_root = os.path.join(tmpdir, "tasks")
    raised = None
    try:
        with patch.object(_data, "DB_PATH", db_path), \
             patch.object(_data, "_task_tables_checked", False), \
             patch.object(_runtime, "TASKS_ROOT", _Path(tasks_root)):
            _data.set_current_tenant(1)
            _data._ensure_task_tables()

            def _event_fail(*a, **k):
                raise sqlite3.OperationalError("event store down")

            # mock 整个 event store 写入（spawn_task 内部 queued event 走 write_event）
            with patch("hipop.server.data.write_event", side_effect=_event_fail):
                try:
                    _runtime.spawn_task(
                        workflow="wf1_stock_v2", tenant_id=1,
                        actor={"source": "chat", "email": "t@t.com"},
                    )
                except Exception as e:
                    raised = e

            # Option A：queued event 写失败 → spawn_task 必抛（任务真没建）
            assert raised is not None, (
                "queued event 写失败时 spawn_task 应抛异常（Option A：任务未创建）"
            )

            # 关键断言：DB 里没有任何 task row（无幽灵任务）
            _data.set_current_tenant(1)
            rows = _data._fetch("SELECT task_id, state FROM tasks")
            ghost = [r for r in rows if r.get("state") in ("queued", "running")]
            assert not ghost, (
                f"queued event 写失败后残留幽灵任务（有行无事件）: {ghost}\n"
                "修前 FAIL 预期：INSERT 先 commit → event 失败 → tasks 表残留 state=queued 幽灵行。\n"
                "修后 Option A：INSERT 与 queued event 同事务，event 失败回滚 INSERT。"
            )
            assert not rows, (
                f"事务回滚后 tasks 表应为空，实际残留: {rows}"
            )
            print(f"    spawn raised={type(raised).__name__}; tasks 表为空（无幽灵任务）✓")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_lifecycle_gate — WS-132 工作流生命周期门（task创建失败路径 + 假完成拦截）")

    tests = [
        ("test_spawn_failure_returns_ok_false", test_spawn_failure_returns_ok_false),
        ("test_refresh_done_claim_blocked_no_run_workflow", test_refresh_done_claim_blocked_no_run_workflow),
        ("test_refresh_done_slang_blocked", test_refresh_done_slang_blocked),
        ("test_scan_done_claim_blocked", test_scan_done_claim_blocked),
        ("test_refresh_done_with_run_workflow_still_warned", test_refresh_done_with_run_workflow_still_warned),
        ("test_task_created_wording_no_false_positive", test_task_created_wording_no_false_positive),
        ("test_scan_complete_claim_blocked", test_scan_complete_claim_blocked),
        ("test_spawn_success_event_write_failure_partial_success", test_spawn_success_event_write_failure_partial_success),
        ("test_exec_tool_run_workflow_event_store_down_preserves_task_id", test_exec_tool_run_workflow_event_store_down_preserves_task_id),
        ("test_spawn_task_queued_event_failure_no_ghost_task", test_spawn_task_queued_event_failure_no_ghost_task),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            first_line = str(e).split("\n")[0]
            print(f"  ✗ {name}: {first_line}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_lifecycle_gate all {len(tests)} passed")

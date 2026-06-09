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

"""smoke_wf3_logistics_t21.py — T21-SUB-3 物流入口专项 smoke + 降级

验收（WS-101）：
  1. T21 原句经确定性路由触发 wf3_logistics_v2
  2. spawn_task(wf3_logistics_v2) → task row + ≥1 queued/started 事件
  3. _logistics_task_evidence_check：证据完整返回 None（无降级）
  4. _logistics_task_evidence_check：事件缺失返回「未确认创建成功」降级消息
  5. _logistics_task_evidence_check：任务表查询抛错也返回降级消息
  6. _logistics_task_evidence_check：孤儿事件（agent_events 有记录但无 tasks 行）也降级

fail-then-pass：
  FAIL（修前）：direct_workflow 路径说「已触发」但不查证据 → 缺少降级路径；
               _logistics_task_evidence_check 只查 get_events_after，孤儿事件误放行
  PASS（修后）：_logistics_task_evidence_check 改用 get_task_with_events，
               同时核 task row 和 events；降级路径有完整测试覆盖。

读写边界：
  读：hipop.server.data.get_task_with_events / agent._logistics_task_evidence_check
  读：hipop.server.runtime.spawn_task（只验 task row + event 写入，不真跑 worker）
  写：Test 6 直接插入 agent_events 孤儿记录（仅测试 DB，不触发业务逻辑）
"""

import sys
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import data as _data
from hipop.server import runtime as _runtime

_TMP_DB = None
_TEST_ACTOR = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}


def _setup():
    """Bootstrap temp SQLite DB + tenant context.

    CI-safe: 与 smoke_task_evidence.py 相同套路，不依赖生产 DB_PATH。
    """
    global _TMP_DB
    if _TMP_DB is None:
        _TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _TMP_DB.close()
    _data.DB_PATH = _TMP_DB.name
    _data._task_tables_checked = False
    _data._ensure_task_tables()
    _data.set_current_tenant(1)


# ──────────────────────────────────────────────────────────────
# Test 1: T21 原句路由
# ──────────────────────────────────────────────────────────────

def test_t21_message_routes_to_wf3_logistics_v2():
    """T21 原句（含「扫」+「物流」）确定性路由到 wf3_logistics_v2。

    FAIL（修前）：此测试与实现无关，始终应 PASS（路由本身已有）。
    PASS（修后）：验证 smoke 使用的原句确实命中物流入口，不依赖 LLM。
    """
    from hipop.server.agent import _deterministic_workflow_request
    # T21 原句（含刷新触发词「扫」+ 模块词「物流」）
    t21_msg = "请帮我扫一下 ERP 物流信息，并告诉我是否真的创建了后台任务。"
    result = _deterministic_workflow_request(t21_msg)
    assert result is not None, (
        f"T21 原句未被路由到直接 workflow 触发路径: {t21_msg!r}"
    )
    assert result["workflow"] == "wf3_logistics_v2", (
        f"T21 原句路由到了错误 workflow: {result['workflow']}，期望 wf3_logistics_v2"
    )
    assert result["label"] == "物流刷新", f"label 不符: {result['label']}"
    print(f"    T21 原句 → workflow={result['workflow']} label={result['label']}")


# ──────────────────────────────────────────────────────────────
# Test 2: spawn_task 落 task row + ≥1 event（wf3_logistics_v2 专项）
# ──────────────────────────────────────────────────────────────

def test_spawn_wf3_logistics_task_evidence():
    """spawn_task(wf3_logistics_v2) 同步落 task row + ≥1 queued/started 事件。

    FAIL（修前）：SUB-1 修复前 spawn_task 无 queued event → events 为空。
    PASS（修后）：SUB-1 已修复，wf3_logistics_v2 同样受益。
    """
    task_id = _runtime.spawn_task("wf3_logistics_v2", tenant_id=1, actor=_TEST_ACTOR)

    # 断言 1: task row 存在
    task = _runtime.task_status(task_id)
    assert task is not None, f"tasks 行不存在 task_id={task_id}"
    assert task["workflow"] == "wf3_logistics_v2", f"workflow 不符: {task['workflow']}"

    # 断言 2: ≥1 queued/started event 立即可读（durable 证据链）
    events = _data.get_events_after(task_id, 0)
    assert len(events) >= 1, (
        f"events_after 为空 task_id={task_id}（wf3_logistics_v2 无 durable 任务证据）\n"
        f"task state={task.get('state')}"
    )
    statuses = [e["status"] for e in events]
    assert any(s in ("queued", "started", "done") for s in statuses), (
        f"无 queued/started/done 事件，只有: {statuses}"
    )
    print(f"    task_id={task_id} state={task['state']} events={statuses}")


# ──────────────────────────────────────────────────────────────
# Test 3: 降级检查 — 证据完整返回 None
# ──────────────────────────────────────────────────────────────

def test_logistics_evidence_check_passes_with_good_evidence():
    """_logistics_task_evidence_check：spawn_task 产生的 task 有事件 → 返回 None（无降级）。

    FAIL（修前）：函数不存在 → AttributeError。
    PASS（修后）：证据完整时返回 None，不触发降级。
    """
    from hipop.server.agent import _logistics_task_evidence_check
    task_id = _runtime.spawn_task("wf3_logistics_v2", tenant_id=1, actor=_TEST_ACTOR)
    result = _logistics_task_evidence_check(task_id)
    assert result is None, (
        f"证据完整时 _logistics_task_evidence_check 应返回 None，实际返回: {result!r}"
    )
    print(f"    task_id={task_id} → no degradation (None) ✓")


# ──────────────────────────────────────────────────────────────
# Test 4: 降级检查 — 事件缺失返回「未确认创建成功」
# ──────────────────────────────────────────────────────────────

def test_logistics_evidence_check_degrades_on_missing_events():
    """_logistics_task_evidence_check：不存在的 task_id（无事件）→ 返回降级消息。

    这覆盖「任务表有行但 agent_events 为空」和「task_id 根本不存在」两种缺失场景
    （两者对 get_events_after 的返回值相同：空列表）。

    FAIL（修前）：函数不存在 → AttributeError。
    PASS（修后）：返回含「未确认创建成功」的降级字符串，而非 None。
    """
    from hipop.server.agent import _logistics_task_evidence_check
    # 用一个从未经 spawn_task 的假 task_id，确保 agent_events 为空
    degrade_msg = _logistics_task_evidence_check("fake-task-id-no-events")
    assert degrade_msg is not None, (
        "事件缺失时 _logistics_task_evidence_check 应返回降级消息，实际返回 None"
    )
    assert "未确认创建成功" in degrade_msg, (
        f"降级消息必须包含「未确认创建成功」，实际: {degrade_msg!r}"
    )
    print(f"    fake task_id → degraded: {degrade_msg!r}")


# ──────────────────────────────────────────────────────────────
# Test 5: 降级检查 — DB 查询抛错也返回降级（不抛出异常冒充成功）
# ──────────────────────────────────────────────────────────────

def test_logistics_evidence_check_degrades_on_db_error():
    """_logistics_task_evidence_check：data 层抛错 → 捕获并返回降级消息，不抛穿。

    FAIL（修前）：函数不存在 → AttributeError。
    PASS（修后）：DB 异常被捕获，返回含「未确认创建成功」的消息，绝不冒充成功。
    """
    from hipop.server.agent import _logistics_task_evidence_check
    import unittest.mock as mock

    # patch data.get_task_with_events 使其抛错，模拟 DB 异常
    with mock.patch.object(_data, "get_task_with_events", side_effect=RuntimeError("DB error")):
        degrade_msg = _logistics_task_evidence_check("any-task-id")

    assert degrade_msg is not None, (
        "DB 报错时 _logistics_task_evidence_check 应返回降级消息，实际返回 None"
    )
    assert "未确认创建成功" in degrade_msg, (
        f"降级消息必须包含「未确认创建成功」，实际: {degrade_msg!r}"
    )
    print(f"    DB error → degraded: {degrade_msg!r}")


# ──────────────────────────────────────────────────────────────
# Test 6: 降级检查 — 孤儿事件（agent_events 有记录但 tasks 行不存在）
# ──────────────────────────────────────────────────────────────

def test_logistics_evidence_check_degrades_on_orphan_events():
    """_logistics_task_evidence_check：agent_events 有记录但 tasks 行不存在（孤儿事件）→ 降级。

    红队场景：直接向 agent_events 插入一条记录，不经过 spawn_task，tasks 表无对应行。
    修前（只用 get_events_after）：events 非空 → 误返回 None（假成功放行）。
    修后（改用 get_task_with_events）：task row 不存在 → 返回降级消息。

    FAIL（修前）：get_events_after 找到孤儿事件，返回 None 放行假成功。
    PASS（修后）：get_task_with_events 返回 None，触发「任务行不存在」降级。
    """
    import sqlite3
    from hipop.server.agent import _logistics_task_evidence_check

    orphan_task_id = "orphan-task-id-no-tasks-row"

    # 直接向 agent_events 写孤儿记录（tasks 表无对应行）
    conn = sqlite3.connect(_data.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO agent_events (task_id, step_no, step_name, status, tenant_id) "
            "VALUES (?, 1, 'orphan_step', 'queued', 1)",
            (orphan_task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    degrade_msg = _logistics_task_evidence_check(orphan_task_id)
    assert degrade_msg is not None, (
        "孤儿事件（有 agent_events 无 tasks 行）时应返回降级消息，实际返回 None（假成功放行）"
    )
    assert "未确认创建成功" in degrade_msg, (
        f"降级消息必须包含「未确认创建成功」，实际: {degrade_msg!r}"
    )
    print(f"    orphan event → degraded: {degrade_msg!r}")


if __name__ == "__main__":
    print("▶ smoke_wf3_logistics_t21 — T21-SUB-3 物流入口专项 smoke + 降级")
    _setup()

    tests = [
        ("test_t21_message_routes_to_wf3_logistics_v2",
         test_t21_message_routes_to_wf3_logistics_v2),
        ("test_spawn_wf3_logistics_task_evidence",
         test_spawn_wf3_logistics_task_evidence),
        ("test_logistics_evidence_check_passes_with_good_evidence",
         test_logistics_evidence_check_passes_with_good_evidence),
        ("test_logistics_evidence_check_degrades_on_missing_events",
         test_logistics_evidence_check_degrades_on_missing_events),
        ("test_logistics_evidence_check_degrades_on_db_error",
         test_logistics_evidence_check_degrades_on_db_error),
        ("test_logistics_evidence_check_degrades_on_orphan_events",
         test_logistics_evidence_check_degrades_on_orphan_events),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            import traceback
            print(f"  ✗ {name}: {e}")
            traceback.print_exc()
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_wf3_logistics_t21 all {len(tests)} passed")

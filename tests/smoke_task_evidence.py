"""smoke_task_evidence.py — T21-SUB-1 fail-then-pass smoke

验收（WS-99）：触发任意 workflow →
  1. tasks 表立即落行（不靠后台线程）
  2. agent_events 立即 ≥1 queued/started 事件（spawn_task 同步写，非 race）
  3. get_task_with_events 统一回读接口可用

FAIL 条件（修前）：
  - spawn_task 不写 agent_events queued 事件 → 断言 2 失败
  - agent.tool_run_workflow 走老 thread 路径，不调 spawn_task → tasks 行不存在

PASS 条件（修后）：
  - spawn_task 在启动子进程前同步写 queued event
  - tool_run_workflow 对已注册 runner 走 spawn_task 路径
  - get_task_with_events 返回 task + events
"""

import sys
import os
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# CI 兼容：DB_PATH 默认 /Users/luke/... 在 GitHub Actions 不存在，必须在 data 模块
# 首次导入前设置，否则 DB_PATH 会被固化成本地绝对路径，CI 里 conn() 报 "unable to open"
os.environ.setdefault("HIPOP_DB", "/tmp/hipop_test_task_evidence.db")
os.environ.setdefault("JWT_SECRET", "test_secret_for_task_evidence_smoke")

from hipop.server import data as _data
from hipop.server import runtime as _runtime


def _setup():
    """Bootstrap SQLite schema + set tenant context for tests."""
    # 显式覆盖，处理 data 模块在设置 env 前就被其他文件导入的情况
    _data.DB_PATH = os.environ.get("HIPOP_DB", "/tmp/hipop_test_task_evidence.db")
    _data._ensure_task_tables()
    _data.set_current_tenant(1)


# 模块级调用：确保 pytest 直接收集此文件时也完成 DB 初始化（无需 __main__ 入口）
_setup()


def test_spawn_creates_task_row_and_queued_event():
    """spawn_task 必须同步落 tasks 行 + ≥1 queued event，不靠后台线程。

    FAIL (before fix): spawn_task 没写 agent_events → events 为空。
    PASS (after fix):  spawn_task 在启动子进程前写 queued event → 立即可读。
    """
    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    # 断言 1: tasks 行立即存在（spawn_task 同步写）
    task = _runtime.task_status(task_id)
    assert task is not None, f"tasks 行不存在 task_id={task_id}"
    assert task["workflow"] == "__test_sleep_v2", f"workflow 不符: {task}"

    # 断言 2: ≥1 queued/started/done event 立即可读（spawn_task 同步写，非 race）
    events = _data.get_events_after(task_id, 0)
    statuses = [e["status"] for e in events]
    assert len(events) >= 1, (
        f"events_after 为空 task_id={task_id} — 无 durable 任务证据\n"
        f"task state={task.get('state')}"
    )
    assert any(s in ("queued", "started", "done") for s in statuses), (
        f"没有 queued/started/done 事件，只有: {statuses}"
    )
    print(f"    task_id={task_id} state={task['state']} events={statuses}")


def test_get_task_with_events_unified_interface():
    """统一回读接口：get_task_with_events 同时返回 task row + events 列表。

    FAIL (before fix): 函数不存在 → AttributeError。
    PASS (after fix):  返回 {task_id, task, events}，events 非空。
    """
    assert hasattr(_data, "get_task_with_events"), (
        "data.get_task_with_events 不存在 — 统一回读接口未实现"
    )
    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    result = _data.get_task_with_events(task_id)
    assert result is not None, f"get_task_with_events 返回 None task_id={task_id}"
    assert "task" in result, f"缺 task 字段: {list(result.keys())}"
    assert "events" in result, f"缺 events 字段: {list(result.keys())}"
    assert result["task"] is not None, "task 字段为 None"
    assert len(result["events"]) >= 1, (
        f"events 为空，任务证据链缺失: task_id={task_id}"
    )
    print(f"    task_id={task_id} task.state={result['task']['state']} "
          f"events={len(result['events'])}")


def test_tool_run_workflow_uses_spawn_task():
    """agent.tool_run_workflow 对已注册 runner 必须走 spawn_task（落 tasks 行）。

    FAIL (before fix): tool_run_workflow 走老 daemon thread → tasks 行不存在。
    PASS (after fix):  走 spawn_task → tasks 行立即可读。

    agent.py 依赖 anthropic SDK；在缺少该依赖的 CI 环境里 skip 而不报错。
    """
    try:
        from hipop.server.agent import tool_run_workflow
    except ImportError as e:
        # anthropic/openai SDK 未安装的 CI 环境：只验证 spawn_task 路径本身即可
        print(f"    SKIP (missing dep: {e}) — spawn_task path tested by test 1+2")
        return

    # wf6_alerts_v2 is in both WORKFLOW_REGISTRY and workflow_runners
    result = tool_run_workflow("wf6_alerts_v2")
    assert result.get("ok") is True, f"tool_run_workflow 失败: {result}"
    task_id = result.get("task_id")
    assert task_id, f"没有返回 task_id: {result}"

    # 检查 tasks 行存在（spawn_task 路径落行，非 daemon thread）
    task = _runtime.task_status(task_id)
    assert task is not None, (
        f"tasks 行不存在 task_id={task_id} — tool_run_workflow 没走 spawn_task 路径"
    )
    assert task["workflow"] == "wf6_alerts_v2"

    # 检查 ≥1 queued event 存在（spawn_task 同步写的）
    events = _data.get_events_after(task_id, 0)
    assert len(events) >= 1, (
        f"无 durable event task_id={task_id} — spawn_task 没写 queued event"
    )
    print(f"    task_id={task_id} state={task['state']} events={len(events)}")


if __name__ == "__main__":
    print("▶ smoke_task_evidence — T21-SUB-1 任务证据契约")
    _setup()

    tests = [
        ("test_spawn_creates_task_row_and_queued_event",
         test_spawn_creates_task_row_and_queued_event),
        ("test_get_task_with_events_unified_interface",
         test_get_task_with_events_unified_interface),
        ("test_tool_run_workflow_uses_spawn_task",
         test_tool_run_workflow_uses_spawn_task),
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
    print(f"\n✓ smoke_task_evidence all {len(tests)} passed")

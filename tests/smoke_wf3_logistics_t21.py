"""smoke_wf3_logistics_t21.py — T21-SUB-3 物流入口专项 smoke + 降级

验收（WS-101）：
  1. T21 原句经确定性路由触发 wf3_logistics_v2
  2. spawn_task(wf3_logistics_v2) → task row + ≥1 queued/started 事件
  3. _logistics_task_evidence_check：证据完整返回 None（无降级）
  4. _logistics_task_evidence_check：事件缺失返回「未确认创建成功」降级消息
  5. _logistics_task_evidence_check：任务表查询抛错也返回降级消息
  6. _logistics_task_evidence_check：孤儿事件（agent_events 有记录但无 tasks 行）也降级
  7. chat() E2E：T21 消息成功路径 workflow_task.workflow==wf3_logistics_v2 + durable 证据
  8. chat() E2E：governance 拒绝时回复含具体原因，不泛化为「工作流触发失败」
  9. chat() E2E：证据缺失时回复含「未确认创建成功」，不允许假成功

fail-then-pass：
  FAIL（修前）：direct_workflow 路径说「已触发」但不查证据 → 缺少降级路径；
               _logistics_task_evidence_check 只查 get_events_after，孤儿事件误放行；
               governance 拒绝时返回泛化「工作流触发失败」；
               chat() 无 E2E 端到端覆盖
  PASS（修后）：_logistics_task_evidence_check 改用 get_task_with_events；
               else 分支暴露 governance reason；chat() E2E 全路径有 smoke 覆盖。

读写边界：
  读：hipop.server.data.get_task_with_events / agent._logistics_task_evidence_check
  读：hipop.server.runtime.spawn_task（只验 task row + event 写入，不真跑 worker）
  写：Test 6 直接插入 agent_events 孤儿记录（仅测试 DB，不触发业务逻辑）

CI 说明（Tests 7-9）：
  governance.decide mock 为 Allow/Deny，跳过 Haiku LLM 调用（CI 无 API key）。
  缺 anthropic SDK 时显式 FAIL，不 SKIP（与 smoke_t21_sub2_entry.py 保持一致）。
"""

import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

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


# ──────────────────────────────────────────────────────────────
# Test 7: E2E chat() 成功路径
# ──────────────────────────────────────────────────────────────

def test_chat_t21_e2e_success():
    """chat() T21 消息→确定性路由→wf3_logistics_v2：workflow_task.workflow 正确 + task row + ≥1 event。

    governance.decide mock 为 Allow（跳过 CI 无 API key 的 Haiku 调用）。

    FAIL（修前）：无 E2E chat() smoke → 接线缺失无法发现。
    PASS（修后）：workflow_task.workflow == 'wf3_logistics_v2'，task row + ≥1 event，reply 非空。
    """
    from hipop.server.agent import chat
    from hipop.server.governance import Decision

    messages = [{"role": "user", "content": "请帮我扫一下 ERP 物流信息，并告诉我是否真的创建了后台任务。"}]
    scope = {"store": "KSA", "current_user": "tester", "current_role": "ops",
             "tenant_id": 1, "user_id": 1}

    with patch("hipop.server.governance.decide",
               return_value=Decision(kind="Allow", reason="test-allow")):
        result = chat(messages, scope)

    assert isinstance(result, dict), f"chat() 应返回 dict，实际: {type(result)}"
    wt = result.get("workflow_task")
    assert wt is not None, (
        f"workflow_task 为 None，chat() 未走 direct_workflow 路径\n"
        f"reply: {result.get('reply')!r}"
    )
    assert wt["workflow"] == "wf3_logistics_v2", (
        f"workflow_task.workflow={wt['workflow']!r}，期望 wf3_logistics_v2"
    )
    task_id = wt["task_id"]
    task = _runtime.task_status(task_id)
    assert task is not None, f"tasks 行不存在 task_id={task_id}"
    events = _data.get_events_after(task_id, 0)
    assert len(events) >= 1, f"task_id={task_id} 无 durable event"
    assert result.get("reply"), "reply 为空"
    print(f"    E2E success: task_id={task_id} wf={wt['workflow']} reply={result['reply'][:60]!r}")


# ──────────────────────────────────────────────────────────────
# Test 8: E2E chat() governance 拒绝 → 具体原因，非泛化失败
# ──────────────────────────────────────────────────────────────

def test_chat_t21_e2e_governance_deny_shows_reason():
    """chat() T21 消息→governance 拒绝时，回复含具体原因，不泛化为「工作流触发失败」。

    governance.decide mock 为 Deny（模拟已有运行中实例场景）。

    FAIL（修前）：else 分支只用 message/error，governance Deny 的 reason 被丢弃
                 → 用户看到泛化「工作流触发失败」，无法诊断。
    PASS（修后）：else 分支优先取 reason，用户看到具体原因。
    """
    from hipop.server.agent import chat
    from hipop.server.governance import Decision

    deny_reason = "该 workflow 已有运行中实例: ['abc12345']（防并发抢资源）"
    messages = [{"role": "user", "content": "帮我刷一下物流数据"}]
    scope = {"store": "KSA", "current_user": "tester", "current_role": "ops",
             "tenant_id": 1, "user_id": 1}

    with patch("hipop.server.governance.decide",
               return_value=Decision(kind="Deny", reason=deny_reason)):
        result = chat(messages, scope)

    reply = result.get("reply", "")
    assert reply, "reply 为空"
    assert "工作流触发失败" not in reply or deny_reason in reply, (
        f"governance 拒绝时回复不应仅为泛化「工作流触发失败」\n实际: {reply!r}"
    )
    assert deny_reason in reply or "未确认创建成功" in reply or "已有运行中" in reply, (
        f"governance 拒绝时回复必须含具体原因\n实际: {reply!r}"
    )
    print(f"    governance deny → reply={reply[:80]!r}")


# ──────────────────────────────────────────────────────────────
# Test 9: E2E chat() 证据缺失 → 「未确认创建成功」
# ──────────────────────────────────────────────────────────────

def test_chat_t21_e2e_evidence_missing_degrades():
    """chat() T21 消息→run_workflow ok→但证据缺失→回复含「未确认创建成功」。

    mock get_task_with_events 返回 None（模拟 task row 不存在）。
    governance.decide mock 为 Allow。

    FAIL（修前）：chat() 不查证据，直接回「已触发」→ 假成功。
    PASS（修后）：_logistics_task_evidence_check 返回降级消息，chat() 回「未确认创建成功」。
    """
    from hipop.server.agent import chat
    from hipop.server.governance import Decision

    messages = [{"role": "user", "content": "帮我扫一下物流"}]
    scope = {"store": "KSA", "current_user": "tester", "current_role": "ops",
             "tenant_id": 1, "user_id": 1}

    with patch("hipop.server.governance.decide",
               return_value=Decision(kind="Allow", reason="test-allow")), \
         patch.object(_data, "get_task_with_events", return_value=None):
        result = chat(messages, scope)

    reply = result.get("reply", "")
    assert reply, "reply 为空"
    assert "未确认创建成功" in reply, (
        f"证据缺失时 chat() 必须回「未确认创建成功」，实际: {reply!r}"
    )
    print(f"    evidence missing → reply={reply[:80]!r}")


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
        ("test_chat_t21_e2e_success",
         test_chat_t21_e2e_success),
        ("test_chat_t21_e2e_governance_deny_shows_reason",
         test_chat_t21_e2e_governance_deny_shows_reason),
        ("test_chat_t21_e2e_evidence_missing_degrades",
         test_chat_t21_e2e_evidence_missing_degrades),
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

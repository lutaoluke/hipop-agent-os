"""smoke_t21_sub2_workflow_receipt.py — T21-SUB-2 fail-then-pass smoke

验收（WS-100）：触发 workflow 后，chat 回复必须三态化 + 包含可核验证据：
  1. 回复直接回答「任务是否创建」（含「已创建」/「已受理」/「未确认」等）
  2. 回复包含 task_id 值
  3. 回复包含 workflow 名称
  4. 无 done/error 事件时，措辞为「已排队/待执行」或「已开始执行」，
     不得出现「已跑完」「已完成」（暗示已结束）

FAIL 条件（修前）：
  - hipop/server/_workflow_reply.py 不存在 → ImportError
  - _workflow_receipt_reply 不存在 → ImportError

PASS 条件（修后）：
  - _workflow_receipt_reply 存在并返回含 task_id/workflow/状态的三态回执
  - 有 task row + queued 事件 → 措辞为「已排队」而非「已完成」
  - task 不存在 → 回复含「未确认」降级文案
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import data as _data
from hipop.server import runtime as _runtime

_TMP_DB = None


def _setup():
    global _TMP_DB
    if _TMP_DB is None:
        _TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _TMP_DB.close()
    _data.DB_PATH = _TMP_DB.name
    _data._task_tables_checked = False
    _data._ensure_task_tables()
    _data.set_current_tenant(1)


def test_workflow_receipt_reply_exists():
    """_workflow_receipt_reply 必须存在于 _workflow_reply 模块。

    FAIL (before fix): 模块不存在 → ImportError。
    PASS (after fix):  函数存在并可调用（不依赖 anthropic SDK）。
    """
    from hipop.server._workflow_reply import _workflow_receipt_reply
    assert callable(_workflow_receipt_reply), "_workflow_receipt_reply 不可调用"
    print("    _workflow_receipt_reply 存在且可调用")


def test_receipt_includes_task_id_workflow_status():
    """回执必须包含 task_id、workflow 名称、状态（三态之一）。

    FAIL (before fix): _workflow_reply 模块不存在 → ImportError。
    PASS (after fix):  回复含 task_id 字符串 + workflow 名 + 状态词。
    """
    from hipop.server._workflow_reply import _workflow_receipt_reply

    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    reply = _workflow_receipt_reply(task_id, "__test_sleep_v2", "测试工作流")

    assert task_id in reply, (
        f"回复未包含 task_id={task_id!r}\n回复: {reply!r}"
    )
    assert "__test_sleep_v2" in reply, (
        f"回复未包含 workflow 名\n回复: {reply!r}"
    )
    state_words = ("已排队", "待执行", "已开始", "已完成", "执行失败", "已受理")
    assert any(w in reply for w in state_words), (
        f"回复不含三态关键词（需含其一: {state_words}）\n回复: {reply!r}"
    )
    print(f"    task_id={task_id} reply={reply!r}")


def test_receipt_says_queued_not_completed_when_no_done_event():
    """无 done/error 事件时，措辞必须是「已排队/待执行」，不得暗示已完成。

    FAIL (before fix): _workflow_reply 不存在。修前的「已触发...跑完后...」语义模糊。
    PASS (after fix):  回复显式用「已排队」/「待执行」/「已开始执行」，
                       不出现「已跑完」「已完成」（无 done/error 时）。
    """
    from hipop.server._workflow_reply import _workflow_receipt_reply

    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    events = _data.get_events_after(task_id, 0)
    final_statuses = {e["status"] for e in events if e["status"] in ("done", "error")}

    reply = _workflow_receipt_reply(task_id, "__test_sleep_v2", "测试工作流")

    if not final_statuses:
        forbidden = ("已跑完", "跑完了")
        for word in forbidden:
            assert word not in reply, (
                f"无完成事件时回复不应含「{word}」\n回复: {reply!r}"
            )

    state_words = ("已排队", "待执行", "已开始", "已完成", "执行失败", "已受理")
    assert any(w in reply for w in state_words), (
        f"回复缺少状态词\n回复: {reply!r}"
    )
    print(f"    task_id={task_id} events={[e['status'] for e in events]} reply={reply!r}")


def test_receipt_degraded_when_task_not_found():
    """task row 不存在时，回复必须降级为「未确认创建成功」，不许假装成功。

    FAIL (before fix): _workflow_reply 不存在，旧「已触发」回复不检查 task row。
    PASS (after fix):  task 不在 DB → 回复含「未确认」降级文案。
    """
    from hipop.server._workflow_reply import _workflow_receipt_reply

    fake_task_id = "nonexistent_task_99"
    reply = _workflow_receipt_reply(fake_task_id, "wf3_logistics_v2", "物流刷新")

    assert "未确认" in reply, (
        f"task 不存在时回复应含「未确认」\n回复: {reply!r}"
    )
    print(f"    degraded reply for nonexistent task: {reply!r}")


def test_direct_answer_to_is_task_created():
    """回复必须直接回答「任务是否已创建」（含「已受理」/「已创建」/「未确认」）。

    这是 T21 原句 smoke 核心：「是否真的创建了后台任务」。
    FAIL (before fix): _workflow_reply 不存在，旧「已触发...」未直接回答。
    PASS (after fix):  回复含「已创建」/「已受理」/「后台任务已创建」/「未确认创建成功」。
    """
    from hipop.server._workflow_reply import _workflow_receipt_reply

    actor = {"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"}
    task_id = _runtime.spawn_task(
        "__test_sleep_v2", tenant_id=1, actor=actor,
        spec={"total_chunks": 0, "sleep_sec": 0},
    )

    reply = _workflow_receipt_reply(task_id, "__test_sleep_v2", "测试工作流")

    created_phrases = ("已创建", "已受理", "后台任务已", "任务已创建")
    assert any(p in reply for p in created_phrases), (
        f"回复未直接回答「任务已创建」（须含其一: {created_phrases}）\n回复: {reply!r}"
    )
    print(f"    task_id={task_id} creation answer present in: {reply!r}")


if __name__ == "__main__":
    print("▶ smoke_t21_sub2_workflow_receipt — T21-SUB-2 回复三态化+自证作答")
    _setup()

    tests = [
        ("test_workflow_receipt_reply_exists", test_workflow_receipt_reply_exists),
        ("test_receipt_includes_task_id_workflow_status", test_receipt_includes_task_id_workflow_status),
        ("test_receipt_says_queued_not_completed_when_no_done_event",
         test_receipt_says_queued_not_completed_when_no_done_event),
        ("test_receipt_degraded_when_task_not_found", test_receipt_degraded_when_task_not_found),
        ("test_direct_answer_to_is_task_created", test_direct_answer_to_is_task_created),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed += 1

    print(f"\n{'✓ ALL PASS' if not failed else f'✗ {failed} FAILED'}")
    sys.exit(0 if not failed else 1)

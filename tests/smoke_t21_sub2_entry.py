"""smoke_t21_sub2_entry.py — T21-SUB-2 entry-path fail-then-pass smoke

验收（WS-100）：chat() 确定性路由触发 wf3_logistics_v2 后，回复必须三态化：
  1. 含 task_id（≥6位十六进制）
  2. 含 workflow 名称（wf3_logistics_v2）
  3. 含三态状态词（已排队/待执行/已开始/已完成/执行失败/已受理）
  4. 无完成事件时不出现「已跑完」/「跑完了」（不许暗示已完成）

FAIL 条件（修前）：
  chat() 回复 「已触发物流刷新（wf3_logistics_v2）。跑完后我会...」
  - 不含 task_id → 断言 1 失败
  - 不含三态状态词 → 断言 3 失败

PASS 条件（修后）：
  chat() 回复含 task_id + wf3_logistics_v2 + 状态词（三态之一）
  且无「已跑完」/「跑完了」
"""

import sys
import re
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import data as _data

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


def test_chat_entry_t21_workflow_receipt():
    """chat() 物流消息走确定性路由，回复必须含 task_id + workflow_name + 状态词。

    FAIL (before fix): 回复「已触发物流刷新…跑完后…」— 无 task_id，无状态词。
    PASS (after fix):  三态回执含 task_id、wf3_logistics_v2、已排队/已受理等状态词。

    agent.py 依赖 anthropic SDK；在缺少该依赖的 CI 环境里 skip 而不报错。
    """
    try:
        from hipop.server.agent import chat
    except ImportError as e:
        print(f"    SKIP (missing dep: {e})")
        return

    messages = [{"role": "user", "content": "请帮我扫一下 ERP 物流信息，并告诉我是否真的创建了后台任务。"}]
    # HTTP API overrides current_role with auth user's English role; use "ops" directly here.
    scope = {"store": "KSA", "current_user": "tester", "current_role": "ops", "tenant_id": 1}

    result = chat(messages, scope)
    assert isinstance(result, dict), f"chat() 应返回 dict，实际: {type(result)}"
    reply = result.get("reply", "")
    assert reply, f"chat() 回复为空: result={result}"

    # ① 含 task_id（6-8 位十六进制）
    assert re.search(r"[0-9a-f]{6,8}", reply), (
        f"回复未包含 task_id（6-8位十六进制）\n"
        f"回复: {reply!r}\n"
        f"(FAIL 期望: 「已触发...跑完后...」无 task_id；PASS 期望: 三态回执含 task_id)"
    )

    # ② 含 workflow 名称
    assert "wf3_logistics_v2" in reply, (
        f"回复未包含 workflow 名称 wf3_logistics_v2\n回复: {reply!r}"
    )

    # ③ 含三态状态词
    state_words = ("已排队", "待执行", "已开始", "已完成", "执行失败", "已受理", "未确认")
    assert any(w in reply for w in state_words), (
        f"回复不含三态状态词（须含其一: {state_words}）\n回复: {reply!r}"
    )

    # ④ 无完成事件时不暗示已完成
    assert "已跑完" not in reply and "跑完了" not in reply, (
        f"回复不应暗示已完成\n回复: {reply!r}"
    )

    print(f"    reply={reply!r}")


if __name__ == "__main__":
    print("▶ smoke_t21_sub2_entry — T21-SUB-2 chat() 入口三态回执")
    _setup()

    tests = [
        ("test_chat_entry_t21_workflow_receipt", test_chat_entry_t21_workflow_receipt),
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

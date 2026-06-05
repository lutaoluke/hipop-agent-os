"""smoke_workflow_failure_explicit.py — T36-S3 fail-then-pass smoke

验收：
1. 失败显式化 — run_workflow ok=False 时 chat 明说哪条失败+原因（禁成功表格）
   - _safety._check_failed_workflow_claimed_success() 存在并正确工作
   - sanitize_reply() 在 tool_log 携带失败 run_workflow 时触发 banner
2. 可见进度断言 — _run_workflow 必须在 set_current_tenant 之后写 step0，
   保证 agent_events 写入正确 tenant。

FAIL（修前）：
- _check_failed_workflow_claimed_success 不存在 → ImportError
- sanitize_reply 未检测 run_workflow ok=False + 声称成功的情形 → 无 warning
- _run_workflow 在 set_current_tenant 之前调 write_event → step0 落入错 tenant

PASS（修后）：
- _check_failed_workflow_claimed_success 存在且拦截 ok=False + 成功声称
- sanitize_reply 通过 tool_log 触发该检测
- _run_workflow 先调 set_current_tenant 再写 step0
"""

import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── Part 1: 失败显式化 ─────────────────────────────────────────────────────────

def test_check_failed_workflow_fn_available():
    """_safety 必须暴露 _check_failed_workflow_claimed_success 函数。"""
    from hipop.server._safety import _check_failed_workflow_claimed_success
    assert callable(_check_failed_workflow_claimed_success), (
        "_check_failed_workflow_claimed_success 必须是可调用函数"
    )


def test_failed_workflow_success_claim_flagged():
    """run_workflow ok=False + reply 声称已启动 → warning。

    T36 场景：wf2_products_v2 启动失败但 LLM 回复说"已启动"。
    """
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_products_v2"},
            "ok": False,
            "error": "unknown workflow: wf2_products_v2",
        }
    ]
    reply = "好的，已启动 ERP 商品库刷新任务，请稍候。"
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, (
        "run_workflow 返回 ok=False 但 reply 声称成功，应触发 warning，但没有"
    )
    # warning 必须包含失败的工作流名称
    assert "wf2_products_v2" in warns[0], (
        f"warning 未包含失败的工作流名称: {warns}"
    )


def test_ok_workflow_no_false_positive():
    """run_workflow ok=True + reply 声称已启动 → 不报警（正常成功路径）。"""
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_sales_v2"},
            "ok": True,
            "task_id": "aabb1122",
        }
    ]
    reply = "好的，已启动 ERP 销量价格刷新，任务号 aabb1122，请稍候。"
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, (
        f"误报：run_workflow ok=True 的正常路径触发了 warning: {warns}"
    )


def test_partial_failure_both_workflows():
    """两个工作流一成功一失败 → 只对失败的报警。"""
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_products_v2"},
            "ok": False,
            "error": "ERP 连接超时",
        },
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_sales_v2"},
            "ok": True,
            "task_id": "ccdd3344",
        },
    ]
    reply = "两个工作流已启动：商品库和销量价格都在后台跑了。"
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "有一个 run_workflow ok=False，回复声称全部成功，应报警"
    assert "wf2_products_v2" in warns[0], (
        f"warning 应包含失败的工作流名称 wf2_products_v2: {warns}"
    )


def test_empty_tool_log_no_failure_warning():
    """tool_log 为空 → 没有失败的 run_workflow → 不触发此 check。"""
    from hipop.server._safety import _check_failed_workflow_claimed_success
    warns = _check_failed_workflow_claimed_success("已启动工作流。", [])
    assert not warns, (
        "tool_log 为空时 _check_failed_workflow_claimed_success 不应报警"
    )


def test_sanitize_reply_integrates_failure_check():
    """sanitize_reply 通过 tool_log 触发 _check_failed_workflow_claimed_success。

    end-to-end: 调用者传 tool_log=[{run_workflow ok=False}] + 成功声称 → 收到 warning。
    """
    from hipop.server._safety import sanitize_reply
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_products_v2"},
            "ok": False,
            "error": "ERP 连接失败",
        }
    ]
    _, warns = sanitize_reply(
        "ERP 商品库刷新已启动，请稍后查看结果。",
        ["run_workflow"],
        tool_log=tool_log,
    )
    assert any("wf2_products_v2" in w or "失败" in w for w in warns), (
        f"sanitize_reply 未通过 tool_log 触发工作流失败检测: {warns}"
    )


# ── 红队补充（round-2，验门人指出的覆盖洞）────────────────────────────────────────

def test_redteam_args_as_json_string_openai_provider():
    """红队: OpenAI-compat provider 把 args 存为 JSON string，必须仍能提取工作流名称。

    验门人红队样例：
    _check_failed_workflow_claimed_success(
        '好的，已启动 ERP 商品库刷新任务，请稍候。',
        [{'name':'run_workflow','args':'{"workflow":"wf2_products_v2"}',
          'ok':False,'error':'permission_denied'}]
    )
    修前返回: 'unknown: permission_denied' (不含工作流名)
    修后返回: 'wf2_products_v2: permission_denied'
    """
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": '{"workflow": "wf2_products_v2"}',  # JSON string, not dict
            "ok": False,
            "error": "permission_denied",
        }
    ]
    reply = "好的，已启动 ERP 商品库刷新任务，请稍候。"
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "JSON-string args 路径：run_workflow ok=False 应触发 warning"
    assert "wf2_products_v2" in warns[0], (
        f"JSON-string args 路径：warning 必须含工作流名称，实际: {warns}"
    )
    assert "permission_denied" in warns[0], (
        f"warning 必须包含错误原因，实际: {warns}"
    )


def test_redteam_success_table_flagged():
    """红队: markdown 成功表格 | 工作流 | 任务号 | 说明 | → 应被识别为成功声称。

    T36 原始场景：Agent 用 markdown 表格回复，有 ✅ 等成功标记。
    """
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_products_v2"},
            "ok": False,
            "error": "ERP 连接超时",
        }
    ]
    # Markdown table with ✅ success indicators (T36 原始回复格式)
    reply = (
        "已启动两个后台任务 ✅\n"
        "| 工作流 | 任务号 | 说明 |\n"
        "|---|---|---|\n"
        "| **ERP 商品库** | `97eea0ed` | 拉取最新商品主数据 |\n"
    )
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "含 ✅ 的成功表格应被识别为成功声称，触发 warning"
    assert "wf2_products_v2" in warns[0], (
        f"warning 必须含失败的工作流名称: {warns}"
    )


def test_redteam_success_status_in_table_cell():
    """红队: markdown 表格中含 | 成功 | 状态 → 应被识别为成功声称。"""
    from hipop.server._safety import _check_failed_workflow_claimed_success
    tool_log = [
        {
            "name": "run_workflow",
            "args": '{"workflow": "wf2_sales_v2"}',
            "ok": False,
            "error": "auth_failed",
        }
    ]
    reply = (
        "工作流执行结果：\n"
        "| 工作流 | 状态 |\n"
        "|---|---|\n"
        "| wf2_sales_v2 | 成功 |\n"
    )
    warns = _check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "表格 | 成功 | 状态单元格应触发 warning"
    assert "wf2_sales_v2" in warns[0], f"warning 必须含工作流名: {warns}"


def test_redteam_extract_workflow_name_helper():
    """_extract_workflow_name 必须处理 dict 和 JSON string 两种 args 格式。"""
    from hipop.server._safety import _extract_workflow_name
    assert _extract_workflow_name({"workflow": "wf2_products_v2"}) == "wf2_products_v2"
    assert _extract_workflow_name('{"workflow": "wf2_sales_v2"}') == "wf2_sales_v2"
    assert _extract_workflow_name(None) == "unknown"
    assert _extract_workflow_name("not-json") == "unknown"
    assert _extract_workflow_name({}) == "unknown"


# ── Part 2: 可见进度断言（set_current_tenant 顺序）──────────────────────────────

def test_run_workflow_set_tenant_before_step0():
    """_run_workflow 必须在 set_current_tenant 之后才写 step0 write_event。

    根因 T36-S1-(b)：daemon 线程写 step0 时 PG/SQLite RLS tenant 上下文尚未设置，
    step0 落入默认 tenant(1) 而非触发者的真实 tenant。
    修法：把 set_current_tenant(tenant_id) 上移到第一个 write_event 之前。
    """
    api_src_path = REPO / "hipop" / "server" / "api.py"
    src = api_src_path.read_text(encoding="utf-8")
    # Find _run_workflow function body
    fn_start = src.index("def _run_workflow(")
    # Find the next function def after _run_workflow to bound the search
    try:
        fn_end = src.index("\ndef ", fn_start + 1)
    except ValueError:
        fn_end = len(src)
    fn_body = src[fn_start:fn_end]
    # Search for actual method calls (prefixed with data.) to skip comments
    tenant_pos = fn_body.index("data.set_current_tenant")
    write_event_pos = fn_body.index("data.write_event")
    assert tenant_pos < write_event_pos, (
        f"_run_workflow 中 data.set_current_tenant (字符位置 {tenant_pos}) 必须在 "
        f"第一个 data.write_event (字符位置 {write_event_pos}) 之前调用，"
        "否则 step0 event 落入错误 tenant，agent_events 对正确 tenant 不可见。"
    )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_workflow_failure_explicit — T36-S3 失败显式化 + 可见进度断言")

    tests = [
        ("test_check_failed_workflow_fn_available",
         test_check_failed_workflow_fn_available),
        ("test_failed_workflow_success_claim_flagged",
         test_failed_workflow_success_claim_flagged),
        ("test_ok_workflow_no_false_positive",
         test_ok_workflow_no_false_positive),
        ("test_partial_failure_both_workflows",
         test_partial_failure_both_workflows),
        ("test_empty_tool_log_no_failure_warning",
         test_empty_tool_log_no_failure_warning),
        ("test_sanitize_reply_integrates_failure_check",
         test_sanitize_reply_integrates_failure_check),
        # 红队补充（round-2）
        ("test_redteam_args_as_json_string_openai_provider",
         test_redteam_args_as_json_string_openai_provider),
        ("test_redteam_success_table_flagged",
         test_redteam_success_table_flagged),
        ("test_redteam_success_status_in_table_cell",
         test_redteam_success_status_in_table_cell),
        ("test_redteam_extract_workflow_name_helper",
         test_redteam_extract_workflow_name_helper),
        # 可见进度断言
        ("test_run_workflow_set_tenant_before_step0",
         test_run_workflow_set_tenant_before_step0),
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
        print(f"\n✗ {failed}/{len(tests)} tests FAILED (expected before fix)")
        sys.exit(1)
    print(f"\n✓ smoke_workflow_failure_explicit all {len(tests)} passed")

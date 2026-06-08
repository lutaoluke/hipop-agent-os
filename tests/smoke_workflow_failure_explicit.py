"""Smoke: WS-97 T36-S3 — 工作流失败显式化 + 可见进度断言

fail-then-pass 钉死三件事：
1. _safety._check_failed_workflow_claimed_success：run_workflow ok=False 时
   reply 必须点名具体 workflow 技术名（如 wf2_sales_v2），否则加 banner。
   "有一个工作流失败了"/"销量价格刷新失败"均不满足（未唯一点名来源）。
2. _extract_workflow_name：Anthropic dict-args 和 OpenAI JSON-string-args 两路都能提取名字。
3. provider 层 tool_log 包含 ok / task_id / error 字段（验接线不缺失）。

改前（无 _check_failed_workflow_claimed_success / 过于宽松只检查"失败"词）：
  test_vague_failure_language_still_triggers_banner FAIL（无 banner）
改后（精确检查 workflow 技术名出现在 reply 里）：全 PASS。

跑法：python3 tests/smoke_workflow_failure_explicit.py
      make test（自动聚合）
"""
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from hipop.server import _safety


# ── _extract_workflow_name ──────────────────────────────────────────────────

def test_extract_workflow_name_dict_args():
    name = _safety._extract_workflow_name({"workflow": "wf2_products_v2"})
    assert name == "wf2_products_v2", f"dict args: got {name!r}"


def test_extract_workflow_name_json_string_args():
    # OpenAI-compat provider stores args as raw JSON string
    name = _safety._extract_workflow_name('{"workflow": "wf2_products_v2"}')
    assert name == "wf2_products_v2", f"JSON string args: got {name!r}"


def test_extract_workflow_name_empty_falls_back():
    assert _safety._extract_workflow_name({}) == "unknown"
    assert _safety._extract_workflow_name("") == "unknown"
    assert _safety._extract_workflow_name(None) == "unknown"


def test_extract_workflow_name_broken_json_falls_back():
    assert _safety._extract_workflow_name("{not json}") == "unknown"


# ── _check_failed_workflow_claimed_success ─────────────────────────────────

def _make_failed_entry(workflow="wf2_products_v2", error="permission_denied",
                        args_as_str=False):
    args = f'{{"workflow": "{workflow}"}}' if args_as_str else {"workflow": workflow}
    return {"name": "run_workflow", "args": args, "ok": False, "error": error}


def test_failure_not_mentioned_adds_banner():
    """ok=False + reply 未提失败 → banner（核心检测）"""
    tool_log = [_make_failed_entry("wf2_products_v2", "permission_denied")]
    reply = "好的，ERP 商品库刷新已启动，请稍候。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "ok=False 且 reply 无失败说明时应有 banner"
    assert "wf2_products_v2" in warns[0], f"banner 应含工作流名: {warns[0]}"
    assert "permission_denied" in warns[0], f"banner 应含错误原因: {warns[0]}"


def test_failure_named_workflow_in_reply_no_banner():
    """reply 点名了具体 workflow 技术名 → 放行（LLM 如实精确说了）"""
    tool_log = [_make_failed_entry("wf2_products_v2", "permission_denied")]
    reply = "wf2_products_v2 启动失败（permission_denied），请检查权限。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, f"reply 含 workflow 技术名时不应加 banner: {warns}"


def test_vague_failure_language_still_triggers_banner():
    """验门人卡点：reply 仅含泛化失败词但未点名 workflow → 仍触发 banner"""
    tool_log = [_make_failed_entry("wf2_sales_v2", "permission_denied")]
    for vague_reply in [
        "有一个工作流失败了，请稍后重试。",
        "销量价格刷新失败，请检查权限。",
        "很遗憾，刷新任务启动失败了。",
        "ERP 操作失败（error: permission_denied）。",
    ]:
        warns = _safety._check_failed_workflow_claimed_success(vague_reply, tool_log)
        assert warns, f"泛化失败语言应触发 banner（未点名 wf2_sales_v2）: {vague_reply!r}"
        assert "wf2_sales_v2" in warns[0], f"banner 应含失败 workflow 名: {warns[0]}"


def test_no_failed_workflow_no_banner():
    """全部成功的 tool_log → 无 banner"""
    tool_log = [{"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
                 "ok": True, "task_id": "abc12345", "error": None}]
    reply = "好的，ERP 商品库刷新已启动，任务号 abc12345。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, f"全成功不应有 banner: {warns}"


def test_empty_tool_log_no_banner():
    warns = _safety._check_failed_workflow_claimed_success("任意回复。", [])
    assert not warns


def test_openai_str_args_failure_detected():
    """OpenAI-compat provider: args 是 JSON string，仍能检测失败"""
    tool_log = [_make_failed_entry("wf2_products_v2", "permission_denied", args_as_str=True)]
    reply = "好的，已启动 ERP 商品库刷新任务，请稍候。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "OpenAI str-args 形状也应被检测"
    assert "wf2_products_v2" in warns[0], f"banner 应含工作流名: {warns[0]}"


def test_partial_success_failure_not_mentioned_adds_banner():
    """两个工作流：一成一败；reply 未提失败 → banner"""
    tool_log = [
        {"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
         "ok": True, "task_id": "abc12345", "error": None},
        _make_failed_entry("wf2_sales_v2", "unknown_workflow"),
    ]
    reply = "ERP 商品库刷新已启动（任务 abc12345），销量价格刷新也已启动。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "部分失败且 reply 未提失败时应有 banner"
    assert "wf2_sales_v2" in warns[0], f"banner 应含失败工作流: {warns[0]}"


def test_partial_success_failure_named_no_banner():
    """两个工作流：一成一败；reply 点名失败 workflow 技术名 → 放行"""
    tool_log = [
        {"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
         "ok": True, "task_id": "abc12345", "error": None},
        _make_failed_entry("wf2_sales_v2", "unknown_workflow"),
    ]
    # reply 包含 "wf2_sales_v2" 技术名 → 满足"哪条失败"要求
    reply = "ERP 商品库刷新成功（任务 abc12345），wf2_sales_v2 启动失败：unknown_workflow。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, f"reply 已点名失败 workflow 不应 banner: {warns}"


def test_partial_success_vague_failure_triggers_banner():
    """两个工作流：一成一败；reply 只说'有一个失败'未点名 → 仍触发 banner"""
    tool_log = [
        {"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
         "ok": True, "task_id": "abc12345", "error": None},
        _make_failed_entry("wf2_sales_v2", "unknown_workflow"),
    ]
    reply = "ERP 商品库刷新成功（任务 abc12345），销量价格刷新失败。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert warns, "未点名失败 workflow 应有 banner"
    assert "wf2_sales_v2" in warns[0]


def test_non_run_workflow_tool_ignored():
    """query_sku 等非 run_workflow 条目含 ok=False 不触发检测"""
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0057A"]},
                 "ok": False, "error": "sku_not_found"}]
    reply = "SKU 查询失败。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns


# ── sanitize_reply 集成（tool_log 被传入后整条链通） ─────────────────────────

def test_sanitize_reply_with_failed_workflow_adds_banner():
    """sanitize_reply 接收 tool_log 后整条链能把失败 banner 加进去"""
    tool_log = [_make_failed_entry("wf2_products_v2", "permission_denied")]
    reply = "好的，ERP 商品库刷新已启动，请稍候。"
    out, warns = _safety.sanitize_reply(reply, ["run_workflow"], tool_log=tool_log)
    assert warns, "整条链应有 warning"
    assert out.startswith("⚠️"), f"reply 应以 banner 开头: {out[:80]}"
    assert "wf2_products_v2" in out, "banner 应包含工作流名"


def test_sanitize_reply_successful_workflow_no_extra_banner():
    """成功的 run_workflow 不引入新 banner"""
    tool_log = [{"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
                 "ok": True, "task_id": "abc12345", "error": None}]
    reply = "ERP 商品库刷新已启动，任务 abc12345。"
    out, warns = _safety.sanitize_reply(reply, ["run_workflow"], tool_log=tool_log)
    workflow_failure_warns = [w for w in (warns or []) if "未点名该工作流" in w]
    assert not workflow_failure_warns, f"成功工作流不应触发失败 banner: {warns}"


# ── provider 层 tool_log 接线验证（源码检查，不 import 避免 anthropic 依赖）─────

def _read_server_src(filename):
    """读 hipop/server/<filename> 源码，不 import（避免 anthropic 顶层依赖）。"""
    path = os.path.join(os.path.dirname(HERE), "hipop", "server", filename)
    with open(path) as f:
        return f.read()


def test_anthropic_provider_enriches_tool_log_on_failure():
    """Anthropic provider 对 run_workflow 失败条目注入 ok/task_id/error 字段"""
    src = _read_server_src("_provider_anthropic.py")
    assert 'entry["ok"]' in src or "entry['ok']" in src, \
        "Anthropic provider 缺少 tool_log ok 字段接线（T36-S3 死法：接线缺失）"
    assert 'entry["error"]' in src or "entry['error']" in src, \
        "Anthropic provider 缺少 tool_log error 字段接线"


def test_openai_provider_enriches_tool_log_on_failure():
    """OpenAI provider 对 run_workflow 失败条目注入 ok/task_id/error 字段"""
    from hipop.server import _provider_openai
    import inspect
    src = inspect.getsource(_provider_openai.run)
    assert 'entry["ok"]' in src or "entry['ok']" in src, \
        "OpenAI provider 缺少 tool_log ok 字段接线（T36-S3 死法：接线缺失）"
    assert 'entry["error"]' in src or "entry['error']" in src, \
        "OpenAI provider 缺少 tool_log error 字段接线"


def test_agent_passes_tool_log_to_sanitize_reply():
    """agent.chat 调 sanitize_reply 时传了 tool_log=（接线不缺失）"""
    src = _read_server_src("agent.py")
    assert "sanitize_reply(clean_reply, tools_used" in src and "tool_log=tool_log" in src, \
        "agent.chat 调 sanitize_reply 时未传 tool_log=tool_log（T36-S3 死法：接线缺失）"


# ── round-4: multi-task provider / agent 接线验证 ─────────────────────────────

def test_anthropic_provider_returns_workflow_tasks_list():
    """Anthropic provider 返回 workflow_tasks（list），不再返回单 workflow_task（dict）。"""
    src = _read_server_src("_provider_anthropic.py")
    assert '"workflow_tasks": workflow_tasks' in src or "'workflow_tasks': workflow_tasks" in src, \
        "Anthropic provider 未返回 workflow_tasks list（round-4 接线缺失）"
    assert '"workflow_task": workflow_task' not in src and "'workflow_task': workflow_task" not in src, \
        "Anthropic provider 仍在返回旧的 workflow_task（未完成 round-4 迁移）"


def test_openai_provider_returns_workflow_tasks_list():
    """OpenAI provider 返回 workflow_tasks（list），不再返回单 workflow_task（dict）。"""
    from hipop.server import _provider_openai
    import inspect
    src = inspect.getsource(_provider_openai.run)
    assert '"workflow_tasks": workflow_tasks' in src or "'workflow_tasks': workflow_tasks" in src, \
        "OpenAI provider 未返回 workflow_tasks list（round-4 接线缺失）"
    assert '"workflow_task": workflow_task' not in src and "'workflow_task': workflow_task" not in src, \
        "OpenAI provider 仍在返回旧的 workflow_task（未完成 round-4 迁移）"


def test_anthropic_provider_appends_not_overwrites():
    """Anthropic provider 用 workflow_tasks.append(...) 不是覆盖赋值 workflow_task = ..."""
    src = _read_server_src("_provider_anthropic.py")
    assert "workflow_tasks.append(" in src, \
        "Anthropic provider 缺少 workflow_tasks.append 调用（多次 run_workflow 会被覆盖）"


def test_openai_provider_appends_not_overwrites():
    """OpenAI provider 用 workflow_tasks.append(...) 不是覆盖赋值 workflow_task = ..."""
    from hipop.server import _provider_openai
    import inspect
    src = inspect.getsource(_provider_openai.run)
    assert "workflow_tasks.append(" in src, \
        "OpenAI provider 缺少 workflow_tasks.append 调用（多次 run_workflow 会被覆盖）"


def test_agent_returns_workflow_tasks_not_single():
    """agent.chat 返回 workflow_tasks（list），不再是 workflow_task（dict）。"""
    src = _read_server_src("agent.py")
    assert '"workflow_tasks": workflow_tasks' in src or "'workflow_tasks': workflow_tasks" in src, \
        "agent.chat 未返回 workflow_tasks list（round-4 接线缺失）"


def test_provider_appends_failed_task_to_list():
    """Anthropic/OpenAI provider 对失败的 run_workflow 也 append 到 workflow_tasks（ok=False 条目）。"""
    src = _read_server_src("_provider_anthropic.py")
    assert '"ok": False' in src or "'ok': False" in src, \
        "Anthropic provider 未对失败 run_workflow 生成 ok=False 条目（用户看不到失败项）"


def test_chat_panel_uses_workflow_tasks():
    """chat_panel.html 使用 workflow_tasks (list) 而不是单 workflow_task。"""
    path = os.path.join(os.path.dirname(HERE), "hipop", "server", "templates", "partials", "chat_panel.html")
    with open(path) as f:
        src = f.read()
    assert "workflow_tasks" in src, \
        "chat_panel.html 未使用 workflow_tasks（前端无法展示多任务卡）"
    assert "attachTask" in src, \
        "chat_panel.html 缺少 attachTask 调用"


# ── round-5: smoke_chat 契约同步 + 任务卡点击详情接线 ─────────────────────────

def test_smoke_chat_uses_workflow_tasks_not_single():
    """smoke_chat.py expected_workflow 检查已改为读 workflow_tasks list（非旧 workflow_task dict）。"""
    path = os.path.join(HERE, "smoke_chat.py")
    with open(path) as f:
        src = f.read()
    assert "workflow_tasks" in src, \
        "smoke_chat.py 未更新为 workflow_tasks（chat smoke 对新契约无保护）"
    assert 'resp.get("workflow_task") or {}' not in src, \
        "smoke_chat.py 仍用旧 workflow_task dict，chat smoke 会对 workflow_tasks list 误判为空"


def test_chat_panel_task_card_has_click_handler():
    """chat_panel.html 任务卡有 @click 点击详情处理（验收：XX 可点击查看详情）。"""
    path = os.path.join(os.path.dirname(HERE), "hipop", "server", "templates", "partials", "chat_panel.html")
    with open(path) as f:
        src = f.read()
    assert "@click" in src, \
        "chat_panel.html 任务卡缺少 @click 点击详情入口（验收：XX 可点击查看详情）"
    assert "/api/tasks/" in src, \
        "chat_panel.html @click 未路由到 /api/tasks/ 任务详情端点"


def test_managed_worker_error_writes_terminal_event():
    """managed worker task state=error 时必须写 step 99 终态 event，SSE 才能结束。"""
    from hipop.server import data as _data
    from hipop.runtime import worker as _worker

    old_db_path = _data.DB_PATH
    old_tenant = _data.get_current_tenant()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        _data.DB_PATH = tmp.name
        _data._task_tables_checked = False
        _data._ensure_task_tables()
        _data.set_current_tenant(1)
        task_id = "t36err99"
        with _data.conn() as c:
            c.execute(
                "INSERT INTO tasks (task_id, tenant_id, workflow, state) VALUES (?, ?, ?, ?)",
                (task_id, 1, "wf2_products_v2", "running"),
            )
            c.commit()

        _worker._finish(
            task_id, "error", "permission_denied",
            workflow="wf2_products_v2", tenant_id=1,
            actor={"user_id": 1, "email": "test@hipop.local", "role": "ops", "source": "test"},
        )

        events = _data.get_events_after(task_id, 0)
        terminal = [e for e in events if e["step_no"] == 99]
        assert terminal, "managed worker error 未写 step 99 终态 event（SSE 会一直等）"
        assert terminal[-1]["status"] == "error", f"终态 event 状态应为 error: {terminal[-1]}"
        assert "permission_denied" in (terminal[-1].get("message") or ""), \
            f"终态 event 应包含失败原因: {terminal[-1]}"
    finally:
        _data.DB_PATH = old_db_path
        _data._task_tables_checked = False
        _data.set_current_tenant(old_tenant)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def test_chat_panel_terminal_error_sets_task_error():
    """chat_panel 收到 step 99 error 时要把 message 展示到任务卡错误详情。"""
    path = os.path.join(os.path.dirname(HERE), "hipop", "server", "templates", "partials", "chat_panel.html")
    with open(path) as f:
        src = f.read()
    assert "taskObj.error = ev.message" in src, \
        "chat_panel 收到 step 99 error 未把失败原因写入任务卡"


if __name__ == "__main__":
    tests = [
        test_extract_workflow_name_dict_args,
        test_extract_workflow_name_json_string_args,
        test_extract_workflow_name_empty_falls_back,
        test_extract_workflow_name_broken_json_falls_back,
        test_failure_not_mentioned_adds_banner,
        test_failure_named_workflow_in_reply_no_banner,
        test_vague_failure_language_still_triggers_banner,
        test_no_failed_workflow_no_banner,
        test_empty_tool_log_no_banner,
        test_openai_str_args_failure_detected,
        test_partial_success_failure_not_mentioned_adds_banner,
        test_partial_success_failure_named_no_banner,
        test_partial_success_vague_failure_triggers_banner,
        test_non_run_workflow_tool_ignored,
        test_sanitize_reply_with_failed_workflow_adds_banner,
        test_sanitize_reply_successful_workflow_no_extra_banner,
        test_anthropic_provider_enriches_tool_log_on_failure,
        test_openai_provider_enriches_tool_log_on_failure,
        test_agent_passes_tool_log_to_sanitize_reply,
        # round-4: multi-task接线
        test_anthropic_provider_returns_workflow_tasks_list,
        test_openai_provider_returns_workflow_tasks_list,
        test_anthropic_provider_appends_not_overwrites,
        test_openai_provider_appends_not_overwrites,
        test_agent_returns_workflow_tasks_not_single,
        test_provider_appends_failed_task_to_list,
        test_chat_panel_uses_workflow_tasks,
        # round-5: chat smoke 契约同步 + 任务卡点击详情
        test_smoke_chat_uses_workflow_tasks_not_single,
        test_chat_panel_task_card_has_click_handler,
        # round-6: managed worker error terminal SSE event
        test_managed_worker_error_writes_terminal_event,
        test_chat_panel_terminal_error_sets_task_error,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

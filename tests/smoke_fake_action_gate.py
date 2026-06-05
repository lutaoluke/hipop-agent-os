"""smoke_fake_action_gate.py — T36-S2 防伪门 fail-then-pass smoke

验收（WS-96）：
  (a) _safety.py 的 task_id provenance check：reply 里出现未被 tool_log 背书的
      task_id → 拦截/banner
  (b) _run_workflow (api.py) 后台线程：set_current_tenant 必须在第一次 write_event 前

FAIL 条件（修前）：
  (a) sanitize_reply 不检查 task_id provenance → 幻觉 task_id 通过，不报警告
  (b) _run_workflow 里 set_current_tenant 在 write_event(step 0) 之后 → 顺序错位

PASS 条件（修后）：
  (a) sanitize_reply(reply, tools_used, tool_log=[...]) 能发现 reply 里的幻觉 task_id
  (b) _run_workflow 先 set_current_tenant 再 write_event，且 step0 有 try-except
"""

import inspect
import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.setdefault("DB_URL", "sqlite:///hipop_test_fake_action.db")
os.environ.setdefault("JWT_SECRET", "test_secret_for_fake_action_smoke")


# ── (a) task_id provenance check in _safety.py ────────────────

def test_sanitize_accepts_tool_log_parameter():
    """sanitize_reply 必须接受 tool_log 关键字参数（可选，默认 None）。

    FAIL (before fix): sanitize_reply(reply, tools_used) — 不接受 tool_log，调用报 TypeError。
    PASS (after fix):  sanitize_reply(reply, tools_used, tool_log=[...]) 正常运行。
    """
    from hipop.server._safety import sanitize_reply
    sig = inspect.signature(sanitize_reply)
    assert "tool_log" in sig.parameters, (
        "sanitize_reply 缺少 tool_log 参数 — 需要加 tool_log=None 接受工具调用日志"
    )
    # 直接调用不报错
    reply, warns = sanitize_reply("测试回复", ["run_workflow"], tool_log=[])
    assert isinstance(warns, list), "sanitize_reply 必须返回 (str, list)"


def test_fake_task_id_triggers_warning():
    """reply 里出现未由 run_workflow 工具返回的 task_id 时，_safety 必须报警告。

    FAIL (before fix): sanitize_reply 不检查 task_id provenance → warns 为空。
    PASS (after fix):  发现幻觉 task_id → warns 含相关警告。

    场景复现 T36：LLM 说"已触发任务 97eea0ed 和 6bd754b3"，
    但 run_workflow 只真实返回了 task_id=6bd754b3；97eea0ed 是幻觉。
    """
    from hipop.server._safety import sanitize_reply
    fake_id = "97eea0ed"
    real_id = "6bd754b3"
    reply = (
        f"已分别触发两个后台任务：\n"
        f"- ERP 商品库：任务 {fake_id}\n"
        f"- ERP 销量：任务 {real_id}\n"
        f"前端将自动订阅 SSE 进度。"
    )
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_sales_v2"},
            "task_id": real_id,
            "result_keys": ["ok", "task_id", "workflow"],
        }
    ]
    tools_used = ["run_workflow"]
    _, warns = sanitize_reply(reply, tools_used, tool_log=tool_log)
    assert any("97eea0ed" in w or "task_id" in w.lower() or "任务号" in w for w in warns), (
        f"_safety 应该拦截幻觉 task_id={fake_id}，但 warns={warns}"
    )
    print(f"    warn triggered: {[w[:80] for w in warns]}")


def test_real_task_id_does_not_trigger_warning():
    """tool_log 背书的真实 task_id 出现在 reply 里，不应报警告。

    FAIL (before fix): N/A（修前没有这个检查）。
    PASS (after fix):  真实 task_id 放行，warns 为空（或只含其他警告）。
    """
    from hipop.server._safety import sanitize_reply
    real_id = "6bd754b3"
    reply = (
        f"已启动后台任务 {real_id}（ERP 销量），"
        f"前端将订阅 SSE 推送进度，完成后自动刷新 sales 模块。"
    )
    tool_log = [
        {
            "name": "run_workflow",
            "args": {"workflow": "wf2_sales_v2"},
            "task_id": real_id,
            "result_keys": ["ok", "task_id", "workflow"],
        }
    ]
    tools_used = ["run_workflow"]
    _, warns = sanitize_reply(reply, tools_used, tool_log=tool_log)
    fake_task_warns = [w for w in warns if "任务号" in w or "task_id" in w.lower() and "幻觉" in w]
    assert not fake_task_warns, (
        f"真实 task_id={real_id} 不应被误报为幻觉，但 warns={fake_task_warns}"
    )
    print(f"    no false positive for real task_id={real_id}")


def test_no_run_workflow_call_no_task_id_warning_if_no_task_id_in_reply():
    """没调 run_workflow 且 reply 里也没有 task_id pattern，不报 task_id 相关警告。

    确保修改不会对普通 reply 造成误报。
    """
    from hipop.server._safety import sanitize_reply
    reply = "当前 KSA 店铺补货建议：TBJ0057A 建议补 50 件，urgency=high。"
    _, warns = sanitize_reply(reply, ["compute_replenishment"], tool_log=[])
    task_warns = [w for w in warns if "任务号" in w or "task_id" in w.lower()]
    assert not task_warns, f"普通回复不应有 task_id 警告: {task_warns}"
    print("    clean reply: no false positive task_id warning")


# ── (b) _run_workflow tenant context ordering ────────────────

def test_run_workflow_sets_tenant_before_first_write_event():
    """_run_workflow 必须在第一次 write_event 前调 set_current_tenant。

    FAIL (before fix): set_current_tenant 在 write_event(step 0) 之后 → 顺序错位。
    PASS (after fix):  set_current_tenant 在 write_event(step 0) 之前。

    检测方法：用 inspect.getsource 确认顺序，或用 ast 分析。
    """
    import ast
    from hipop.server import api as _api
    src = inspect.getsource(_api._run_workflow)
    # 在源码里 set_current_tenant 的位置必须早于第一个 write_event("初始化")
    set_tenant_pos = src.find("set_current_tenant(tenant_id)")
    first_write_event_pos = src.find('write_event(')
    assert set_tenant_pos != -1, "_run_workflow 源码中未找到 set_current_tenant(tenant_id)"
    assert first_write_event_pos != -1, "_run_workflow 源码中未找到 write_event("
    assert set_tenant_pos < first_write_event_pos, (
        f"_run_workflow 里 set_current_tenant (pos={set_tenant_pos}) 必须在 "
        f"write_event (pos={first_write_event_pos}) 之前 — T36-S2(b) 根因修复"
    )
    print(f"    set_current_tenant@{set_tenant_pos} < write_event@{first_write_event_pos} ✓")


def test_run_workflow_step0_has_try_except():
    """_run_workflow 里 step0 write_event 必须有 try-except 保护，防线程静默崩溃。

    FAIL (before fix): step0 write_event 裸写，PG 连接失败时 daemon 线程静默挂。
    PASS (after fix):  step0 write_event 包在 try-except 里。
    """
    from hipop.server import api as _api
    src = inspect.getsource(_api._run_workflow)
    # 找到 "初始化" 附近的 try-except
    init_write_pos = src.find('"初始化"')
    assert init_write_pos != -1, "_run_workflow 源码中未找到 '初始化' event 写入"
    # step0 write_event 前后应该有 try-except
    # 取 step0 write_event 的上下文（往前看 200 chars）
    context = src[max(0, init_write_pos - 200):init_write_pos + 50]
    assert "try:" in context, (
        f"step0 write_event('初始化') 缺 try 块保护 — 线程崩溃风险。"
        f"上下文：\n{context}"
    )
    print("    step0 write_event has try-except ✓")


# ── (c) workflow enum + WORKFLOW_REGISTRY 一致性 ──────────────

def test_run_workflow_tool_enum_matches_registry():
    """run_workflow tool 的 enum 中的每个 workflow 名必须在 WORKFLOW_REGISTRY 里。

    防止 enum 里有 workflow 名但 WORKFLOW_REGISTRY 没注册 → Agent 选了但调不到 → 编。

    FAIL (before fix): 如果 enum 和 registry 不一致 → 调不到合法 workflow。
    PASS (after fix):  enum 中所有名字在 registry 里都存在。
    """
    from hipop.server import agent as _agent, api as _api
    # 从 TOOLS 里找 run_workflow 定义
    tool_def = next((t for t in _agent.TOOLS if t["name"] == "run_workflow"), None)
    assert tool_def is not None, "TOOLS 里没有 run_workflow"
    enum_values = tool_def["input_schema"]["properties"]["workflow"]["enum"]
    registry_keys = set(_api.WORKFLOW_REGISTRY.keys())
    missing = [w for w in enum_values if w not in registry_keys]
    assert not missing, (
        f"run_workflow enum 里有 workflow 名但 WORKFLOW_REGISTRY 未注册: {missing}\n"
        f"注册的 workflow: {sorted(registry_keys)}"
    )
    print(f"    all {len(enum_values)} enum values found in WORKFLOW_REGISTRY ✓")


def test_run_workflow_returns_error_for_unknown_workflow():
    """tool_run_workflow 对不在 WORKFLOW_REGISTRY 里的 workflow 必须返失败而非编。

    FAIL (before fix): 调不动 → LLM 编 task_id（T36 根因 (c) 方向的防御）。
    PASS (after fix):  tool_run_workflow 返 {ok: False, error: 'unknown workflow'...}。
    """
    from hipop.server import agent as _agent, data as _data
    _data._ensure_task_tables()
    _data.set_current_tenant(1)
    result = _agent.tool_run_workflow("nonexistent_wf_xyz")
    assert result.get("ok") is False or "error" in result, (
        f"tool_run_workflow 对未知 workflow 必须返失败，但得到: {result}"
    )
    assert "unknown" in (result.get("error") or "").lower() or "valid" in str(result), (
        f"error 消息应包含 unknown 或 valid: {result}"
    )
    print(f"    unknown workflow → {result.get('error') or result.get('ok')}")


if __name__ == "__main__":
    tests = [
        test_sanitize_accepts_tool_log_parameter,
        test_fake_task_id_triggers_warning,
        test_real_task_id_does_not_trigger_warning,
        test_no_run_workflow_call_no_task_id_warning_if_no_task_id_in_reply,
        test_run_workflow_sets_tenant_before_first_write_event,
        test_run_workflow_step0_has_try_except,
        test_run_workflow_tool_enum_matches_registry,
        test_run_workflow_returns_error_for_unknown_workflow,
    ]
    print(f"▶ smoke_fake_action_gate — T36-S2 防伪门")
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'✓' if not failed else '✗'} smoke_fake_action_gate: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

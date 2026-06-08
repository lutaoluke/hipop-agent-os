"""Smoke: WS-97 T36-S3 — 工作流失败显式化 + 可见进度断言

fail-then-pass 钉死三件事：
1. _safety._check_failed_workflow_claimed_success：run_workflow ok=False 时
   若 reply 未说明失败原因 → banner；若 reply 已说明 → 放行。
2. _extract_workflow_name：Anthropic dict-args 和 OpenAI JSON-string-args 两路都能提取名字。
3. provider 层 tool_log 包含 ok / task_id / error 字段（验接线不缺失）。

改前（无 _check_failed_workflow_claimed_success）：
  test_*_failure_not_mentioned FAIL（check 不存在 → 没有 banner）
改后（加 check + sanitize_reply 传 tool_log）：全 PASS。

跑法：python3 tests/smoke_workflow_failure_explicit.py
      make test（自动聚合）
"""
import os
import sys
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


def test_failure_mentioned_in_reply_no_banner():
    """reply 已说明失败 → 放行（LLM 如实说了不误报）"""
    tool_log = [_make_failed_entry("wf2_products_v2", "permission_denied")]
    reply = "ERP 商品库刷新失败（permission_denied），请检查权限。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, f"reply 已含失败说明时不应加 banner: {warns}"


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


def test_partial_success_failure_mentioned_no_banner():
    """两个工作流：一成一败；reply 正确说了 'XX 成功，XX 失败' → 放行"""
    tool_log = [
        {"name": "run_workflow", "args": {"workflow": "wf2_products_v2"},
         "ok": True, "task_id": "abc12345", "error": None},
        _make_failed_entry("wf2_sales_v2", "unknown_workflow"),
    ]
    reply = "ERP 商品库刷新成功（任务 abc12345），销量价格刷新失败：unknown_workflow。"
    warns = _safety._check_failed_workflow_claimed_success(reply, tool_log)
    assert not warns, f"reply 已说明混合结果不应 banner: {warns}"


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
    workflow_failure_warns = [w for w in (warns or []) if "实际启动失败" in w]
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
    assert "sanitize_reply(clean_reply, tools_used, tool_log=tool_log)" in src, \
        "agent.chat 调 sanitize_reply 时未传 tool_log=tool_log（T36-S3 死法：接线缺失）"


if __name__ == "__main__":
    tests = [
        test_extract_workflow_name_dict_args,
        test_extract_workflow_name_json_string_args,
        test_extract_workflow_name_empty_falls_back,
        test_extract_workflow_name_broken_json_falls_back,
        test_failure_not_mentioned_adds_banner,
        test_failure_mentioned_in_reply_no_banner,
        test_no_failed_workflow_no_banner,
        test_empty_tool_log_no_banner,
        test_openai_str_args_failure_detected,
        test_partial_success_failure_not_mentioned_adds_banner,
        test_partial_success_failure_mentioned_no_banner,
        test_non_run_workflow_tool_ignored,
        test_sanitize_reply_with_failed_workflow_adds_banner,
        test_sanitize_reply_successful_workflow_no_extra_banner,
        test_anthropic_provider_enriches_tool_log_on_failure,
        test_openai_provider_enriches_tool_log_on_failure,
        test_agent_passes_tool_log_to_sanitize_reply,
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

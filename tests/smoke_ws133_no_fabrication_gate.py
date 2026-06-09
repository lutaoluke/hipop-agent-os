"""smoke_ws133_no_fabrication_gate.py — WS-133 全局禁编门 fail-then-pass smoke

背景
----
WS-133 把"查不到/实时失败/不存在必须明说原因"做成全局硬规则，覆盖：
  - 实时源 ERP 登录失败（无缓存）：query_order_live → erp_login_failed_no_cache
  - 实时源失败回退缓存：query_sku_live → live_query_failed_reason
  - 实时源网络异常：query_sku_live/query_order_live → erp_fetch_error
  - 工作流创建失败：run_workflow 返回 ok=False 但回复仍宣称"已触发"

fail-then-pass 口径
-------------------
FAIL（修前）：新规则不存在，上述场景 _safety 不警告，编造假状态放行。
PASS（修后）：每种失败模式被拦截；有真实成功证据的正常回答不被误拦。

跑法：python3 tests/smoke_ws133_no_fabrication_gate.py
（也由 make test 自动聚合）
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hipop.server import _safety  # noqa: E402


# ─── Rule F: query_order_live → erp_login_failed_no_cache ─────────────────────

def test_rule_f_order_erp_login_failed_no_cache_triggers_gate():
    """Rule F: query_order_live 返回 erp_login_failed_no_cache，
    但回复没说明 ERP 失败 → _safety 拦截并补充说明。

    FAIL（修前）：warns 为空，假状态放行。
    PASS（修后）：warns 非空，含 ERP 失败说明。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-TEST-9999"},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]
    vague_reply = "您好，该货单目前没有可用数据。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"erp_login_failed_no_cache 应触发警告，但 warns={warns}"
    assert any("Rule F" in w or "erp_login_failed" in w or "ERP" in w or "WS-133" in w for w in warns), \
        f"警告应提及 ERP 失败原因: {warns}"
    import re
    assert re.search(r"ERP|实时.*失败|登录.*失败|查询失败|无缓存", out), \
        f"回复应包含 ERP 失败说明: {out[:300]}"


def test_rule_f_no_false_positive_when_reply_already_says_erp_failed():
    """Rule F: 回复已明确说明 ERP 失败 → 不重复触发。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-TEST-9999"},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]
    good_reply = "ERP 实时查询失败（登录失败），单货单查询无缓存兜底，请稍后重试。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert not rule_f_warns, f"回复已说明 ERP 失败，不应重复触发 Rule F: {rule_f_warns}"


def test_rule_f_no_false_positive_for_successful_order_query():
    """Rule F: query_order_live 返回正常结果 → 不触发。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-OK-0001"},
        "result_error": None,
        "result_keys": ["ok", "order_no", "forwarder", "tracking_no", "nodes"],
    }]
    good_reply = "货单 PD-OK-0001 当前在途，货代为 YTO，跟踪号 YT123456。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert not rule_f_warns, f"正常订单查询不应触发 Rule F: {rule_f_warns}"


# ─── Rule G: query_sku_live → live_query_failed_reason (ERP 失败返回缓存) ────────

def test_rule_g_sku_live_query_failed_cache_returned_triggers_gate():
    """Rule G: query_sku_live 返回 live_query_failed_reason（ERP 失败回退缓存），
    但回复没告知用户是缓存数据 → _safety 拦截并补充说明。

    FAIL（修前）：warns 为空，缓存被当实时数据呈现。
    PASS（修后）：warns 非空，含缓存/ERP 失败说明。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBC0168A"},
        "result_error": None,  # ok=True，但有 live_query_failed_reason
        "result_keys": ["ok", "sku", "in_transit_total_qty", "stale_warn",
                        "live_query_failed_reason", "in_transit_orders"],
    }]
    vague_reply = "TBC0168A 当前在途 5 件，货单号 PD-001。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"live_query_failed_reason 应触发警告，但 warns={warns}"
    assert any("Rule G" in w or "缓存" in w or "ERP" in w or "实时" in w or "WS-133" in w for w in warns), \
        f"警告应提及缓存/ERP 失败: {warns}"
    import re
    assert re.search(r"缓存|ERP.*失败|实时.*失败|非实时|wf3", out), \
        f"回复应包含缓存/实时失败说明: {out[:300]}"


def test_rule_g_no_false_positive_when_reply_already_says_cache():
    """Rule G: 回复已明确说明是缓存数据 → 不重复触发。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBC0168A"},
        "result_error": None,
        "result_keys": ["ok", "sku", "in_transit_total_qty", "stale_warn",
                        "live_query_failed_reason"],
    }]
    good_reply = "TBC0168A ERP 实时拉失败，以下是 wf3 缓存数据（更新于 2026-06-08），在途 5 件。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_g_warns = [w for w in warns if "Rule G" in w or "live_query_failed_reason" in w or "WS-133 Rule G" in w]
    assert not rule_g_warns, f"回复已说明缓存，不应触发 Rule G: {rule_g_warns}"


def test_rule_g_no_false_positive_for_successful_sku_live_query():
    """Rule G: query_sku_live 返回实时数据（无 live_query_failed_reason）→ 不触发。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBC0168A"},
        "result_error": None,
        "result_keys": ["ok", "sku", "in_transit_total_qty", "fetched_from",
                        "in_transit_orders", "references"],
    }]
    good_reply = "TBC0168A 实时在途 8 件（来源：ERP realtime），货单号 PD-001。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_g_warns = [w for w in warns if "Rule G" in w or "live_query_failed_reason" in w or "WS-133 Rule G" in w]
    assert not rule_g_warns, f"实时成功查询不应触发 Rule G: {rule_g_warns}"


# ─── Rule H: erp_fetch_error (网络/异常) ──────────────────────────────────────

def test_rule_h_sku_erp_fetch_error_triggers_gate():
    """Rule H: query_sku_live 返回 erp_fetch_error（网络/接口异常），
    回复没说明失败 → _safety 拦截并补充说明。

    FAIL（修前）：warns 为空，Agent 可能用旧数或编默认值。
    PASS（修后）：warns 非空，含实时失败说明。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SDA1874A"},
        "result_error": "erp_fetch_error: ConnectionError: Connection refused",
        "result_keys": ["ok", "error"],
    }]
    vague_reply = "SDA1874A 当前没有在途货单。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"erp_fetch_error 应触发警告，但 warns={warns}"
    assert any("Rule H" in w or "erp_fetch_error" in w or "查询异常" in w or "WS-133" in w or "失败" in w for w in warns), \
        f"警告应提及查询失败: {warns}"
    import re
    assert re.search(r"查询失败|ERP.*失败|实时.*失败|获取失败|请求失败|暂时无法", out), \
        f"回复应包含失败说明: {out[:300]}"


def test_rule_h_order_erp_fetch_error_triggers_gate():
    """Rule H: query_order_live 返回 erp_fetch_error → 触发警告。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-FAIL-001"},
        "result_error": "erp_fetch_error: TimeoutError: 30s timeout",
        "result_keys": ["ok", "error"],
    }]
    vague_reply = "货单 PD-FAIL-001 暂无状态。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"order erp_fetch_error 应触发警告，但 warns={warns}"
    assert any("Rule H" in w or "erp_fetch_error" in w or "查询异常" in w or "失败" in w or "WS-133" in w for w in warns), \
        f"警告应提及查询失败: {warns}"


def test_rule_h_no_false_positive_when_reply_acknowledges_error():
    """Rule H: 回复已明确说明查询异常 → 不重复触发。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SDA1874A"},
        "result_error": "erp_fetch_error: ConnectionError: Connection refused",
        "result_keys": ["ok", "error"],
    }]
    good_reply = "ERP 查询失败（网络异常），暂时无法获取 SDA1874A 的实时物流数据，请稍后重试。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_h_warns = [w for w in warns if "Rule H" in w or "erp_fetch_error" in w or "WS-133 Rule H" in w]
    assert not rule_h_warns, f"回复已说明失败，不应触发 Rule H: {rule_h_warns}"


def test_rule_h_no_false_positive_for_successful_query():
    """Rule H: 正常工具结果 (无 erp_fetch_error) → 不触发。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBJ0059A"},
        "result_error": None,
        "result_keys": ["ok", "sku", "in_transit_total_qty", "in_transit_orders"],
    }]
    good_reply = "TBJ0059A 在途 3 件，ERP 实时数据。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_h_warns = [w for w in warns if "Rule H" in w or "erp_fetch_error" in w or "WS-133 Rule H" in w]
    assert not rule_h_warns, f"正常查询不应触发 Rule H: {rule_h_warns}"


# ─── Rule I: run_workflow 返回 ok=False 但回复宣称"已触发" ────────────────────

def test_rule_i_workflow_failed_but_reply_claims_triggered():
    """Rule I: run_workflow 调用了但返回 ok=False（工作流创建失败），
    回复仍宣称"已触发/任务已创建" → _safety 拦截。

    FAIL（修前）：warns 为空，假触发放行。
    PASS（修后）：warns 非空，含工作流失败说明。
    """
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "result_error": "erp_fetch_error: Cannot connect to ERP",
        "result_keys": ["ok", "error"],
    }]
    fake_reply = "好的，已为您触发物流刷新任务，任务已在后台运行。"
    out, warns = _safety.sanitize_reply(fake_reply, tools_used=["run_workflow"], tool_log=tool_log)
    assert warns, f"run_workflow 失败但回复宣称已触发 → 应警告，但 warns={warns}"
    assert any("Rule I" in w or "工作流" in w or "WS-133" in w or "失败" in w or "触发" in w for w in warns), \
        f"警告应提及工作流失败: {warns}"


def test_rule_i_workflow_failed_unknown_workflow():
    """Rule I: run_workflow 返回 unknown workflow 错误 → 不得宣称已启动。"""
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf99_nonexistent"},
        "result_error": "unknown workflow: wf99_nonexistent",
        "result_keys": ["ok", "error", "available_workflows"],
    }]
    fake_reply = "任务已提交，系统正在后台跑物流刷新。"
    out, warns = _safety.sanitize_reply(fake_reply, tools_used=["run_workflow"], tool_log=tool_log)
    assert warns, f"unknown workflow 但回复宣称已提交 → 应警告，但 warns={warns}"


def test_rule_i_no_false_positive_when_workflow_succeeded():
    """Rule I: run_workflow 成功（无 result_error）→ 不触发 Rule I。"""
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "result_error": None,
        "result_keys": ["ok", "task_id", "workflow", "label", "total_steps"],
    }]
    good_reply = "已为您触发物流刷新，任务 ID: a1b2c3d4，正在后台运行。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["run_workflow"], tool_log=tool_log)
    rule_i_warns = [w for w in warns if "Rule I" in w or "WS-133 Rule I" in w or "工作流失败" in w]
    assert not rule_i_warns, f"工作流成功不应触发 Rule I: {rule_i_warns}"


def test_rule_i_no_false_positive_when_reply_acknowledges_failure():
    """Rule I: run_workflow 失败，但回复已说明失败 → 不重复触发。"""
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "result_error": "erp_fetch_error: Cannot connect",
        "result_keys": ["ok", "error"],
    }]
    good_reply = "工作流触发失败（ERP 连接失败），请检查 ERP 登录状态后重试。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["run_workflow"], tool_log=tool_log)
    rule_i_warns = [w for w in warns if "Rule I" in w or "WS-133 Rule I" in w]
    assert not rule_i_warns, f"回复已说明工作流失败，不应重复触发 Rule I: {rule_i_warns}"


# ─── 回归：现有 T26 规则不受影响 ──────────────────────────────────────────────

def test_t26_order_not_found_still_works():
    """回归：T26 Rule B (order_not_found_in_erp) 仍触发。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-NOTEXIST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    # 故意不说"未找到"的模糊回复，触发 T26 补充前缀
    vague_reply = "该货单信息正在更新，稍后可查。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"T26 order_not_found 应仍触发，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无记录|核实货单号|无物流", out), \
        f"T26 未找到前缀应被插入: {out[:200]}"


def test_t26_sku_not_found_still_works():
    """回归：T26-ext Rule D (sku_no_orders_in_erp) 仍触发。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SKU-GHOST-0000"},
        "result_error": "sku_no_orders_in_erp",
        "result_keys": ["ok", "error", "sku", "message"],
    }]
    # 故意不说"未找到"的模糊回复，触发 T26-ext 补充前缀
    vague_reply = "该 SKU 的货单状态未知。"
    out, warns = _safety.sanitize_reply(vague_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"T26-ext sku_no_orders 应仍触发，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无在途|无记录|核实.*SKU", out), \
        f"T26-ext 未找到前缀应被插入: {out[:200]}"


def test_no_false_positive_normal_replenishment_reply():
    """回归：正常补货建议（无任何失败/不存在场景）→ 新规则全不触发。"""
    tool_log = [
        {"name": "compute_replenishment", "args": {}, "result_error": None,
         "result_keys": ["ok", "items", "total"]},
    ]
    good_reply = "KSA 当前补货建议：TBJ0059A 补 50 件（urgency=high），SDA1874A 补 30 件。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["compute_replenishment"], tool_log=tool_log)
    new_rule_warns = [w for w in warns if any(k in w for k in ("Rule F", "Rule G", "Rule H", "Rule I", "WS-133"))]
    assert not new_rule_warns, f"正常补货不应触发新规则: {new_rule_warns}"


if __name__ == "__main__":
    import traceback
    tests = [
        test_rule_f_order_erp_login_failed_no_cache_triggers_gate,
        test_rule_f_no_false_positive_when_reply_already_says_erp_failed,
        test_rule_f_no_false_positive_for_successful_order_query,
        test_rule_g_sku_live_query_failed_cache_returned_triggers_gate,
        test_rule_g_no_false_positive_when_reply_already_says_cache,
        test_rule_g_no_false_positive_for_successful_sku_live_query,
        test_rule_h_sku_erp_fetch_error_triggers_gate,
        test_rule_h_order_erp_fetch_error_triggers_gate,
        test_rule_h_no_false_positive_when_reply_acknowledges_error,
        test_rule_h_no_false_positive_for_successful_query,
        test_rule_i_workflow_failed_but_reply_claims_triggered,
        test_rule_i_workflow_failed_unknown_workflow,
        test_rule_i_no_false_positive_when_workflow_succeeded,
        test_rule_i_no_false_positive_when_reply_acknowledges_failure,
        test_t26_order_not_found_still_works,
        test_t26_sku_not_found_still_works,
        test_no_false_positive_normal_replenishment_reply,
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

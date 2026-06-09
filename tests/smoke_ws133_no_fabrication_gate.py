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


# ─── Rule F 后缀编造收口（验门人 Round-3 打回点）─────────────────────────────────
# erp_login_failed_no_cache 时，回复即使已说明"ERP 登录失败/无缓存"，
# 后缀若又出现确定物流/库存结论仍必须告警 —— 不能因失败措辞整条放行。

def _erp_login_failed_no_cache_log(order_no="PD-FAIL"):
    return [{
        "name": "query_order_live",
        "args": {"order_no": order_no},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]


def test_rule_f_suffix_in_transit_conclusion_still_warns():
    """失败措辞 + 后缀"当前在途/预计到仓"编造结论 → 仍须告警。

    FAIL（修前）：出现"ERP 登录失败/无缓存"措辞即整条放行，warns 为空。
    PASS（修后）：_ERROR_FABRICATION_RE 命中后缀结论 → Rule F 告警。
    """
    reply = "ERP 登录失败，单货单查询无缓存；不过货单 PD-FAIL 当前在途，预计明天到仓。"
    out, warns = _safety.sanitize_reply(
        reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_no_cache_log()
    )
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+后缀在途结论应触发 Rule F，但 warns={warns}"


def test_rule_f_suffix_negative_in_transit_conclusion_still_warns():
    """失败措辞 + 后缀"当前没有在途，状态正常"编造结论 → 仍须告警。"""
    reply = "ERP 查询失败且没有缓存；该货单当前没有在途，状态正常。"
    out, warns = _safety.sanitize_reply(
        reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_no_cache_log()
    )
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+后缀'没有在途/状态正常'结论应触发 Rule F，但 warns={warns}"


def test_rule_f_suffix_forwarder_conclusion_still_warns():
    """失败措辞 + 后缀"货代为 YTO，跟踪号"编造结论 → 仍须告警。"""
    reply = "实时查询失败，无缓存可用；货代为 YTO，跟踪号 YT123456。"
    out, warns = _safety.sanitize_reply(
        reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_no_cache_log()
    )
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+后缀'货代为 X'结论应触发 Rule F，但 warns={warns}"


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


# ─── 验门人打回 Round 2：3 类洞补 guard ──────────────────────────────────────────

# 洞1: order_lookup_unavailable_no_erp_credentials — 未被拦截

def test_rule_f2_order_lookup_unavailable_no_erp_credentials_triggers_gate():
    """洞1: query_order_live 返回 order_lookup_unavailable_no_erp_credentials
    (ERP 账号未配置)，但回复编造了在途状态 → _safety 必须拦截。

    FAIL（修前）：sanitize_reply 不识别此错误码，warns=[]。
    PASS（修后）：warns 非空，含 ERP 未配置说明。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_lookup_unavailable_no_erp_credentials",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    fabricated_reply = "货单 PD-GHOST 当前在途，货代为 YTO，预计明天到仓。"
    out, warns = _safety.sanitize_reply(fabricated_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"order_lookup_unavailable_no_erp_credentials 应触发警告，但 warns={warns}"
    import re
    assert re.search(r"ERP|配置|无法确认|未配置|账号|凭据|credentials", out), \
        f"回复应包含 ERP 未配置说明: {out[:300]}"


def test_rule_f2_no_false_positive_when_reply_says_unavailable():
    """洞1: 回复已明确说明 ERP 未配置 → 不重复触发。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_lookup_unavailable_no_erp_credentials",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    good_reply = "ERP 账号未配置，无法确认该货单是否存在，请先配置 dbuyerp 后重试。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f2_warns = [w for w in warns if "no_erp_credentials" in w or "Rule F2" in w or "WS-133 Rule F2" in w]
    assert not rule_f2_warns, f"回复已说明 ERP 未配置，不应重复触发 Rule F2: {rule_f2_warns}"


# 洞2: Rule B/D 可被否定句绕过（"不是不存在"）

def test_rule_b_negative_sentence_bypass_blocked():
    """洞2: order_not_found_in_erp 下，回复用否定句'该货单不是不存在'绕过门 → 必须仍触发。

    FAIL（修前）：'不存在' 被正则命中当作'已说明未找到'，门放行。
    PASS（修后）：用负向前置处理排除'不是不存在'上下文。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    bypass_reply = "该货单不是不存在的，当前已在途，货代为 YTO，状态正常。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"否定句绕过 '不是不存在' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无记录|核实货单号|无物流", out), \
        f"应插入未找到前缀: {out[:300]}"


def test_rule_d_negative_sentence_bypass_blocked():
    """洞2: sku_no_orders_in_erp 下，回复用否定句'这个SKU不是不存在'绕过门 → 必须仍触发。

    FAIL（修前）：'不存在' 被命中当作已说明，门放行。
    PASS（修后）：负向处理后不再被绕过。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SKU-TEST"},
        "result_error": "sku_no_orders_in_erp",
        "result_keys": ["ok", "error", "sku", "message"],
    }]
    bypass_reply = "这个 SKU 不是不存在的，目前在途 12 件，货单号 PD-001。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"否定句绕过 '不是不存在' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无在途|无记录|核实.*SKU", out), \
        f"应插入未找到前缀: {out[:300]}"


def test_rule_b_legitimate_not_found_still_passes():
    """洞2 反例: '该货单不存在'（非否定句）仍应被当作'已说明未找到'、不触发 Rule B。"""
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    good_reply = "经查询，该货单不存在于 ERP，请核实货单号。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    t26_warns = [w for w in warns if "order_not_found_in_erp" in w or "T26" in w]
    assert not t26_warns, f"合法'不存在'声明不应触发 Rule B: {t26_warns}"


def test_rule_d_legitimate_not_found_still_passes():
    """洞2 反例: 'SKU TBC 无在途记录'仍应被当作'已说明未找到'、不触发 Rule D。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBC0168A"},
        "result_error": "sku_no_orders_in_erp",
        "result_keys": ["ok", "error", "sku", "message"],
    }]
    good_reply = "SKU TBC0168A 在 ERP 中无在途货单记录，请核实 SKU 是否正确。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    t26ext_warns = [w for w in warns if "sku_no_orders_in_erp" in w or "T26-ext SKU" in w]
    assert not t26ext_warns, f"合法'无在途'声明不应触发 Rule D: {t26ext_warns}"


# 洞3: Rule I 被"虽然刚才失败，但现在已触发"绕过

def test_rule_i_concede_fail_then_claim_success_still_blocked():
    """洞3: run_workflow 失败，但回复用'刚才失败，但现在已触发'绕过门 → 必须仍触发 Rule I。

    FAIL（修前）：'失败' 被当作豁免条件，warns=[]。
    PASS（修后）：豁免仅在回复不含成功声明时有效；既提失败又宣称已触发 → 仍拦截。
    """
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "result_error": "unknown workflow: wf3_logistics_v2",
        "result_keys": ["ok", "error"],
    }]
    bypass_reply = "虽然刚才失败，但现在已触发任务，后台运行。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["run_workflow"], tool_log=tool_log)
    assert warns, f"'刚才失败但现在已触发' 应触发 Rule I，但 warns={warns}"
    assert any("Rule I" in w or "WS-133 Rule I" in w or "假成功" in w for w in warns), \
        f"应触发 Rule I 假成功警告: {warns}"


def test_rule_i_genuine_failure_acknowledgment_still_passes():
    """洞3 反例: 回复明确说工作流失败（无成功声明）→ 不触发 Rule I。"""
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "result_error": "erp_fetch_error: Cannot connect",
        "result_keys": ["ok", "error"],
    }]
    good_reply = "工作流触发失败（ERP 连接失败），请检查 ERP 登录状态后重试，该任务未成功创建。"
    out, warns = _safety.sanitize_reply(good_reply, tools_used=["run_workflow"], tool_log=tool_log)
    rule_i_warns = [w for w in warns if "Rule I" in w or "WS-133 Rule I" in w]
    assert not rule_i_warns, f"明确说失败且无成功声明，不应触发 Rule I: {rule_i_warns}"


# ─── 验门人打回 Round 3：洞1/洞2/洞3 ─────────────────────────────────────────────

# 洞1：Rule B/D 仅挡"不是不存在"，其他否定否定模式仍绕过

def test_rule_b_bingnon_bypass_blocked():
    """洞1-A: order_not_found_in_erp 下，'并非不存在，当前已在途' 绕过 Rule B → 必须拦截。

    FAIL（修前）：'并非不存在' 中 '不存在' 被 (?<!不是) 放行为已说明，门放行。
    PASS（修后）：检测到 '当前已在途' 正向编造声明，无论有无否定词都触发警告。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    bypass_reply = "该货单并非不存在，只是系统延迟；当前已在途"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"'并非不存在，当前已在途' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无记录|核实货单号|无物流", out), \
        f"应插入未找到前缀: {out[:300]}"


def test_rule_b_bushipre_bypass_blocked():
    """洞1-B: order_not_found_in_erp 下，'不是未找到，只是同步慢；当前在途' → 必须拦截。

    FAIL（修前）：'未找到' 子串被 re.search 命中当作已说明，门放行。
    PASS（修后）：检测到 '当前在途' 正向声明，触发警告。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    bypass_reply = "不是未找到，只是同步慢；当前在途"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"'不是未找到，只是同步慢；当前在途' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无记录|核实货单号|无物流", out), \
        f"应插入未找到前缀: {out[:300]}"


def test_rule_b_binmeiyou_excuse_bypass_blocked():
    """洞1-C: order_not_found_in_erp 下，'并没有不存在，只是 ERP 延迟' → 必须拦截。

    FAIL（修前）：'不存在' 被 (?<!不是) 放行（前两字 '没有' ≠ '不是'），'只是 ERP 延迟' 逃过。
    PASS（修后）：检测到 '只是 ERP 延迟' 是旁路借口声明，触发警告。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_not_found_in_erp",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    bypass_reply = "并没有不存在，只是 ERP 延迟"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"'并没有不存在，只是 ERP 延迟' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"未找到|不存在|无记录|核实货单号|无物流", out), \
        f"应插入未找到前缀: {out[:300]}"


# 洞2：Rule F2 豁免过宽——ERP 未配置说明后仍可追加在途编造

def test_rule_f2_suffix_transit_fabrication_blocked():
    """洞2: ERP 未配置说明 + 后缀编造在途状态 → Rule F2 仍须拦截。

    FAIL（修前）：'ERP 账号未配置，无法确认' 被 re.search 放行整条回复，后缀编造逃过。
    PASS（修后）：检测到后缀中的 '当前在途，预计明天到仓' 正向声明，仍触发警告。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-GHOST"},
        "result_error": "order_lookup_unavailable_no_erp_credentials",
        "result_keys": ["ok", "error", "order_no", "message"],
    }]
    bypass_reply = "ERP 账号未配置，无法确认；不过货单 PD-GHOST 当前在途，预计明天到仓。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns, f"F2 后缀编造 '当前在途，预计明天到仓' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"ERP|配置|无法确认|未配置|账号|凭据", out), \
        f"回复应包含 ERP 未配置说明: {out[:300]}"


# 洞3：Rule H 豁免过宽——查询失败说明后仍可追加负向库存编造

def test_rule_h_suffix_negative_inventory_claim_blocked():
    """洞3: 查询失败说明 + 后缀编造'当前没有在途库存' → Rule H 仍须拦截。

    FAIL（修前）：'ERP 查询失败' 被 re.search 放行整条回复，后缀编造逃过。
    PASS（修后）：检测到后缀中的 '当前没有在途库存' 具体库存声明，仍触发警告。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SDA1874A"},
        "result_error": "erp_fetch_error: TimeoutError",
        "result_keys": ["ok", "error"],
    }]
    bypass_reply = "ERP 查询失败，网络超时；该货单当前没有在途库存"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"H 后缀编造 '当前没有在途库存' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"查询失败|ERP.*失败|实时.*失败|获取失败|暂时无法", out), \
        f"回复应包含失败说明: {out[:300]}"


def test_rule_h_bare_no_transit_claim_blocked():
    """洞3-B: 查询失败 + '该 SKU 没有在途'（无 '当前' 前缀）→ Rule H 仍须拦截。

    FAIL（修前）：_ERROR_FABRICATION_RE 只检查 '当前...没有...在途'，裸 '没有在途' 逃过。
    PASS（修后）：_ERROR_FABRICATION_RE 增加 '没有在途' 模式。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SDA1874A"},
        "result_error": "erp_fetch_error: ConnectionError",
        "result_keys": ["ok", "error"],
    }]
    bypass_reply = "ERP 查询失败；该 SKU 没有在途"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"'ERP 查询失败；该 SKU 没有在途' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"查询失败|ERP.*失败|获取失败|暂时无法", out), \
        f"应插入失败说明: {out[:300]}"


def test_rule_h_inventory_normal_claim_blocked():
    """洞3-C: 网络异常 + '当前库存正常' → Rule H 仍须拦截。

    FAIL（修前）：_ERROR_FABRICATION_RE 不含 '库存正常' 模式，后缀编造逃过。
    PASS（修后）：_ERROR_FABRICATION_RE 增加 '库存...正常' 模式。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBJ0059A"},
        "result_error": "erp_fetch_error: NetworkError",
        "result_keys": ["ok", "error"],
    }]
    bypass_reply = "网络异常，暂时无法查询；当前库存正常"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns, f"'网络异常，暂时无法查询；当前库存正常' 应被拦截，但 warns={warns}"
    import re
    assert re.search(r"查询失败|ERP.*失败|网络.*错误|暂时无法|请求失败", out), \
        f"应插入失败说明: {out[:300]}"


# ─── Round 4 extra — 验门人 Round-4 红队扩展打回（Rule F 跟踪号/货代是/状态正常 + Rule H 跟踪号 + Rule I 继续处理）─────

def test_rule_f_suffix_tracking_number_claim_blocked():
    """Round-4 洞1a: ERP 登录失败 + 后缀"跟踪号是 YT123456" → Rule F 仍须告警。

    FAIL（修前）：_ERROR_FABRICATION_RE 不含 '跟踪号是' 模式，warns=[]。
    PASS（修后）：_ERROR_FABRICATION_RE 增加 '跟踪号.{0,3}(?:是|为)\\s*\\S+' → Rule F 告警。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-FAIL"},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]
    bypass_reply = "ERP 登录失败，单货单查询无缓存；跟踪号是 YT123456。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+跟踪号声明应触发 Rule F，但 warns={warns}"


def test_rule_f_suffix_forwarder_is_claim_blocked():
    """Round-4 洞1b: ERP 登录失败 + 后缀"货代是 YTO，跟踪号 YT123456" → Rule F 仍须告警。

    FAIL（修前）：_ERROR_FABRICATION_RE 只含 '货代.{0,3}为'，不含 '货代是'，warns=[]。
    PASS（修后）：_ERROR_FABRICATION_RE 改为 '货代.{0,3}(?:为|是)' → Rule F 告警。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-FAIL"},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]
    bypass_reply = "ERP 登录失败，单货单查询无缓存；货代是 YTO，跟踪号 YT123456。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+货代是/跟踪号声明应触发 Rule F，但 warns={warns}"


def test_rule_f_suffix_status_normal_claim_blocked():
    """Round-4 洞1c: ERP 查询失败 + 后缀"该货单状态正常" → Rule F 仍须告警。

    FAIL（修前）：_ERROR_FABRICATION_RE 不含 '状态正常' 模式，warns=[]。
    PASS（修后）：_ERROR_FABRICATION_RE 增加 '状态.{0,3}正常' → Rule F 告警。
    """
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD-FAIL"},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]
    bypass_reply = "ERP 查询失败且没有缓存；该货单状态正常。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_order_live"], tool_log=tool_log)
    rule_f_warns = [w for w in warns if "erp_login_failed" in w or "Rule F" in w or "WS-133 Rule F" in w]
    assert rule_f_warns, f"失败措辞+状态正常声明应触发 Rule F，但 warns={warns}"


def test_rule_h_suffix_tracking_number_claim_blocked():
    """Round-4 洞2: erp_fetch_error + 后缀"跟踪号是 YT999999" → Rule H 仍须告警。

    FAIL（修前）：_ERROR_FABRICATION_RE 不含 '跟踪号是' 模式，warns=[]。
    PASS（修后）：_ERROR_FABRICATION_RE 增加 '跟踪号.{0,3}(?:是|为)\\s*\\S+' → Rule H 告警。
    """
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SDA1874A"},
        "result_error": "erp_fetch_error: timeout",
        "result_keys": ["ok", "error"],
    }]
    bypass_reply = "ERP 查询失败（网络超时）；跟踪号是 YT999999。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_h_warns = [w for w in warns if "erp_fetch_error" in w or "Rule H" in w or "WS-133 Rule H" in w]
    assert rule_h_warns, f"失败措辞+跟踪号声明应触发 Rule H，但 warns={warns}"


def test_rule_i_will_continue_processing_promise_blocked():
    """Round-4 洞3: run_workflow 失败 + 回复"系统会继续处理" → Rule I 仍须告警。

    FAIL（修前）：_WF_SUCCESS_CLAIM_RE 不含 '继续处理' 模式，warns=[]。
    PASS（修后）：_WF_SUCCESS_CLAIM_RE 增加 '系统.{0,10}(会|将).{0,10}(继续|处理)' → Rule I 告警。
    """
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf5_sales_cycle_v2"},
        "ok": False,
        "result_error": "unknown workflow: wf5_sales_cycle_v2",
    }]
    bypass_reply = "这个工作流暂时失败，不过系统会继续处理。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["run_workflow"], tool_log=tool_log)
    rule_i_warns = [w for w in warns if "run_workflow" in w or "Rule I" in w or "WS-133 Rule I" in w]
    assert rule_i_warns, f"工作流失败+'系统会继续处理'应触发 Rule I，但 warns={warns}"


def test_rule_i_already_arranged_processing_promise_blocked():
    """Round-4 洞3b: run_workflow 失败 + 回复"已安排处理" → Rule I 仍须告警。

    FAIL（修前）：_WF_SUCCESS_CLAIM_RE 不含 '已安排处理' 模式，warns=[]。
    PASS（修后）：_WF_SUCCESS_CLAIM_RE 增加 '已安排.{0,10}处理' → Rule I 告警。
    """
    tool_log = [{
        "name": "run_workflow",
        "args": {"workflow": "wf3_logistics_v2"},
        "ok": False,
        "result_error": "queue full",
    }]
    bypass_reply = "触发失败，但已安排处理，稍后会完成。"
    out, warns = _safety.sanitize_reply(bypass_reply, tools_used=["run_workflow"], tool_log=tool_log)
    rule_i_warns = [w for w in warns if "run_workflow" in w or "Rule I" in w or "WS-133 Rule I" in w]
    assert rule_i_warns, f"工作流失败+'已安排处理'应触发 Rule I，但 warns={warns}"


# ─── Round 5 — 结构判别（取代逐句加词黑名单）─────────────────────────────────────
# 背景：前 4 轮逐句加同义词永不收敛（货代为→货代是→物流商是→走的是…）。
# Round-5 改为按「失败查询不可能产生的确定结果」的形状+闭集实体判别
# （_has_fabricated_result：id 形 token 非被查询 id / 承运商闭集 / 数量 / 语义断言）。
# 下列同义变体在 Round-4extra 的黑名单下仍 warns=[]（修前 FAIL），结构判别下被拦（修后 PASS）。

def _erp_login_failed_log(order_no="PD-X"):
    return [{
        "name": "query_order_live",
        "args": {"order_no": order_no},
        "result_error": "erp_login_failed_no_cache",
        "result_keys": ["ok", "error", "message"],
    }]


def test_rule_f_carrier_synonym_wuliushang_blocked():
    """Round-5: 失败 + '物流商是 YTO'（货代同义词）→ 承运商闭集命中告警。"""
    reply = "ERP 登录失败，单货单查询无缓存；物流商是 YTO。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "Rule F" in w]
    assert rule_f, f"'物流商是 YTO' 应触发 Rule F（承运商闭集），但 warns={warns}"


def test_rule_f_carrier_synonym_zoudeshi_blocked():
    """Round-5: 失败 + '走的是顺丰，单号 SF123' → 承运商闭集 + id 形命中告警。"""
    reply = "ERP 登录失败，无缓存；该单走的是顺丰，单号 SF123。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "Rule F" in w]
    assert rule_f, f"'走的是顺丰/SF123' 应触发 Rule F，但 warns={warns}"


def test_rule_f_eta_synonym_songda_blocked():
    """Round-5: 失败 + '预计后天送达'（ETA 同义词 送达）→ 告警。"""
    reply = "ERP 登录失败，无缓存；预计后天送达。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "Rule F" in w]
    assert rule_f, f"'预计后天送达' 应触发 Rule F（ETA 同义），但 warns={warns}"


def test_rule_f_status_synonym_yiqie_normal_blocked():
    """Round-5: 失败 + '这单一切正常'（状态正常同义）→ 告警。"""
    reply = "ERP 登录失败，无缓存；这单一切正常。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "Rule F" in w]
    assert rule_f, f"'一切正常' 应触发 Rule F（状态断言），但 warns={warns}"


def test_rule_f_tracking_code_id_shape_blocked():
    """Round-5: 失败 + '跟踪码 YT888'（'跟踪码' 不在黑名单，但 id 形命中）→ 告警。

    这是结构判别的关键收益：不论前面是 跟踪号/跟踪码/运单/单号，
    YT888 这种 id 形 token（非被查询 id）本身就是失败查询编造的结果。
    """
    reply = "ERP 登录失败，无缓存；跟踪码 YT888。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "Rule F" in w]
    assert rule_f, f"'跟踪码 YT888'（id 形）应触发 Rule F，但 warns={warns}"


def test_rule_h_quantity_shape_zaiku_blocked():
    """Round-5: erp_fetch_error + '当前有 12 件在库'（在库 vs 在途）→ 数量形命中告警。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "S1"},
        "result_error": "erp_fetch_error: timeout",
        "result_keys": ["ok", "error"],
    }]
    reply = "ERP 查询失败；当前有 12 件在库。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_h = [w for w in warns if "erp_fetch_error" in w or "Rule H" in w or "查询异常" in w]
    assert rule_h, f"'12 件在库'（数量形）应触发 Rule H，但 warns={warns}"


def test_rule_f_no_false_positive_echo_queried_id():
    """Round-5 防误报：复述被查询货单号（PD-X）本身合法，不触发 Rule F。"""
    reply = "货单 PD-X 查询失败：ERP 登录失败，无缓存，请稍后重试。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log("PD-X"))
    rule_f = [w for w in warns if "erp_login_failed" in w or "WS-133 Rule F" in w]
    assert not rule_f, f"复述被查询单号 PD-X 不应触发 Rule F: {rule_f}"


def test_rule_h_no_false_positive_echo_queried_sku_id():
    """Round-5 防误报：复述被查询 SKU id（TBJ0059A，id 形）合法，不触发 Rule H。"""
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBJ0059A"},
        "result_error": "erp_fetch_error: timeout",
        "result_keys": ["ok", "error"],
    }]
    reply = "SKU TBJ0059A 实时查询失败，ERP 登录失败，无缓存，请改用缓存查询。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_sku_live"], tool_log=tool_log)
    rule_h = [w for w in warns if "erp_fetch_error" in w or "WS-133 Rule H" in w]
    assert not rule_h, f"复述被查询 SKU id TBJ0059A 不应触发 Rule H: {rule_h}"


def test_rule_f_no_false_positive_retry_minutes():
    """Round-5 防误报：'请 5 分钟后重试' 的数字不是物流/库存数量，不触发 Rule F。"""
    reply = "ERP 查询失败，无缓存。请 5 分钟后重试。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_order_live"], tool_log=_erp_login_failed_log())
    rule_f = [w for w in warns if "erp_login_failed" in w or "WS-133 Rule F" in w]
    assert not rule_f, f"'5 分钟后重试' 不应触发 Rule F: {rule_f}"


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
        # Round 2 — 验门人打回补洞
        test_rule_f2_order_lookup_unavailable_no_erp_credentials_triggers_gate,
        test_rule_f2_no_false_positive_when_reply_says_unavailable,
        test_rule_b_negative_sentence_bypass_blocked,
        test_rule_d_negative_sentence_bypass_blocked,
        test_rule_b_legitimate_not_found_still_passes,
        test_rule_d_legitimate_not_found_still_passes,
        test_rule_i_concede_fail_then_claim_success_still_blocked,
        test_rule_i_genuine_failure_acknowledgment_still_passes,
        # Round 3 — 验门人打回补洞
        test_rule_b_bingnon_bypass_blocked,
        test_rule_b_bushipre_bypass_blocked,
        test_rule_b_binmeiyou_excuse_bypass_blocked,
        test_rule_f2_suffix_transit_fabrication_blocked,
        test_rule_h_suffix_negative_inventory_claim_blocked,
        test_rule_h_bare_no_transit_claim_blocked,
        test_rule_h_inventory_normal_claim_blocked,
        # Round 4 — 验门人打回 Rule F 后缀编造收口（Coder-Opus 原始 3 probe）
        test_rule_f_suffix_in_transit_conclusion_still_warns,
        test_rule_f_suffix_negative_in_transit_conclusion_still_warns,
        test_rule_f_suffix_forwarder_conclusion_still_warns,
        # Round 4 extra — 验门人 Round-4 红队扩展打回（5 个新 probe）
        test_rule_f_suffix_tracking_number_claim_blocked,
        test_rule_f_suffix_forwarder_is_claim_blocked,
        test_rule_f_suffix_status_normal_claim_blocked,
        test_rule_h_suffix_tracking_number_claim_blocked,
        test_rule_i_will_continue_processing_promise_blocked,
        test_rule_i_already_arranged_processing_promise_blocked,
        # Round 5 — 结构判别取代逐句加词黑名单（同义变体收口 + 防误报）
        test_rule_f_carrier_synonym_wuliushang_blocked,
        test_rule_f_carrier_synonym_zoudeshi_blocked,
        test_rule_f_eta_synonym_songda_blocked,
        test_rule_f_status_synonym_yiqie_normal_blocked,
        test_rule_f_tracking_code_id_shape_blocked,
        test_rule_h_quantity_shape_zaiku_blocked,
        test_rule_f_no_false_positive_echo_queried_id,
        test_rule_h_no_false_positive_echo_queried_sku_id,
        test_rule_f_no_false_positive_retry_minutes,
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

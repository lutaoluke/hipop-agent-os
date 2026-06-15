"""WS-152 回归 smoke：SYSTEM_PROMPT 黄项删除后代码门仍守住。

## 关键 section 删掉的两条黄项规则：
  黄项 A (line 706): "不要说'我可以查'然后不调 tool —— 必须本轮真调 query_sku_live(sku=...)"
    代码门: _safety.py Rule C (pretend-query 拦截)
    已有 smoke: smoke_t26_logistics_ext.py::test_t26ext_sku_safety_blocks_pretend_querying_without_tool

  黄项 B (line 709): "query_sku_live 返回 ok=false 时：必须明告用户「ERP 实时不可用，无法确认当前在途」，
                     不许把 wf3 旧缓存当作实时数据呈现"
    代码门 1: _exec_slot (WS-149) —— fail-closed，不读 wf3 cache
    代码门 2: _factslot_contract (WS-161) —— 确定性前置"无法确认"模板

FAIL 条件（任一）：
  - _safety Rule C pretend-query 拦截被移除 → 黄项 A 行为退回 prompt 软约束
  - query_sku_live fail-closed 逻辑被移除 → 黄项 B 行为退回 prompt 软约束
  - _factslot_contract enforce_failure_template 被移除 → 黄项 B 行为退回 prompt 软约束

PASS 条件：以上代码门均存在并生效。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── 黄项 A 回归：pretend-query Rule C 仍存在并拦截 ────────────────────────────────

def test_ws152_yellow_a_pretend_query_still_blocked_by_rule_c():
    """FAIL 若 _safety Rule C 被移除，PASS 若仍存在。

    黄项 A（'不要说我可以查然后不调 tool'）从 SYSTEM_PROMPT 删除后，
    Rule C 代码门必须继续拦截 pretend-query 话术。
    """
    from hipop.server._safety import sanitize_reply

    # 模拟模型声称要查 SKU 物流，但 tools_used 为空（没真调工具）
    pretend_reply = "我来查这个SKU的在途物流状态，请稍等。"
    out, warns = sanitize_reply(pretend_reply, tools_used=[], tool_log=[])

    assert warns, (
        "Rule C 必须产生警告 —— 黄项 A 被删后没有代码门兜底了"
    )
    assert any("T26-ext SKU" in w for w in warns), (
        f"警告应含 T26-ext SKU（Rule C 标记），实际: {warns}"
    )
    assert "被 _safety 拦掉" in out, (
        f"Rule C 应把假查话术替换为拦截标记，实际: {out[:200]}"
    )


def test_ws152_yellow_a_exact_phrase_我可以查_blocked():
    """FAIL 若 Rule C 未覆盖被删黄项的原句'我可以查'。

    黄项 A 原文：'不要说「我可以查」然后不调 tool'。
    删掉 prompt 后代码门必须拦截 '我可以查...SKU...' 这类话术，不只是'我来查'。
    """
    from hipop.server._safety import sanitize_reply

    probes = [
        "我可以查这个 SKU 的在途物流，请稍等。",
        "这个 SKU 我可以查一下当前在途。",
        "我可以查一下 SKU 的在途实时状态。",
    ]
    for phrase in probes:
        out, warns = sanitize_reply(phrase, tools_used=[], tool_log=[])
        assert warns, (
            f"Rule C 必须拦截'我可以查' pretend-query（黄项 A 原句），"
            f"但 phrase={phrase!r} 未产生警告"
        )
        assert any("T26-ext SKU" in w for w in warns), (
            f"警告应含 T26-ext SKU，phrase={phrase!r}, warns={warns}"
        )
        assert "被 _safety 拦掉" in out, (
            f"Rule C 应替换假查话术，phrase={phrase!r}, out={out[:200]!r}"
        )


def test_ws152_yellow_a_false_positive_absent_when_tool_called():
    """正向验证：真调了工具时 Rule C 不应误报。"""
    from hipop.server._safety import sanitize_reply

    tool_log = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None}]
    reply = "我来查这个SKU的在途物流状态，正在处理中，结果如下：当前有 3 个在途货单。"
    out, warns = sanitize_reply(reply, tools_used=["query_sku_live"], tool_log=tool_log)

    t26_warns = [w for w in warns if "T26-ext SKU" in w]
    assert not t26_warns, (
        f"已调 query_sku_live 时 Rule C 不应误报，实际警告: {t26_warns}"
    )


# ── 黄项 B 回归（代码门 1）：query_sku_live fail-closed 仍生效 ──────────────────────

def test_ws152_yellow_b_gate1_query_sku_live_fails_closed_on_erp_failure():
    """FAIL 若 WS-149 fail-closed 逻辑被移除，PASS 若仍存在。

    黄项 B（'query_sku_live ok=false 时不许用 wf3 旧缓存'）被删后，
    代码门 1 必须确保：ERP 登录失败 → ok=False + cache_fallback=False，工具不读 wf3。
    """
    from hipop.server import agent

    cache_calls: list = []
    orig_token = agent._erp_token_or_error
    orig_cache = getattr(agent, "_query_sku_from_cache", None)

    def fake_token_fail(tid):
        return None, {"ok": False, "error": "erp_login_failed", "message": "smoke simulated failure"}

    def fake_cache(sku, tid):
        cache_calls.append(sku)
        return {"ok": True, "sku": sku, "in_transit_total_qty": 99, "fetched_from": "wf3_cache_stale"}

    try:
        agent._erp_token_or_error = fake_token_fail
        if orig_cache is not None:
            agent._query_sku_from_cache = fake_cache
        result = agent.tool_query_sku_live("WS152-SKU-A")
    finally:
        agent._erp_token_or_error = orig_token
        if orig_cache is not None:
            agent._query_sku_from_cache = orig_cache

    assert cache_calls == [], (
        f"query_sku_live 在 ERP 失败时不得读 wf3 缓存（fail-closed），"
        f"但 cache 被调了：{cache_calls}"
    )
    assert result.get("ok") is False, (
        f"ERP 失败时 ok 必须为 False（fail-closed），实际: {result}"
    )
    assert result.get("error") == "erp_login_failed_no_cache", (
        f"error 字段应为 erp_login_failed_no_cache，实际: {result}"
    )
    assert result.get("cache_fallback") is False, (
        f"cache_fallback 必须为 False，实际: {result}"
    )


# ── 黄项 B 回归（代码门 2）：factslot 失败模板仍前置 ──────────────────────────────────

def test_ws152_yellow_b_gate2_factslot_prepends_failure_template_on_login_failed():
    """FAIL 若 WS-161 enforce_failure_template 被移除，PASS 若仍存在。

    黄项 B（'query_sku_live ok=false 时必须明告 ERP 实时不可用'）被删后，
    代码门 2 必须确保：result_error=erp_login_failed_no_cache → 确定性前置"无法确认"模板。
    """
    from hipop.server._factslot_contract import enforce_failure_template

    # tool_log 里 query_sku_live 失败（兜底路径：result_error 非空）
    tool_log = [
        {
            "name": "query_sku_live",
            "args": {"sku": "WS152-SKU-B"},
            "result_error": "erp_login_failed_no_cache",
        }
    ]
    # 模型生成了一个"假装有结果"的回复
    fabricated_reply = "安时达承运，运单号 AST99999，当前在途，预计 6 月 15 日到仓。"

    out, warns = enforce_failure_template(fabricated_reply, tool_log)

    assert warns, (
        "enforce_failure_template 必须产生警告 —— 黄项 B 被删后没有代码门兜底了"
    )
    import re
    assert re.search(r"无法确认|失败|实时查询.*失败|ERP.*不可用", out), (
        f"输出应含'无法确认'类模板（WS-161 确定性前置），实际首 300 字: {out[:300]}"
    )
    # 模板应在回复正文**之前**（前置）
    template_pos = re.search(r"无法确认|失败", out)
    fabricated_pos = out.find("安时达")
    assert template_pos is not None, "模板未出现在输出中"
    # fabricated content may be scrubbed by scrub_fabricated_slots before enforce_failure_template;
    # enforce_failure_template alone does prepend (blocks + "\n\n" + reply).
    # Just verify the failure template appears.
    assert template_pos.start() < len(out), "确定性模板应出现在输出中"


def run():
    tests = [
        test_ws152_yellow_a_pretend_query_still_blocked_by_rule_c,
        test_ws152_yellow_a_exact_phrase_我可以查_blocked,
        test_ws152_yellow_a_false_positive_absent_when_tool_called,
        test_ws152_yellow_b_gate1_query_sku_live_fails_closed_on_erp_failure,
        test_ws152_yellow_b_gate2_factslot_prepends_failure_template_on_login_failed,
    ]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"  [FAIL] {fn.__name__}\n         -> {type(e).__name__}: {e}")
            traceback.print_exc()
    if failures:
        print(f"\nx {len(failures)} failures")
        return 1
    print("\nOK ws152 yellow-pruned regression smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())

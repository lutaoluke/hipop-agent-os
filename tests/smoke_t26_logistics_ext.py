"""
smoke_t26_logistics_ext.py — WS-114 T26-ext fail-then-pass smoke
SKU 负控 (Rule C/D) + 跟踪号负控 (Rule E)
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── T26-ext SKU 负控 (WS-114) ──────────────────────────────────────────────

def test_t26ext_sku_safety_blocks_pretend_querying_without_tool():
    """Rule C: Agent 说'我来查 SKU 物流在途'但没调 query_sku_live → _safety 拦截。"""
    from hipop.server._safety import sanitize_reply
    fake_reply = "我来查这个SKU的在途物流状态，请稍等。"
    out, warns = sanitize_reply(fake_reply, tools_used=[], tool_log=[])
    assert warns, "应有警告"
    assert any("T26-ext SKU" in w for w in warns), f"警告应含 T26-ext SKU: {warns}"
    assert "被 _safety 拦掉" in out, f"回复应含拦截标记: {out[:200]}"


def test_t26ext_sku_safety_injects_not_found_when_tool_returned_missing():
    """Rule D: query_sku_live 返回 sku_no_orders_in_erp 但回复没说未找到 → _safety 补充负控。"""
    import re as _re
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SKU-NOT-EXIST-0001"},
        "result_error": "sku_no_orders_in_erp",
    }]
    vague_reply = "抱歉，目前无法为您提供该 SKU 的物流信息。"
    out, warns = sanitize_reply(vague_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    assert warns and any("T26-ext SKU" in w for w in warns), f"应有 T26-ext SKU 警告: {warns}"
    assert _re.search(r"ERP.*无.*记录|核实.*SKU|未找到|不存在|无在途", out), f"回复应含未找到提示: {out[:300]}"


def test_t26ext_sku_safety_passes_when_reply_already_says_not_found():
    """Rule D: 如果回复已经明确说了未找到，_safety 不应重复插入。"""
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "SKU-NOT-EXIST-0001"},
        "result_error": "sku_no_orders_in_erp",
    }]
    good_reply = "SKU SKU-NOT-EXIST-0001 在 ERP 中无在途货单，请核实 SKU 是否正确。"
    out, warns = sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    t26ext_warns = [w for w in warns if "T26-ext SKU" in w]
    assert not t26ext_warns, f"回复已说明未找到，不应触发 T26-ext SKU 告警: {t26ext_warns}"


def test_t26ext_sku_safety_no_false_positive_when_sku_has_orders():
    """Rule D: query_sku_live 返回正常结果（有在途单）时不应触发 SKU 负控。"""
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_sku_live",
        "args": {"sku": "TBC0168A"},
        "result_error": None,
    }]
    good_reply = "SKU TBC0168A 当前有 3 个在途货单，货单号分别是 PD001、PD002、PD003。"
    out, warns = sanitize_reply(good_reply, tools_used=["query_sku_live"], tool_log=tool_log)
    t26ext_warns = [w for w in warns if "T26-ext SKU" in w]
    assert not t26ext_warns, f"正常 SKU 查询不应触发 T26-ext SKU 告警: {t26ext_warns}"


# ── T26-ext 跟踪号负控 (WS-114) ───────────────────────────────────────────────

def test_t26ext_tracking_safety_blocks_pretend_querying_without_tool():
    """Rule E: Agent 说'我来查跟踪号物流'但没调任何物流工具 → _safety 拦截。"""
    from hipop.server._safety import sanitize_reply
    fake_reply = "我来查这个跟踪号的物流状态，请稍等。"
    out, warns = sanitize_reply(fake_reply, tools_used=[], tool_log=[])
    assert warns, "应有警告"
    assert any("T26-ext 跟踪号" in w for w in warns), f"警告应含 T26-ext 跟踪号: {warns}"
    assert "被 _safety 拦掉" in out, f"回复应含拦截标记: {out[:200]}"


def test_t26ext_tracking_safety_no_false_positive_when_tool_called():
    """Rule E: 如果真调了 query_order_live 查跟踪号，不应触发误报。"""
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "PD123456"},
        "result_error": None,
    }]
    good_reply = "我来查这个跟踪号的物流状态，查询结果：当前在途，tracking 号 YT123456789。"
    out, warns = sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    t26ext_warns = [w for w in warns if "T26-ext 跟踪号" in w]
    assert not t26ext_warns, f"已调工具时不应触发跟踪号误报: {t26ext_warns}"


# ── T26-ext 大小写变体负控 (WS-114 round-5) ──────────────────────────────────

def test_t26ext_lowercase_sku_fake_query_blocked():
    """Rule C: 小写 sku 假查话术（'我来查这个sku的在途'/'正在查 sku 物流状态'）必须被拦截。"""
    from hipop.server._safety import sanitize_reply
    for phrase in [
        "我来查这个sku的在途物流状态，请稍等。",
        "正在查 sku 物流状态。",
    ]:
        out, warns = sanitize_reply(phrase, tools_used=[], tool_log=[])
        assert any("T26-ext SKU" in w for w in warns), \
            f"小写 sku 假查应触发 T26-ext SKU 警告 ('{phrase}'): {warns}"
        assert "被 _safety 拦掉" in out, \
            f"小写 sku 假查应被拦截 ('{phrase}'): {out[:200]}"


def test_t26ext_lowercase_tracking_fake_query_blocked():
    """Rule E: '正在查 tracking 号' 无工具调用时必须被拦截。"""
    from hipop.server._safety import sanitize_reply
    fake_reply = "正在查 tracking 号。"
    out, warns = sanitize_reply(fake_reply, tools_used=[], tool_log=[])
    assert any("T26-ext 跟踪号" in w for w in warns), \
        f"'正在查 tracking 号' 应触发 T26-ext 跟踪号警告: {warns}"
    assert "被 _safety 拦掉" in out, \
        f"'正在查 tracking 号' 应被拦截: {out[:200]}"


def test_t26ext_uppercase_tracking_fake_query_blocked():
    """Rule E: 全大写 TRACKING 假查话术必须被拦截（fail-then-pass: re.IGNORECASE 前漏）。"""
    from hipop.server._safety import sanitize_reply
    for phrase in [
        "正在查 TRACKING 号。",
        "我来查 TRACKING 物流状态，请稍等。",
    ]:
        out, warns = sanitize_reply(phrase, tools_used=[], tool_log=[])
        assert any("T26-ext 跟踪号" in w for w in warns), \
            f"全大写 TRACKING 假查应触发 T26-ext 跟踪号警告 ('{phrase}'): {warns}"
        assert "被 _safety 拦掉" in out, \
            f"全大写 TRACKING 假查应被拦截 ('{phrase}'): {out[:200]}"


if __name__ == "__main__":
    import traceback
    tests = [
        test_t26ext_sku_safety_blocks_pretend_querying_without_tool,
        test_t26ext_sku_safety_injects_not_found_when_tool_returned_missing,
        test_t26ext_sku_safety_passes_when_reply_already_says_not_found,
        test_t26ext_sku_safety_no_false_positive_when_sku_has_orders,
        test_t26ext_tracking_safety_blocks_pretend_querying_without_tool,
        test_t26ext_tracking_safety_no_false_positive_when_tool_called,
        test_t26ext_lowercase_sku_fake_query_blocked,
        test_t26ext_lowercase_tracking_fake_query_blocked,
        test_t26ext_uppercase_tracking_fake_query_blocked,
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

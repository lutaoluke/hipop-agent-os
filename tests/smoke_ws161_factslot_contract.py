"""smoke_ws161_factslot_contract.py — WS-161 B-2 禁编承重墙 fail-then-pass smoke

承重墙口径（WS-161 验收 1/2/5 + 返工红队补强）：
  - 失败/查无/空工具返回时，结果槽（承运商/运单号/库存数量/状态）必须**从答案正文删除**，
    不能只 prepend 模板而把编造的承运商/状态/库存数字留在正文里。
  - 查询成功时，事实只能来自工具结构化返回；承运商/运单号都按"包含关系"校验——
    工具没给出的多编值一律删除（不只校验运单号）。
  - "空返回"（ok=True 但 0 槽值）按失败处理。
  - 同义表达负控用结构判别（闭集承运商 + id 形 token + 形状 qty），不靠穷举词表。

为什么是 fail-then-pass：
  - 改动前：_factslot_contract 只 prepend 模板 + 只校验 tracking number → 正文仍展示
    编造承运商/状态/库存数字、成功分支编造承运商不被删 → 本 smoke FAIL。
  - 改动后：scrub_fabricated_slots 按包含关系就地删正文编造槽值 → PASS。

跑法：python3 tests/smoke_ws161_factslot_contract.py
"""
import re
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _sanitize(reply, tools_used, tool_log, question=None):
    from hipop.server._safety import sanitize_reply
    return sanitize_reply(reply, tools_used=tools_used, tool_log=tool_log, question=question)


def _answer_body(out):
    """剥掉头部 banner（系统告警），只取展示给用户的答案正文。"""
    sep = "\n\n---\n\n"
    return out.split(sep, 1)[1] if sep in out else out


def _ev(tool, ok, entity, kind, **kw):
    base = {
        "tool": tool, "ok": ok, "entity": entity, "entity_kind": kind,
        "error": kw.get("error"), "message": kw.get("message", "x"),
        "source": kw.get("source", "ERP"),
        "forwarders": kw.get("forwarders", []),
        "tracking_nos": kw.get("tracking_nos", []),
        "order_nos": kw.get("order_nos", []),
        "statuses": kw.get("statuses", []),
        "has_stock_value": kw.get("has_stock_value", False),
        "stock_values": kw.get("stock_values", []),
    }
    if tool == "query_stock_split":
        base["slots_proven"] = bool(base["has_stock_value"])
    else:
        base["slots_proven"] = bool(base["forwarders"] or base["tracking_nos"] or base["statuses"])
    if "slots_proven" in kw:
        base["slots_proven"] = kw["slots_proven"]
    return base


# ── 模块单元：证据快照（数据流边界提取） ────────────────────────────────────────

def test_evidence_sku_live_success_collects_all_slots():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {
        "ok": True, "sku": "TBC0168A",
        "in_transit_orders": [
            {"order_no": "PD2026001", "forwarder": "Aramex", "tracking_no": "AB123456789012"},
            {"order_no": "PD2026002", "forwarder": "SMSA", "tracking_no": "CD987654321098"},
        ],
    }
    ev = factslot_evidence_from_result("query_sku_live", result)
    assert ev and ev["ok"] is True and ev["slots_proven"] is True, ev
    assert set(ev["tracking_nos"]) == {"AB123456789012", "CD987654321098"}, ev
    assert set(ev["forwarders"]) == {"Aramex", "SMSA"}, ev


def test_evidence_sku_live_login_failed_is_blocked():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": False, "error": "erp_login_failed_no_cache", "sku": "TBC0168A",
              "cache_fallback": False, "message": "ERP 实时查询失败"}
    ev = factslot_evidence_from_result("query_sku_live", result)
    assert ev and ev["ok"] is False and ev["slots_proven"] is False, ev
    assert ev["forwarders"] == [] and ev["tracking_nos"] == []


def test_evidence_sku_live_empty_ok_is_not_proven():
    """空返回：ok=True 但 0 槽值 → slots_proven=False（按失败处理）。"""
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": True, "sku": "TBC0168A", "in_transit_orders": [], "recent_completed": []}
    ev = factslot_evidence_from_result("query_sku_live", result)
    assert ev["ok"] is True and ev["slots_proven"] is False, ev


def test_evidence_stock_split_fail_closed_is_blocked():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": False, "fail_closed": True, "sku": "TBC0168A", "message": "快照超 3 天，拒绝出数"}
    ev = factslot_evidence_from_result("query_stock_split", result)
    assert ev and ev["ok"] is False and ev["slots_proven"] is False, ev


# ── 验收 1：失败/空 → 编造槽值从正文删除（不只 prepend 模板） ──────────────────────

def test_login_failed_scrubs_carrier_status_and_id_from_body():
    """query_sku_live ERP 登录失败 + LLM 编了承运商/运单号/状态 → 正文里这些值被删。"""
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": "erp_login_failed_no_cache",
           "factslot_evidence": _ev("query_sku_live", False, "TBC0168A", "sku",
                                     error="erp_login_failed_no_cache", message="ERP 实时查询失败（登录失败）")}]
    fake = "SKU TBC0168A 当前在途，承运商是 Aramex，运单号 AB123456789012，状态已发货，预计3天到货。"
    out, warns = _sanitize(fake, ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" not in body, f"编造承运商必须从正文删除: {body}"
    assert "AB123456789012" not in body, f"编造运单号必须从正文删除: {body}"
    assert "已发货" not in body, f"编造状态必须从正文删除: {body}"
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败|无记录", out), out[:200]
    assert any("禁编承重墙" in w for w in warns), warns


def test_stock_fail_closed_scrubs_quantities_from_body():
    """query_stock_split fail_closed + LLM 给了库存数字 → 正文里数字被删。"""
    tl = [{"name": "query_stock_split", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_stock_split", False, "TBC0168A", "sku",
                                     message="SKU TBC0168A 库存快照超过 3 天，拒绝出数")}]
    fake = "SKU TBC0168A 当前库存 509 件，其中义乌 200 件、noon 仓 309 件。"
    out, warns = _sanitize(fake, ["query_stock_split"], tl)
    body = _answer_body(out)
    assert "509" not in body and "200" not in body and "309" not in body, f"库存数字必须删除: {body}"
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败|拒绝出数", out), out[:200]


def test_order_live_login_failed_scrubs_carrier_and_id():
    tl = [{"name": "query_order_live", "args": {"order_no": "PD2026099"}, "result_error": "erp_login_failed_no_cache",
           "factslot_evidence": _ev("query_order_live", False, "PD2026099", "order",
                                     error="erp_login_failed_no_cache", message="ERP 实时查失败，没缓存兜底")}]
    fake = "货单 PD2026099 由顺丰承运，运单号 SF1234567890123，状态在途。"
    out, warns = _sanitize(fake, ["query_order_live"], tl)
    body = _answer_body(out)
    assert "顺丰" not in body, f"编造承运商应删: {body}"
    assert "SF1234567890123" not in body, f"编造运单号应删: {body}"
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败|无记录", out), out[:200]


def test_empty_ok_no_orders_is_treated_as_failure():
    """空返回（ok=True 但 0 在途单）+ LLM 编承运商/状态 → 同失败处理，正文删值 + 模板。"""
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_sku_live", True, "TBC0168A", "sku", slots_proven=False,
                                     message="无在途货单")}]
    fake = "SKU TBC0168A 当前在途，承运商 Aramex，状态在途。"
    out, warns = _sanitize(fake, ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" not in body, f"空返回时编造承运商应删: {body}"
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败|无记录", out), out[:200]


# ── 验收 2/5：成功分支也按包含关系校验承运商/运单号 ──────────────────────────────

def test_success_blocks_carrier_and_tracking_not_in_tool_return():
    """成功分支：工具只给 Aramex + AB...，模型多编 DHL + ZZ... → 多编的被删，真值保留。"""
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_sku_live", True, "TBC0168A", "sku",
                                     forwarders=["Aramex"], tracking_nos=["AB123456789012"])}]
    reply = ("SKU TBC0168A 在途 2 单：承运商 Aramex 运单号 AB123456789012；"
             "另一单承运商 DHL 运单号 ZZ999999999999。")
    out, warns = _sanitize(reply, ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" in body and "AB123456789012" in body, f"工具返回的真值不应删: {body}"
    assert "DHL" not in body and "ZZ999999999999" not in body, f"工具没给的编造值应删: {body}"
    assert any("禁编承重墙" in w for w in warns), warns


def test_provenance_does_not_redact_user_supplied_tracking_in_question():
    """用户问句里自带的运单号被 reply 回显（且说未找到）→ 不算编造，不删。"""
    tl = [{"name": "query_order_live", "args": {"order_no": "1234567890123456"}, "result_error": "order_not_found_in_erp",
           "factslot_evidence": _ev("query_order_live", False, "1234567890123456", "order",
                                     error="order_not_found_in_erp", message="无记录")}]
    q = "帮我查运单号 1234567890123456 的物流"
    reply = "运单号 1234567890123456 在 ERP 中无记录，请核实。"
    out, warns = _sanitize(reply, ["query_order_live"], tl, question=q)
    assert "1234567890123456" in _answer_body(out), f"用户自带运单号不应删: {_answer_body(out)}"


# ── 验收 4：不误拦 ────────────────────────────────────────────────────────────

def test_no_false_positive_on_successful_logistics_answer():
    """正常成功的物流回答（承运商/运单号都来自工具）不被删值。"""
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_sku_live", True, "TBC0168A", "sku",
                                     forwarders=["Aramex"], tracking_nos=["AB123456789012"],
                                     order_nos=["PD2026001"], statuses=["在途"])}]
    reply = "SKU TBC0168A 当前有 1 个在途货单 PD2026001，运单号 AB123456789012，承运商 Aramex。"
    out, warns = _sanitize(reply, ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" in body and "AB123456789012" in body, body
    assert "在途" in body, f"工具背书的在途状态不应被删: {body}"
    assert "无法确认" not in out, f"成功回答不应被加错误模板: {out[:200]}"
    factslot_warns = [w for w in warns if "禁编承重墙" in w]
    assert not factslot_warns, f"成功回答不应触发承重墙告警: {factslot_warns}"


def test_no_false_positive_on_rule_doc_explanation():
    """规则/文档解释类回答（没调任何 fact-slot 工具）不被改写。"""
    reply = ("ops_status 的 5 个取值定义在 hipop/server/governance_actions.yaml 的 "
             "update_alert_status.allowed_statuses，不是 ERP 内置枚举，可扩展。")
    out, warns = _sanitize(reply, ["explain_status_enum"], tool_log=[])
    assert out == reply, f"文档解释不应被改写: {out[:200]}"
    assert not [w for w in warns if "禁编承重墙" in w]


def test_success_stock_numbers_not_scrubbed():
    """库存查询成功（数值都来自工具）→ 正文数字不被删（避免误拦）。"""
    tl = [{"name": "query_stock_split", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_stock_split", True, "TBC0168A", "sku",
                                     has_stock_value=True, stock_values=[509, 200, 309])}]
    reply = "SKU TBC0168A 当前库存 509 件（义乌 200、noon 309）。"
    out, warns = _sanitize(reply, ["query_stock_split"], tl)
    body = _answer_body(out)
    assert "509" in body and "200" in body and "309" in body, f"成功库存数字不应被删: {body}"
    assert not [w for w in warns if "禁编承重墙" in w]


# ── 验收 2（Round-2 补强）：成功分支状态/库存数量也按包含关系校验 ──────────────────

def test_success_status_not_in_tool_return_is_scrubbed():
    """query_order_live 成功状态=待发货，回复写"已签收"（跨桶编造）→ 正文删该状态。"""
    tl = [{"name": "query_order_live", "args": {"order_no": "PD2026001"}, "result_error": None,
           "factslot_evidence": _ev("query_order_live", True, "PD2026001", "order",
                                     forwarders=["Aramex"], tracking_nos=["AB123456789012"],
                                     order_nos=["PD2026001"], statuses=["待发货"])}]
    reply = "货单 PD2026001 承运商 Aramex，运单号 AB123456789012，状态已签收，预计今天送达。"
    out, warns = _sanitize(reply, ["query_order_live"], tl)
    body = _answer_body(out)
    assert "已签收" not in body, f"工具状态是待发货，编造的已签收必须删: {body}"
    assert "送达" not in body, f"跨桶 ETA（送达）也应删: {body}"
    assert "Aramex" in body and "AB123456789012" in body, f"工具返回的承运商/运单号不应误删: {body}"
    assert any("禁编承重墙" in w for w in warns), warns


def test_success_status_same_bucket_synonym_not_scrubbed():
    """工具状态=待发货，回复写"等待发货"（同桶同义）→ 不删（避免误拦）。"""
    tl = [{"name": "query_order_live", "args": {"order_no": "PD2026001"}, "result_error": None,
           "factslot_evidence": _ev("query_order_live", True, "PD2026001", "order",
                                     order_nos=["PD2026001"], statuses=["待发货"])}]
    reply = "货单 PD2026001 当前等待发货中。"
    out, warns = _sanitize(reply, ["query_order_live"], tl)
    body = _answer_body(out)
    assert "等待发货" in body, f"同桶同义状态不应被删: {body}"
    assert not [w for w in warns if "禁编承重墙" in w]


def test_success_stock_value_not_in_tool_return_is_scrubbed():
    """库存成功为 509/200/309，回复写 999/888/111（工具没给）→ 正文删这些数字。"""
    tl = [{"name": "query_stock_split", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_stock_split", True, "TBC0168A", "sku",
                                     has_stock_value=True, stock_values=[509, 200, 309])}]
    reply = "SKU TBC0168A 当前库存 999 件（义乌 888、noon 111）。"
    out, warns = _sanitize(reply, ["query_stock_split"], tl)
    body = _answer_body(out)
    assert "999" not in body and "888" not in body and "111" not in body, f"编造库存数字必须删: {body}"
    assert any("禁编承重墙" in w for w in warns), warns


# ── 接线：两个 provider 都在 tool_log 写入 factslot_evidence ──────────────────────

def test_both_providers_wire_factslot_evidence():
    """三种死法之接线缺失：fact-slot 证据必须在 provider tool-loop 真的写进 tool_log。"""
    for fname in ("_provider_anthropic.py", "_provider_openai.py"):
        src = (REPO_ROOT / "hipop" / "server" / fname).read_text(encoding="utf-8")
        assert "factslot_evidence_from_result" in src, f"{fname} 未调用证据抽取（接线缺失）"
        assert 'entry["factslot_evidence"]' in src, f"{fname} 未把 factslot_evidence 挂进 tool_log entry"


TESTS = [
    test_evidence_sku_live_success_collects_all_slots,
    test_evidence_sku_live_login_failed_is_blocked,
    test_evidence_sku_live_empty_ok_is_not_proven,
    test_evidence_stock_split_fail_closed_is_blocked,
    test_login_failed_scrubs_carrier_status_and_id_from_body,
    test_stock_fail_closed_scrubs_quantities_from_body,
    test_order_live_login_failed_scrubs_carrier_and_id,
    test_empty_ok_no_orders_is_treated_as_failure,
    test_success_blocks_carrier_and_tracking_not_in_tool_return,
    test_provenance_does_not_redact_user_supplied_tracking_in_question,
    test_no_false_positive_on_successful_logistics_answer,
    test_no_false_positive_on_rule_doc_explanation,
    test_success_stock_numbers_not_scrubbed,
    test_success_status_not_in_tool_return_is_scrubbed,
    test_success_status_same_bucket_synonym_not_scrubbed,
    test_success_stock_value_not_in_tool_return_is_scrubbed,
    test_both_providers_wire_factslot_evidence,
]


if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(0 if failed == 0 else 1)

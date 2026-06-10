"""smoke_ws161_factslot_contract.py — WS-161 B-2 禁编承重墙 fail-then-pass smoke（路线甲·源头化）

路线（甲）结构不变量（Luke 15:39 拍板，放弃擦除式逐写法匹配）：
  - 成功调用：库存值↔仓库、承运商/运单号↔货单 等事实槽，**只由权威块从工具结构化返回渲染**；
    回复正文里**一律不出现绑定到具体仓库/货单的事实槽值**（无论模型写得对错，统一移到块、
    正文留指向引用）。"配对错"这条死因从结构上不存在，也没有"换更长句子又漏"的攻击面。
  - 不误伤一般数字：趋势/占比(3.06%/近30天)、补货建议(补 50 件)、SKU 编号、耗时(3 秒) 等
    **非事实槽绑定**的数字/文本原样保留。
  - 失败/查无/空：不渲染权威块，走确定性错误模板（编造槽值移除 + 告警）。

为什么 fail-then-pass：擦除式（路线乙）"判对错、对的留错的删"会被更长句子绕过；路线甲改成
"正文整体不含事实槽绑定"的可判定不变量 —— 验门人 Round-4 的两个例句应是"正文整体不含该绑定"。

跑法：python3 tests/smoke_ws161_factslot_contract.py
"""
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CARRIERS = ["Aramex", "SMSA", "DHL", "顺丰", "UPS"]


def _sanitize(reply, tools_used, tool_log, question=None):
    from hipop.server._safety import sanitize_reply
    return sanitize_reply(reply, tools_used=tools_used, tool_log=tool_log, question=question)


def _answer_body(out):
    """剥掉头部 banner，取展示正文（含权威块 + 正文）。"""
    sep = "\n\n---\n\n"
    return out.split(sep, 1)[1] if sep in out else out


def _prose(out):
    """剥掉权威明细块（** 开头含'明细'的块及其 '- ' 列表行），只留模型正文部分。
    路线甲不变量校验只针对**正文**——权威块里出现事实值是正确的。"""
    body = _answer_body(out)
    keep, in_block = [], False
    for ln in body.split("\n"):
        if ln.startswith("**") and "明细" in ln:
            in_block = True
            continue
        if in_block:
            if ln.startswith("- ") or ln.strip() == "":
                continue
            in_block = False
        keep.append(ln)
    return "\n".join(keep)


def _ev(tool, ok, entity, kind, **kw):
    base = {
        "tool": tool, "ok": ok, "entity": entity, "entity_kind": kind,
        "error": kw.get("error"), "message": kw.get("message", "x"),
        "source": kw.get("source", "ERP"),
        "forwarders": kw.get("forwarders", []), "tracking_nos": kw.get("tracking_nos", []),
        "order_nos": kw.get("order_nos", []), "statuses": kw.get("statuses", []),
        "has_stock_value": kw.get("has_stock_value", False), "stock_values": kw.get("stock_values", []),
        "stock_render": kw.get("stock_render", []), "stock_bind": kw.get("stock_bind", {}),
        "orders": kw.get("orders", []),
    }
    if tool == "query_stock_split":
        base["slots_proven"] = bool(base["has_stock_value"])
    else:
        base["slots_proven"] = bool(base["forwarders"] or base["tracking_nos"] or base["statuses"])
    if "slots_proven" in kw:
        base["slots_proven"] = kw["slots_proven"]
    return base


def _stock_result(sku="TBC0168A", total=509, yiwu=200, dongguan=0, saudi=0, noon=309, inbound=0):
    return {"ok": True, "fail_closed": False, "sku": sku, "store": "KSA", "source": "noon+erp",
            "split": {"yiwu": yiwu, "dongguan": dongguan, "overseas_saudi_1": saudi,
                      "noon": noon, "inbound": inbound, "domestic": yiwu + dongguan},
            "total": total, "erp_in_transit": None}


def _stock_tl(result):
    from hipop.server._factslot_contract import factslot_evidence_from_result
    return [{"name": "query_stock_split", "args": {"sku": result["sku"]}, "result_error": None,
             "factslot_evidence": factslot_evidence_from_result("query_stock_split", result)}]


def _sku_live_two_orders():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": True, "sku": "TBC0168A", "in_transit_orders": [
        {"order_no": "PD2026001", "forwarder": "Aramex", "tracking_no": "AB123456789012", "qty": 30},
        {"order_no": "PD2026002", "forwarder": "SMSA", "tracking_no": "CD987654321098", "qty": 20}]}
    return [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
             "factslot_evidence": factslot_evidence_from_result("query_sku_live", result)}]


# ── 证据快照单元 ────────────────────────────────────────────────────────────────

def test_evidence_sku_live_success_collects_all_slots():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": True, "sku": "TBC0168A", "in_transit_orders": [
        {"order_no": "PD2026001", "forwarder": "Aramex", "tracking_no": "AB123456789012"},
        {"order_no": "PD2026002", "forwarder": "SMSA", "tracking_no": "CD987654321098"}]}
    ev = factslot_evidence_from_result("query_sku_live", result)
    assert ev and ev["ok"] is True and ev["slots_proven"] is True, ev
    assert set(ev["tracking_nos"]) == {"AB123456789012", "CD987654321098"}
    assert set(ev["forwarders"]) == {"Aramex", "SMSA"}


def test_evidence_sku_live_login_failed_is_blocked():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    ev = factslot_evidence_from_result("query_sku_live", {
        "ok": False, "error": "erp_login_failed_no_cache", "sku": "TBC0168A", "message": "x"})
    assert ev and ev["ok"] is False and ev["slots_proven"] is False, ev


def test_evidence_empty_ok_is_not_proven():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    ev = factslot_evidence_from_result("query_sku_live", {
        "ok": True, "sku": "TBC0168A", "in_transit_orders": [], "recent_completed": []})
    assert ev["ok"] is True and ev["slots_proven"] is False, ev


def test_evidence_stock_fail_closed_is_blocked():
    from hipop.server._factslot_contract import factslot_evidence_from_result
    ev = factslot_evidence_from_result("query_stock_split", {
        "ok": False, "fail_closed": True, "sku": "TBC0168A", "message": "快照超 3 天"})
    assert ev and ev["ok"] is False and ev["slots_proven"] is False, ev


# ── 路线甲·正向不变量：成功分支正文不含事实槽绑定（无论模型写对写错都一样） ──────────

def _assert_no_stock_binding_in_prose(out):
    prose = _prose(out)
    for n in ("509", "200", "309", "888", "111", "999", "11"):
        assert n not in prose, f"成功分支正文不得出现绑定到仓库的库存数字（{n}）: {prose!r}"


def test_success_stock_correct_values_only_in_block_not_prose():
    """正确值：总库存 509/义乌 200/noon 309 —— 正文不复述，只在权威块。"""
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    out, warns = _sanitize("SKU TBC0168A 总库存 509 件，义乌 200，noon 仓 309。", ["query_stock_split"], tl)
    _assert_no_stock_binding_in_prose(out)
    assert "总库存：509" in _answer_body(out) and "义乌：200" in _answer_body(out), "权威块应有正确绑定值"
    assert "[详见上方明细]" in _prose(out), "正文应留指向权威块的引用"
    assert not [w for w in warns if "禁编承重墙" in w], "成功移块是正常渲染，不应报 banner 告警"


def test_success_stock_wrong_values_also_removed_from_prose():
    """错值（含 Round-4 后置标签抢绑/冒号标签/自然连接词长句）：正文整体不含该绑定，块给正确值。"""
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    for reply in (
        "总库存:200、义乌:509。",                                            # 验门人冒号标签例句
        "总库存这一项当前系统确认下来是 200 件，义乌这一项当前确认是 509 件。",  # 后置标签抢绑长句
        "SKU TBC0168A 当前总库存约为 200 件，义乌大约 509 件。",             # 自然连接词
        "义乌为 509 件，总库存是 200 件。",                                   # 换序对称
    ):
        out, warns = _sanitize(reply, ["query_stock_split"], tl)
        _assert_no_stock_binding_in_prose(out)
        assert "总库存：509" in _answer_body(out) and "义乌：200" in _answer_body(out), reply


def test_success_stock_value_before_label_and_parenthetical_removed():
    """验门人 Round-1 打回点 1：value-before-label / 括号标签 也是库存值↔仓库绑定 → 移除。
    `509 件是总库存`、`509 件(总库存)`、`200 件在义乌` 这类标签在数字之后/括号里的绑定。"""
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    for reply in (
        "509 件是总库存，200 件在义乌，309 件在 noon 仓。",
        "509 件(总库存)，200 件(义乌)，309 件(noon 仓)。",
        "经核对，总计 509 件为总库存，其中义乌 200 件。",
    ):
        out, warns = _sanitize(reply, ["query_stock_split"], tl)
        _assert_no_stock_binding_in_prose(out)
        assert "总库存：509" in _answer_body(out) and "义乌：200" in _answer_body(out), reply


def test_value_before_label_does_not_overdelete_advice():
    """负向：逗号后另起的补货数字、趋势/占比不被后置标签误删。"""
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    out, warns = _sanitize("近 30 天动销良好，建议补货 50 件，退货率占比 3.06%。", ["query_stock_split"], tl)
    prose = _prose(out)
    assert "30 天" in prose and "补货 50 件" in prose and "3.06%" in prose, f"非库存数字不应误删: {prose!r}"


def test_stock_binding_is_connector_agnostic_not_enumerated():
    """验门人 Round-2 打回点 1：换任意连接词（对应/即/=…）都不漏——判定靠"数字与槽标签同句"
    这个结构信号，不枚举连接词小表。"""
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    for reply in (
        "509 件对应总库存，200 件对应义乌，309 件对应 noon 仓。",
        "509 件即总库存。",
        "总库存 = 509 件。",
        "经统计，509 这个数对应的是总库存口径。",
    ):
        out, warns = _sanitize(reply, ["query_stock_split"], tl)
        _assert_no_stock_binding_in_prose(out)
        assert "总库存：509" in _answer_body(out), reply
    # 负向：非库存语境数字不被误删
    out, _ = _sanitize("近 30 天动销良好，建议补货 50 件。", ["query_stock_split"], tl)
    assert "30 天" in _prose(out) and "补货 50 件" in _prose(out)


def _sku_live_custom_forwarder(forwarder="Naqel", tracking="NQ123456789", order="PD2026003"):
    from hipop.server._factslot_contract import factslot_evidence_from_result
    result = {"ok": True, "sku": "TBC0168A", "in_transit_orders": [
        {"order_no": order, "forwarder": forwarder, "tracking_no": tracking, "qty": 12}]}
    return [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
             "factslot_evidence": factslot_evidence_from_result("query_sku_live", result)}]


def test_carrier_value_driven_non_closed_set_removed():
    """验门人 Round-2 打回点 2：承运商移块以**工具本轮返回的 forwarder 字面值**为准——
    Naqel 不在预置闭集 _CARRIER_RE，但工具返回了它 → 正文里的 Naqel 也必须移到权威块。"""
    tl = _sku_live_custom_forwarder(forwarder="Naqel", tracking="NQ123456789", order="PD2026003")
    out, warns = _sanitize("货单 PD2026003 由 Naqel 承运，运单号 NQ123456789。", ["query_sku_live"], tl)
    prose = _prose(out)
    assert "Naqel" not in prose, f"工具返回的非闭集承运商 Naqel 应移块: {prose!r}"
    assert "NQ123456789" not in prose, f"运单号应移块: {prose!r}"
    assert "PD2026003" in prose, "货单号本身保留"
    assert "承运商：Naqel" in _answer_body(out), "权威块应保留承运商"


def test_carrier_value_driven_another_unknown_carrier():
    """对称：换另一个非闭集承运商（iMile）同样不漏——证明不靠词表。"""
    tl = _sku_live_custom_forwarder(forwarder="iMile", tracking="IM987654321", order="PD2026007")
    out, warns = _sanitize("PD2026007 这一单走的是 iMile，单号 IM987654321。", ["query_sku_live"], tl)
    prose = _prose(out)
    assert "iMile" not in prose and "IM987654321" not in prose, f"非闭集承运商/运单应移块: {prose!r}"


def test_success_logistics_no_carrier_or_tracking_in_prose():
    """承运商/运单号（对的、错的、对调的）都不进正文，只在权威块；货单号本身保留。"""
    tl = _sku_live_two_orders()
    for reply in (
        "货单 PD2026001 由 Aramex 承运，运单号 AB123456789012；PD2026002 由 SMSA，运单号 CD987654321098。",  # 正确
        "货单 PD2026001 由 SMSA 承运；货单 PD2026002 由 Aramex 承运。",                                       # 对调
        "PD2026002 和 PD2026001 中，分别由 Aramex 和 SMSA 承运。",                                            # 验门人概览句
        "货单 PD2026001，经过这两天的多方确认核对，目前由 SMSA 承运；货单 PD2026002，目前由 Aramex 承运。",   # 长说明插队
    ):
        out, warns = _sanitize(reply, ["query_sku_live"], tl)
        prose = _prose(out)
        for c in ("Aramex", "SMSA", "DHL", "顺丰"):
            assert c not in prose, f"正文不得出现承运商名（{c}）: {prose!r}"
        for t in ("AB123456789012", "CD987654321098"):
            assert t not in prose, f"正文不得出现运单号（{t}）: {prose!r}"
        assert "PD2026001" in prose and "PD2026002" in prose, f"货单号本身应保留: {prose!r}"
        body = _answer_body(out)
        assert "Aramex" in body and "SMSA" in body, "权威块应保留正确承运商"


def test_success_move_is_silent_no_banner():
    """成功分支把事实槽移到权威块是正常渲染 → 不触发 hallucinate banner。"""
    tl = _sku_live_two_orders()
    out, warns = _sanitize("货单 PD2026001 由 Aramex 承运。", ["query_sku_live"], tl)
    assert not [w for w in warns if "禁编承重墙" in w], warns
    assert "⚠️ **系统检测到" not in out, "正确答案不应出现不准确 banner"


# ── 路线甲·负向不误删：非事实槽绑定的数字/文本原样保留 ───────────────────────────

def test_nonstock_numbers_and_advice_preserved():
    tl = _stock_tl(_stock_result(total=509, yiwu=200, noon=309))
    reply = ("库存查询耗时 3 秒。SKU TBC0168A 总库存 509 件，近 30 天动销良好，"
             "退货率占比 3.06%，建议补货 50 件。")
    out, warns = _sanitize(reply, ["query_stock_split"], tl)
    prose = _prose(out)
    assert "3 秒" in prose and "30 天" in prose and "3.06%" in prose, f"非库存数字应保留: {prose!r}"
    assert "补货 50 件" in prose, f"补货建议数字不应误删: {prose!r}"
    assert "建议补货" in prose


def test_doc_rule_explanation_not_touched():
    reply = ("ops_status 的 5 个取值定义在 governance_actions.yaml 的 allowed_statuses，"
             "不是 ERP 内置枚举，可扩展。")
    out, warns = _sanitize(reply, ["explain_status_enum"], tool_log=[])
    assert out == reply, f"文档解释（无 fact-slot 工具）不应被改写: {out[:120]}"


def test_sku_and_order_ids_preserved():
    """SKU 编号、货单号本身是用户的引用，不是事实槽绑定 → 保留。"""
    tl = _sku_live_two_orders()
    out, warns = _sanitize("SKU TBC0168A 的货单 PD2026001 与 PD2026002 详情如下。", ["query_sku_live"], tl)
    prose = _prose(out)
    assert "TBC0168A" in prose and "PD2026001" in prose and "PD2026002" in prose, prose


# ── 失败/查无/空：编造槽值移除 + 确定性错误模板（仍告警） ─────────────────────────

def test_login_failed_scrubs_facts_and_templates():
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": "erp_login_failed_no_cache",
           "factslot_evidence": _ev("query_sku_live", False, "TBC0168A", "sku",
                                    error="erp_login_failed_no_cache", message="ERP 登录失败")}]
    out, warns = _sanitize("SKU TBC0168A 承运商 Aramex，运单号 AB123456789012，状态已发货。", ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" not in body and "AB123456789012" not in body and "已发货" not in body, body
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败", out), out[:200]
    assert any("禁编承重墙" in w for w in warns), warns


def test_stock_fail_closed_scrubs_and_no_block():
    tl = [{"name": "query_stock_split", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_stock_split", False, "TBC0168A", "sku", message="快照超 3 天，拒绝出数")}]
    out, warns = _sanitize("SKU TBC0168A 总库存 509 件（义乌 200、noon 309）。", ["query_stock_split"], tl)
    body = _answer_body(out)
    assert "509" not in body and "200" not in body and "309" not in body, body
    assert "库存明细" not in body, "失败不渲染权威块"
    import re as _re
    assert _re.search(r"无法确认|拒绝出数|不能确认", out), out[:200]


def test_order_live_login_failed_scrubs_facts():
    tl = [{"name": "query_order_live", "args": {"order_no": "PD2026099"}, "result_error": "erp_login_failed_no_cache",
           "factslot_evidence": _ev("query_order_live", False, "PD2026099", "order",
                                    error="erp_login_failed_no_cache", message="ERP 实时查失败")}]
    out, warns = _sanitize("货单 PD2026099 由顺丰承运，运单号 SF1234567890123。", ["query_order_live"], tl)
    body = _answer_body(out)
    assert "顺丰" not in body and "SF1234567890123" not in body, body
    import re as _re
    assert _re.search(r"无法确认|不能确认|失败", out), out[:200]


def test_empty_ok_treated_as_failure():
    tl = [{"name": "query_sku_live", "args": {"sku": "TBC0168A"}, "result_error": None,
           "factslot_evidence": _ev("query_sku_live", True, "TBC0168A", "sku", slots_proven=False, message="无在途货单")}]
    out, warns = _sanitize("SKU TBC0168A 在途，承运商 Aramex。", ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "Aramex" not in body, body
    import re as _re
    assert _re.search(r"无法确认|不能确认", out), out[:200]


def test_user_supplied_tracking_in_question_not_removed():
    """用户问句里自带的运单号被 reply 回显（失败、说未找到）→ 不算编造，不删。"""
    tl = [{"name": "query_order_live", "args": {"order_no": "1234567890123456"}, "result_error": "order_not_found_in_erp",
           "factslot_evidence": _ev("query_order_live", False, "1234567890123456", "order",
                                    error="order_not_found_in_erp", message="无记录")}]
    out, warns = _sanitize("运单号 1234567890123456 在 ERP 中无记录，请核实。", ["query_order_live"],
                           tl, question="帮我查运单号 1234567890123456 的物流")
    assert "1234567890123456" in _answer_body(out), _answer_body(out)


def test_success_branch_question_tracking_id_moved_not_echoed():
    """验门人 Round-1 打回点 2：成功分支里 question 自带的运单号不能当成"返回货单的运单号"
    残留正文——`货单 PD2026001 的运单号是 ZZ999999999999`（ZZ 是用户自带号）必须移到权威块。
    "用户自带号可回显"只用于失败/未找到场景，不在成功分支做 运单号↔货单 事实出口。"""
    tl = _sku_live_two_orders()
    out, warns = _sanitize("货单 PD2026001 的运单号是 ZZ999999999999。", ["query_sku_live"],
                           tl, question="查 PD2026001 的运单号 ZZ999999999999")
    prose = _prose(out)
    assert "ZZ999999999999" not in prose, f"成功分支 question 自带运单号不应残留正文: {prose!r}"
    assert "PD2026001" in prose, "货单号本身保留"


# ── 权威块确定性渲染（值-槽绑定原样） ────────────────────────────────────────────

def test_deterministic_stock_block_renders_bound_values():
    tl = _stock_tl(_stock_result(total=509, yiwu=200, dongguan=11, noon=309, inbound=5))
    out, warns = _sanitize("你的库存情况如下，请查收。", ["query_stock_split"], tl)
    body = _answer_body(out)
    assert "总库存：509" in body and "义乌：200" in body and "东莞：11" in body, body
    assert "noon 仓：309" in body and "待发货(在途待入库)：5" in body, body


def test_deterministic_orders_block_renders_bound_rows():
    tl = _sku_live_two_orders()
    out, warns = _sanitize("有在途货单，详情如下。", ["query_sku_live"], tl)
    body = _answer_body(out)
    assert "PD2026001" in body and "承运商：Aramex" in body and "运单号：AB123456789012" in body, body
    assert "PD2026002" in body and "承运商：SMSA" in body, body


# ── 接线 ────────────────────────────────────────────────────────────────────────

def test_both_providers_wire_factslot_evidence():
    for fname in ("_provider_anthropic.py", "_provider_openai.py"):
        src = (REPO_ROOT / "hipop" / "server" / fname).read_text(encoding="utf-8")
        assert "factslot_evidence_from_result" in src, f"{fname} 未调用证据抽取"
        assert 'entry["factslot_evidence"]' in src, f"{fname} 未挂进 tool_log entry"


TESTS = [
    test_evidence_sku_live_success_collects_all_slots,
    test_evidence_sku_live_login_failed_is_blocked,
    test_evidence_empty_ok_is_not_proven,
    test_evidence_stock_fail_closed_is_blocked,
    test_success_stock_correct_values_only_in_block_not_prose,
    test_success_stock_wrong_values_also_removed_from_prose,
    test_success_logistics_no_carrier_or_tracking_in_prose,
    test_success_move_is_silent_no_banner,
    test_nonstock_numbers_and_advice_preserved,
    test_doc_rule_explanation_not_touched,
    test_sku_and_order_ids_preserved,
    test_login_failed_scrubs_facts_and_templates,
    test_stock_fail_closed_scrubs_and_no_block,
    test_order_live_login_failed_scrubs_facts,
    test_empty_ok_treated_as_failure,
    test_user_supplied_tracking_in_question_not_removed,
    test_success_branch_question_tracking_id_moved_not_echoed,
    test_success_stock_value_before_label_and_parenthetical_removed,
    test_value_before_label_does_not_overdelete_advice,
    test_stock_binding_is_connector_agnostic_not_enumerated,
    test_carrier_value_driven_non_closed_set_removed,
    test_carrier_value_driven_another_unknown_carrier,
    test_deterministic_stock_block_renders_bound_values,
    test_deterministic_orders_block_renders_bound_rows,
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

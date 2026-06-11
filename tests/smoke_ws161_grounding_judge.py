"""smoke_ws161_grounding_judge.py — WS-161 路线(B) 语义判别 + 确定性 grounding（judge 走 stub）

口径（码长 2026-06-11 切 B 方向）：抽取用语义（可注入/可 mock）、判定保持确定性。
本 smoke 把语义抽取器注入成**确定性 stub**（不依赖 live LLM），用固定工具返回 + 固定正文，
断言"哪些事实槽值被移、哪些保留、哪些 ungrounded 告警"。确定性规则进 verifier、不进 prompt。

钉死验门人 Round-3 两个 fail-open：
  ① 混合 query（stock + sku_live 同轮成功）时库存断言**仍移块**（不被物流成功关掉）；
  ② `由 Naqel 承运`（工具本轮未返回的承运商）**fail-closed 移块** + 告警，与在不在闭集无关。
外加：连接词/句式无关、负控不误删、grounding **按槽位命中无无槽兜底**、抽取失败 fail-closed 回退。

跑法：python3 tests/smoke_ws161_grounding_judge.py
"""
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
    sep = "\n\n---\n\n"
    return out.split(sep, 1)[1] if sep in out else out


def _prose(out):
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


def _ev(tool, result):
    from hipop.server._factslot_contract import factslot_evidence_from_result
    return {"name": tool, "args": {"sku": result.get("sku")}, "result_error": result.get("error"),
            "factslot_evidence": factslot_evidence_from_result(tool, result)}


def _stock_result(total=509, yiwu=200, noon=309, dongguan=0, inbound=0, saudi=0):
    return {"ok": True, "fail_closed": False, "sku": "TBC0168A", "store": "KSA", "source": "noon+erp",
            "split": {"yiwu": yiwu, "dongguan": dongguan, "overseas_saudi_1": saudi,
                      "noon": noon, "inbound": inbound, "domestic": yiwu + dongguan},
            "total": total, "erp_in_transit": None}


def _sku_live_result(orders=(("PD2026001", "Aramex", "AB123456789012", 30),)):
    return {"ok": True, "sku": "TBC0168A", "in_transit_orders": [
        {"order_no": o, "forwarder": f, "tracking_no": t, "qty": q} for (o, f, t, q) in orders]}


def _stub(assertions):
    def fn(reply, hints):
        return list(assertions)
    return fn


def _with_extractor(fn, body):
    from hipop.server import _factslot_grounding as g
    g.set_assertion_extractor(fn)
    try:
        return body()
    finally:
        g.reset_assertion_extractor()


# ── grounding 确定性单元：按槽位命中，无"值出现过即放行"兜底 ────────────────────────

def test_grounding_index_per_slot_no_valueset_fallback():
    from hipop.server import _factslot_grounding as g
    idx = g.grounding_index([_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))])
    assert g.is_grounded({"kind": "stock", "anchor": "总库存", "value": "509"}, idx)
    assert g.is_grounded({"kind": "stock", "anchor": "义乌", "value": "200"}, idx)
    # 关键：200 是工具返回过的值（义乌），但它**不是总库存槽的值** → ungrounded（无无槽兜底）
    assert not g.is_grounded({"kind": "stock", "anchor": "总库存", "value": "200"}, idx)
    assert not g.is_grounded({"kind": "stock", "anchor": "总库存", "value": "999"}, idx)
    idxc = g.grounding_index([_ev("query_sku_live", _sku_live_result())])
    assert g.is_grounded({"kind": "carrier", "anchor": "PD2026001", "value": "Aramex"}, idxc)
    assert not g.is_grounded({"kind": "carrier", "anchor": "PD2026001", "value": "Naqel"}, idxc)


# ── 验门人 fail-open ①：混合 query，库存断言仍移块 ─────────────────────────────────

def test_mixed_query_stock_assertion_still_moved():
    """stock + sku_live 同轮都成功；库存断言走同一条 grounding → 仍移块（不被物流成功关掉）。"""
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309)),
          _ev("query_sku_live", _sku_live_result())]
    reply = "总库存 509 件；另有在途货单 PD2026001。"
    out, warns = _with_extractor(
        _stub([{"kind": "stock", "anchor": "总库存", "value": "509", "span": "总库存 509"}]),
        lambda: _sanitize(reply, ["query_stock_split", "query_sku_live"], tl))
    assert "总库存 509" not in _prose(out), f"混合 query 库存断言仍须移块: {_prose(out)!r}"
    assert "PD2026001" in _prose(out), "货单号本身保留"


# ── 验门人 fail-open ②：工具未返回的承运商 fail-closed 移块 ─────────────────────────

def test_carrier_not_returned_fail_closed_moved():
    tl = [_ev("query_sku_live", _sku_live_result((("PD2026001", "Aramex", "AB123456789012", 30),)))]
    reply = "货单 PD2026001 由 Naqel 承运。"
    out, warns = _with_extractor(
        _stub([{"kind": "carrier", "anchor": "PD2026001", "value": "Naqel", "span": "Naqel"}]),
        lambda: _sanitize(reply, ["query_sku_live"], tl))
    assert "Naqel" not in _prose(out), f"工具未返回的承运商 Naqel 应 fail-closed 移块: {_prose(out)!r}"
    assert any("grounding" in w for w in warns), f"ungrounded 应报告警: {warns}"


# ── 连接词/句式无关：语义抽取负责"任意写法都抽到" ──────────────────────────────────

def test_connector_and_phrasing_agnostic():
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))]
    for reply, span in (
        ("509 件对应总库存。", "509"),
        ("总库存这一项确认是 509。", "509"),
        ("总库存 = 509 件。", "509"),
    ):
        out, warns = _with_extractor(
            _stub([{"kind": "stock", "anchor": "总库存", "value": "509", "span": span}]),
            lambda r=reply: _sanitize(r, ["query_stock_split"], tl))
        assert "509" not in _prose(out), f"任意写法的库存断言都应移块: {reply} -> {_prose(out)!r}"


# ── 负控：非事实槽断言（语义不抽）→ 不移 ───────────────────────────────────────────

def test_non_factslot_not_extracted_not_moved():
    """补货建议/趋势/占比 语义不会抽成事实槽断言 → stub 返回空 → 不移、不误删。"""
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))]
    reply = "近 30 天动销良好，建议补货 50 件，退货率 3.06%。"
    out, warns = _with_extractor(_stub([]),
                                 lambda: _sanitize(reply, ["query_stock_split"], tl))
    prose = _prose(out)
    assert "30 天" in prose and "补货 50 件" in prose and "3.06%" in prose, f"非事实槽数字不应移: {prose!r}"
    assert not [w for w in warns if "grounding" in w]


# ── fail-closed：抽取器异常/不可用 → 回退确定性结构门（仍兜住编造值）────────────────

def test_extractor_failure_falls_back_to_structural_floor():
    def _boom(reply, hints):
        raise RuntimeError("judge llm down")
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))]
    reply = "总库存 999 件。"   # 999 工具没返回；语义抽取挂了 → 结构门 floor 应兜住
    out, warns = _with_extractor(_boom, lambda: _sanitize(reply, ["query_stock_split"], tl))
    assert "999" not in _prose(out), f"抽取失败时结构门 floor 应 fail-closed 兜住: {_prose(out)!r}"


def test_extractor_returns_none_uses_structural_floor():
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))]
    out, warns = _with_extractor(_stub(None) if False else (lambda r, h: None),
                                 lambda: _sanitize("总库存 200 件。", ["query_stock_split"], tl))
    # 200 是义乌的值、绑到总库存是错配；结构门 floor 移块
    assert "200" not in _prose(out), f"None 抽取应回退结构门: {_prose(out)!r}"


# ── 验门人 round-1 打回：extractor=None 时 floor 必须 fail-closed（不弱于 B）──────────
#    None = 语义抽取器超时/异常/无凭据/被关闭 → 走结构门 floor。下面三条钉死 floor 不再 fail-open。

_NONE = (lambda r, h: None)


def _stock_fail_result():
    return {"ok": False, "fail_closed": True, "sku": "TBC0168A", "store": "KSA",
            "error": "erp_login_failed_no_cache", "message": "快照不可用"}


def test_floor_mixed_query_no_stock_value_residue():
    """① 混合 query：stock + sku_live 同轮成功，extractor=None 时正文不得残留库存值↔仓位。"""
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309)),
          _ev("query_sku_live", _sku_live_result())]
    reply = "总库存 509 件，义乌 200，noon 仓 309；货单 PD2026001 由 Aramex 承运，运单号 AB123456789012。"
    out, warns = _with_extractor(_NONE, lambda: _sanitize(reply, ["query_stock_split", "query_sku_live"], tl))
    prose = _prose(out)
    for n in ("509", "200", "309"):
        assert n not in prose, f"混合 query 库存值仍残留（{n}）: {prose!r}"
    assert "Aramex" not in prose and "AB123456789012" not in prose, f"物流值也应移: {prose!r}"
    assert "PD2026001" in prose, "货单号本身保留"


def test_floor_unreturned_noncloseset_carrier_no_residue():
    """② 成功物流里未返回的非闭集承运商（Naqel 三种写法）extractor=None 时不得残留。"""
    tl = [_ev("query_sku_live", _sku_live_result((("PD2026001", "Aramex", "AB123456789012", 30),)))]
    for reply in ("货单 PD2026001 由 Naqel 承运。", "承运商：Naqel。", "该单物流商 Naqel 负责。"):
        out, warns = _with_extractor(_NONE, lambda r=reply: _sanitize(r, ["query_sku_live"], tl))
        assert "Naqel" not in _prose(out), f"未返回承运商 Naqel 仍残留: {reply} -> {_prose(out)!r}"


def test_floor_stock_fail_logistics_success_error_template():
    """③ stock 失败 + logistics 成功，extractor=None 时库存数字走错误模板/未确认，不残留、不指物流块。"""
    tl = [_ev("query_stock_split", _stock_fail_result()),
          _ev("query_sku_live", _sku_live_result())]
    reply = "总库存 509 件；货单 PD2026001 由 Aramex 承运。"
    out, warns = _with_extractor(_NONE, lambda: _sanitize(reply, ["query_stock_split", "query_sku_live"], tl))
    import re as _re
    assert "509" not in _prose(out), f"stock 失败时库存数字应移: {_prose(out)!r}"
    assert _re.search(r"无法确认|不能确认|未确认|拒绝出数", out), f"应走确定性错误模板: {out[:160]}"


def test_floor_does_not_overdelete_advice():
    """负控：floor 路径下补货建议/趋势/占比不误删。"""
    tl = [_ev("query_stock_split", _stock_result(total=509, yiwu=200, noon=309))]
    out, warns = _with_extractor(_NONE, lambda: _sanitize(
        "近 30 天动销良好，建议补货 50 件，退货率 3.06%。", ["query_stock_split"], tl))
    prose = _prose(out)
    assert "30 天" in prose and "补货 50 件" in prose and "3.06%" in prose, f"非库存数字不应误删: {prose!r}"


# ── 注入接线：抽取器可注入/可复位（确定性验证前提）─────────────────────────────────

def test_extractor_is_injectable_and_resettable():
    from hipop.server import _factslot_grounding as g
    assert g._ASSERTION_EXTRACTOR is None
    g.set_assertion_extractor(lambda r, h: [])
    assert g._ASSERTION_EXTRACTOR is not None
    g.reset_assertion_extractor()
    assert g._ASSERTION_EXTRACTOR is None


TESTS = [
    test_grounding_index_per_slot_no_valueset_fallback,
    test_mixed_query_stock_assertion_still_moved,
    test_carrier_not_returned_fail_closed_moved,
    test_connector_and_phrasing_agnostic,
    test_non_factslot_not_extracted_not_moved,
    test_extractor_failure_falls_back_to_structural_floor,
    test_extractor_returns_none_uses_structural_floor,
    test_floor_mixed_query_no_stock_value_residue,
    test_floor_unreturned_noncloseset_carrier_no_residue,
    test_floor_stock_fail_logistics_success_error_template,
    test_floor_does_not_overdelete_advice,
    test_extractor_is_injectable_and_resettable,
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

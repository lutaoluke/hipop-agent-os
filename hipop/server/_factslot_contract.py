"""WS-161 B-2 禁编承重墙 —— fact-slot 证据契约（结构判别，非穷举黑名单）。

为什么是承重墙而不是又一层正则补洞
----------------------------------
WS-55 / WS-128 的教训：反幻觉门逐句往黑名单加同义词永远补不完。本模块把"查不到 /
实时源失败 / 工具返回为空时不能编结果"前移到**数据流和回答结构**上，并且——按 WS-161
返工口径——不只是"前置一段模板 + 加 warning"，而是**真的把答案正文里编造的结果槽值删掉**：

  1. 数据流边界（provider tool-loop）：每个 fact-slot 工具（query_sku_live /
     query_order_live / query_stock_split）的结构化返回被抽成证据快照，挂进 tool_log。
     快照里带出工具**实际返回**的结果槽值集合（承运商 forwarders / 运单号 tracking_nos /
     状态 statuses）；失败/空/未取到槽值的分支这些集合为空。

  2. 回答结构（sanitize_reply 入口，第一层）：
     a) scrub_fabricated_slots —— **包含关系判别**：reply 里出现的承运商（闭集实体）/
        运单号（id 形 token）必须 ∈ 工具实际返回的对应集合，否则就是编造，**就地从正文
        删除该值**（不是只加 banner）。失败/空分支 allow-set 为空 → 任何承运商/运单号被删；
        成功分支只删工具没给出的"多编的那个"。库存数量(qty)与状态(status)在**纯失败/空**
        分支（无任何成功槽值背书）下整段结果断言被删。
     b) enforce_failure_template —— 失败/空的实体确定性前置错误模板（点名实体 + 哪个源
        失败 + 当前不能确认 + 结果槽留空），保证用户看到"查不到"而不是被编造结果误导。

  承运商闭集 / id 形 token / qty 形是"领域建模 + 形状判别"，不是穷举措辞，复用 _safety 里
  WS-133 Round-5 已建好的 _CARRIER_RE / _ID_TOKEN_RE / _QTY_RESULT_RE。B-1（_safety 下游
  各正则/语义门）保留作第二层纵深防御（WS-161 验收 3）。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

# 产出"承运商 / 运单号 / 库存数量 / 状态"等结果槽的实时/库存工具。
FACTSLOT_TOOLS = {"query_sku_live", "query_order_live", "query_stock_split"}

# 携带承运商/运单号/状态槽的实时物流工具。
_LOGISTICS_TOOLS = {"query_sku_live", "query_order_live"}
# 携带库存数量槽的工具。
_STOCK_TOOLS = {"query_stock_split"}

# 这两个"查无此实体"错误已由 _safety 的 T26 Rule B/D 出确定性负控前缀；
# 本契约仍对它们做正文 scrub + provenance（allow-set 空），但模板 prepend 交给 T26，避免双前缀。
_DEFERRED_TO_T26 = {"order_not_found_in_erp", "sku_no_orders_in_erp"}

_ENTITY_LABEL = {"sku": "SKU", "order": "货单"}
_SLOT_LABEL = {
    "query_sku_live": "在途承运商/运单号/状态",
    "query_order_live": "承运商/运单号/状态",
    "query_stock_split": "库存数量",
}
_DEFAULT_SOURCE = {
    "query_sku_live": "ERP 实时查询",
    "query_order_live": "ERP 实时查询",
    "query_stock_split": "库存快照(wf1_stock)",
}

# 状态/ETA 类正向断言（纯失败分支才删；闭集 ≈ 物流领域状态词，非穷举措辞）。
_STATUS_PHRASE_RE = re.compile(
    r"(?:已|正在|正)?(?:发货|发出|揽收|签收|出库|发运|送达|派送|妥投|清关|到仓|到货|到达|抵达)"
    r"|在途(?:中)?"
    r"|运输途中|派送中|配送中|已揽收|已签收|已发货"
    r"|预计.{0,10}(?:到仓|到货|到达|送达|送到|签收|抵达)"
    r"|状态[:：]?\s*(?:在途|已发货|运输中|派送中|待发货|已签收)"
)

_REDACT_CARRIER = "[承运商未确认]"
_REDACT_ID = "[单号未确认]"
_REDACT_QTY = "[数量未确认]"
_REDACT_STATUS = "[状态未确认]"


def _safety_detectors():
    """惰性引用 _safety 的 WS-133 Round-5 领域检测器，避免模块级循环导入。"""
    from . import _safety
    return _safety._CARRIER_RE, _safety._ID_TOKEN_RE, _safety._QTY_RESULT_RE


# ── 证据快照（数据流边界，provider 调用） ────────────────────────────────────────

def factslot_evidence_from_result(tool_name: str, result) -> Optional[dict]:
    """把 fact-slot 工具的结构化返回抽成证据快照。

    供 _provider_anthropic / _provider_openai 在 tool-loop 里调用并挂进 tool_log。
    抽成函数避免两个 provider 各写一份，smoke 也能直接 import 测。
    返回 None 表示该工具不在 fact-slot 范围。

    关键字段：
      ok            —— 工具是否成功（ok=True 且未 fail_closed）。
      slots_proven  —— 是否真返回了 ≥1 个具体结果槽值（承运商/运单号/库存数量）。
                       ok=True 但 0 槽值（空返回）也算"无可信结果槽"，与失败同等处理。
      forwarders / tracking_nos / statuses —— 工具实际返回的槽值 allow-set。
    """
    if tool_name not in FACTSLOT_TOOLS or not isinstance(result, dict):
        return None
    ok = bool(result.get("ok")) and not result.get("fail_closed")
    ev: Dict = {
        "tool": tool_name,
        "ok": ok,
        "error": result.get("error"),
        "message": result.get("message"),
        "source": result.get("source") or _DEFAULT_SOURCE.get(tool_name),
        "forwarders": [],
        "tracking_nos": [],
        "order_nos": [],
        "statuses": [],
        "has_stock_value": False,
    }
    if tool_name == "query_order_live":
        ev["entity"] = result.get("order_no")
        ev["entity_kind"] = "order"
        if result.get("order_no"):
            ev["order_nos"].append(str(result["order_no"]))
        if ok:
            if result.get("forwarder"):
                ev["forwarders"].append(str(result["forwarder"]))
            if result.get("tracking_no"):
                ev["tracking_nos"].append(str(result["tracking_no"]))
            if result.get("status"):
                ev["statuses"].append(str(result["status"]))
    elif tool_name == "query_sku_live":
        ev["entity"] = result.get("sku")
        ev["entity_kind"] = "sku"
        if ok:
            for o in (result.get("in_transit_orders") or []):
                if not isinstance(o, dict):
                    continue
                if o.get("forwarder"):
                    ev["forwarders"].append(str(o["forwarder"]))
                if o.get("tracking_no"):
                    ev["tracking_nos"].append(str(o["tracking_no"]))
                if o.get("order_no"):
                    ev["order_nos"].append(str(o["order_no"]))
            # recent_completed 的承运商/货单号也是工具真给出的合法实体，纳入 allow-set
            for o in (result.get("recent_completed") or []):
                if not isinstance(o, dict):
                    continue
                if o.get("forwarder"):
                    ev["forwarders"].append(str(o["forwarder"]))
                if o.get("order_no"):
                    ev["order_nos"].append(str(o["order_no"]))
    else:  # query_stock_split → entity = sku，槽 = 库存数量
        ev["entity"] = result.get("sku")
        ev["entity_kind"] = "sku"
        if ok and result.get("total") is not None:
            ev["has_stock_value"] = True
    # slots_proven：物流工具看是否返回承运商/运单号/状态；库存工具看 has_stock_value
    if tool_name in _STOCK_TOOLS:
        ev["slots_proven"] = bool(ev["has_stock_value"])
    else:
        ev["slots_proven"] = bool(ev["forwarders"] or ev["tracking_nos"] or ev["statuses"])
    return ev


# ── tool_log 解析 ────────────────────────────────────────────────────────────

def _args_dict(entry: dict) -> dict:
    args = entry.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _evidence_for_entry(entry: dict) -> Optional[dict]:
    """优先用 provider 写入的 factslot_evidence；缺失时从 result_error + args 兜底。

    兜底只在能**结构性证明失败**时返回失败证据（result_error 非空）；否则返回 None，
    宁可漏渲染也绝不误拦正常回答（WS-161 验收 4）。
    """
    name = entry.get("name")
    if name not in FACTSLOT_TOOLS:
        return None
    ev = entry.get("factslot_evidence")
    if isinstance(ev, dict):
        return ev
    error = entry.get("result_error")
    if not error:
        return None
    args = _args_dict(entry)
    if name == "query_order_live":
        entity, kind = args.get("order_no"), "order"
    else:
        entity, kind = args.get("sku"), "sku"
    return {
        "tool": name, "ok": False, "slots_proven": False, "error": error,
        "message": None, "source": _DEFAULT_SOURCE.get(name), "entity": entity,
        "entity_kind": kind, "forwarders": [], "tracking_nos": [], "order_nos": [],
        "statuses": [], "has_stock_value": False,
    }


def _all_evidence(tool_log: list) -> List[dict]:
    out = []
    for entry in (tool_log or []):
        ev = _evidence_for_entry(entry)
        if ev:
            out.append(ev)
    return out


def _blocked_verdicts(evs: List[dict]) -> List[dict]:
    """无可信结果槽的 fact-slot 调用：失败、fail_closed、或 ok 但 0 槽值（空返回）。

    去重：同 (tool, entity) 取一条。
    """
    seen = set()
    out: List[dict] = []
    for ev in evs:
        if ev.get("ok") and ev.get("slots_proven"):
            continue  # 成功且真有槽值 → 不 block
        key = (ev.get("tool"), ev.get("entity"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _norm(s) -> str:
    return str(s or "").strip().upper()


# ── 验收 1/2/5：包含关系 scrub（正文删除编造的结果槽值） ──────────────────────────

def scrub_fabricated_slots(
    reply: str, tool_log: list, question: Optional[str] = None
) -> Tuple[str, List[str]]:
    """把 reply 正文里"工具没返回过"的承运商/运单号/数量/状态就地删掉。

    包含关系（结构判别，非穷举词表）：
      - 承运商：闭集实体命中，但不在工具返回的 forwarders allow-set → 编造 → 删值。
      - 运单号：id 形 token，但不在 tracking_nos allow-set、也不是被查实体 id /
        用户问句里的 token → 编造 → 删值。
      - 库存数量：仅当库存工具**纯失败/空**（无任何成功 has_stock_value 背书）→ 删。
      - 状态：仅当物流工具**纯失败/空**（无任何成功槽值背书）→ 删状态断言。
    allow-set 取所有"成功且有槽值"调用的并集，天然处理混合（A 成功 B 失败）场景。
    """
    evs = _all_evidence(tool_log)
    if not evs:
        return reply, []
    names = {e.get("tool") for e in evs}
    logistics_called = bool(names & _LOGISTICS_TOOLS)
    stock_called = bool(names & _STOCK_TOOLS)

    carrier_re, id_re, qty_re = _safety_detectors()

    proven = [e for e in evs if e.get("ok") and e.get("slots_proven")]
    carrier_allow = set()
    for e in proven:
        for c in (e.get("forwarders") or []):
            carrier_allow.add(_norm(c))
            # 工具返回的承运商名可能含额外词（"Aramex Express"）；把其中的闭集实体
            # token（ARAMEX）也加进 allow-set，避免模型只写 "Aramex" 被误删。
            for m in carrier_re.findall(str(c)):
                tok = m if isinstance(m, str) else (m[0] if m else "")
                if tok:
                    carrier_allow.add(_norm(tok))
    tracking_allow = {_norm(t) for e in proven for t in (e.get("tracking_nos") or [])}
    # 工具返回的货单号也是合法 id（即便该实体未取到承运商/运单号），纳入 id allow-set
    id_allow = set(tracking_allow)
    for e in evs:
        for o in (e.get("order_nos") or []):
            id_allow.add(_norm(o))

    # 被查实体 id + 用户问句 token（合法复述，放行）
    qids = set()
    for entry in (tool_log or []):
        if entry.get("name") not in FACTSLOT_TOOLS:
            continue
        a = _args_dict(entry)
        for k in ("sku", "order_no"):
            if a.get(k):
                qids.add(_norm(a[k]))
        ev = entry.get("factslot_evidence")
        if isinstance(ev, dict) and ev.get("entity"):
            qids.add(_norm(ev["entity"]))
    q_upper = _norm(question)

    warns: List[str] = []
    redacted = {"carrier": [], "id": [], "qty": 0, "status": 0}

    # 承运商：命中闭集但不在 allow-set → 删
    if logistics_called:
        def _carrier_sub(m: re.Match) -> str:
            val = m.group(0)
            if _norm(val) in carrier_allow:
                return val
            redacted["carrier"].append(val)
            return _REDACT_CARRIER
        reply = carrier_re.sub(_carrier_sub, reply)

        # 运单号 / id 形 token：不在 allow-set、非被查 id、非问句 token → 删
        def _id_sub(m: re.Match) -> str:
            tok = m.group(0)
            up = _norm(tok)
            if up in id_allow or up in qids or (up and up in q_upper):
                return tok
            # 复述被查 id（包含关系）也放行
            if any(up == d or up in d or d in up for d in qids):
                return tok
            redacted["id"].append(tok)
            return _REDACT_ID
        reply = id_re.sub(_id_sub, reply)

    # 库存数量：纯库存失败/空（无成功 has_stock_value）→ 删数量断言
    stock_proven = any(
        e.get("tool") in _STOCK_TOOLS and e.get("ok") and e.get("has_stock_value")
        for e in evs
    )
    stock_blocked = any(
        e.get("tool") in _STOCK_TOOLS and not (e.get("ok") and e.get("has_stock_value"))
        for e in evs
    )
    logistics_proven = any(e.get("tool") in _LOGISTICS_TOOLS and e.get("ok") and e.get("slots_proven") for e in evs)
    logistics_blocked = any(e.get("tool") in _LOGISTICS_TOOLS and not (e.get("ok") and e.get("slots_proven")) for e in evs)
    # 纯库存失败/空，且本轮没有成功物流查询（成功物流会合法产出"在途 N"数量）→ 删数量断言
    if stock_called and stock_blocked and not stock_proven and not logistics_proven:
        def _qty_sub(m: re.Match) -> str:
            redacted["qty"] += 1
            return _REDACT_QTY
        reply = qty_re.sub(_qty_sub, reply)

    # 状态：纯物流失败/空（无任何成功槽值背书）→ 删状态断言
    if logistics_called and logistics_blocked and not logistics_proven:
        def _status_sub(m: re.Match) -> str:
            redacted["status"] += 1
            return _REDACT_STATUS
        reply = _STATUS_PHRASE_RE.sub(_status_sub, reply)

    parts = []
    if redacted["carrier"]:
        parts.append(f"承运商（{', '.join(sorted(set(redacted['carrier'])))}）")
    if redacted["id"]:
        parts.append(f"运单号（{', '.join(sorted(set(redacted['id'])))}）")
    if redacted["qty"]:
        parts.append("库存数量")
    if redacted["status"]:
        parts.append("状态")
    if parts:
        warns.append(
            "⚠️ 禁编承重墙：回复正文出现工具未返回的结果槽值 "
            f"[{'、'.join(parts)}]，按包含关系判定为编造，已就地删除（WS-161 B-2）"
        )
    return reply, warns


# ── 验收 1：失败/空 → 确定性错误模板 ──────────────────────────────────────────

def _failure_block(ev: dict) -> str:
    entity = ev.get("entity") or "(未指定)"
    elabel = _ENTITY_LABEL.get(ev.get("entity_kind"), "")
    slot = _SLOT_LABEL.get(ev.get("tool"), "结果")
    source = ev.get("source") or "数据源"
    if ev.get("ok") and not ev.get("slots_proven"):
        reason = ev.get("message") or "工具成功返回但无任何结果记录（空返回）"
    else:
        reason = ev.get("message") or ev.get("error") or "实时查询失败"
    return (
        f"**无法确认{elabel} {entity} 的{slot}**：{source}未取到可信结果"
        f"（原因：{reason}）。当前不能确认，结果槽留空 —— 以上字段不存在可信值，"
        f"请勿采纳任何承运商/运单号/库存数量/状态；请核实后重试或刷新对应数据源。"
    )


def enforce_failure_template(reply: str, tool_log: list) -> Tuple[str, List[str]]:
    """fact-slot 工具失败/空/无槽值 → 确定性前置错误模板。"""
    warnings: List[str] = []
    blocks: List[str] = []
    for ev in _blocked_verdicts(_all_evidence(tool_log)):
        if ev.get("error") in _DEFERRED_TO_T26:
            continue  # T26 Rule B/D 已出确定性负控前缀，避免双前缀
        block = _failure_block(ev)
        entity = ev.get("entity") or ""
        # 幂等 + 与 B-1（_safety Rule F/F2/G/H、T26 Rule B/D）去重：
        # 若正文已就该实体给出失败/不确定披露（B-1 多以"…失败（PD-X）""货单 PD-X…无记录"
        # 形式 prepend），本层不再重复前置模板，避免双前缀。
        _disclose = r"(无法确认|失败|无记录|未找到|未配置|不能确认|无在途|拒绝出数|查询异常|账号未配)"
        already = bool(entity) and re.search(
            rf"{_disclose}[^\n]{{0,40}}{re.escape(entity)}"
            rf"|{re.escape(entity)}[^\n]{{0,40}}{_disclose}",
            reply,
        )
        if already or block in reply:
            continue
        blocks.append(block)
        warnings.append(
            f"⚠️ {ev.get('tool')} 对 {entity or '该实体'} 返回失败/空/无结果，"
            f"已按禁编承重墙渲染确定性错误模板（结果槽留空）"
        )
    if blocks:
        reply = "\n\n".join(blocks) + "\n\n" + reply
    return reply, warnings


# ── 统一入口（供 _safety.sanitize_reply 调用） ────────────────────────────────────

def apply(reply: str, tool_log: list, question: Optional[str] = None) -> Tuple[str, List[str]]:
    """承重墙总入口：先正文 scrub 编造槽值，再确定性前置失败模板。返回 (reply, warnings)。"""
    warnings: List[str] = []
    reply, w1 = scrub_fabricated_slots(reply, tool_log or [], question)
    warnings.extend(w1)
    reply, w2 = enforce_failure_template(reply, tool_log or [])
    warnings.extend(w2)
    return reply, warnings

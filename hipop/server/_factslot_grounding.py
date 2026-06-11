"""WS-161 路线(B)·语义判别 + 确定性 grounding —— 把"抽取"与"判定"切开。

为什么是 B 而不是又一轮擦词表
------------------------------
纯规则擦写 6 轮收不成结构闭口：正则**抽不全**正文里"哪句是事实槽断言"（value-before-label、
任意连接词、Naqel 这种不在闭集的承运商）。B 的关键设计（务必照做，否则会把不收敛从正则搬到
LLM 判官）：

  · **抽取用语义**（可注入、可 mock）：识别正文里的"事实槽断言"——库存值↔(总库存/各仓)、
    承运商↔货单、运单号↔货单、状态↔货单。不靠连接词小表、不靠承运商闭集；任意句式都能抽到。
  · **判定保持确定性**（不让模型模糊地"觉得对不对"）：每条断言的 (槽位, 值) 必须能在**本轮
    工具结构化返回**里逐字/归一化命中。命中不上（编造 / 错配 / 工具压根没返回这个承运商/这个数）
    → fail-closed。**不做"只要值在工具返回集合里就放行"的无槽兜底**（那正是 value-before-label
    漏的根因）。

落点：本模块（非 CODEOWNERS 锁定）由 _factslot_contract.apply 调用，权威块那套确定性渲染
（render_factslot_block）保留不动。事实槽值的唯一出口仍是权威块；正文里被抽到的事实槽值统一
移成 `[详见上方明细]`，ungrounded 的额外报 banner（反幻觉）。

可确定性验证：抽取器 _ASSERTION_EXTRACTOR 可注入 —— smoke 用固定工具返回 + 固定正文 + 确定性
stub 抽取器，断言"哪些值被移、哪些保留、哪些 ungrounded 告警"，judge 不进 prompt、不依赖 live LLM。
live 路径的 LLM 抽取器只在 HIPOP_FACTSLOT_SEMANTIC=1 时启用，且任何异常/超时/低信心 → fail-closed
（返回 None → 调用方回退到确定性结构门兜底）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from . import _factslot_contract as _fc

_REF_BLOCK = "[详见上方明细]"
_REDACT = "[当前不能确认]"

# 可注入的语义抽取器（route B 的"语义"部分）。None → 看 HIPOP_FACTSLOT_SEMANTIC 决定是否用 LLM。
# smoke 注入确定性 stub，使 grounding 判定可复现。
_ASSERTION_EXTRACTOR: Optional[Callable[[str, dict], Optional[List[dict]]]] = None


def set_assertion_extractor(fn: Optional[Callable]) -> None:
    """测试/接线用：注入确定性抽取器。fn(reply, hints) -> List[assertion] | None。"""
    global _ASSERTION_EXTRACTOR
    _ASSERTION_EXTRACTOR = fn


def reset_assertion_extractor() -> None:
    global _ASSERTION_EXTRACTOR
    _ASSERTION_EXTRACTOR = None


def _norm(s) -> str:
    return str(s or "").strip().upper()


# ── 确定性 grounding 索引：本轮工具结构化返回的"真相" ────────────────────────────

def grounding_index(tool_log: list) -> dict:
    """从 fact-slot 证据建本轮工具返回真相索引（确定性判定的唯一依据）。"""
    idx = {
        "stock_by_label": {},   # {归一化仓位标签: set(工具返回的该槽数值)}
        "order_carrier": {},    # {归一化货单号: set(该单工具返回的承运商归一化)}
        "order_tracking": {},   # {归一化货单号: set(该单工具返回的运单号归一化)}
        "has_stock": False,
        "has_logistics": False,
    }
    for ev in _fc._all_evidence(tool_log):
        if not (ev.get("ok") and ev.get("slots_proven")):
            continue
        if ev.get("tool") in _fc._STOCK_TOOLS:
            idx["has_stock"] = True
            for kw, v in (ev.get("stock_bind") or {}).items():
                if isinstance(v, int):
                    idx["stock_by_label"].setdefault(_norm(kw), set()).add(v)
        if ev.get("tool") in _fc._LOGISTICS_TOOLS:
            idx["has_logistics"] = True
            for o in (ev.get("orders") or []):
                if not isinstance(o, dict):
                    continue
                on = _norm(o.get("order_no"))
                if not on:
                    continue
                if o.get("forwarder"):
                    idx["order_carrier"].setdefault(on, set()).add(_norm(o["forwarder"]))
                if o.get("tracking_no"):
                    idx["order_tracking"].setdefault(on, set()).add(_norm(o["tracking_no"]))
    return idx


def is_grounded(assertion: dict, idx: dict) -> bool:
    """确定性：断言的 (槽位, 值) 是否逐字/归一化命中本轮工具返回。

    **必须按槽位命中，不做无槽兜底**（"值在某处出现过"不算 grounded）。
    """
    kind = assertion.get("kind")
    anchor = _norm(assertion.get("anchor"))    # 库存=仓位标签；物流=货单号
    value = assertion.get("value")
    if kind == "stock":
        try:
            n = int(re.sub(r"[^\d]", "", str(value)))
        except (ValueError, TypeError):
            return False
        allowed = set()
        for lbl, vals in idx["stock_by_label"].items():
            if lbl and (lbl == anchor or lbl in anchor or anchor in lbl):
                allowed |= vals
        return n in allowed                    # 该仓位工具确实返回过这个数
    if kind == "carrier":
        return _norm(value) in idx["order_carrier"].get(anchor, set())
    if kind == "tracking":
        return _norm(value) in idx["order_tracking"].get(anchor, set())
    if kind == "status":
        # 状态 grounding 交 _factslot_contract 的桶逻辑（此处保守视为需移块）
        return False
    return False


# ── 语义抽取（可注入；live 走 LLM，flag-gated + fail-closed）──────────────────────

_EXTRACT_SYSTEM = (
    "你是事实槽抽取器。给定一段中文客服回复，抽出其中所有【事实槽断言】并只输出 JSON 数组，"
    "不要解释。每个断言对象：{\"kind\":\"stock|carrier|tracking|status\",\"anchor\":\"仓位标签或货单号\","
    "\"value\":\"被断言的值\",\"span\":\"回复里陈述这条的最小子串\"}。"
    "kind=stock：库存数量↔仓位（总库存/义乌/东莞/noon/在途…），anchor 填仓位标签，value 填数字；"
    "kind=carrier：承运商↔货单，anchor 填货单号，value 填承运商名；"
    "kind=tracking：运单号↔货单，anchor 填货单号，value 填运单号；"
    "kind=status：物流状态↔货单。"
    "只抽【绑定到具体仓位/货单的事实值】；趋势/占比/补货建议/天数/SKU 编号/货单号本身不是事实槽断言，不要抽。"
)


def _llm_extract_assertions(reply: str, hints: dict) -> Optional[List[dict]]:
    """live LLM 抽取器（flag-gated）。任何异常 → None（fail-closed，调用方回退结构门）。"""
    try:
        from . import _provider
        r = _provider.chat_with_tools(
            messages=[{"role": "user", "content": reply}],
            system=_EXTRACT_SYSTEM, tools=[], tool_funcs={},
            scope={"_factslot_extract": True},
        )
        text = (r.get("reply") if isinstance(r, dict) else None) or ""
        m = re.search(r"\[.*\]", text, re.DOTALL)
        data = json.loads(m.group(0) if m else text)
        out = []
        for a in data if isinstance(data, list) else []:
            if isinstance(a, dict) and a.get("kind") and a.get("value") and a.get("span"):
                out.append(a)
        return out
    except Exception:
        return None


_PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY", "doubao": "DOUBAO_API_KEY",
}


def _provider_available() -> bool:
    """LLM provider 是否配了凭据。无凭据 → judge fail-closed 回退结构门 floor。"""
    try:
        from . import _provider
        p = _provider.get_provider()
        if p == "anthropic":
            return bool(os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        return bool(os.environ.get(_PROVIDER_KEY_ENV.get(p, "DEEPSEEK_API_KEY")))
    except Exception:
        return False


def _extract(reply: str, hints: dict) -> Optional[List[dict]]:
    """注入的确定性 stub（smoke）优先；否则只在**显式启用** HIPOP_FACTSLOT_SEMANTIC=1
    且 provider 有凭据时走 live LLM。server 入口（main.py）默认 setdefault=1 → 生产 / make
    test-chat 跑 live；单测 make test 不导入 main、不设此 flag → 关 → 结构门 floor → 确定性
    （即便 shell 里恰好有 API key 也不会偷偷打真 LLM）。"""
    if _ASSERTION_EXTRACTOR is not None:
        try:
            return _ASSERTION_EXTRACTOR(reply, hints)
        except Exception:
            return None
    if os.environ.get("HIPOP_FACTSLOT_SEMANTIC") == "1" and _provider_available():
        return _llm_extract_assertions(reply, hints)
    return None                                 # 未显式启用/无凭据 → 调用方走确定性结构门兜底


# ── 总入口：抽取 → grounding → 移块（事实槽只在权威块） ─────────────────────────────

def ground_and_move(reply: str, tool_log: list, question: Optional[str] = None,
                    success: bool = True) -> Tuple[str, List[str], bool]:
    """返回 (new_reply, warnings, used_semantic)。used_semantic=False → 抽取不可用，调用方回退。

    每条被抽到的事实槽断言：其值-span 统一移成权威块引用（事实槽只在权威块出现，route 甲不变量）；
    grounding 命中不上（编造/错配/工具未返回）→ 额外报 banner（反幻觉），且——无论成功失败分支——
    都走同一条 grounding 检查（不因有没有物流成功而特判，灭掉混合查询 fail-open）。
    """
    idx = grounding_index(tool_log)
    if not (idx["has_stock"] or idx["has_logistics"] or _has_any_factslot(tool_log)):
        return reply, [], True                  # 本轮无 fact-slot 工具 → 无需 grounding
    assertions = _extract(reply, {"index": idx})
    if assertions is None:
        return reply, [], False                 # 抽取不可用/失败 → fail-closed 回退结构门
    warnings: List[str] = []
    ungrounded_vals: List[str] = []
    marker = _REF_BLOCK if success else _REDACT
    for a in assertions:
        span = str(a.get("span") or a.get("value") or "")
        if not span or span not in reply:
            # span 对不上就退而移除 value 字面
            span = str(a.get("value") or "")
            if not span or span not in reply:
                continue
        grounded = is_grounded(a, idx)
        reply = reply.replace(span, marker)
        if not grounded:
            ungrounded_vals.append(str(a.get("value")))
    if ungrounded_vals:
        warnings.append(
            "⚠️ 禁编承重墙（语义 grounding）：正文出现工具本轮未返回/对不上的事实槽值 "
            f"[{', '.join(sorted(set(ungrounded_vals)))}]，已 fail-closed 移到权威块（WS-161 路线B）"
        )
    return reply, warnings, True


def _has_any_factslot(tool_log: list) -> bool:
    for ev in _fc._all_evidence(tool_log):
        return True
    return False

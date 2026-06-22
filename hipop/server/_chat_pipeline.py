"""Chat judge, history cleanup, and reply post-processing helpers.

Kept outside `agent.py` by WS-169; agent.py re-exports the symbols for existing
unit tests and patch points.
"""
import json
from typing import Dict, List

from . import data as _data
from ._agent_context import _last_replenishment_stock_status, _last_sku_rate_stats
from ._prompts import _JUDGE_SYSTEM_PROMPT


def _run_llm_judge(question, reply, tool_log, warnings):
    """独立 LLM 给回复打分。复用 governance 的 LLM 调用 + JSON 抽取 pattern。
    走当前 provider（默认 deepseek，便宜）。失败返 None → 调用方退回启发式分。"""
    from . import _provider, governance as _gov
    prompt = (
        f"用户问：{(question or '')[:300]}\n\n"
        f"Agent 回复：{(reply or '')[:600]}\n\n"
        f"调用工具：{[t['name'] for t in tool_log]}\n"
        f"系统检测到的幻觉信号：{warnings or '无'}\n\n"
        "严格返回 JSON。"
    )
    try:
        r = _provider.chat_with_tools(
            messages=[{"role": "user", "content": prompt}],
            system=_JUDGE_SYSTEM_PROMPT, tools=[], tool_funcs={},
            scope={"_judge_only": True},
        )
        return _gov._extract_json(r.get("reply") or "")
    except Exception:
        return None


def _compute_judge_confidence(question, reply, tool_log, refs, warnings):
    """judge + confidence 混合算法。
    启发式 baseline 每次跑（0 成本）；低置信 OR destructive 时触发 LLM judge 复核。
    返回 (judge_text, confidence_float, method)。
    """
    from . import governance as _gov
    n_tools = len(tool_log)
    n_fields = sum(len(t.get("result_keys") or []) for t in tool_log)
    n_warn = len(warnings or [])
    has_refs = bool(refs)

    # 启发式打分
    conf = 0.85
    conf -= 0.15 * min(n_warn, 3)        # 幻觉信号惩罚（最多 -0.45）
    if n_tools == 0: conf -= 0.20        # 凭空作答（没调任何 tool）
    if not has_refs: conf -= 0.10        # 无数据源引用
    conf = max(0.1, min(conf, 0.95))

    parts = [f"{n_tools}工具/{n_fields}字段"]
    if n_warn: parts.append(f"{n_warn}个幻觉信号")
    if refs: parts.append("源:" + ",".join((r.get("table") or "")[:20] for r in refs[:2]))
    judge = " · ".join(parts)[:200]
    method = "heuristic"

    # 混合：低置信 OR destructive tool → LLM judge 复核
    is_destr = any(_gov.is_destructive(t["name"]) for t in tool_log)
    if conf < 0.6 or is_destr:
        llm = _run_llm_judge(question, reply, tool_log, warnings)
        if llm and "confidence" in llm:
            try:
                conf = max(0.1, min(float(llm["confidence"]), 0.99))
                judge = (llm.get("verdict") or judge)[:200]
                method = "llm"
            except (TypeError, ValueError):
                pass  # LLM 返回的 confidence 不是数字 → 保留启发式分
    return judge, conf, method


import re as _re

# _safety 加的 banner / 低置信 tip 是给用户的展示层，绝不能回流进 LLM 历史 —— 否则
# LLM 复读 banner 文字（含"之前触发任务还在跑"触发词）→ sanitize 再包一层 → 无限自激双 banner。
_SAFETY_BANNER_RE = _re.compile(
    r"⚠️ \*\*系统检测到 Agent 回复中可能存在不准确之处\*\*：[\s\S]*?"
    r"以下是原始回复（已标记可疑部分）：\s*---\s*"
)
_LOWCONF_TIP_RE = _re.compile(r"⚠️ 我对这个回答的置信度较低（\d+%）[^\n]*\n\n---\n\n")
_INTENT_EXPLAIN_RE = _re.compile(r"\*{0,2}本轮我先不动手\*{0,2}[（(]你是在问能不能[）)]|[（(]\*{0,2}本轮不执行\*{0,2}[）)]|按你说的\*{0,2}不执行\*{0,2}这步刷新|本轮未执行。需要执行请明确说")


def _strip_safety_banner(text):
    """剥掉 _safety banner + 低置信 tip，拿回干净正文（用于持久化 + 喂 LLM 历史）。"""
    if not text or not isinstance(text, str):
        return text
    text = _SAFETY_BANNER_RE.sub("", text)
    text = _LOWCONF_TIP_RE.sub("", text)
    return text


def _clean_history(messages: List[Dict]) -> List[Dict]:
    """喂 LLM 前清掉 assistant 历史里残留的 banner（清 DB 已有的脏数据 + 防自激）。"""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            c = _strip_safety_banner(m["content"])
            out.append({**m, "content": "[上轮：询问/假设/影响面，未执行操作]" if _INTENT_EXPLAIN_RE.search(c) else c})
        else:
            out.append(m)
    return out


# ── 反馈/需求捕获：撞限时确定性补一句 offer（WS-26）──────────────
# 验收①要求 agent 回复『做不到/超范围』时**必含**一句 offer。靠 prompt 提醒不可靠，
# 这里做成确定性后处理 hook：只认 Agent 自述能力受限的措辞（第一人称做不了 / 功能不支持），
# 不碰数据陈述（如『库存撑不到下周』），避免污染正常回答路径（验收④）。
_DEADEND_RE = _re.compile("|".join([
    r"我(暂时|目前|这边|现在)?(做不了|做不到|无法|没法|帮不了|处理不了|实现不了|搞不定)",
    r"(帮不了|做不了|做不到|满足不了|无法满足|无能为力)(你|您)",
    r"超出(我|我的|当前|目前|系统)?.{0,4}(能力|范围|权限)",
    r"不在(我|我的|当前|目前)?.{0,6}(能力|范围|功能)(范围|内|之内)?",
    r"(这个|该|此)?功能(暂时|目前)?(还)?(不支持|没有|未上线|做不了|做不到)",
    r"系统(暂时|目前)?(还)?(不支持|没有这个功能|不具备|做不了)",
    r"暂(时)?(不|未)支持",
    r"目前(还)?(做不到|不支持|无法|没有这个)",
]))
# offer 标记串：_OFFER_MARK 是 hook 补的话术里的固定串；_OFFER_SEEN 用于判定 LLM
# 是否已经自己 offer 过（避免重复补 —— LLM 常自带「要我记成需求吗」）。
_OFFER_MARK = "记成一条需求"
_OFFER_LINE = "💡 要我把它记成一条需求反馈给产品吗？你回「记一下」我就转过去。"
_OFFER_SEEN = ("记成需求", "记成一条需求", "记成反馈", "记成一条反馈",
               "提个需求", "记下来当需求", "记录成需求")


def _needs_feedback_offer(reply: str, tools_used: List[str]) -> bool:
    """reply 表达了『我做不了/超范围』，且本轮没调过 capture_feedback、reply 里也还没 offer。"""
    if not reply or not isinstance(reply, str):
        return False
    if "capture_feedback" in (tools_used or []):
        return False            # 已经在记了，别再 offer
    if any(m in reply for m in _OFFER_SEEN):
        return False            # LLM 自己已经 offer 了，不重复
    return bool(_DEADEND_RE.search(reply))


def _maybe_append_feedback_offer(reply: str, tools_used: List[str]) -> str:
    """撞限回复确定性补一句 offer；正常回复原样返回。"""
    if _needs_feedback_offer(reply, tools_used):
        return reply.rstrip() + "\n\n" + _OFFER_LINE
    return reply


def _maybe_append_stock_readiness_warning(reply: str) -> str:
    status = _last_replenishment_stock_status.get()
    if not status or status.get("ready"):
        return reply
    text = reply or ""
    if any(k in text for k in ("未更新", "不新鲜", "滞后", "偏保守", "旧数据", "数据旧", "库存旧")):
        return reply
    warning = "提示：库存数据未更新或不完整，当前补货结论偏保守；请先完成库存更新后再计算。"
    return text.rstrip() + "\n\n" + warning


def _ensure_export_download_link(reply: str, tool_log: list) -> str:
    """If export_table produced a file, make the real /api/download link visible."""
    text = reply or ""
    for t in (tool_log or []):
        if t.get("name") != "export_table":
            continue
        url = t.get("result_download_url")
        if not url or url in text:
            continue
        filename = t.get("result_filename") or url.rsplit("/", 1)[-1] or "导出文件.xlsx"
        if "(download_url)" in text:
            text = text.replace("(download_url)", f"({url})")
        else:
            text = text.rstrip() + f"\n\n下载链接：[{filename}]({url})"
    return text


def _maybe_inject_missing_rates(reply: str, question: str) -> str:
    """
    确定性后注入：用户问取消率/退货率时，若 LLM 回复未包含该数值，
    从 _last_sku_rate_stats 提取 pct 字段并追加，防止遗漏。
    数据过期（data_stale=True）或字段为 null 时追加说明缺失原因。
    """
    q = (question or "").lower()
    wants_cancel = any(x in q for x in ("取消率", "cancel_rate"))
    wants_return = any(x in q for x in ("退货率", "return_rate"))
    if not (wants_cancel or wants_return):
        return reply
    items = _last_sku_rate_stats.get()
    if not items:
        return reply
    text = reply or ""
    injected = []
    for item in items:
        if not item.get("found"):
            continue
        sku = item.get("sku", "")
        data_stale = item.get("data_stale", False)
        if data_stale:
            if wants_cancel and "取消率" not in text and "cancel" not in text.lower():
                stale_days = item.get("stale_days", 0)
                injected.append(f"{sku} 取消率数据已过期（{stale_days} 天未刷新），无法给出准确数值")
            continue
        cancel_pct = item.get("cancel_rate_30d_pct")
        return_pct = item.get("return_rate_30d_pct")
        if wants_cancel and cancel_pct:
            num_str = cancel_pct.rstrip("%")
            if num_str not in text:
                injected.append(f"{sku} 取消率（30d）：{cancel_pct}")
        elif wants_cancel and cancel_pct is None and "取消率" not in text:
            injected.append(f"{sku} 取消率数据暂缺，请确认 wf2_orders 是否已导入")
        if wants_return and return_pct:
            num_str = return_pct.rstrip("%")
            if num_str not in text:
                injected.append(f"{sku} 退货率（30d）：{return_pct}")
    if not injected:
        return reply
    return text.rstrip() + "\n\n（补充）" + "；".join(injected)


def _asks_workflow_impact(question: str) -> bool:
    q = question or ""
    return any(
        marker in q
        for marker in (
            "会更新哪些表",
            "更新哪些表",
            "会更新哪些数据",
            "更新哪些数据",
            "影响哪些数据",
            "影响哪些",
            "影响面",
        )
    )


def _workflow_business_impact_reply(workflow: str, question: str) -> str:
    if workflow != "wf1_stock_v2" or not _asks_workflow_impact(question):
        return ""
    return (
        "影响面：会刷新 ERP 6 仓库存快照；补货建议、售罄天数和补货判断"
        "会使用新库存重算。"
    )


_DATA_HEALTH_DATE_RE = _re.compile(r"5\s*月|2026-05|\b05-\d{2}")
_DATA_HEALTH_QUESTION_RE = _re.compile(r"数据.{0,12}(?:什么时候|何时|更新|新鲜)|(?:什么时候|何时).{0,12}数据|更新的数据")


def _maybe_append_oldest_data_health_date(
    reply: str, question: str, tools_used: List[str], scope: Dict
) -> str:
    if "data_health_check" not in tools_used or not _DATA_HEALTH_QUESTION_RE.search(question or ""):
        return reply
    if _DATA_HEALTH_DATE_RE.search(reply or ""):
        return reply
    try:
        health = _data.get_data_health((scope or {}).get("store", "KSA"))
    except Exception:
        return reply

    labels = {
        "erp_products": "ERP 商品",
        "erp_sales": "ERP 销量",
        "erp_stock": "ERP 库存",
        "noon_orders": "noon 销量订单",
        "noon_stock": "noon 库存",
        "wf3_logistics": "物流数据",
        "wf5_replenish": "销售周期/补货决策",
        "wf6_alerts": "物流告警",
    }
    dated = []
    for key, source in (health.get("sources") or {}).items():
        latest = str((source or {}).get("latest") or "")[:10]
        if _re.match(r"\d{4}-\d{2}-\d{2}$", latest):
            dated.append((latest, labels.get(key, key)))
    if not dated:
        return reply
    oldest_date, oldest_label = min(dated, key=lambda item: item[0])
    return (
        (reply or "").rstrip()
        + f"\n\n补充：当前最旧的数据来源是{oldest_label}，最新日期 {oldest_date}。"
    )


_ORDER_NEGATIVE_HINT_RE = _re.compile(r"未找到|不存在|无物流|无记录|找不到|核实货单号")
_ORDER_BLOCKER_SHAPED_RE = _re.compile(
    r"没有.{0,10}(?:单号|货单号)|不像.{0,12}货单号|无法.{0,12}(?:查询|复核)|ERP.{0,20}(?:账号|凭据).{0,10}(?:未|没|无)"
)


def _maybe_append_order_lookup_negative_hint(reply: str, question: str, tools_used: List[str]) -> str:
    if "query_order_live" not in tools_used:
        return reply
    if _ORDER_NEGATIVE_HINT_RE.search(reply or ""):
        return reply
    if not _ORDER_BLOCKER_SHAPED_RE.search(reply or ""):
        return reply
    return (
        (reply or "").rstrip()
        + "\n\n补充：请核实货单号；当前没有可用 ERP 实时记录。"
    )


def _maybe_append_navigation_url(reply: str, tool_log: List[Dict]) -> str:
    if "localhost:8765" in (reply or ""):
        return reply
    for tool in tool_log or []:
        if tool.get("name") != "navigate_user_to":
            continue
        raw_args = tool.get("args") or {}
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        module = args.get("module")
        if not module:
            return reply
        from . import agent
        nav = agent.tool_navigate_user_to(module, args.get("store") or "KSA")
        if not nav.get("ok"):
            return reply
        return (reply or "").rstrip() + f"\n\n入口：{nav['url']}"
    return reply




def _dedup_refs(refs: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for r in refs:
        k = (r.get("table"), r.get("where"))
        if k in seen: continue
        seen.add(k)
        out.append(r)
    return out

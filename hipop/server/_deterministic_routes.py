"""确定性意图路由 + 配套回复 formatter（WS-167 / WS-164-S3）。

为什么存在
---------
WS-164 棘轮要把业务逻辑从 CODEOWNERS 锁定的 `agent.py` 外移。S2（WS-166）已把 `tool_*`
业务实现搬到 `tools_impl.py`；本模块（S3）承接**确定性意图路由**（`_deterministic_*`）与
**配套用户可见回复 formatter**（`_format_*`），以及它们的私有辅助（库存刷新意图 / 拒绝检测、
整数 / 百分比格式化、实时货单号抽取、只读告警计数正则）。`agent.py` 只保留 `chat()` 主编排里
对这些函数的调用接线。

口径不变：纯函数物理搬迁，行为等价 —— 不新增业务口径、不把模糊判断塞进 prompt、不改用户可见
字段 / 顺序 / 缺数据提示。`chat()` 的生产路径通过 `agent.py` 的
`from ._deterministic_routes import (...)` 再导出调用本模块，故 `agent._deterministic_*` /
`agent._format_*` 仍解析到这里的同一函数对象（api.py / 既有测试按 `agent.*` 取，外移后不断裂）。
"""
import importlib
import re
import re as _re
from typing import Any, Dict, List, Optional


_PROCUREMENT_RATE_RULE_SOURCE = "hipop/rules/procurement_rate.py"


# T37: 库存刷新意图（刷/刷新/同步/重算 + 库存，任意语序）。单一来源，路由判定
# 与拒绝词否决共用，避免正向 pattern 与否定 pattern 各写一份导致口径漂移。
_STOCK_REFRESH_INTENT_RE = _re.compile(
    r"(?:刷|刷新|同步|重算).{0,10}库存|库存.{0,5}(?:刷新|刷一下|同步|重算)"
)
# T38 宽口径库存动作词（扫/跑一下 等）也算库存刷新意图，用于把拒绝词否决扩展到
# 宽口径路由（如「不要扫库存」），不只覆盖窄口径正向 pattern。
_STOCK_REFRESH_WIDE_VERBS = ("刷", "刷新", "同步", "重算", "更新", "扫", "刷一下",
                             "拉一下", "跑一下", "重跑", "重新计算")

# T37 round-15（Luke 2026-06-09 指令①：路由层拒绝词过滤）。库存刷新是副作用动作；
# 用户在消息任意位置表达「不做/暂停/禁止」这类明确拒绝，就必须否决路由，与词序无关。
# 前 14 轮用「拒绝词必须紧贴在 刷/同步 之前」的位置型正则，运营换语序（如
# 「库存先别同步」「ERP库存不用同步」「库存请勿重算」）即可穿透。本表是位置无关的
# 拒绝标记，只在已检出库存刷新意图时才查询，误判面极小；且误判方向是「少触发一次
# 副作用」（安全侧），不会造成未授权的后台任务。
_STOCK_REFRESH_REFUSAL_MARKERS = (
    # 「不/无需」族
    "不用", "不要", "无需", "不需要", "不需", "不必", "不想", "不打算", "无须",
    "不准", "不许", "不让", "甭", "莫", "休要", "拒绝",
    # 「别/先别/暂」族（含「先不要 / 先不用」的子串「先不」）
    "别", "先别", "暂时别", "先不", "暂时不", "暂不",
    "没必要", "没必",
    # 动作直接否定
    "不刷", "不刷新", "不同步", "不重算", "不更新", "不拉", "不扫",
    # 「请勿/停/暂停/缓/搁置」族——用 halt 词根（停/缓/搁）而非穷举后缀，
    # 一次覆盖 停止/停下/停掉/停一下/叫停/喊停、暂缓/缓一缓/缓一下/先缓、搁置/搁一搁。
    "请勿", "切勿", "勿", "停", "打住", "取消",
    "暂停", "暂缓", "缓", "中止", "终止", "禁止", "严禁", "搁置", "搁",
    # 英文常见拒绝（q 已 lower）
    "don't", "don’t", "do not", "dont", "no need", "no sync", "hold off",
    "stop", "cancel", "pause", "skip",
)


def _has_stock_refresh_intent(q: str) -> bool:
    """库存刷新意图检测（窄口径正则 + 宽口径动作词，任意语序）。"""
    if _STOCK_REFRESH_INTENT_RE.search(q):
        return True
    return "库存" in q and any(v in q for v in _STOCK_REFRESH_WIDE_VERBS)


def _stock_refresh_refused(q: str) -> bool:
    """位置无关的库存刷新拒绝检测：消息任意位置含拒绝标记即视为拒绝。"""
    return any(m in q for m in _STOCK_REFRESH_REFUSAL_MARKERS)


def _stock_refresh_refusal_reply(question: str) -> Optional[str]:
    """round-15（Luke 指令①）：检出库存刷新意图但用户明确拒绝时，给出确定性回复，
    绝不路由 wf1_stock_v2（不创建后台任务）。无意图或无拒绝词时返回 None（不接管）。"""
    q = (question or "").lower()
    if not _has_stock_refresh_intent(q) or not _stock_refresh_refused(q):
        return None
    return (
        "收到，本轮不执行库存刷新 / 同步（未创建后台任务、未启动后台流程）。"
        "需要刷新时，直接说「刷库存」或「同步 ERP 6 仓库存」即可。"
    )


_PROCUREMENT_RATE_QUESTION_RE = _re.compile(
    r"采购\s*(?:议价率|折扣率)"
    r"|(?:议价率|折扣率).{0,12}采购"
    r"|plus\s*折扣.{0,24}(?:采购|议价|绩效|KPI|考核)"
    r"|(?:采购|议价|绩效|KPI|考核).{0,24}plus\s*折扣",
    _re.IGNORECASE,
)


def _procurement_rate_rule_request(question: str) -> bool:
    """采购议价率/plus 绩效口径是规则问答，必须从权威规则源确定性回答。"""
    return bool(_PROCUREMENT_RATE_QUESTION_RE.search(question or ""))


def _format_procurement_rate_rule_reply(question: str) -> str:
    """Render T48's authoritative procurement-rate answer from the audited rule file.

    If the rule module is missing or malformed, fail closed and do not restate a
    formula from memory. The caller may use the same formatter for all matching
    procurement-rate questions.
    """
    try:
        rules = importlib.import_module("hipop.rules.procurement_rate")
        formula = getattr(rules, "FORMULA")
        thresholds = getattr(rules, "THRESHOLDS")
        plus_discount = getattr(rules, "PLUS_DISCOUNT")

        formula_text = formula["formula_text"]
        numerator_definition = formula["numerator_definition"]
        denominator_components = formula["denominator_components"]
        if not (
            isinstance(denominator_components, list)
            and any("1688" in c and "标准价" in c for c in denominator_components)
            and any("头程运费" in c for c in denominator_components)
        ):
            raise ValueError("missing authoritative denominator components")
        if plus_discount.get("included_in_procurement_rate") is not False:
            raise ValueError("plus discount procurement-rate flag is not false")
        if plus_discount.get("included_in_kpi") is not False:
            raise ValueError("plus discount KPI flag is not false")

        fail_pct = int(round(float(thresholds["fail_below"]) * 100))
        pass_pct = int(round(float(thresholds["pass_above"]) * 100))
    except Exception:
        return (
            f"无法读取采购议价率权威规则源 {_PROCUREMENT_RATE_RULE_SOURCE}，本轮不编公式。"
            "请先恢复规则源后再回答。"
        )

    return (
        f"采购议价率口径（规则来源：{_PROCUREMENT_RATE_RULE_SOURCE}）：\n"
        f"- 公式：{formula_text}\n"
        f"- 议价差额：{numerator_definition}。\n"
        "- 分母必须同时包含：1688采购标准价 + 头程运费分摊。\n"
        f"- 阈值样例：< {fail_pct}% 不合格；≥ {pass_pct}% 正常。\n"
        "- plus 折扣不计入采购议价率/绩效；"
        f"{plus_discount.get('classification')}，{plus_discount.get('note')}"
    )


def _procurement_rate_rule_response(question: str, provider: str) -> Optional[Dict[str, Any]]:
    if not _procurement_rate_rule_request(question):
        return None
    reply = _format_procurement_rate_rule_reply(question)
    return {
        "reply": reply,
        "clean_reply": reply,
        "references": [{"table": "rule_source", "where": _PROCUREMENT_RATE_RULE_SOURCE}],
        "action_id": None,
        "tools_used": [],
        "tag": "查询",
        "workflow_task": None,
        "workflow_tasks": [],
        "provider": provider,
        "confidence": 1.0,
        "judge_method": "deterministic_procurement_rate_rule_router",
        "hallucination_warnings": None,
    }


def _deterministic_workflow_request(question: str) -> Optional[Dict[str, str]]:
    q = (question or "").lower()
    # WS-145 肯定执行意图门:只有「肯定祈使 + 低风险」才进真实执行路由。
    # 否定/询问/假设/只问影响面的句子（即使含「刷新/重算」）一律不路由（结构判别，
    # 非逐句关键词黑名单）。高风险动作在 chat() 走 confirm-first，不到这里。
    from . import _execution_intent_gate as _intent_gate
    if not _intent_gate.enters_execution(question or ""):
        return None
    # T37 round-15：库存刷新是副作用动作，已检出意图但用户明确拒绝 → 不路由（任意语序）。
    if _has_stock_refresh_intent(q) and _stock_refresh_refused(q):
        return None
    # T37: 直接路由库存刷新口语意图（刷/刷新/同步/重算 + 库存）。
    if _STOCK_REFRESH_INTENT_RE.search(q):
        return {"workflow": "wf1_stock_v2", "label": "库存刷新"}
    # T38: 宽口径——"重跑"/"重新计算" 也属于执行意图触发词
    if not any(v in q for v in ("刷新", "刷库存", "刷库", "同步", "重算", "跑一下", "拉一下",
                                 "扫", "刷一下", "重跑", "重新计算")):
        return None
    if "物流" in q:
        return {"workflow": "wf3_logistics_v2", "label": "物流刷新"}
    if "库存" in q:
        return {"workflow": "wf1_stock_v2", "label": "库存刷新"}
    # T38: 销售周期/补货建议 → wf5_sales_cycle_v2（低风险内部重算，直跑）
    if any(k in q for k in ("销售周期", "补货建议")):
        return {"workflow": "wf5_sales_cycle_v2", "label": "销售周期与补货重算"}
    return None


def _deterministic_multi_workflow_request(question: str) -> List[Dict[str, str]]:
    q = (question or "").lower()
    # WS-145 肯定执行意图门同样约束多 workflow 路由。非执行语气
    # （能不能/如果/影响面/否定）不能先试 run_workflow 再把门的拒绝渲染成「启动失败」。
    from . import _execution_intent_gate as _intent_gate
    if not _intent_gate.enters_execution(question or ""):
        return []
    if not any(v in q for v in ("刷新", "同步", "重算", "跑一下", "拉一下", "扫", "刷一下",
                                 "重跑", "重新计算")):
        return []

    if "erp" not in q:
        return []

    wants_products = "商品库" in q or any(k in q for k in ("商品", "产品"))
    wants_sales_price = "销量价格" in q or ("销量" in q and "价格" in q)
    if wants_products and wants_sales_price:
        return [
            {"workflow": "wf2_products_v2", "label": "ERP 商品库刷新"},
            {"workflow": "wf2_sales_v2", "label": "销量价格刷新"},
        ]
    return []


def _deterministic_erp_refresh_time_request(question: str) -> bool:
    q = (question or "").lower()
    from . import _execution_intent_gate as _intent_gate
    gate_decision = _intent_gate.evaluate(question or "")
    is_time_query = _intent_gate.is_refresh_time_query(question or "")
    if gate_decision.enters_execution:
        return False
    if "erp" not in q:
        return False
    if not (
        gate_decision.has_refresh_trigger
        or is_time_query
        or any(v in q for v in ("更新", "刷新过", "更新过", "同步过", "刷过", "刷的"))
    ):
        return False
    has_time_question = is_time_query or any(x in q for x in (
        "上次", "什么时候", "多久前", "几天前", "哪天", "何时", "最近一次",
        "刷新时间", "刷新日期", "更新时间", "更新日期", "刷新过", "更新过", "刷过", "刷的",
    ))
    if not has_time_question:
        return False
    wants_products = "商品库" in q or any(k in q for k in ("商品", "产品"))
    wants_sales_price = "销量价格" in q or ("销量" in q and "价格" in q)
    return wants_products and wants_sales_price


def _format_erp_refresh_time_reply(store: str, health: dict) -> str:
    sources = (health or {}).get("sources") or {}

    def _source_text(key: str, label: str) -> str:
        source = sources.get(key) or {}
        latest = source.get("latest") or "无记录"
        stale_days = source.get("stale_days")
        if stale_days is None:
            age = "暂无可计算天数"
        elif stale_days <= 0:
            age = "今天"
        else:
            age = f"{stale_days} 天前"
        return f"{label}最近刷新时间：{latest}（{age}）"

    return (
        f"{store.upper()} "
        + "；".join([
            _source_text("erp_products", "ERP 商品库"),
            _source_text("erp_sales", "ERP 销量价格"),
        ])
        + "。这里只能按日期粒度回答，没有具体几点；本轮没有触发后台刷新。"
    )


def _deterministic_export_request(question: str) -> Optional[Dict[str, str]]:
    q = question or ""
    if not any(x in q for x in ("导出", "下载", "excel", "Excel", "表格", "xlsx")):
        return None
    view = "sales"
    if "补货" in q:
        view = "replenish"
    elif "物流" in q or "货单" in q:
        view = "logistics"
    elif "未上架" in q:
        view = "unlisted_with_sales"
    return {"view": view, "filter_desc": q[:80]}


def _deterministic_data_freshness_request(question: str) -> bool:
    q = question or ""
    if "数据" not in q:
        return False
    return any(x in q for x in (
        "什么时候更新", "啥时候更新", "多久前", "几天前", "更新的数据",
        "更新时间", "更新日期", "新鲜", "具体到几点",
    ))


def _deterministic_total_stock_topn_request(question: str) -> "Optional[int]":
    q = question or ""
    triggers = ("总库存最高", "库存最多", "积压最多", "库存 TopN", "库存topn",
                "总库存 Top", "总库存top", "当前库存量排行", "库存量最高", "库存最大")
    if not any(t in q for t in triggers):
        return None
    m = _re.search(r"(\d+)\s*个", q)
    if m:
        return max(1, min(int(m.group(1)), 50))
    return 10


def _deterministic_product_sales_topn_request(question: str) -> "Optional[int]":
    q = question or ""
    if "销量" not in q:
        return None
    if any(x in q for x in ("库存", "补货", "货单", "物流")):
        return None
    if any(x in q for x in ("180天", "历史", "总销量")):
        return None
    has_subject = any(x in q for x in ("商品", "产品", "SKU", "sku", "Sku", "款"))
    has_top_intent = any(x in q for x in ("最高", "最多", "排行", "排名", "Top", "top", "TOP", "前"))
    if not (has_subject and has_top_intent):
        return None
    patterns = (
        r"(?:Top|top|TOP)\s*(\d+)",
        r"前\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"最高的\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"最多的\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"(\d+)\s*(?:个|名|款|条)\s*(?:商品|产品|SKU|sku)",
    )
    for pat in patterns:
        m = _re.search(pat, q)
        if m:
            return max(1, min(int(m.group(1)), 50))
    return 10


def _format_product_sales_topn_reply(store: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"{store} 近30天销量 TopN 暂时不可用。"
    if tool_result.get("error"):
        return f"{store} 近30天销量 TopN 暂时不可用：{tool_result.get('error')}"
    if tool_result.get("fail_closed"):
        return tool_result.get("message") or f"{store} 近30天销量 TopN 数据超过 3 天，不能出数。请先刷新销量后重问。"
    items = tool_result.get("items") or []
    if not items:
        return f"{store} 暂无可排序的近30天销量商品数据。"
    from hipop.scripts.evidence_contract import (
        assert_query_evidence as _assert_query_evidence,
        render_evidence_suffix as _render_evidence_suffix,
        ContractViolation as _ContractViolation,
    )
    try:
        evidence = _assert_query_evidence(tool_result.get("evidence"), context="list_products_sales_topn_reply")
    except _ContractViolation as _e:
        return (
            f"{store} 近30天销量 TopN 缺少可追溯证据（来源/取数时间/口径），"
            f"按规则不出数。详情：{_e}"
        )
    lines = [f"{store} 近30天销量最高的 {len(items)} 个商品：", ""]
    for i, item in enumerate(items[:10], 1):
        sku = item.get("sku") or "?"
        title = (item.get("title") or "").strip()
        name = f"{sku}（{title}）" if title else sku
        sales_30d = item.get("sales_30d")
        lines.append(f"{i}. **{name}**：近30天销量 {_fmt_int(sales_30d)}")
    lines.append("")
    lines.append(_render_evidence_suffix(evidence))
    return "\n".join(lines)


_WINDOW_ISO_DATE_RE = _re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_WINDOW_CN_DATE_RE = _re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?")

# 相对「近N天」的**结构化**识别（不靠逐句穷举写法）：
#   前缀（近/最近/过去/过往/这/前）+ 数字（阿拉伯或中文）+ 单位（天/日），或裸「N天」。
# 「日」只在带前缀时认（裸「N日」会和「6月30日」这种月内日撞），裸窗只认「天」。
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM_CHARS = "0-9零〇一二两三四五六七八九十百千"
_REL_WINDOW_RE = _re.compile(
    rf"(?:最近|近|过去|过往|这|前)的?\s*([{_NUM_CHARS}]+)\s*[天日]"
    rf"|(?<![0-9年月])([{_NUM_CHARS}]+)\s*天"
)


def _cn_to_int(s: str):
    """中文数字 → int（支持 十/百/千，覆盖天数量级）；非法字符返回 None。"""
    total, current = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10; current = 0
        elif ch == "百":
            total += (current or 1) * 100; current = 0
        elif ch == "千":
            total += (current or 1) * 1000; current = 0
        else:
            return None
    return total + current


def _window_days_token(tok: str):
    """窗口天数 token（阿拉伯或中文）→ int；非法返回 None。"""
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    return _cn_to_int(tok)


def _deterministic_window_sales_topn_request(question: str) -> "Optional[Dict]":
    """WS-120 [T07]：识别『指定日期窗口 / 近N天』销量 TopN —— 与 WS-148 裸 TopN 互斥且优先。

    显式起止日期窗口、以及『近/最近/过去/过往/这/前 N 天(日)』(N 可阿拉伯或中文、含 N=30)
    都走 top_sales_by_window，按 wf2_orders 逐单现算并以最新订单业务日倒推；无**时间窗**的
    裸 TopN（如『销量最高的3个商品』）才由 _deterministic_product_sales_topn_request 走
    list_products/sales_30d 固定桶。结构化识别 N天，避免红队换同义写法（过去30天/近三十天/
    近30日）把口径带回固定桶。

    返回 {"start_date","end_date","limit"} | {"relative_days":N,"limit":L} | None。
    """
    import datetime as _dt
    q = question or ""
    if not any(k in q for k in ("销量", "卖", "热销", "畅销")):
        return None
    if any(x in q for x in ("库存", "补货", "货单", "物流")):
        return None
    if any(x in q for x in ("180天", "历史", "总销量")):
        return None
    has_top_intent = any(x in q for x in (
        "最高", "最多", "排行", "排名", "Top", "top", "TOP", "前",
        "热销", "畅销", "最好卖", "卖得最好", "卖得最多",
    ))
    if not has_top_intent:
        return None

    limit = 10
    for pat in (
        r"(?:Top|top|TOP)\s*(\d+)",
        r"前\s*(\d+)(?!\s*[天日])",   # 「前N天」是窗口、不是 TopN 个数；只认「前N(个/名…)」为 limit
        r"最高的?\s*(\d+)",
        r"最多的?\s*(\d+)",
        r"(\d+)\s*(?:个|名|款|条)\s*(?:商品|产品|SKU|sku)?",
    ):
        m = _re.search(pat, q)
        if m:
            limit = max(1, min(int(m.group(1)), 50))
            break

    dates = []
    for rx in (_WINDOW_ISO_DATE_RE, _WINDOW_CN_DATE_RE):
        for mm in rx.finditer(q):
            try:
                dates.append(_dt.date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3))).isoformat())
            except ValueError:
                continue
    dates = sorted(set(dates))
    if dates:
        return {"start_date": dates[0], "end_date": dates[-1], "limit": limit}

    m = _REL_WINDOW_RE.search(q)
    if m:
        n = _window_days_token(m.group(1) or m.group(2))
        if n and 1 <= n <= 3650:
            return {"relative_days": n, "limit": limit}
    return None


def _format_window_sales_topn_reply(store: str, tool_result: dict) -> str:
    """WS-120：渲染窗口 TopN 结果。缺数/陈旧 fail-closed 明确报，不返排名（承重墙）。"""
    if not isinstance(tool_result, dict):
        return f"{store} 指定窗口销量 TopN 暂时不可用。"
    if tool_result.get("error"):
        return f"{store} 指定窗口销量 TopN 暂时不可用：{tool_result.get('error')}"
    # 近N天时效门 fail-closed（最新订单 >3 天）—— 复用 WS-134 message 口径
    if tool_result.get("fail_closed"):
        return tool_result.get("message") or (
            f"{store} 近N天销量 TopN 数据超过 3 天未更新，不能出数。请先刷新销量后重问。")
    start = tool_result.get("start_date") or "?"
    end = tool_result.get("end_date") or "?"
    if not tool_result.get("available"):
        reason = tool_result.get("reason")
        cov = tool_result.get("coverage") or {}
        latest = cov.get("max_order_date") or ""
        earliest = cov.get("min_order_date") or ""
        if reason == "bad_window":
            return f"{store} 销量窗口 {start}~{end} 不合法（请用 YYYY-MM-DD 且起始 ≤ 结束）。"
        if reason == "no_order_data":
            return f"{store} 暂无订单数据，无法计算 {start}~{end} 的窗口销量 TopN。"
        if reason == "window_start_not_covered":
            tail = f"（订单数据从 {earliest} 起）" if earliest else ""
            return (f"数据不足：{store} 销量窗口起点 {start} 早于已有订单数据{tail}，前半段缺数，"
                    f"按规则不出排名。请缩小窗口起点或补齐更早订单后重问。")
        tail = f"，当前订单最新到 {latest}" if latest else ""
        return (f"数据不足：{store} 销量窗口终点 {end} 暂未被订单数据覆盖{tail}，按规则不出排名。"
                f"如需该窗口请先刷新/补齐订单数据后重问。")
    items = tool_result.get("items") or []
    if not items:
        return f"{store} 销量窗口 {start}~{end} 内暂无成交 SKU。"
    from hipop.scripts.evidence_contract import (
        assert_query_evidence as _assert_query_evidence,
        render_evidence_suffix as _render_evidence_suffix,
        ContractViolation as _ContractViolation,
    )
    try:
        evidence = _assert_query_evidence(tool_result.get("evidence"), context="top_sales_by_window_reply")
    except _ContractViolation as _e:
        return (f"{store} 销量窗口 {start}~{end} TopN 缺少可追溯证据（来源/取数时间/口径），"
                f"按规则不出数。详情：{_e}")
    lines = [f"{store} {start} ~ {end} 销量最高的 {len(items)} 个 SKU：", ""]
    for i, item in enumerate(items[:50], 1):
        sku = item.get("partner_sku") or "?"
        title = (item.get("title") or "").strip()
        name = f"{sku}（{title}）" if title else sku
        lines.append(f"{i}. **{name}**：窗口销量 {_fmt_int(item.get('window_sales'))}")
    lines.append("")
    lines.append(_render_evidence_suffix(evidence))
    return "\n".join(lines)


def _window_sales_topn_route(question: str, scope: dict, exec_tool, provider_name: str = "?"):
    """WS-120：指定日期窗口 / 近N天 销量 TopN 的完整确定性路由（返回 chat 响应 dict 或 None）。

    放在本非锁模块里、agent.py 只留一行接线 —— 遵守 agent.py 防回潮行数棘轮（WS-165/167）。
    top_sales_by_window 结果无 references 键，故 references 固定 []。
    """
    win_req = _deterministic_window_sales_topn_request(question)
    if win_req is None:
        return None
    store = (scope.get("store") or "KSA").upper()
    tool_args = {"store": store, "limit": win_req["limit"], "listing": "all"}
    tool_args.update({k: v for k, v in win_req.items() if k != "limit"})
    tool_result = exec_tool("top_sales_by_window", tool_args, user=scope)
    reply = _format_window_sales_topn_reply(store, tool_result)
    return {
        "reply": reply, "clean_reply": reply, "references": [],
        "action_id": None, "tools_used": ["top_sales_by_window"], "tag": "查询",
        "workflow_task": None, "provider": provider_name,
        "confidence": 1.0 if not (tool_result or {}).get("error") else 0.8,
        "judge_method": "deterministic_window_sales_topn_router",
        "hallucination_warnings": None,
    }


def _format_total_stock_topn_reply(store: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"{store} 库存查询暂不可用，请稍后重试。"
    if tool_result.get("fail_closed"):
        max_age = tool_result.get("max_age_days", 3)
        return tool_result.get("message") or (
            f"{store} 库存数据超过 {max_age} 天未更新，不能出数。请先刷新库存后重问。"
        )
    if tool_result.get("empty"):
        return tool_result.get("message") or f"{store} 暂无库存数据。"
    items = tool_result.get("items") or []
    # WS-144 统一证据契约：出数前强制校验证据三要素（来源/取数时间/口径）。
    # 无证据 → fail-closed 不出数，不许旁路旧字段直接渲染裸数字。
    from hipop.scripts.evidence_contract import (
        assert_query_evidence as _assert_query_evidence,
        render_evidence_suffix as _render_evidence_suffix,
        ContractViolation as _ContractViolation,
    )
    try:
        evidence = _assert_query_evidence(tool_result.get("evidence"), context="total_stock_topn_reply")
    except _ContractViolation as _e:
        return (
            f"{store} 总库存查询缺少可追溯证据（来源/取数时间/口径），"
            f"按规则不出数。详情：{_e}"
        )
    lines = [
        f"{store} 总库存最高的 {len(items)} 个 SKU，",
        "**口径**：total_stock = noon官方仓 + 海外仓 + 国内仓 + 送仓未上架(pending)，"
        "与 noon 可售数(saleable)不同。",
        "",
    ]
    for i, r in enumerate(items[:10], 1):
        sku = r.get("partner_sku", "?")
        total = r.get("total_stock", 0)
        saleable = r.get("noon_saleable_qty", 0)
        pending = r.get("pending_inbound_qty", 0)
        lines.append(
            f"{i}. **{sku}**  总库存 {total:,}（可售 {saleable:,} / 送仓未上架 {pending:,}）"
        )
    lines.append("")
    lines.append(_render_evidence_suffix(evidence))
    return "\n".join(lines)


def _deterministic_scope_overview_request(question: str) -> bool:
    q = question or ""
    if "红色告警" not in q:
        return False
    return any(x in q for x in ("几个", "多少", "几条", "数量", "有几"))


def _deterministic_products_count_request(question: str) -> bool:
    q = question or ""
    if any(x in q for x in ("需要我关注", "哪些需要关注", "哪些要关注", "需要关注")):
        return False
    has_product_subject = any(x in q for x in ("商品", "产品", "SKU", "sku", "Sku", "未上架", "上架"))
    has_count_intent = any(x in q for x in ("总共", "总数", "多少", "数量", "几个", "几款"))
    return has_product_subject and has_count_intent


def _fmt_int(value) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def _format_products_count_reply(store: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"{store} 商品总数暂时不可用。"
    products = tool_result.get("summary_products") or {}
    skus = tool_result.get("summary_skus") or {}
    return (
        f"{store} 商品总数：product 维度 {_fmt_int(products.get('total'))} 个，"
        f"SKU 维度 {_fmt_int(skus.get('total'))} 个。"
        f"其中 product 已上架 {_fmt_int(products.get('listed'))} 个、未上架 {_fmt_int(products.get('unlisted'))} 个；"
        f"SKU 已上架 {_fmt_int(skus.get('listed'))} 个、未上架 {_fmt_int(skus.get('unlisted'))} 个。"
    )


def _format_scope_overview_reply(store: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"{store} 店铺概览暂时不可用。"
    red = tool_result.get("alerts_red", 0)
    pending = tool_result.get("alerts_pending", 0)
    sku_count = tool_result.get("sku_count", 0)
    return f"{store} 当前红色告警 {red} 个；待处理告警 {pending} 个；在售 SKU {sku_count} 个。"


def _format_data_freshness_reply(store: str, tool_result: dict) -> str:
    sources = (tool_result or {}).get("sources") or {}
    labels = {
        "erp_products": "ERP 商品",
        "erp_sales": "ERP 销量",
        "erp_stock": "ERP 库存",
        "noon_orders": "noon 销量",
        "noon_stock": "noon 库存",
        "wf3_logistics": "物流",
        "wf5_replenish": "补货建议",
        "wf6_alerts": "物流告警",
    }
    rows = []
    for key, source in sources.items():
        latest = source.get("latest") or "无记录"
        stale_days = source.get("stale_days")
        if stale_days is None:
            age = "暂无可计算天数"
        elif stale_days <= 0:
            age = "今天"
        else:
            age = f"{stale_days} 天前"
        rows.append((stale_days if stale_days is not None else -1, key, labels.get(key, key), latest, age))
    stale_rows = [r for r in rows if isinstance(r[0], int) and r[0] > 0]
    shown = sorted(stale_rows, reverse=True)[:4] or sorted(rows, reverse=True)[:4]
    parts = [f"{label}最新到 {latest}（{age}）" for _d, _key, label, latest, age in shown]
    if not parts:
        return f"{store} 暂时没有可用的数据更新时间记录。"
    return (
        f"{store} 数据按来源看，存在旧快照："
        + "；".join(parts)
        + "。data_health_check 只提供日期粒度，没有具体几点。"
    )


def _deterministic_sku_metric_request(question: str) -> Optional[str]:
    q = (question or "").upper()
    if "30" not in q:
        return None
    if not any(x in question for x in ("销量", "总单量", "历史总销量", "退货率", "取消率")):
        return None
    m = _re.search(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", q)
    return m.group(0) if m else None


def _deterministic_replenishment_sku_request(question: str) -> Optional[str]:
    q = question or ""
    q_up = q.upper()
    if not any(x in q for x in (
        "补货", "pipeline", "Pipeline", "风险标签", "紧急度", "待发", "在途",
    )):
        return None
    if not any(x in q for x in ("补货", "pipeline", "Pipeline")):
        return None
    # WS-180/T29: 排除 TOP\d+ 序数词（Top5/Top10 → TOP5/TOP10），它们是 TopN 个数、不是业务 SKU。
    # 真实 SKU（TBS0228A/TBU0010A）末位是字母；TOP5 末位是数字，用 fullmatch 精确区分。
    for _m in _re.finditer(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", q_up):
        if not _re.fullmatch(r"TOP\d+", _m.group(0)):
            return _m.group(0)
    return None


def _deterministic_replenishment_list_request(question: str) -> "Optional[int]":
    q = question or ""
    # WS-180/T29: 排除 TOP\d+ (Top5/Top10) —— 它们是 TopN 序数词，不是业务 SKU 代码。
    # 真实业务 SKU（TBS0228A/TBU0010A）末位是字母；"TOP5" 末位是数字。仅当出现真 SKU
    # 时才让位给单 SKU 路由（返回 None），裸 TopN 补货询问应进确定性补货清单路由。
    _sku_toks = _re.findall(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", q.upper())
    if any(not _re.fullmatch(r"TOP\d+", tok) for tok in _sku_toks):
        return None
    triggers = ("补货建议", "本周必补", "该补货", "要补货", "哪些要补", "哪些货要补", "补多少")
    if not any(t in q for t in triggers):
        return None
    if any(t in q for t in ("刷新", "同步", "重算", "跑一下", "重跑", "重新计算")):
        return None
    patterns = (
        r"(?:Top|top|TOP)\s*(\d+)",
        r"前\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"最高的\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"最多的\s*(\d+)\s*(?:个|名|款|条|SKU|sku)?",
        r"(\d+)\s*(?:个|名|款|条)",
    )
    for pat in patterns:
        m = _re.search(pat, q)
        if m:
            return max(1, min(int(m.group(1)), 50))
    return 10


def _deterministic_stock_split_request(question: str) -> Optional[str]:
    """检测「单 SKU 四仓库存拆分」意图，返回 SKU 代码或 None。"""
    q = question or ""
    triggers = ("库存拆分", "四仓", "义乌仓", "沙特仓", "沙特一号仓", "noon仓",
                "仓库明细", "各仓库存", "仓库分布", "库存分仓", "总库存拆分", "仓拆分",
                "yiwu", "saudi_1", "overseas_saudi")
    if not any(t in q for t in triggers):
        # 也检测「XXX总库存」/「XXX库存多少」模式（需含 SKU 模式）
        if not any(x in q for x in ("总库存", "库存多少", "多少库存", "库存是多少")):
            return None
    m = _re.search(r"\b([A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*)\b", q.upper())
    return m.group(1) if m else None


def _format_stock_split_reply(sku: str, tool_result: dict) -> str:
    if not tool_result or tool_result.get("fail_closed"):
        msg = (tool_result or {}).get("message") or f"SKU {sku} 库存数据不可用。"
        return msg
    split = tool_result.get("split") or {}
    total = tool_result.get("total", 0)
    ts = tool_result.get("updated_at") or "未知时间"
    stale_warn = tool_result.get("stale_warn") or ""
    noon_note = "（noon未拉取）" if tool_result.get("noon_missing") else ""
    lines = [
        f"{sku} 四仓库存拆分（截至 {ts}）：",
        f"  义乌仓：{split.get('yiwu', 0)}",
        f"  沙特一号仓：{split.get('overseas_saudi_1', 0)}",
        f"  noon仓：{split.get('noon', 0)}{noon_note}",
        f"  在途：{split.get('inbound', 0)}",
        f"  **合计：{total}**",
    ]
    if stale_warn:
        lines.append(stale_warn)
    decision = tool_result.get("freshness_decision")
    if isinstance(decision, dict) and decision.get("can_output_number"):
        from hipop.scripts.freshness_gate import render_freshness_suffix as _render_freshness_suffix
        suffix = _render_freshness_suffix(decision)
        if suffix:
            lines.append(suffix)
    return "\n".join(lines)


def _format_replenishment_list_reply(store: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"{store} 补货建议暂时不可用。"
    if tool_result.get("fail_closed"):
        return tool_result.get("message") or f"{store} 补货建议来源不完整或超过 3 天，不能出数。请先刷新库存/销量/补货工作流。"
    items = tool_result.get("items") or []
    if not items:
        stock_status = tool_result.get("stock_status") or {}
        if stock_status.get("ready") is False:
            return stock_status.get("message") or f"{store} 库存未就绪，不能给确定补货建议。"
        return f"{store} 当前没有 weekly_total_replenish > 0 的补货建议。"
    from hipop.scripts.evidence_contract import (
        assert_query_evidence as _assert_query_evidence,
        render_evidence_suffix as _render_evidence_suffix,
        ContractViolation as _ContractViolation,
    )
    try:
        evidence = _assert_query_evidence(tool_result.get("evidence"), context="replenishment_list_reply")
    except _ContractViolation as _e:
        return (
            f"{store} 补货建议缺少可追溯证据（来源/取数时间/口径），"
            f"按规则不出数。详情：{_e}"
        )
    lines = [
        f"{store} 本周补货建议前 {len(items)} 个 SKU：",
        "**口径**：统一库存不含国际在途；补货建议来自 wf5_sales_cycle 工作流公式。",
        "",
    ]
    for i, item in enumerate(items[:10], 1):
        sku = item.get("sku") or "?"
        title = (item.get("title") or "").strip()
        name = f"{sku}（{title}）" if title else sku
        lines.append(
            f"{i}. **{name}**：建议补货 {_fmt_int(item.get('qty'))} 件，"
            f"紧急度 {item.get('urgency') or '未标注'}，日销 {_format_metric_value(item.get('daily_rate'))}。"
        )
    lines.append("")
    lines.append(_render_evidence_suffix(evidence))
    return "\n".join(lines)


def _format_pct(value) -> str:
    try:
        pct = float(value or 0)
    except Exception:
        pct = 0.0
    if abs(pct) <= 1:
        pct *= 100
    return f"{pct:.2f}".rstrip("0").rstrip(".")


def _format_metric_value(value) -> str:
    return "暂无数据" if value is None else str(value)


def _format_sku_metric_reply(sku: str, tool_result: dict) -> str:
    items = (tool_result or {}).get("items") or []
    item = next((x for x in items if (x.get("sku") or "").upper() == sku.upper()), None)
    if not item or not item.get("found"):
        if item and item.get("stale_expired"):
            as_of = item.get("as_of_date") or "未知"
            stale_days = item.get("stale_days")
            age = f"{stale_days} 天" if stale_days is not None else "过期"
            # WS-131 口径对齐：快照超 3 天即不能使用缓存数（与 freshness 门同语）。
            over3 = (
                "数据已超过 3 天，不能使用缓存。"
                if (stale_days is not None and stale_days > 3) else ""
            )
            return (
                f"查不到 {sku} 的有效近期数据（快照截至 {as_of}，"
                f"已超期 {age}）。{over3}需先刷新 ERP 数据后重新查询。"
            )
        return f"未找到 SKU {sku} 的记录，请核实 SKU 是否正确。"
    decision = item.get("sales_freshness_decision")
    suffix = ""
    if isinstance(decision, dict):
        if not decision.get("can_output_number"):
            return decision.get("message") or f"{sku} 当前不能出数。"
        from hipop.scripts.freshness_gate import render_freshness_suffix as _render_freshness_suffix
        suffix = _render_freshness_suffix(decision)
    elif item.get("data_stale"):
        as_of = item.get("as_of_date") or "未知日期"
        stale_days = item.get("stale_days")
        age = f"{stale_days} 天前" if stale_days is not None else "较旧"
        return f"{sku} 的数据快照截至 {as_of}（{age}），当前数值已过期，不能按新鲜 30 天口径报数。"
    as_of = item.get("as_of_date") or "当前快照"
    return (
        f"{sku} 30 天口径截至 {as_of}："
        f"30 天销量 {_format_metric_value(item.get('sales_30d'))}，"
        f"30 天总单量 {_format_metric_value(item.get('total_orders_30d'))}，"
        f"历史总销量 {_format_metric_value(item.get('history_total'))}，"
        f"退货率 {_format_pct(item.get('return_rate_30d'))}%，"
        f"取消率 {_format_pct(item.get('cancel_rate_30d'))}%。"
        f"{suffix}"
    )


def _extract_live_order_no(question: str) -> Optional[str]:
    q = question or ""
    if "货单" not in q and "物流" not in q and "状态" not in q:
        return None
    if not any(x in q for x in ("物流", "状态", "到哪", "当前", "实时", "查")):
        return None
    m = _re.search(r"\b[A-Z]{2,}[A-Z0-9-]{5,}\b", q.upper())
    return m.group(0) if m else None


def _format_order_live_reply(order_no: str, tool_result: dict) -> str:
    if not isinstance(tool_result, dict):
        return f"未找到货单 {order_no} 的物流记录，请核实货单号。"
    if tool_result.get("error") == "order_not_found_in_erp":
        return f"未找到货单 {order_no}：ERP 中无记录，当前无物流数据，请核实货单号。"
    if not tool_result.get("ok"):
        msg = tool_result.get("message") or tool_result.get("error") or "实时查询失败"
        return f"未找到货单 {order_no} 的实时物流记录，或当前无法完成 ERP 实时查询：{msg}。请核实货单号。"
    forwarder = tool_result.get("forwarder") or "未知承运商"
    tracking = tool_result.get("tracking_no") or "无跟踪号"
    status = tool_result.get("status") or "未知状态"
    current_node = tool_result.get("current_node") or {}
    node_text = current_node.get("desc") or current_node.get("status") or ""
    tail = f"；最新节点：{node_text}" if node_text else ""
    return f"货单 {order_no} 当前状态：{status}，承运商 {forwarder}，跟踪号 {tracking}{tail}。"


_READONLY_REFRESH_VERB_RE = re.compile(
    r"刷新|同步|重算|跑一下|拉一下|扫|刷一下|重跑|重新计算|生成|创建|启动|触发|更新"
)
_ALERT_COUNT_QUERY_RE = re.compile(
    r"(?:红色告警|告警)[^。\n!?]{0,12}(?:几个|多少|数量|数|总数)"
    r"|(?:几个|多少|数量|总数)[^。\n!?]{0,12}(?:红色告警|告警)"
)


def _deterministic_readonly_request(question: str) -> Optional[Dict[str, Any]]:
    """Pure read-only chat intents that must not be upgraded into run_workflow."""
    q = (question or "").strip().lower()
    if not q or _READONLY_REFRESH_VERB_RE.search(q):
        return None
    if _ALERT_COUNT_QUERY_RE.search(q):
        return {"tool": "scope_overview", "intent": "alert_count"}
    return None


def _deterministic_readonly_reply(intent: str, tool_result: dict, store: str) -> str:
    if not isinstance(tool_result, dict) or tool_result.get("error"):
        reason = (tool_result or {}).get("message") or (tool_result or {}).get("error") or "查询失败"
        return f"本轮没有查到红色告警数量：{reason}。请稍后重试。"

    if intent == "alert_count":
        red = tool_result.get("alerts_red")
        pending = tool_result.get("alerts_pending")
        if red is None:
            return "本轮没有查到红色告警数量：scope_overview 未返回告警数。请稍后重试。"
        suffix = f"，待处理告警 {pending} 个" if pending is not None else ""
        return f"{store.upper()} 当前红色告警 {red} 个{suffix}。"

    return "本轮查询已完成。"

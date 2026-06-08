"""Layer 3 — chat reply 后处理验证（fail-safe，事后捕获 hallucinate）

返回 (sanitized_reply, warnings: list[str]) — 命中规则时给 reply 头部加 banner，
让用户看见 Agent 在编造，并在前端展开真相。
"""
from __future__ import annotations
import json
import re
from typing import Dict, List, Optional, Tuple

# 已知合法域名（可以出现在 reply 里的）
ALLOWED_DOMAINS = {
    "localhost",
    "127.0.0.1",
    "my.feishu.cn",
    "feishu.cn",
    "open.feishu.cn",
    "dbuyerp.com",
    "noon.com",
    "saudi-en.noon.com",
}

# 已知 hallucinate 域名 pattern（白名单不命中时进一步用这个 hard-block）
SUSPICIOUS_DOMAINS = {
    "diangou.ai",      # Qwen 编过
    "agent.diangou",
    ".dgo.com",
    "hipop-agent",
    "dianpou-os",
}

# wf2 / wf5 真实存在的字段（白名单）
WF5_REAL_FIELDS = {
    "partner_sku", "trend", "daily_rate", "urgency", "weekly_total_replenish",
    "current_pipeline", "target_pipeline", "ops_advice", "risk_label",
    "sellable_days", "decision_days", "wf5_replenish_qty", "lost_replenish_qty",
    "trigger_reasons",
}
WF2_REAL_FIELDS = {
    "partner_sku", "noon_sku", "product_id", "title", "image_url", "brand",
    "cost_price", "latest_price", "avg_price", "latest_profit_rate",
    "sales_10d", "sales_30d", "sales_60d", "sales_90d", "sales_120d", "sales_180d",
    "is_listed", "sales_grade", "forecast_10d", "forecast_30d",
    "total_orders", "valid_orders", "cancel_count", "return_count",
    "cancel_rate", "return_rate", "anomalies_json",
}
# 真正不存在的字段（无任何真实字段背书）—— 永远拦。
HALLUCINATED_FIELDS = {
    "海运ROI预估", "空运ROI预估", "推荐物流方式",
    "weekly_priority", "replenish_priority", "next_ship_date",
    "7天销量",  # 真实是 sales_10d/30d 等
}
# 真实字段的中文人话别名（WS-55）：提及它们是合法的，不是幻觉。
# `可撑天数` = wf5 真实字段 sellable_days，deepseek 描述补货页时会自然带出。
# 合法提及一律放行；只有当它和"真正编造字段"同框出现（被当成编造字段堆里的一员）
# 才一并点名 —— 此时拦截由 HALLUCINATED_FIELDS 命中触发，别名只是补充说明。
LEGIT_FIELD_ALIASES = {
    "可撑天数": "sellable_days",
}

# 精确时间戳模式（data_health_check 只返回日期粒度，没时分秒）
PRECISE_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})"
)
# UTC 偏移 / 时区换算
TIMEZONE_HINT_RE = re.compile(r"(?:UTC[+-]\d|沙特时间|时区换算|（UTC\+)")

URL_RE = re.compile(r"https?://([a-zA-Z0-9.-]+)(?::\d+)?(/[^\s)\"'>]*)?")

# T45: 选品/库存约束判断必须有工具证据。包含“已查询/已拉取/根据完整数据”
# 这类数据已取到的声明时，也必须有 query_sku 或 list_products(limit>0) 背书。
_SELECTION_INVENTORY_GATE_RE = re.compile(
    r"库存约束|库存反向约束|本期选品|选品.{0,30}库存|库存.{0,8}只.{0,10}约束|"
    r"已查询.{0,20}库存数据|已拉取.{0,20}库存数据|根据.{0,10}完整数据.{0,40}库存"
)

# T38: 完成态假证据 — "已重新计算/跑完了/任务已完成" 等宣称
# sanitize_reply() 仅被 LLM 路径调用；确定性 _workflow_receipt_reply() 在此之前退出，
# 所以任何到达这里的完成声明都是 LLM 自编，无真实回读证据。
_DONE_CLAIM_RE = re.compile(
    r"已重新计算"
    r"|重算.{0,5}(?:完|好|了)"
    r"|跑完了|跑好了"
    r"|(?:销售周期|补货).{0,15}(?:已完成|完成了|跑完)"
    r"|任务.{0,5}已完成"
)


def _check_urls(text: str) -> List[str]:
    """扫文本里所有 URL，返回违规列表"""
    warns = []
    for m in URL_RE.finditer(text):
        host = (m.group(1) or "").lower()
        # 白名单全通过
        if any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            continue
        # 黑名单立即报
        if any(s in host for s in SUSPICIOUS_DOMAINS):
            warns.append(f"⚠️ Agent 编造了不存在的域名: {host}")
            continue
        # 其他外站 URL 加温和提示（可能是合法引用，但默认不信）
        if not (host.endswith(".cn") or host.endswith(".com.cn")):
            warns.append(f"⚠️ Agent 给了一个外部 URL ({host})，请人工确认是否真实")
    return warns


def _check_fake_timestamps(text: str) -> List[str]:
    warns = []
    if PRECISE_TIMESTAMP_RE.search(text):
        warns.append("⚠️ Agent 给了精确到秒的时间戳，但本系统只存日期粒度（YYYY-MM-DD）— 此时间戳可能是编造的")
    if TIMEZONE_HINT_RE.search(text):
        warns.append("⚠️ Agent 在做时区换算，但本系统不返回带时区的时间—可能是编造")
    return warns


def _check_fake_fields(text: str) -> List[str]:
    warns = []
    hits = [f for f in HALLUCINATED_FIELDS if f in text]
    if hits:
        # 只有真正编造字段出现时，才把同框的合法别名一并点名（它被当成编造字段堆里
        # 的一员）。别名单独出现 = 真实字段的人话说法 = 放行（WS-55 修误报）。
        alias_hits = [a for a in LEGIT_FIELD_ALIASES if a in text]
        names = ", ".join(hits + alias_hits)
        warns.append(f"⚠️ Agent 提到的字段不在 wf2/wf5 表中: {names} — 数字可能是编造的")
    return warns


# T36/T38: 任务号提及模式（8位十六进制，大小写均捕获）；T38 扩展连接词"是"/"为"
_TASK_ID_MENTION_RE = re.compile(
    r'任务\s*(?:号|[Ii][Dd]|编号)?[\s:：是为]*([0-9a-fA-F]{8})\b'
)


def _check_fake_task_ids(reply: str, tool_log: list) -> List[str]:
    """T36/T38: reply 中出现未由 run_workflow 工具返回的 task_id → banner。

    只信 tool_log 里 name=="run_workflow" 条目的 task_id；其他工具
    （query_sku、query_order_live 等）返回的 task_id 不能洗白任务号声明。
    大小写归一（T38：38377C42 与 38377c42 视为同一 id）。
    """
    warns: List[str] = []
    # 大小写归一：捕获后全部小写
    mentioned = {m.lower() for m in _TASK_ID_MENTION_RE.findall(reply)}
    if not mentioned:
        return warns
    # T36 防伪关键：只有 run_workflow 工具调用返回的 task_id 才算真实任务号
    real_ids = {
        (t["task_id"] or "").lower() for t in (tool_log or [])
        if t.get("name") == "run_workflow" and t.get("task_id")
    }
    fake = mentioned - real_ids
    if fake:
        warns.append(
            f"⚠️ Agent 回复中出现了未由 run_workflow 工具返回的任务号 "
            f"({', '.join(sorted(fake))}) — 这些 task_id 可能是编造的"
        )
    return warns


# 声明对象 -> 能证明该对象被查询过的工具。不要用"任意数据工具"互相背书。
_CLAIM_TOOL_MAP = {
    "product_sku": frozenset({"list_products", "query_sku"}),
    "order": frozenset({"query_order"}),
    "store_overview": frozenset({"scope_overview", "compute_replenishment", "data_health_check"}),
}

_QUERY_ACTION_RE = r"(?:我|已)?(?:再次|重新)?(?:查了一下|查了|已查|查好了|拉了|已拉|拉好了|看了|看完了)"
_SPECIFIC_PRODUCT_INVENTORY_RE = re.compile(
    r"(?:"
    r"(?:这个|该|这款|这件|某个)?\s*(?:SKU|sku|商品|产品).{0,6}库存"
    r"|库存.{0,6}(?:有|还有|剩余|为|是)\s*\d+\s*件"
    r"|(?:商品|产品)库存.{0,4}(?:都)?正常"
    r"|(?:SKU|sku|商品|产品).{0,20}(?:有|还有|剩余)\s*\d+\s*件.{0,5}库存"
    r"|\d+\s*件\s*(?:库存|现货)"
    r")"
)


def _claim_match(reply: str, keywords: str, gap: int = 15):
    return re.search(
        rf"{_QUERY_ACTION_RE}.{{0,{gap}}}(?P<object>{keywords})",
        reply,
        re.IGNORECASE,
    )


def _normalize_args(args) -> dict:
    """Normalize tool args to dict — GPT provider stores raw JSON string, Anthropic stores dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _limit_is_positive(args: dict) -> bool:
    try:
        return int(args.get("limit", 0)) > 0
    except (TypeError, ValueError):
        return False


def _list_products_has_rows(tool_log) -> bool:
    if not tool_log:
        return False
    for t in tool_log:
        if t.get("name") != "list_products":
            continue
        args = _normalize_args(t.get("args") or {})
        if _limit_is_positive(args):
            return True
    return False


def _has_specific_product_inventory_claim(reply: str) -> bool:
    return bool(_SPECIFIC_PRODUCT_INVENTORY_RE.search(reply))


def _has_claim_evidence(claim_type: str, tools_used: List[str], tool_log=None) -> bool:
    allowed_tools = _CLAIM_TOOL_MAP[claim_type]
    tool_names = set(tools_used or [])
    if tool_log:
        tool_names.update(t.get("name") for t in tool_log if t.get("name"))

    for tool_name in tool_names:
        if tool_name not in allowed_tools:
            continue
        if tool_name == "list_products":
            if _list_products_has_rows(tool_log):
                return True
            continue
        return True
    return False


def _check_fake_query_claims(reply: str, tools_used: List[str], tool_log=None) -> List[str]:
    """检测'声称查了/拉了商品/SKU数据'但无真实工具证据的情况。"""
    warns = []
    claimed_product_sku = _claim_match(
        reply,
        r"商品|产品|SKU|sku|库存|ERP|erp|全部货|所有货",
    )
    claimed_order_query = _claim_match(reply, r"货单|订单|order", gap=10)
    claimed_store_overview = _claim_match(
        reply,
        r"店铺数据|补货数据|数据新鲜度|数据健康|你的数据|数据",
    )
    claimed_specific_inventory = _has_specific_product_inventory_claim(reply)

    product_is_primary_claim = (
        claimed_product_sku
        and (
            not claimed_store_overview
            or claimed_product_sku.start("object") <= claimed_store_overview.start("object")
        )
    )
    product_needs_evidence = product_is_primary_claim or (
        claimed_store_overview and claimed_specific_inventory
    )
    if product_needs_evidence and not _has_claim_evidence("product_sku", tools_used, tool_log):
        warns.append(
            "⚠️ Agent 声称已查询/拉取商品或数据，但没有对应工具调用证据"
            "（list_products with limit>0 或 query_sku 均未调用）— 这是 hallucinate"
        )

    if claimed_order_query and not _has_claim_evidence("order", tools_used, tool_log):
        warns.append(
            "⚠️ Agent 声称已查货单/订单，但没有调用 query_order — 这是 hallucinate"
        )

    if claimed_store_overview:
        broad_pos = claimed_store_overview.start("object")
        has_specific_claim_before_broad = any(
            m and m.start("object") <= broad_pos
            for m in (claimed_product_sku, claimed_order_query)
        )
        # product_sku evidence (query_sku / list_products) also backs a general data claim
        has_product_evidence = (
            claimed_product_sku
            and _has_claim_evidence("product_sku", tools_used, tool_log)
        )
        if (
            not has_specific_claim_before_broad
            and not _has_claim_evidence("store_overview", tools_used, tool_log)
            and not has_product_evidence
        ):
            warns.append(
                "⚠️ Agent 声称已查询/拉取店铺或补货数据，但没有对应工具调用证据"
                "（scope_overview / compute_replenishment / data_health_check 均未调用）— 这是 hallucinate"
            )
    return warns


def _is_substantive_action(tool_log: list) -> bool:
    """list_products(limit=0) 只计数，不算真执行；其他工具调用 = 真执行。"""
    for t in (tool_log or []):
        if t.get("name") != "list_products":
            return True
        args = _normalize_args(t.get("args") or {})
        if _limit_is_positive(args):
            return True
    return False


def _get_tool_arg(t: dict, key: str):
    """从 tool_log 条目安全取 args 里的字段（Anthropic=dict, OpenAI=str）。"""
    args = t.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    return (args.get(key) or "") if isinstance(args, dict) else ""


def _check_inventory_selection_evidence(
    reply: str, tools_used: List[str], tool_log: List
) -> List[str]:
    """T45: 选品/库存约束类回答必须有 list_products(limit>0) 或 query_sku 证据。"""
    if not _SELECTION_INVENTORY_GATE_RE.search(reply):
        return []

    for t in (tool_log or []):
        name = t.get("name", "")
        if name == "query_sku":
            return []
        if name == "list_products":
            limit_raw = _get_tool_arg(t, "limit")
            try:
                limit = int(limit_raw) if limit_raw not in (None, "") else 0
            except (ValueError, TypeError):
                limit = 0
            if limit > 0:
                return []

    return [
        "⚠️ Agent 回答涉及选品/库存约束或声称已查询库存数据，但没有 "
        "list_products(limit>0) 或 query_sku 的工具调用证据 — 无法确认，"
        "叙述查过 ≠ 查过"
    ]


# T03: 销量数字声明的检测模式
# 命中：「近30天销量是65」「销量为662件」「卖了25单」「30d销量：25」等
_STALE_SALES_CLAIM_RE = re.compile(
    r"(?:"
    r"近\d+天.{0,15}销量.{0,10}(?:是|为|有|达|共|约)\s*\d+"
    r"|销量.{0,5}(?:是|为|有|达|约)\s*\d+\s*(?:件|单|个|条)?"
    r"|(?:30|180|90|60|10|120)\s*天.{0,10}(?:卖|售|销)\s*\d+"
    r"|(?:卖了|售出|销售了|共卖)\s*\d+\s*(?:件|单|个)"
    r")",
    re.IGNORECASE,
)


def _check_stale_sales_claim(reply: str, tool_log: list) -> List[str]:
    """T03: query_sku 实时取数失败(live_sales_failed=True)但回复中仍含具体销量数字 → 警告。

    防止 LLM 把无法确认实时来源的销量数字当作确定值呈现给用户。
    """
    stale_skus: list = []
    for t in (tool_log or []):
        if t.get("name") == "query_sku":
            stale = t.get("result_stale_skus") or []
            stale_skus.extend(stale)
    if not stale_skus:
        return []
    if not _STALE_SALES_CLAIM_RE.search(reply):
        return []
    sku_str = "、".join(sorted(set(stale_skus)))
    return [
        f"⚠️ SKU {sku_str} 的实时取数失败，"
        "Agent 回复中仍含具体销量数字 — 可能来自旧快照或幻觉（T03）。"
        "请告知用户当前无法实时确认销量，不得输出旧缓存数字。"
    ]


# T27: 补货实时证据失败后的结论/数字声明检测。
_REPLENISHMENT_NUMERIC_OR_CONCLUSIVE_RE = re.compile(
    r"(?:无需补货|不需要补货|无补货建议|无风险|风险低|低风险|"
    r"待发.{0,8}\d+|在途.{0,8}\d+|Noon.{0,8}\d+|东莞.{0,8}\d+|"
    r"补货.{0,8}\d+|pipeline.{0,12}\d+)",
    re.IGNORECASE,
)


def _check_blocked_replenishment_claim(reply: str, tool_log: list) -> List[str]:
    """T27: query_replenishment_sku blocked but reply still gives numbers/conclusion."""
    blocked_skus: list = []
    for t in (tool_log or []):
        if t.get("name") == "query_replenishment_sku":
            blocked_skus.extend(t.get("result_replenishment_blocked_skus") or [])
    if not blocked_skus:
        return []
    if not _REPLENISHMENT_NUMERIC_OR_CONCLUSIVE_RE.search(reply or ""):
        return []
    sku_str = "、".join(sorted(set(blocked_skus)))
    return [
        f"⚠️ SKU {sku_str} 的补货实时/权威证据不可用，"
        "Agent 回复中仍含补货结论或 pipeline/库存数字 — 可能把缓存 0 当成业务真相（T27）。"
        "请说明实时源失败/缓存不可用，不得输出看似确定的补货数字或无风险结论。"
    ]


# 纯数字问题检测：用户只问 X 是多少/分别是多少
_PURE_NUM_RE = re.compile(r'分别是多少|各.*是多少|是多少\s*$|是多少[？?。，,]')
# 质量/表现评价词（行级匹配，不用 DOTALL 以免吃掉整表）
_QUALITY_JUDGMENT_RE = re.compile(
    r'表现.{0,10}不错|毛利.{0,10}不错|健康.{0,10}不错|正常范围|质量.{0,10}稳定'
    r'|利润.{0,10}不错|表现良好|不错.{0,10}表现|整体.{0,20}表现|非常健康|很健康'
    r'|整体.*表现|表现良好'
)


def sanitize_reply(reply: str, tools_used: List[str], tool_log: Optional[list] = None, question: Optional[str] = None) -> Tuple[str, List[str]]:
    """对 reply 做一遍体检，命中违规给头部加 banner。"""
    warnings: List[str] = []
    if not reply:
        return reply, warnings

    # 纯数字问题质量评价过滤（行级，不用 re.DOTALL 以免吃掉整表）
    if question and _PURE_NUM_RE.search(question):
        cutoff_pat = re.compile(
            r'\n+(?:补充信息|其他信息|额外信息|另外补充(?:几个)?(?:关键信息)?|'
            r'补充几个关键信息)[：:]?'
        )
        m = cutoff_pat.search(reply)
        if m:
            reply = reply[:m.start()]
        if _QUALITY_JUDGMENT_RE.search(reply):
            lines = reply.split('\n')
            lines = [ln for ln in lines if not _QUALITY_JUDGMENT_RE.search(ln)]
            reply = '\n'.join(lines).strip()

    warnings.extend(_check_urls(reply))
    warnings.extend(_check_fake_timestamps(reply))
    warnings.extend(_check_fake_fields(reply))
    warnings.extend(_check_stale_sales_claim(reply, tool_log or []))
    warnings.extend(_check_blocked_replenishment_claim(reply, tool_log or []))
    warnings.extend(_check_fake_task_ids(reply, tool_log or []))
    warnings.extend(_check_inventory_selection_evidence(reply, tools_used, tool_log or []))
    warnings.extend(_check_fake_query_claims(reply, tools_used, tool_log))
    # WS-128: task completion/refresh bypass gate (已完成/已刷新 without run_workflow)
    from ._chat_boundary import check_task_completion_bypass
    warnings.extend(check_task_completion_bypass(reply, tool_log or []))

    # "已为你导出/下载/生成 Excel" 这种宣称 → 检查是否真调了 export_table tool
    promise_export = re.search(r"(已[为给]?你?(?:导出|生成|发送)|下载链接|Excel.*已)", reply)
    if promise_export and "export_table" not in tools_used:
        warnings.append("⚠️ Agent 宣称已导出/生成下载，但没调 export_table 工具 — 这是 hallucinate")

    # "已发到飞书 / 已通知" → 检查是否真调了 notify_via_feishu
    promise_notify = re.search(r"(已发到飞书|已通知|已推送到群|已发给.*同事)", reply)
    if promise_notify and "notify_via_feishu" not in tools_used:
        warnings.append("⚠️ Agent 宣称已通知/发飞书，但没调 notify_via_feishu — 这是 hallucinate")

    # "已触发 / 已启动 / 再次触发 wf*" → 检查是否真调了 run_workflow
    promise_workflow = re.search(
        r"(已触发|已启动|已开始|再次触发|已经在.{0,5}(后台|跑)|"
        r"任务.{0,10}(提交|启动)|已让.{0,5}系统|系统已经在.{0,5}后台|后台跑了)",
        reply,
    )
    if promise_workflow and "run_workflow" not in tools_used:
        warnings.append(
            "⚠️ Agent 宣称已触发/启动工作流，但本轮没真调 run_workflow tool — "
            "这是 hallucinate（实际没创建后台任务，请重发"
            "『帮我扫一下 ERP 物流』之类更明确的指令）"
        )

    # T38: 假任务状态证据 — accepted / SSE 进度 单独出现但无 run_workflow 证据
    # "状态为 accepted" / "任务 accepted" 以及 SSE 推送进度都是假启动的特征词
    fake_task_evidence = re.search(
        r"((?:状态|status)[^。\n!?]{0,12}\baccepted\b"
        r"|任务[^。\n!?]{0,20}\baccepted\b"
        r"|SSE[^。\n!?]{0,20}(?:推送|进度|实时|订阅)"
        r"|前端.{0,10}(?:SSE|订阅).{0,10}(?:进度|推送))",
        reply,
        re.IGNORECASE,
    )
    if fake_task_evidence and "run_workflow" not in tools_used:
        warnings.append(
            "⚠️ Agent 回复含假任务证据（accepted 状态或 SSE 进度），但本轮没真调 run_workflow — "
            "这是 T38 禁止的假任务启动证据"
        )

    # T38: 完成态假证据 — LLM 路径无法读回任务完成状态，任何完成声明均是编造
    done_claim = _DONE_CLAIM_RE.search(reply)
    if done_claim:
        if "run_workflow" not in tools_used:
            warnings.append(
                "⚠️ Agent 宣称重算已完成，但本轮没真调 run_workflow — 假完成证据（T38）"
            )
        else:
            # run_workflow 只创建任务，LLM 没有 task-status readback 工具；
            # 真实完成回执走 _workflow_receipt_reply()，在 sanitize_reply() 之前退出。
            warnings.append(
                "⚠️ Agent 宣称任务已完成，但仅有创建证据（run_workflow），无完成回读 — "
                "假完成声明（T38）"
            )

    # Chat 没有人类可依赖的"稍后自动回来通知/答复"承诺；任务进度只能看任务面板，
    # 或在完成后由用户重新提问。即使本轮真的调了 run_workflow，也不能把异步
    # follow-up 说成 Agent 会主动回来。
    auto_callback_promise = re.search(
        r"((?:跑完|完成后|结束后|任务完成后|处理完)[^。\n!?]{0,18}"
        r"(?:自动)?(?:回来|通知|告诉|答复|回复|回报|继续回答|接续答)"
        r"|自动[^。\n!?]{0,8}(?:回来|通知|告诉|答复|回复|回报)"
        r"|(?:我会|系统会)[^。\n!?]{0,12}(?:回来|通知|告诉|答复|回复|回报))",
        reply,
    )
    if auto_callback_promise:
        warnings.append(
            "⚠️ Agent 承诺任务完成后自动回报/通知/答复，但 chat 不保证主动回调；"
            "应让用户查看任务面板，完成后需要时再重试或重新提问"
        )

    # 新型撒谎模式：用过去时编"任务还在跑、等 ingest 完" 绕开上面的 hook
    pretend_running = re.search(
        r"(之前触发.{0,20}(任务|物流|ingest).{0,20}(没|还在|跑完)|"
        r"等.{0,8}ingest.{0,5}完|"
        r"过.{0,3}\d+.{0,5}分钟.{0,8}(再问|完成)|"
        r"任务.{0,5}还.{0,5}(没|在).{0,5}(跑|完|ingest))",
        reply,
    )
    if pretend_running and "run_workflow" not in tools_used:
        warnings.append(
            "⚠️ Agent 编了'之前触发的任务还在跑/等 X 分钟 ingest 完'但本轮没调"
            " run_workflow，且过去也未必有真在跑的任务。这是用过去时绕开 hook "
            "的撒谎。wf3 陈旧时应该用 query_sku_live / query_order_live 实时查 ERP"
        )

    # 结构性约束（Anthropic Agentic Misalignment 教训：prompt 无效，必须改写 reply）：
    # pretend_running 类撒谎句子直接用 ~~~ 划掉 + 替换成 "[hallucinate 已删]"
    if pretend_running and "run_workflow" not in tools_used:
        # 用正则把整段含撒谎短语的句子（。/换行 之间）换掉
        sentence_pat = re.compile(
            r"[^。\n!?]*("
            r"之前触发.{0,30}(任务|物流|wf3|ingest).{0,30}(没|还在|跑完|完成)"
            r"|等.{0,10}ingest.{0,10}完"
            r"|过.{0,5}\d+.{0,10}分钟.{0,10}(再问|完成|跑完)"
            r"|任务.{0,10}还.{0,10}(没|在).{0,10}(跑|完|ingest)"
            r"|wf3.{0,15}任务.{0,15}(还没|未).{0,8}(跑完|完成)"
            r")[^。\n!?]*[。!?]?",
        )
        reply = sentence_pat.sub(
            "[⚠️ 句子被 _safety 拦掉：未真调 run_workflow，请用 query_sku_live 实时查] ",
            reply,
        )

    # ── T26 货单负控 ──────────────────────────────────────────────────────────────
    # Rule A: 没调 query_order_live 却说"我来查货单实时状态/正在查" — 假称在查，直接删句
    pretend_order_query = re.search(
        r"(我来查这个货单号的实时状态"
        r"|我.{0,6}来.{0,6}查.{0,10}货单.{0,10}实时"
        r"|正在查.{0,10}货单.{0,10}(状态|物流|实时)"
        r"|帮.{0,5}查.{0,15}货单.{0,10}实时"
        r"|查.{0,5}货单.{0,8}实时状态"
        r"|让我.{0,5}查.{0,10}货单)",
        reply,
    )
    if pretend_order_query and "query_order_live" not in tools_used:
        warnings.append(
            "⚠️ Agent 说'我来查货单实时状态/正在查货单'但本轮没真调 query_order_live — "
            "禁止假称在查（T26 货单负控）"
        )
        sentence_pat_order = re.compile(
            r"[^。\n!?]*("
            r"我来查这个货单号的实时状态"
            r"|我.{0,6}来.{0,6}查.{0,10}货单.{0,10}实时"
            r"|正在查.{0,10}货单.{0,10}(状态|物流|实时)"
            r"|帮.{0,5}查.{0,15}货单.{0,10}实时"
            r"|查.{0,5}货单.{0,8}实时状态"
            r"|让我.{0,5}查.{0,10}货单"
            r")[^。\n!?]*[。!?]?",
        )
        reply = sentence_pat_order.sub(
            "[⚠️ 被 _safety 拦掉：未调 query_order_live，不许假称正在查货单] ",
            reply,
        )

    # Rule B: query_order_live 返回 order_not_found_in_erp 时，reply 必须明确说未找到
    order_not_found_entries = [
        t for t in (tool_log or [])
        if t.get("name") == "query_order_live"
        and t.get("result_error") == "order_not_found_in_erp"
    ]
    if order_not_found_entries and not re.search(
        r"(未找到|不存在|无物流|找不到|无记录|没有.{0,5}找到|该货单.{0,10}(不|无)|ERP.*无记录|核实货单号)",
        reply,
    ):
        order_nos = [_get_tool_arg(t, "order_no") for t in order_not_found_entries]
        order_str = "、".join(filter(None, order_nos)) or "该货单"
        not_found_prefix = (
            f"**货单 {order_str} 在 ERP 中无记录**，请核实货单号是否正确。"
            "当前无物流数据。\n\n"
        )
        reply = not_found_prefix + reply
        warnings.append(
            f"⚠️ query_order_live 返回 order_not_found_in_erp（{order_str}）"
            "但回复未明确说明未找到，已自动补充负控提示（T26）"
        )

    # ── T26-ext 物流负控扩展：SKU / 跟踪号 ────────────────────────────────────────
    # Rule C: 没调 query_sku_live 却说"我来查 SKU 物流/在途" — 假称在查，直接删句
    # re.IGNORECASE 覆盖 sku/SKU/Sku 等大小写变体
    pretend_sku_query = re.search(
        r"(我.{0,6}来.{0,6}查.{0,15}SKU.{0,15}(物流|在途|实时|状态)"
        r"|正在查.{0,10}SKU.{0,10}(状态|物流|实时|在途)"
        r"|帮.{0,5}查.{0,15}SKU.{0,10}(物流|在途)"
        r"|让我.{0,5}查.{0,10}SKU)",
        reply,
        re.IGNORECASE,
    )
    if pretend_sku_query and "query_sku_live" not in tools_used:
        warnings.append(
            "⚠️ Agent 说'我来查 SKU 物流/在途'但本轮没真调 query_sku_live — "
            "禁止假称在查（T26-ext SKU 负控）"
        )
        sentence_pat_sku = re.compile(
            r"[^。\n!?]*("
            r"我.{0,6}来.{0,6}查.{0,15}SKU.{0,15}(物流|在途|实时|状态)"
            r"|正在查.{0,10}SKU.{0,10}(状态|物流|实时|在途)"
            r"|帮.{0,5}查.{0,15}SKU.{0,10}(物流|在途)"
            r"|让我.{0,5}查.{0,10}SKU"
            r")[^。\n!?]*[。!?]?",
            re.IGNORECASE,
        )
        reply = sentence_pat_sku.sub(
            "[⚠️ 被 _safety 拦掉：未调 query_sku_live，不许假称正在查 SKU 物流] ",
            reply,
        )

    # Rule D: query_sku_live 返回 sku_no_orders_in_erp 时，reply 必须明确说未找到
    sku_not_found_entries = [
        t for t in (tool_log or [])
        if t.get("name") == "query_sku_live"
        and t.get("result_error") == "sku_no_orders_in_erp"
    ]
    if sku_not_found_entries and not re.search(
        r"(未找到|不存在|无货单|找不到|无记录|没有.{0,5}找到|该SKU.{0,10}(不|无)|ERP.*无记录|无在途|核实.*SKU)",
        reply,
    ):
        sku_nos = [_get_tool_arg(t, "sku") for t in sku_not_found_entries]
        sku_str = "、".join(filter(None, sku_nos)) or "该 SKU"
        sku_not_found_prefix = (
            f"**SKU {sku_str} 在 ERP 中无在途或近期完成货单记录**，请核实 SKU 是否正确。"
            "当前无物流数据。\n\n"
        )
        reply = sku_not_found_prefix + reply
        warnings.append(
            f"⚠️ query_sku_live 返回 sku_no_orders_in_erp（{sku_str}）"
            "但回复未明确说明未找到，已自动补充负控提示（T26-ext SKU）"
        )

    # Rule E: 没调任何物流查询工具却说"我来查跟踪号" — 假称在查，直接删句
    # re.IGNORECASE 覆盖 tracking/TRACKING/Tracking 等大小写变体（同 Rule C 做法）
    pretend_tracking_query = re.search(
        r"(我.{0,6}来.{0,6}查.{0,15}跟踪.{0,5}(号|状态|物流)"
        r"|正在查.{0,10}跟踪.{0,5}(号|状态|物流)"
        r"|帮.{0,5}查.{0,15}跟踪号"
        r"|让我.{0,5}查.{0,10}跟踪号"
        r"|我来查.{0,10}tracking"
        r"|正在查.{0,10}tracking)",
        reply,
        re.IGNORECASE,
    )
    if pretend_tracking_query and "query_order_live" not in tools_used and "query_sku_live" not in tools_used:
        warnings.append(
            "⚠️ Agent 说'我来查跟踪号物流'但本轮没真调 query_order_live 或 query_sku_live — "
            "禁止假称在查跟踪号（T26-ext 跟踪号负控）"
        )
        sentence_pat_tracking = re.compile(
            r"[^。\n!?]*("
            r"我.{0,6}来.{0,6}查.{0,15}跟踪.{0,5}(号|状态|物流)"
            r"|正在查.{0,10}跟踪.{0,5}(号|状态|物流)"
            r"|帮.{0,5}查.{0,15}跟踪号"
            r"|让我.{0,5}查.{0,10}跟踪号"
            r"|我来查.{0,10}tracking"
            r"|正在查.{0,10}tracking"
            r")[^。\n!?]*[。!?]?",
            re.IGNORECASE,
        )
        reply = sentence_pat_tracking.sub(
            "[⚠️ 被 _safety 拦掉：未调物流查询工具，不许假称正在查跟踪号] ",
            reply,
        )

    if warnings:
        banner = (
            "⚠️ **系统检测到 Agent 回复中可能存在不准确之处**：\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n\n以下是原始回复（已标记可疑部分）：\n\n---\n\n"
        )
        return banner + reply, warnings
    return reply, warnings

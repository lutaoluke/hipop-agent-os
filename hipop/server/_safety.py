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

_STALE_REPLY_RE = re.compile(
    r"陈旧|过期|不新鲜|滞后|偏旧|较旧|"
    r"旧(?:的)?\s*(?:数据|口径|快照|销量|库存)|"
    r"(?:数据|销量|库存|口径|快照|同步|noon).{0,15}旧|"
    r"\bstale\b",
    re.IGNORECASE,
)

# WS-133 Round-3: 物流编造声明检测器
# Rule B/D（货单/SKU 未找到——确定性结论）：捡拾在途声明 AND 否定旁路借口
# "只是延迟/同步慢" 在 not-found 语境下是隐含"货单其实存在"的旁路声明。
_NOT_FOUND_FABRICATION_RE = re.compile(
    r"当前.{0,3}在途|已在途|目前在途"    # 正向在途声明
    r"|在途\s*\d+\s*件?"                # 具体在途数量
    r"|预计.{0,8}(?:到仓|到货|到达)"    # 预计到货
    r"|货代.{0,3}为\s*\S+"              # 货代已分配
    r"|只是.{0,8}(?:延迟|慢|同步)"      # 旁路借口（暗示货单存在只是延迟）
    r"|当前.{0,5}(?:没有|无).{0,3}在途" # 编造"无在途"结论
    r"|没有在途库存"
)

# Rule F2/H/F（查询出错——结果不确定）：同上但不含旁路借口
# 查询失败时说"只是网络延迟，请重试"是合理的，不应触发警告。
_ERROR_FABRICATION_RE = re.compile(
    r"当前.{0,3}在途|已在途|目前在途"
    r"|在途\s*\d+\s*件?"
    r"|预计.{0,8}(?:到仓|到货|到达)"
    r"|货代.{0,3}(?:为|是)\s*\S+"
    r"|跟踪号.{0,3}(?:是|为)\s*\S+"
    r"|当前.{0,5}(?:没有|无).{0,3}在途"
    r"|没有在途库存"
    r"|没有在途"
    r"|(?:状态|库存|一切|都).{0,4}正常"          # 状态/库存/一切正常 等积极结论
    r"|预计.{0,8}(?:到仓|到货|到达|送达|送到|签收|抵达)"  # ETA 同义扩展
    r"|已\s*(?:发货|发出|揽收|签收|出库|发运|送达)"        # 已发货/已签收 等完成态
)

# ── WS-133 Round-5：结构判别（取代逐句加词的黑名单）─────────────────────────────
# 教训（WS-55/WS-128 + 本 PR 前 4 轮）：对自由中文逐句枚举"编造措辞"永不收敛——
# 红队每轮换同义词就能绕（货代为→货代是→物流商是→走的是…）。
# 改为按"失败的查询不可能产生的确定结果"的【形状 + 闭集实体】判别：
#   1) id 形 token（跟踪号/别的单号）且不等于被查询 id —— 失败查询拿不到新 id；
#   2) 承运商闭集命中 —— 货代是有限现实实体，枚举=领域建模，非穷举措辞；
#   3) 具体数量（N 件 / 在途 N）；
#   4) 既有语义断言短语（在途/ETA/状态正常…，见 _ERROR_FABRICATION_RE）。

# 真实承运商/货代闭集（有限现实域实体）
_CARRIERS_CN = ["顺丰", "圆通", "中通", "申通", "韵达", "百世", "极兔",
                "德邦", "京东", "邮政", "菜鸟", "跨越", "宅急送"]
_CARRIERS_EN = ["EMS", "YTO", "STO", "ZTO", "YUNDA", "DHL", "UPS",
                "FEDEX", "TNT", "ARAMEX", "SMSA", "DPD", "GLS"]
_CARRIER_RE = re.compile(
    "|".join(re.escape(c) for c in _CARRIERS_CN)
    + "|" + "|".join(rf"\b{re.escape(c)}\b" for c in _CARRIERS_EN),
    re.IGNORECASE,
)
# id 形 token：≥2 字母后接 ≥3 数字（YT123456 / SF123 / YT888），或 ≥8 位纯数字（长跟踪号）
_ID_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,}\d{3,}[A-Za-z0-9]*\b|\b\d{8,}\b")
# 具体数量声明（物流/库存语境）
_QTY_RESULT_RE = re.compile(
    r"\d+\s*(?:件|个|箱|pcs|现货)"
    r"|(?:在途|在库|现货|库存)\s*\d+",
    re.IGNORECASE,
)

# Rule I: workflow failure false-success claims.
# Keep the broad "already triggered/started/submitted" claims, but scope
# "processing" language so honest failure text such as "不会继续处理" or
# "请到后台处理" does not look like a backend-success promise.
_WF_FALSE_SUCCESS_CLAIM_RE = re.compile(
    r"(已触发|已启动|已开始|任务已.{0,5}(创建|提交|启动|运行)|"
    r"已为.{0,5}(你|您).{0,5}触发|系统已经在.{0,5}后台|后台.{0,5}(跑|运行)了|"
    r"(?:系统|后台)(?![^。；\n!?]{0,6}(?:不|不会|不能|无法|未|没)).{0,10}(会|将).{0,10}(继续|处理)|"
    r"(?:系统|后台).{0,10}(?:自动|正在).{0,5}处理|已安排.{0,10}处理|"
    r"会.{0,4}继续.{0,4}(?:帮你)?处理|将继续.{0,5}处理)"
)
_WF_NON_SUCCESS_PROCESSING_RE = re.compile(
    r"(?:"
    r"(?:不会|不能|无法|未|没|不再).{0,4}继续.{0,4}处理"
    r"|不\s*继续.{0,4}处理"
    r"|请.{0,4}到后台.{0,4}处理"
    r"|到后台.{0,4}处理"
    r"|(?:人工|手动).{0,4}处理"
    r")"
)


def _workflow_failure_claims_success(reply: str) -> bool:
    """Return True when a failed workflow reply still promises backend success."""
    for part in re.split(r"[。；\n!?！？]", reply or ""):
        if not _WF_FALSE_SUCCESS_CLAIM_RE.search(part):
            continue
        scrubbed = _WF_NON_SUCCESS_PROCESSING_RE.sub("", part)
        if _WF_FALSE_SUCCESS_CLAIM_RE.search(scrubbed):
            return True
    return False


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


# T36-S3: 检测工作流失败时 reply 是否未说明失败原因
def _extract_workflow_name(args) -> str:
    """从 run_workflow args 安全取 workflow 名（dict 或 JSON string 两种形式）。"""
    if isinstance(args, dict):
        return args.get("workflow", "unknown")
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed.get("workflow", "unknown")
        except Exception:
            pass
    return "unknown"


def _extract_failed_workflows(tool_log: list) -> List[tuple]:
    """从 tool_log 中收集所有 run_workflow 失败条目，返回 [(wf_name, error_msg), ...]。"""
    failed = []
    for t in (tool_log or []):
        if t.get("name") != "run_workflow":
            continue
        ok = t.get("ok")
        has_error = bool(t.get("error"))
        if ok is False or (ok is None and has_error):
            wf_name = _extract_workflow_name(t.get("args") or {})
            error_msg = t.get("error") or "未知错误"
            failed.append((wf_name, error_msg))
    return failed


_WORKFLOW_RESULT_LABELS = {
    "wf2_products_v2": "商品库刷新",
    "wf2_sales_v2": "销量价格刷新",
    "wf2_sales_refresh_v2": "销量刷新",
    "wf1_stock_v2": "库存刷新",
    "wf3_logistics_v2": "物流刷新",
    "wf5_sales_cycle_v2": "销售周期刷新",
    "wf6_alerts_v2": "告警刷新",
    "refresh_all_v2": "全量刷新",
}


def _workflow_result_label(workflow: str) -> str:
    return _WORKFLOW_RESULT_LABELS.get(workflow or "", workflow or "未知刷新")


def _extract_workflow_results(tool_log: list) -> List[dict]:
    """Collect run_workflow results in display order for mixed-result receipts."""
    results: List[dict] = []
    for t in (tool_log or []):
        if t.get("name") != "run_workflow":
            continue
        wf_name = _extract_workflow_name(t.get("args") or {})
        ok_val = t.get("ok")
        error_msg = t.get("error") or t.get("result_error")
        ok = bool(ok_val) if ok_val is not None else not bool(error_msg)
        results.append({
            "workflow": wf_name,
            "label": _workflow_result_label(wf_name),
            "ok": ok,
            "task_id": t.get("task_id"),
            "error": error_msg or "未知错误",
        })
    return results


def _rewrite_mixed_workflow_result(reply: str, tool_log: list) -> str:
    """Rewrite mixed run_workflow outcomes into the required human receipt.

    When one refresh starts and another fails to start, keeping the model's
    original prose is dangerous because it often says the failed item also
    started. This receipt is built only from tool_log facts.
    """
    results = _extract_workflow_results(tool_log)
    if not results:
        return reply
    has_success = any(r["ok"] for r in results)
    has_failure = any(not r["ok"] for r in results)
    if not (has_success and has_failure):
        return reply

    headline = "，".join(
        f"{r['label']}{'成功' if r['ok'] else '失败'}"
        for r in results
    )
    lines = [f"**刷新结果**：{headline}。", "", "**详情**："]
    for r in results:
        if r["ok"]:
            task_text = f"任务号 {r['task_id']}" if r.get("task_id") else "已创建后台任务"
            lines.append(f"- {r['label']}成功：{task_text}（{r['workflow']}）。")
        else:
            lines.append(f"- {r['label']}失败：{r['error']}（{r['workflow']}）。")
    lines.append("")
    lines.append("请点击下方对应项查看详情。")
    return "\n".join(lines)


def _reply_names_workflow(reply: str, wf_name: str) -> bool:
    """reply 是否点名了该 workflow（技术名称必须出现）。

    验收口径："哪条失败+原因" — reply 必须包含 workflow 技术标识符
    （如 wf2_sales_v2 / wf2_products_v2），仅说"有一个工作流失败了"或
    "销量价格刷新失败"均不满足要求（无法唯一定位失败来源）。
    """
    if not wf_name or wf_name == "unknown":
        return False
    return wf_name in reply


# 成功声明模式（同行内）
_SUCCESS_CLAIM_RE = re.compile(
    r'(?:成功|✅|✓|√|已启动|已开始|started successfully|已完成|已触发|启动成功)'
)
# 失败声明模式（覆盖失败表述，用于排除包含明确失败语言的行）
_FAILURE_CLAIM_RE = re.compile(
    r'(?:失败|error|failed|错误|启动失败|failure)',
    re.IGNORECASE,
)


def _workflow_claimed_succeeded(reply: str, wf_name: str) -> bool:
    """reply 中是否把 wf_name 标成了"成功"（当工作流实际失败时的假成功声明）。

    按行扫描：找到包含 wf_name 的行，若同行有成功语言且无失败语言覆盖，视为假声明。
    这样能准确捕获 Markdown 表格行（| wf2_sales_v2 | 成功 |）和内联句子，
    同时不误伤正确写法（ERP 商品库刷新成功…, wf2_sales_v2 启动失败…）。
    """
    if not wf_name or wf_name == "unknown":
        return False
    for line in reply.split('\n'):
        if wf_name not in line:
            continue
        # 同行有明确失败语言 → 不算假成功声明（模型如实描述了失败）
        if _FAILURE_CLAIM_RE.search(line):
            continue
        # 同行有成功语言且无失败语言覆盖 → 假声明
        if _SUCCESS_CLAIM_RE.search(line):
            return True
    return False


def _check_failed_workflow_claimed_success(reply: str, tool_log: list) -> List[str]:
    """T36-S3: run_workflow 返回 ok=False 时检测两种假成功声明。

    覆盖两种 provider 形状：
    - Anthropic: tool_log[i].args 是 dict
    - OpenAI-compat (deepseek/qwen/doubao): tool_log[i].args 是 JSON string

    两种需要 banner 的情况：
    1. reply 未点名失败 workflow（原有逻辑）
    2. reply 点名了失败 workflow 但仍声称其"成功/已启动"（红队新增）
    """
    warns: List[str] = []
    failed = _extract_failed_workflows(tool_log)

    if not failed:
        return warns

    for wf_name, error_msg in failed:
        if not _reply_names_workflow(reply, wf_name):
            warns.append(
                f"⚠️ 工作流 {wf_name} 实际启动失败（{error_msg}），"
                f"但 Agent 回复未点名该工作流 — 请明确告知用户 {wf_name} 失败及原因"
            )
        elif _workflow_claimed_succeeded(reply, wf_name):
            warns.append(
                f"⚠️ 工作流 {wf_name} 实际启动失败（{error_msg}），"
                f"但 Agent 回复声称 {wf_name} 已成功/已启动 — 请明确告知用户失败及原因"
            )
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


def _has_fabricated_result(reply: str, failed_entries: list) -> bool:
    """实时查询/登录失败且无数据时，回复出现「失败查询不可能产生」的确定结果即判编造。

    结构判别（非穷举措辞），见 _CARRIER_RE / _ID_TOKEN_RE / _QTY_RESULT_RE 注释。
    被查询 id 本身在失败说明里复述是合法的（"货单 PD-X 查询失败"），需排除。
    """
    if _ERROR_FABRICATION_RE.search(reply) or _CARRIER_RE.search(reply) \
            or _QTY_RESULT_RE.search(reply):
        return True
    # 被查询 id 集合（order_no / sku）——合法复述，从 id 形检测里剔除
    qids = set()
    for t in (failed_entries or []):
        for k in ("order_no", "sku"):
            v = _get_tool_arg(t, k)
            if v:
                qids.add(str(v).strip().upper())
    for m in _ID_TOKEN_RE.finditer(reply):
        tok = m.group(0).upper()
        if any(tok == q or tok in q or q in tok for q in qids):
            continue  # 复述被查询 id，放行
        return True  # 出现新的 id 形 token = 失败查询编造出的结果
    return False


def _stale_query_skus(tool_log: list) -> List[str]:
    skus: List[str] = []
    for t in (tool_log or []):
        if t.get("name") != "query_sku":
            continue
        stale = t.get("result_stale_skus") or []
        if isinstance(stale, str):
            stale = [stale]
        for sku in stale:
            sku_s = str(sku or "").strip()
            if sku_s and sku_s not in skus:
                skus.append(sku_s)
    return skus


def _ensure_stale_query_sku_warning(reply: str, tool_log: list) -> Tuple[str, List[str]]:
    """query_sku 返回旧快照时，LLM 漏警示也要让用户看见。"""
    stale_skus = _stale_query_skus(tool_log)
    if not stale_skus or _STALE_REPLY_RE.search(reply or ""):
        return reply, []

    sku_str = "、".join(stale_skus)
    prefix = (
        f"**数据过期提醒**：SKU {sku_str} 的销量/订单快照是旧数据，"
        "本次查询返回的数值已被隐藏；请刷新或上传最新 noon CSV 后再确认。\n\n"
    )
    return prefix + reply, [
        f"⚠️ query_sku 返回 data_stale=True（{sku_str}），但回复未明确说明数据过期/旧快照，已自动补充提示（T04）"
    ]


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


# WS-121: 运营事实证据门。只在"回答已经给出排名/状态/数字事实"时拦；
# 结构化 unavailable / workflow 接管文案不含事实，放行。
_OPS_UNAVAILABLE_RE = re.compile(
    r"无法|不能|不可用|暂无|未返回|未提供|缺数|数据不足|暂未覆盖|未覆盖|"
    r"请先刷新|请刷新|请补齐|请上传|查询失败|触发更新|已触发更新"
)
_OPS_SKU_TOKEN = r"\b[A-Z]{2,}[A-Z0-9-]{3,}\b"
_OPS_SKU_NUM_RE = re.compile(
    rf"{_OPS_SKU_TOKEN}.{{0,80}}\d[\d,，]*\s*(?:件|单|个|次|pcs)?",
    re.IGNORECASE | re.DOTALL,
)
_OPS_RANK_RE = re.compile(
    r"(?:^|\n|\|)\s*(?:\d+[\.\、)]|第\s*[一二三四五六七八九十\d]+|排名|Top|TOP|top|前\s*\d)",
    re.IGNORECASE,
)
_WINDOW_TOKEN_RE = re.compile(
    r"今天|今日|最新|本周|这周|近\s*[0-9一二两三四五六七八九十百]+\s*[天日]|"
    r"最近\s*[0-9一二两三四五六七八九十百]+\s*[天日]|"
    r"过去\s*[0-9一二两三四五六七八九十百]+\s*[天日]|"
    r"过往\s*[0-9一二两三四五六七八九十百]+\s*[天日]|"
    r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}"
)
_SALES_TOPN_CONTEXT_RE = re.compile(
    r"(?:销量|销售|卖|热销|畅销).{0,40}(?:最高|最多|排行|排名|榜|Top|TOP|top|前\s*\d|最好卖|卖得最好)"
    r"|(?:最高|最多|排行|排名|榜|Top|TOP|top|前\s*\d).{0,40}(?:销量|销售|卖|热销|畅销)",
    re.IGNORECASE | re.DOTALL,
)
_STOCK_RANK_CONTEXT_RE = re.compile(
    r"(?:库存|可售|现货|在库|缺货|断货|积压).{0,40}(?:最高|最多|最大|最低|最少|排行|排名|榜|Top|TOP|top|前\s*\d)"
    r"|(?:最高|最多|最大|最低|最少|排行|排名|榜|Top|TOP|top|前\s*\d).{0,40}(?:库存|可售|现货|在库|缺货|断货|积压)",
    re.IGNORECASE | re.DOTALL,
)
_STOCK_FACT_RE = re.compile(
    rf"{_OPS_SKU_TOKEN}.{{0,80}}(?:总库存|库存|可售|现货|在库|缺货|断货|积压).{{0,30}}\d[\d,，]*"
    rf"|{_OPS_SKU_TOKEN}.{{0,80}}\d[\d,，]*\s*(?:件|个).{{0,30}}(?:库存|可售|现货|在库)",
    re.IGNORECASE | re.DOTALL,
)
_LOGISTICS_CONTEXT_RE = re.compile(
    r"(?:物流|在途|货单|卡单|滞留|发货|跟踪).{0,40}(?:状态|排行|排名|榜|最多|最高|多少|承运|货代|预计|到仓|到货|送达)"
    r"|(?:状态|排行|排名|榜|最多|最高|多少|承运|货代|预计|到仓|到货|送达).{0,40}(?:物流|在途|货单|卡单|滞留|发货|跟踪)",
    re.IGNORECASE | re.DOTALL,
)
_LOGISTICS_POSITIVE_FACT_RE = re.compile(
    r"当前.{0,8}在途|目前.{0,8}在途|已在途|在途\s*\d+\s*(?:件|个|箱|pcs)?|"
    r"已\s*(?:发货|发出|揽收|签收|出库|发运|送达|到仓|到货)|"
    r"运输中|清关|派送中|状态.{0,8}(?:正常|在途|运输|清关|签收|发货)|"
    r"承运商.{0,8}(?:为|是)|货代.{0,8}(?:为|是)|物流商.{0,8}(?:为|是)|"
    r"跟踪号.{0,8}(?:为|是)|运单号.{0,8}(?:为|是)|预计.{0,12}(?:到仓|到货|送达|签收|抵达)|ETA",
    re.IGNORECASE,
)


def _tool_names(tools_used: List[str], tool_log: Optional[list]) -> set:
    names = set(t for t in (tools_used or []) if t)
    for entry in (tool_log or []):
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            names.add(name)
    return names


def _non_unavailable_parts(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"[。；\n!?！？]+", text or "") if p.strip()]
    return [p for p in parts if not _OPS_UNAVAILABLE_RE.search(p)]


def _has_ranked_numeric_fact(reply: str) -> bool:
    if not _OPS_SKU_NUM_RE.search(reply or ""):
        return False
    return bool(_OPS_RANK_RE.search(reply or "") or _SALES_TOPN_CONTEXT_RE.search(reply or ""))


def _has_stock_rank_fact(reply: str) -> bool:
    text = "\n".join(_non_unavailable_parts(reply))
    return bool(text and _STOCK_FACT_RE.search(text) and (_OPS_RANK_RE.search(text) or _STOCK_RANK_CONTEXT_RE.search(text)))


def _has_logistics_fact(reply: str) -> bool:
    for part in _non_unavailable_parts(reply):
        if _LOGISTICS_POSITIVE_FACT_RE.search(part) or _CARRIER_RE.search(part) or _QTY_RESULT_RE.search(part):
            return True
    return False


def _check_operational_fact_evidence(
    reply: str, tools_used: List[str], tool_log: Optional[list], question: Optional[str]
) -> List[str]:
    """WS-121: sales-window/stock/logistics operational facts need matching evidence."""
    warnings: List[str] = []
    q = question or ""
    text = "\n".join([q, reply or ""])
    names = _tool_names(tools_used, tool_log)

    sales_window_topn = (
        _WINDOW_TOKEN_RE.search(text)
        and _SALES_TOPN_CONTEXT_RE.search(text)
        and _has_ranked_numeric_fact(reply or "")
    )
    if sales_window_topn and "top_sales_by_window" not in names:
        warnings.append(
            "⚠️ WS-121: 指定日期窗口销量 TopN 回复含 SKU 排名/销量数字，"
            "但本轮没有 top_sales_by_window 工具证据。不能用 export/list_products/"
            "query_sku 或模型散文冒充窗口 TopN；应返回 unavailable 或先触发/等待合法刷新。"
        )

    stock_ranking = (
        _STOCK_RANK_CONTEXT_RE.search(text)
        and _has_stock_rank_fact(reply or "")
    )
    if stock_ranking and "total_stock_topn" not in names:
        warnings.append(
            "⚠️ WS-121: 库存排序/排名回复含 SKU 库存数字，"
            "但本轮没有 total_stock_topn 工具证据。不能编库存排名；"
            "应返回 unavailable 或先走库存刷新/确定性查询。"
        )

    logistics_status = (
        _LOGISTICS_CONTEXT_RE.search(text)
        and _has_logistics_fact(reply or "")
    )
    live_logistics_tools = {"query_order", "query_order_live", "query_sku_live"}
    if logistics_status and not (names & live_logistics_tools):
        warnings.append(
            "⚠️ WS-121: 物流排序/状态回复含在途、承运商、运单或 ETA 等事实，"
            "但本轮没有 query_order/query_order_live/query_sku_live 的工具证据。"
            "不能编物流状态或假排名；应返回 unavailable 或先触发/等待合法刷新。"
        )

    return warnings


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
    r'表现.{0,30}不错|毛利.{0,30}不错|健康.{0,30}不错|正常范围|质量.{0,30}稳定'
    r'|利润.{0,30}不错|表现良好|不错.{0,30}表现|整体.{0,30}表现|非常健康|很健康'
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

    reply = _rewrite_mixed_workflow_result(reply, tool_log or [])

    warnings.extend(_check_urls(reply))
    warnings.extend(_check_fake_timestamps(reply))
    warnings.extend(_check_fake_fields(reply))
    warnings.extend(_check_stale_sales_claim(reply, tool_log or []))
    warnings.extend(_check_blocked_replenishment_claim(reply, tool_log or []))
    warnings.extend(_check_fake_task_ids(reply, tool_log or []))
    warnings.extend(_check_failed_workflow_claimed_success(reply, tool_log or []))
    reply, stale_query_warnings = _ensure_stale_query_sku_warning(reply, tool_log or [])
    warnings.extend(stale_query_warnings)
    warnings.extend(_check_inventory_selection_evidence(reply, tools_used, tool_log or []))
    warnings.extend(_check_operational_fact_evidence(reply, tools_used, tool_log or [], question))
    warnings.extend(_check_fake_query_claims(reply, tools_used, tool_log))
    # WS-128: two-phase task completion/refresh bypass gate
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

    # T38: 假任务状态证据 — accepted / SSE 进度 单独出现但无 run_workflow 证据（仅告警；正文的
    # 结构化删除交给文末 WS-146 执行声明承重墙 _exec_slot_contract，统一在 hallu 告警生成后做）。
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
    if order_not_found_entries and (
        # 有正向编造声明（含旁路借口）→ 不论回复是否同时说了"未找到"都要拦截
        _NOT_FOUND_FABRICATION_RE.search(reply)
        or not re.search(
            # (?<!不是) 防止"不是不存在"绕过；其他否定否定模式已由上方
            # _NOT_FOUND_FABRICATION_RE 中的正向声明检测兜底。
            r"(未找到|(?<!不是)不存在|无物流|找不到|无记录|没有.{0,5}找到|ERP.*无记录|核实货单号)",
            reply,
        )
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
    if sku_not_found_entries and (
        _NOT_FOUND_FABRICATION_RE.search(reply)
        or not re.search(
            r"(未找到|(?<!不是)不存在|无货单|找不到|无记录|没有.{0,5}找到|ERP.*无记录|无在途|核实.*SKU)",
            reply,
        )
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

    # ── WS-133 全局禁编门：实时源失败 / 工作流失败 必须明说原因 ──────────────────────

    # Rule F: query_order_live 返回 erp_login_failed_no_cache（ERP 登录失败，单货单无缓存）
    # reply 必须说明 ERP 失败和无缓存；否则用户以为可以稍等，实际上根本没数据。
    order_erp_login_failed = [
        t for t in (tool_log or [])
        if t.get("name") == "query_order_live"
        and t.get("result_error") == "erp_login_failed_no_cache"
    ]
    if order_erp_login_failed and (
        # 即使回复开头已说明 ERP 登录失败/无缓存，后缀只要再出现「失败查询不可能产生」
        # 的确定结果（id 形跟踪号 / 承运商 / 数量 / 在途·ETA·状态正常…）仍须告警 —
        # Round-5：结构判别（_has_fabricated_result），取代逐句加词的黑名单（洞F 收口）。
        _has_fabricated_result(reply, order_erp_login_failed)
        or not re.search(
            r"(ERP.{0,10}(失败|登录|无法)|实时.{0,5}失败|登录.{0,5}失败|查询失败|无缓存|没有缓存|暂时无法)",
            reply,
        )
    ):
        order_nos = [_get_tool_arg(t, "order_no") for t in order_erp_login_failed]
        order_str = "、".join(filter(None, order_nos)) or "该货单"
        erp_fail_prefix = (
            f"**ERP 实时查询失败（{order_str}）**：ERP 登录失败，单货单查询无缓存兜底。"
            "请稍后重试，或改用 query_sku_live 查 SKU 级缓存。\n\n"
        )
        reply = erp_fail_prefix + reply
        warnings.append(
            f"⚠️ query_order_live({order_str}) 返回 erp_login_failed_no_cache，"
            "回复未说明 ERP 失败和无缓存，已自动补充（WS-133 Rule F）"
        )

    # Rule F2: query_order_live 返回 order_lookup_unavailable_no_erp_credentials
    # （ERP 账号未配置，与 erp_login_failed 不同）— reply 不得编造货单在途状态。
    order_no_credentials = [
        t for t in (tool_log or [])
        if t.get("name") == "query_order_live"
        and t.get("result_error") == "order_lookup_unavailable_no_erp_credentials"
    ]
    if order_no_credentials and (
        # 即使回复开头已说明 ERP 未配置，后缀出现编造结果仍须拦截（Round-5 结构判别）
        _has_fabricated_result(reply, order_no_credentials)
        or not re.search(
            r"(ERP.{0,10}(未配置|无配置|没配置|账号|凭据|credentials)|配置.{0,5}dbuyerp"
            r"|无法确认|ERP.{0,5}(失败|不可用)|账号未配|暂无凭据)",
            reply,
        )
    ):
        order_nos2 = [_get_tool_arg(t, "order_no") for t in order_no_credentials]
        order_str2 = "、".join(filter(None, order_nos2)) or "该货单"
        no_cred_prefix = (
            f"**无法查询货单 {order_str2}**：本店铺 ERP 账号未配置，"
            "无法确认该货单是否存在。请先配置 dbuyerp 后重试。\n\n"
        )
        reply = no_cred_prefix + reply
        warnings.append(
            f"⚠️ query_order_live({order_str2}) 返回 order_lookup_unavailable_no_erp_credentials，"
            "回复未说明 ERP 账号未配置，已自动补充（WS-133 Rule F2）"
        )

    # Rule G: query_sku_live 返回实时失败 + 回退缓存（result_keys 含 live_query_failed_reason）
    # reply 必须告知用户数据来自 wf3 缓存，不是实时；否则用户误以为是实时数据。
    sku_live_failed_cache = [
        t for t in (tool_log or [])
        if t.get("name") == "query_sku_live"
        and "live_query_failed_reason" in (t.get("result_keys") or [])
    ]
    if sku_live_failed_cache and not re.search(
        r"(ERP.{0,10}失败|实时.{0,10}失败|缓存.{0,5}数据|wf3.{0,5}缓存|非实时|来自.{0,5}缓存"
        r"|登录.{0,5}失败|实时拉失败|缓存版本)",
        reply,
    ):
        sku_strs = [_get_tool_arg(t, "sku") for t in sku_live_failed_cache]
        sku_str = "、".join(filter(None, sku_strs)) or "该 SKU"
        live_fail_prefix = (
            f"**⚠️ {sku_str} ERP 实时查询失败，以下数据来自 wf3 缓存（非实时）**："
            "ERP 登录失败，已回退到缓存数据。若需准确实时数据请稍后重试。\n\n"
        )
        reply = live_fail_prefix + reply
        warnings.append(
            f"⚠️ query_sku_live({sku_str}) ERP 实时失败（live_query_failed_reason），"
            "回复未标注为缓存数据，已自动补充（WS-133 Rule G）"
        )

    # Rule H: query_sku_live 或 query_order_live 返回 erp_fetch_error（网络/接口异常）
    # reply 必须说明查询失败；否则 Agent 可能用旧数据或编默认值。
    erp_fetch_errors = [
        t for t in (tool_log or [])
        if t.get("name") in ("query_sku_live", "query_order_live")
        and (t.get("result_error") or "").startswith("erp_fetch_error")
    ]
    if erp_fetch_errors and (
        # 即使回复已说明查询失败，后缀出现编造结果仍须拦截（Round-5 结构判别）
        _has_fabricated_result(reply, erp_fetch_errors)
        or not re.search(
            r"(查询失败|ERP.{0,5}(失败|异常)|实时.{0,5}失败|获取失败|抓取失败|接口.{0,5}失败"
            r"|网络.{0,5}(错误|异常)|请求失败|暂时无法.{0,5}(查|获取)|无法查询)",
            reply,
        )
    ):
        items = [
            _get_tool_arg(t, "order_no") or _get_tool_arg(t, "sku")
            for t in erp_fetch_errors
        ]
        item_str = "、".join(filter(None, items)) or "该查询"
        fetch_fail_prefix = (
            f"**ERP 查询异常（{item_str}）**：实时接口请求失败，当前无法获取数据。"
            "请稍后重试。\n\n"
        )
        reply = fetch_fail_prefix + reply
        warnings.append(
            f"⚠️ ERP 查询异常 erp_fetch_error（{item_str}），"
            "回复未说明失败原因，已自动补充（WS-133 Rule H）"
        )

    # Rule I: run_workflow 被调用但返回 ok=False（工作流创建失败），
    # reply 不得宣称"已触发/任务已创建/已启动" — 这是假成功声明。
    # 注意：仅在 run_workflow 有 result_error 时才检查（ok=True 时 result_error=None）。
    wf_failed_entries = [
        t for t in (tool_log or [])
        if t.get("name") == "run_workflow"
        and t.get("result_error")  # ok=False → result dict 有 "error" key
    ]
    # 不设"已说失败"豁免——"虽然刚才失败，但现在已触发"类混淆句 既提失败又宣称成功，
    # 属于更严重的编造；合法的失败回复（"触发失败，请重试"）不含成功声明词，不匹配此检查。
    if wf_failed_entries and _workflow_failure_claims_success(reply):
        wf_names = [_get_tool_arg(t, "workflow") for t in wf_failed_entries]
        wf_str = "、".join(filter(None, wf_names)) or "工作流"
        warnings.append(
            f"⚠️ run_workflow({wf_str}) 返回失败（{wf_failed_entries[0].get('result_error', '')[:60]}），"
            "但回复宣称已触发/已启动 — 这是假成功声明（WS-133 Rule I）"
        )

    # WS-161 B-2 禁编承重墙（结构判别，承重墙层）：放在所有 B-1 规则之后，
    # 让上面各规则先按既有口径告警/补前缀（不破坏 WS-133 等回归），再由本层做 B-1 做不到的事——
    # 把答案正文里"工具没返回过"的承运商/运单号/库存数量/状态（包含关系判别）就地删掉，
    # 并对 B-1 未覆盖的失败模式（库存 fail_closed / 空返回）补确定性错误模板。
    # WS-146 执行声明假活承重墙（结构判别，取代逐句正则的 promise_workflow 黑名单）：
    # 「已启动/已刷新/已开始重算/任务号/accepted/SSE 进度」等**执行声明槽**，以「本轮有无真实
    # 后台任务证据」的证据契约为源头——无证据 → 证成空 → 就地删执行声明 + 前置确定性「未执行」
    # 模板；真实回执（run_workflow 返回 task_id）放行、只删伪造任务号。闭集执行动作 + 分句边界含
    # 中文逗号 + 时效日期豁免 = 结构收敛，不再相位打地鼠（熔断 3 轮根因）。与 WS-161 同根不同槽。
    # 放在所有 hallu 告警生成之后（与 factslot 承重墙并列）：上面各检测器先按原文告警，再由本层
    # 做正文的结构化删除。
    from . import _exec_slot_contract
    reply, exec_warnings = _exec_slot_contract.apply(reply, tool_log or [], question, tools_used)
    warnings.extend(exec_warnings)

    from . import _factslot_contract
    reply, factslot_warnings = _factslot_contract.apply(reply, tool_log or [], question)
    warnings.extend(factslot_warnings)

    if warnings:
        banner = (
            "⚠️ **系统检测到 Agent 回复中可能存在不准确之处**：\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n\n以下是原始回复（已标记可疑部分）：\n\n---\n\n"
        )
        return banner + reply, warnings
    return reply, warnings

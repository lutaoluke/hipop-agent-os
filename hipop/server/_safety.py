"""Layer 3 — chat reply 后处理验证（fail-safe，事后捕获 hallucinate）

返回 (sanitized_reply, warnings: list[str]) — 命中规则时给 reply 头部加 banner，
让用户看见 Agent 在编造，并在前端展开真相。
"""
from __future__ import annotations
import re
from typing import Tuple, List

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


# 能证明"查了商品/数据"的工具集合（显式白名单，不是"非list_products的任意工具"）
_DATA_QUERY_TOOLS = frozenset({
    "list_products",        # 需要 limit>0
    "query_sku",
    "query_order",
    "scope_overview",
    "compute_replenishment",
    "data_health_check",
})


def _has_real_data_query(tools_used: list, tool_log=None) -> bool:
    """检查本轮是否有真实数据查询工具调用。"""
    for tool_name in tools_used:
        if tool_name not in _DATA_QUERY_TOOLS:
            continue  # 非查询工具，跳过
        if tool_name == "list_products":
            if tool_log:
                for t in tool_log:
                    if t.get("name") == "list_products":
                        args = t.get("args") or {}
                        if isinstance(args, dict) and args.get("limit", 1) > 0:
                            return True
            # 有 list_products 但没 tool_log（降级保守处理）→ 也不信，要求 limit>0
            continue
        return True  # 白名单里其他工具 = 合法查询证据
    return False


def _check_fake_query_claims(reply: str, tools_used: List[str], tool_log=None) -> List[str]:
    """检测'声称查了/拉了商品/SKU数据'但无真实工具证据的情况。"""
    warns = []
    claimed_query = re.search(
        r"(我|已)?(再次|重新)?(查了|已查|查好了|拉了|已拉|拉好了|看了|看完了)"
        r".{0,15}(商品|产品|SKU|sku|数据|库存|ERP|erp|全部货|所有货)",
        reply, re.IGNORECASE
    )
    if claimed_query and not _has_real_data_query(tools_used, tool_log):
        warns.append(
            "⚠️ Agent 声称已查询/拉取商品或数据，但没有对应工具调用证据"
            "（list_products with limit>0 或 query_sku 均未调用）— 这是 hallucinate"
        )

    claimed_order_query = re.search(
        r"(我|已)?(查了|已查|查好了|看了).{0,10}(货单|订单|order)",
        reply, re.IGNORECASE
    )
    if claimed_order_query and "query_order" not in tools_used:
        warns.append(
            "⚠️ Agent 声称已查货单/订单，但没有调用 query_order — 这是 hallucinate"
        )
    return warns


def sanitize_reply(reply: str, tools_used: List[str], tool_log=None) -> Tuple[str, List[str]]:
    """对 reply 做一遍体检，命中违规给头部加 banner。"""
    warnings: List[str] = []
    if not reply:
        return reply, warnings

    warnings.extend(_check_urls(reply))
    warnings.extend(_check_fake_timestamps(reply))
    warnings.extend(_check_fake_fields(reply))
    warnings.extend(_check_fake_query_claims(reply, tools_used, tool_log))

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

    if warnings:
        banner = (
            "⚠️ **系统检测到 Agent 回复中可能存在不准确之处**：\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n\n以下是原始回复（已标记可疑部分）：\n\n---\n\n"
        )
        return banner + reply, warnings
    return reply, warnings

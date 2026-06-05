"""
LLM Coordinator - 把 hipop 的 5 个 handler + 2 个新工具包装成 Anthropic tool-use API。

7 个 tool:
  1. query_sku                   - 查 SKU 健康（来自 wf2/wf3/wf5/wf6）
  2. query_order                 - 查货单告警 + 涉及 SKU
  3. update_alert_status         - 反馈货单状态（已确认丢货 / 已约仓 / ...）
  4. scope_overview              - 店铺概览（指定国家+平台）
  5. compute_replenishment       - 列出当前店铺的补货建议
  6. compute_air_freight_roi     - 海运 vs 空运 ROI 估算（基于 SKU 利润 + 销量）
  7. data_health_check           - 数据新鲜度检查（最新 imported_at / updated_at）

每次 tool 调用都会写入 agent_actions 表 (action_type='execute')，并把 references_json
回传给前端用于"📎 出处"展示。
"""
import os, sys, json, sqlite3, time, contextvars
from typing import List, Dict, Any, Optional

# ── chat 当前请求 context（tenant_id + scope）─────────────
# 由 chat() 入口 set，所有 tool 函数同线程读
_chat_tenant: contextvars.ContextVar[int] = contextvars.ContextVar("chat_tenant", default=1)
_chat_scope: contextvars.ContextVar[dict] = contextvars.ContextVar("chat_scope", default={})
_last_replenishment_stock_status: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "last_replenishment_stock_status", default=None
)


def _get_tenant() -> int:
    return _chat_tenant.get() or 1


def _resolve_entity_alias(store_code: str) -> Optional[str]:
    """把工作台顶部 dropdown 的 store code（KSA/UAE）转成本租户的 entity_alias。
    按 (tenant_id, country) 查 sales_entities 表。
    KSA → SA, UAE → AE
    """
    from . import data as _d
    tid = _get_tenant()
    country = {"KSA": "SA", "UAE": "AE", "SA": "SA", "AE": "AE"}.get(store_code.upper())
    if not country:
        return None
    rows = _d._fetch(
        "SELECT alias FROM sales_entities WHERE tenant_id=? AND country=? AND active=1 LIMIT 1",
        (tid, country),
    )
    return rows[0]["alias"] if rows else None

# 让 hipop/scripts/* import
HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(HIPOP_ROOT)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from . import data as _data

import anthropic
from . import _auth


def _get_client():
    return _auth.get_client()


# ── 工具定义（Anthropic tool schema）────────────────────
TOOLS = [
    {
        "name": "query_sku",
        "description": "查 SKU 的健康情况（趋势、利润率、库存可撑天数、在途、告警）。最多 3 个 SKU。",
        "input_schema": {
            "type": "object",
            "properties": {
                "skus": {"type": "array", "items": {"type": "string"}, "description": "SKU 列表，如 ['TBJ0057A']"},
                "store": {"type": "string", "description": "店铺代号 KSA 或 UAE", "enum": ["KSA", "UAE"]},
            },
            "required": ["skus"],
        },
    },
    {
        "name": "query_order",
        "description": "查货单（如 PDZ0027158）的告警 + 涉及 SKU + 当前处理状态",
        "input_schema": {
            "type": "object",
            "properties": {"order_no": {"type": "string"}},
            "required": ["order_no"],
        },
    },
    {
        "name": "update_alert_status",
        "description": "反馈某货单告警的处理结果，例如 PDxxx 已确认丢货 / 已约仓 / 已结案。会真写入 wf6_logistics_alerts。",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string"},
                "status": {"type": "string", "enum": ["已确认推进", "已确认丢货", "已约仓", "已结案", "处理中", "延迟"]},
                "note": {"type": "string", "description": "运营备注，可选"},
            },
            "required": ["order_no", "status"],
        },
    },
    {
        "name": "scope_overview",
        "description": "店铺概览：在售 SKU 数 / 急速下降数 / 在途总量 / 红色告警数。",
        "input_schema": {
            "type": "object",
            "properties": {"store": {"type": "string", "enum": ["KSA", "UAE"]}},
            "required": ["store"],
        },
    },
    {
        "name": "compute_replenishment",
        "description": "列出当前店铺的补货建议（来自 wf5_sales_cycle 的 weekly_total_replenish > 0 的 SKU）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["KSA", "UAE"]},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["store"],
        },
    },
    {
        "name": "compute_air_freight_roi",
        "description": "估算单个 SKU 海运 vs 空运的 ROI 决策。假设：海运 0.4 USD/件、空运 2.5 USD/件、海运 25 天、空运 5 天。结合 SKU 销量 + 利润率，估算损失订单数 + 净 ROI 差。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "store": {"type": "string", "enum": ["KSA", "UAE"]},
                "qty": {"type": "integer", "description": "本次发运件数", "default": 100},
            },
            "required": ["sku", "store"],
        },
    },
    {
        "name": "data_health_check",
        "description": "返回各数据表（wf2 / wf5 / wf3 / wf6 / feishu_digest）的最新写入时间，告诉用户数据是不是新鲜。",
        "input_schema": {
            "type": "object",
            "properties": {"store": {"type": "string", "enum": ["KSA", "UAE"]}},
            "required": ["store"],
        },
    },
    {
        "name": "list_products",
        "description": (
            "列出店铺商品。返回两个维度的统计：summary_products（product 维度，与 ERP 后台数字一致）+ "
            "summary_skus（SKU 维度，含变体）。"
            "用户问『商品/产品总数』时优先报 summary_products.total（这是 ERP 后台筛店铺看到的数字）。"
            "用户问『SKU 总数』或『变体』时报 summary_skus.total。"
            "is_listed=1 = 已绑定 noon 平台 SKU id（在线上能搜到/可下单）；is_listed=0 = 草稿/未挂平台。"
            "listing='listed'/'unlisted'/'all' 控制示例返回；sales_only=true 仅含 sales_180d>0；limit=0 时不返示例。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["KSA", "UAE"]},
                "listing": {"type": "string", "enum": ["all", "listed", "unlisted"], "default": "all"},
                "sales_only": {"type": "boolean", "default": False, "description": "true=仅含 180 天内有销量"},
                "limit": {"type": "integer", "default": 0, "description": "返回示例 SKU 行数，0=只要聚合"},
            },
            "required": ["store"],
        },
    },
    {
        "name": "export_table",
        "description": (
            "用户要求『导出/下载/给我表格/打成表格/Excel』时调本工具。"
            "**真生成 xlsx 文件** 到 ~/hipop/exports/，返 {download_url, row_count, filename}。"
            "返完后用 markdown 链接 `[文件名](download_url)` 给用户。"
            "\n**禁** 自己编『下载链接』；**禁** 说『系统只能返 X 个示例』(filtered_count 才是真总数，items 由 limit 控制)。"
            "\n常用 view："
            "\n  - `unlisted_with_sales` 未上架但 180d 有销量 SKU（Luke 高频）"
            "\n  - `sales` wf2_sku 销量全字段（配 listing/sales_only 细化）"
            "\n  - `sku_health` 销量+库存+在途跨表"
            "\n  - `replenish` wf5_sales_cycle 补货建议"
            "\n  - `logistics` wf3 卡单告警"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": [
                    "unlisted_with_sales", "sales", "sku_health", "replenish", "logistics"
                ]},
                "store": {"type": "string", "enum": ["KSA", "UAE"], "default": "KSA"},
                "listing": {"type": "string", "enum": ["all", "listed", "unlisted"], "default": "all",
                            "description": "用于 sales/sku_health view"},
                "sales_only": {"type": "boolean", "default": False,
                               "description": "用于 sales/sku_health view — 只要 180d 有销量"},
                "format": {"type": "string", "enum": ["excel"], "default": "excel"},
                "filter_desc": {"type": "string", "description": "用户筛选条件描述（写到文件名/响应里）"},
            },
            "required": ["view"],
        },
    },
    {
        "name": "navigate_user_to",
        "description": (
            "用户要求『打开 X 页面/进 X 模块/看 X 看板』时调本工具，返回**真实**的工作台模块路径。"
            "\n严禁编造 URL（如 agent.diangou.ai 这种虚构域名）；模块路径只能是 localhost:8765/module/<name>。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "enum": ["overview", "sales", "logistics", "replenish", "selection", "feishu", "audit", "role_liuhe"],
                },
                "store": {"type": "string", "enum": ["KSA", "UAE"]},
            },
            "required": ["module"],
        },
    },
    {
        "name": "notify_via_feishu",
        "description": (
            "用户说『发到飞书 / 通知刘鹤 / 推到群里 / @同事』时调本工具。"
            "\n本系统飞书推送当前为只读集成（拉取告警/补货决策同步），**不能主动发消息**。"
            "\n严禁不调本 tool 直接说『已发到飞书』。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "飞书群 / 个人 / @某同事"},
                "message_summary": {"type": "string"},
            },
            "required": ["message_summary"],
        },
    },
    {
        "name": "run_workflow",
        "description": (
            "异步触发后台工作流（耗时操作，立即返回 task_id 让前端订 SSE 进度）。\n"
            "**只可选以下 v2 workflow（per-tenant，会用 onboarding 存的加密 ERP 凭据 headless 登录拉数）**：\n"
            "- refresh_all_v2：全量刷新（商品→销量→库存→销售周期→物流→告警），用户说『拉/同步/刷』全部数据时优先用\n"
            "- wf2_products_v2：只拉 ERP 商品库（写 wf2_sku）\n"
            "- wf2_sales_v2：拉商品库 + 销量价格 6 时间窗（写 wf2_sku）\n"
            "- wf2_sales_refresh_v2：用现有 noon 订单重算窗口销量 + 评级/预测（不拉新 CSV/ERP，秒级）；用户说『刷新销量 / 重算销量 / 重新评级』时选它\n"
            "- wf1_stock_v2：拉 ERP 6 仓库存（写 wf1_stock）\n"
            "- wf5_sales_cycle_v2：基于现有 wf2/wf1/wf3 数据重算销售周期 + 补货决策\n"
            "- wf3_logistics_v2：从 ERP 拉物流货单 + 抓物流站节点（默认只扫近 60 天有销量的 SKU，~30 分钟；用户问『扫物流』走这个）\n"
            "- wf6_alerts_v2：物流告警生成（依赖 wf3 真数据；wf3 跑完再调）\n"
            "- wf4_replenish_suggest：补货建议（三类静态数据 → 每 SKU 补货量表，WS-7）；用户问『该补多少 / 算补货量 / 出补货清单』时选它\n"
            "用户说『拉/同步/刷新/重算/跑』数据时调本工具。**除上面列出的之外严禁选其它 workflow，老 workflow 只读全局 env，会让多租户用户必崩**。\n"
            "**重要**：如果用户原始问题需要等数据跑完才能答（如『我该补货吗』而 wf5 陈旧），"
            "在 followup_prompt 填上『需要等工作流跑完后接续答的问题』，前端会在 task 完成后自动重发一轮 chat，"
            "你那时再用最新数据答最终结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": ["refresh_all_v2", "wf2_products_v2", "wf2_sales_v2",
                             "wf2_sales_refresh_v2",
                             "wf1_stock_v2", "wf5_sales_cycle_v2",
                             "wf3_logistics_v2", "wf6_alerts_v2",
                             "wf4_replenish_suggest"],
                },
                "followup_prompt": {
                    "type": "string",
                    "description": "工作流跑完后前端会自动作为新一轮 user 消息重发。一般填用户的原始问题（如『我该补货吗』）。不需要等的纯触发场景留空。",
                },
            },
            "required": ["workflow"],
        },
    },
    {
        "name": "query_sku_live",
        "description": (
            "**实时**查单个 SKU 的 ERP 在途货单 + 物流公司 + tracking 号（不读 wf3_hub_v2 缓存，直连 ERP）。"
            "默认快版（只 ERP，5-15s）。with_nodes=True 时**额外用 playwright 抓物流站节点**"
            "（义特无忧/安时达/阳光/飞坦 4 家），多花 5-10s 每单，但能告诉用户'卡在哪个节点'。"
            "**触发 with_nodes=True 的关键词**：节点 / 卡哪 / 物流轨迹 / 走到哪了 / 详细物流状态。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "SKU 码，如 TBC0168A"},
                "with_nodes": {"type": "boolean", "default": False,
                                "description": "是否抓物流站节点（慢但准）"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "query_order_live",
        "description": (
            "**实时**查单个货单的 ERP 当前状态 + 物流公司 + tracking 号 + 物流站直链。"
            "适用：用户问 'PDxxx 现在到哪了' / 'PDxxx 物流卡几天' / 'PDxxx 实时状态'。"
            "比 query_order 慢（实时拉），但 ERP 状态最新（hipop wf3 缓存可能 N 天前）。"
            "返回 forwarder + tracking_no + 物流站 URL（让用户/Agent 点过去看节点）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "货单号 PDxxx"},
            },
            "required": ["order_no"],
        },
    },
    {
        "name": "tenant_notes_get",
        "description": (
            "读 tenant 跨 session 沉淀的 NOTES.md（客户偏好 / 业务规则 / 学到的经验）。"
            "适用：用户问'我的偏好' / '上次我说的 X' / Agent 准备做高风险决策前查 context。"
            "section 可选；不给返全文。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                             "description": "可选，只返指定 # 标题段（如'补货偏好'/'选品方向'）"},
            },
        },
    },
    {
        "name": "tenant_notes_append",
        "description": (
            "把用户明确表达的偏好/规则/经验追加到 NOTES.md 跨 session 沉淀。"
            "**只在用户明确说'以后都这么办' / '记住' / '默认' 等持久化语义时调**，"
            "不要把每条聊天都写进 NOTES（NOTES.md 要短而高信号，按 Anthropic 哲学）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "要沉淀的一句话规则/偏好"},
                "section": {"type": "string", "default": "通用",
                             "description": "归入哪个 # 标题（补货偏好/选品方向/物流规则/通用 等）"},
            },
            "required": ["note"],
        },
    },
    {
        "name": "confirm_proposal",
        "description": (
            "用户已确认 / 取消之前 destructive 行动的 Plan。\n"
            "**触发时机**：上一轮你给用户返了 plan_text（action_type='plan'），用户回复 'OK / 是 / 确认' "
            "或 '取消 / 不要 / no'，本轮必须调本工具推进。\n"
            "**绝不要**自己再次调原 destructive tool（governance pipeline 会拒）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string",
                                 "description": "上一轮 plan 返回的 proposal_id"},
                "user_decision": {"type": "string", "enum": ["ok", "cancel"],
                                   "description": "用户的最终决定"},
            },
            "required": ["proposal_id", "user_decision"],
        },
    },
    {
        "name": "query_1688_similar",
        "description": (
            "拿一张 noon/amazon 商品图片 URL, 走 1688 主站图搜找同款 + 比价. "
            "用户场景: 粘一张 noon URL 或图片 URL 说『找同款』『1688 有这个吗』『找货源』. "
            "返回 top 5 候选: offer_id / 标题 / 价格 / 店铺 / cosScore / verdict (inquiry|differentiation|watch|drop) / 跳转 URL. "
            "verdict=inquiry 即可直接询盘; differentiation 进 N8 差异化挖掘; failed=True 时给文搜兜底关键词."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "公网可达商品图 URL (noon CDN / amazon CDN / 1688 CDN)"},
                "pack": {"type": "integer", "default": 1,
                         "description": "件套数: 1=单只 / 2-6=多件套. 多件套自动关 yoloCrop 提升 set 组识别"},
                "material": {"type": "string",
                             "description": "query 材质 (ABS/PC/PP/铝框/软箱/皮箱/未知), 用于跨材质降权"},
                "title": {"type": "string", "description": "query 标题, 仅记录用"},
            },
            "required": ["image_url"],
        },
    },
    {
        "name": "capture_feedback",
        "description": (
            "用户撞到你做不了 / 超出当前能力范围的事，且用户**确认要记成需求**时调本工具，"
            "**真写入 feedback 表**（产品迭代会读这张表，需求不再石沉大海）。"
            "\n触发时机：上一轮你回过『这个我做不了 / 超出范围』并 offer 了『要我记成需求吗』，"
            "用户回『记一下 / 好 / 帮我记 / 提个需求』——本轮必须调本工具。"
            "\n**严禁**不调本 tool 就说『已记下 / 已反馈给产品』——没写库 = 没记。"
            "\n写失败工具会返 ok=False + error，此时如实告诉用户『没记成，等会儿再说一次』，"
            "**绝不许假装记了**（报告即事实）。content 尽量保留用户原话。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "用户的需求原话 / 诉求，尽量保留原文"},
                "scene": {"type": "string",
                          "description": "触发场景，如『想把销量导成 PDF 月报』『问能不能改商品价』"},
                "category": {"type": "string",
                             "enum": ["需求", "bug", "数据问题", "其他"], "default": "需求"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "explain_status_enum",
        "description": (
            "用户问『5 个状态哪里来的 / 能加 X 状态吗 / 状态定义在哪 / 这是 ERP 内置还是 hipop 自己的字段』时调本工具。"
            "返回字段出处、当前枚举值、是否可扩展、扩展方法。不要凭空说『状态写死在系统里』；必须真调本 tool 拿到 source 引用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": ["alert_status"], "default": "alert_status",
                          "description": "目前只支持 alert_status (告警 ops_status)"},
            },
        },
    },
]


# ── 工具实现（v2 列存：按 tenant_id + entity_alias 过滤）──
def tool_query_sku(skus: List[str], store: str = "KSA") -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    out = []
    refs = []
    for sku in skus[:3]:
        rows = _data._fetch("""
            SELECT w2.partner_sku, w2.title, w2.sales_grade, w2.latest_profit_rate,
                   w2.sales_30d, w2.sales_10d, w2.latest_price,
                   w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
                   w5.current_pipeline, w5.weekly_total_replenish,
                   h.in_transit_total_qty, h.has_stuck_batch, h.needs_ops_input
            FROM wf2_sku w2
            LEFT JOIN wf5_sales_cycle w5
              ON w2.tenant_id=w5.tenant_id AND w2.entity_alias=w5.entity_alias
              AND w2.partner_sku=w5.partner_sku
            LEFT JOIN wf3_logistics_hub_v2 h
              ON w2.tenant_id=h.tenant_id AND w2.partner_sku=h.sku
            WHERE w2.tenant_id=? AND w2.entity_alias=? AND w2.partner_sku=?
        """, (tid, alias, sku))
        if not rows:
            out.append({"sku": sku, "found": False})
            continue
        r = rows[0]
        out.append({
            "sku": sku,
            "found": True,
            "title": r["title"],
            "trend": r["trend"],
            "profit_rate_pct": round((r["latest_profit_rate"] or 0) * 100, 1),
            "sales_30d": r["sales_30d"],
            "sales_10d": r["sales_10d"],
            "daily_rate": r["daily_rate"],
            "urgency": r["urgency"],
            "ops_advice": r["ops_advice"],
            "in_transit": r["in_transit_total_qty"],
            "has_stuck_batch": bool(r["has_stuck_batch"]),
            "weekly_replenish": r["weekly_total_replenish"],
        })
        refs.append({"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"})
        refs.append({"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"})
        refs.append({"table": "wf3_logistics_hub_v2", "where": f"tenant_id={tid} AND sku='{sku}'"})
    return {"items": out, "references": refs}


def tool_query_order(order_no: str) -> Dict:
    tid = _get_tenant()
    rows = _data._fetch("""
        SELECT alert_id, alert_level, alert_reason, sku_list_json, ops_status,
               actual_stay_days, history_stage_days, stage, created_at, action_owner
        FROM wf6_logistics_alerts_v2
        WHERE tenant_id=? AND order_no=? ORDER BY created_at DESC
    """, (tid, order_no))
    for r in rows:
        try:
            r["skus"] = json.loads(r["sku_list_json"] or "[]")
        except Exception:
            r["skus"] = []
    return {
        "order_no": order_no,
        "alerts": rows,
        "references": [{"table": "wf6_logistics_alerts_v2", "where": f"tenant_id={tid} AND order_no='{order_no}'"}],
    }


def tool_update_alert_status(order_no: str, status: str, note: str = "") -> Dict:
    try:
        from workflows.wf_logistics_alerts import update_alert_status as _u
    except Exception as e:
        return {"ok": False, "error": str(e)}
    tid = _get_tenant()
    rows = _data._fetch(
        "SELECT alert_id FROM wf6_logistics_alerts_v2 "
        "WHERE tenant_id=? AND order_no=? AND resolved_at IS NULL",
        (tid, order_no),
    )
    if not rows:
        return {"ok": False, "error": f"{order_no} 无 active 告警"}
    affected = []
    for r in rows:
        try:
            _u(r["alert_id"], status, note or None, "Agent (LLM 触发)")
            affected.append(r["alert_id"])
        except Exception as e:
            return {"ok": False, "error": f"alert#{r['alert_id']}: {e}"}
    return {
        "ok": True,
        "order_no": order_no,
        "updated_alerts": affected,
        "new_status": status,
        "references": [{"table": "wf6_logistics_alerts_v2", "where": f"tenant_id={tid} AND order_no='{order_no}' (写入)"}],
    }


def tool_scope_overview(store: str) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    o = _data.get_today(store)
    return {
        **o,
        "references": [
            {"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND is_listed=1"},
            {"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}'"},
            {"table": "wf3_logistics_hub_v2", "where": f"tenant_id={tid}"},
            {"table": "wf6_logistics_alerts_v2", "where": f"tenant_id={tid} AND ops_status='待处理'"},
        ],
    }


def tool_compute_replenishment(store: str, limit: int = 10) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    # WS-62：与 HTTP 入口同源——带库存就绪度。库存未就绪/不完整时，chat 不能把
    # 空建议当成「不用补货」，必须如实说「库存未就绪/不完整」（防死代码短路）。
    view = _data.get_replenishment_view(store, limit=limit)
    stock_status = view["stock_status"]
    _last_replenishment_stock_status.set(stock_status)
    rows = view["rows"]
    items = [{
        "sku": r["partner_sku"], "title": r["title"], "qty": r["qty"],
        "urgency": r["urgency_level"], "daily_rate": r["daily_rate"], "trend": r["trend"],
        "advice": r["ops_advice"],
    } for r in rows]
    return {
        "store": store, "count": len(items), "items": items,
        "stock_status": stock_status,
        "warning": None if stock_status.get("ready") else stock_status.get("message"),
        "stale_warning": None if stock_status.get("ready") else "库存数据未更新或不完整，当前补货结论偏保守",
        "references": [
            {"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND weekly_total_replenish>0"},
            {"table": "wf6_replenishment_queue_v2", "where": f"tenant_id={tid} AND entity_alias='{alias}'"},
        ],
    }


def tool_compute_air_freight_roi(sku: str, store: str, qty: int = 100) -> Dict:
    """简化模型: 海运 0.4 / 件, 空运 2.5 / 件, 海运 25d, 空运 5d."""
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    rows = _data._fetch("""
        SELECT w2.partner_sku, w2.latest_price, w2.latest_profit_rate,
               w5.daily_rate, w5.trend
        FROM wf2_sku w2
        LEFT JOIN wf5_sales_cycle w5
          ON w2.tenant_id=w5.tenant_id AND w2.entity_alias=w5.entity_alias
          AND w2.partner_sku=w5.partner_sku
        WHERE w2.tenant_id=? AND w2.entity_alias=? AND w2.partner_sku=?
    """, (tid, alias, sku))
    if not rows:
        return {"ok": False, "error": f"SKU {sku} 不存在于 wf2_sku (tenant={tid}, entity={alias})"}
    r = rows[0]
    daily_rate = r["daily_rate"] or 0
    price = r["latest_price"] or 0
    pr = r["latest_profit_rate"] or 0
    profit_per = price * pr
    delta_days = 20  # 25 - 5
    extra_freight_cost = (2.5 - 0.4) * qty
    saved_revenue = daily_rate * delta_days * profit_per
    roi_delta = saved_revenue - extra_freight_cost
    rec = "建议空运" if roi_delta > 0 else "建议海运"
    return {
        "sku": sku, "store": store, "qty": qty,
        "daily_rate": daily_rate, "profit_per": round(profit_per, 2),
        "extra_air_cost": extra_freight_cost,
        "saved_revenue_if_air": round(saved_revenue, 2),
        "net_roi_delta": round(roi_delta, 2),
        "recommendation": rec,
        "assumptions": "海运 0.4 USD/件, 空运 2.5 USD/件, 时长差 20 天",
        "references": [
            {"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"},
            {"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"},
        ],
    }


def tool_data_health_check(store: str) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    h = _data.get_data_health(store)
    return {
        **h,
        "references": [
            {"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' MAX(imported_at)"},
            {"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' MAX(updated_at)"},
            {"table": "wf3_logistics_hub_v2", "where": f"tenant_id={tid} MAX(updated_at)"},
        ],
    }


def tool_list_products(store: str, listing: str = "all",
                       sales_only: bool = False, limit: int = 0) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    tbl = "wf2_sku"
    # 聚合 — SKU 维度
    base_where = f"tenant_id={tid} AND entity_alias='{alias}'"
    agg = _data._fetch(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN is_listed=1 THEN 1 ELSE 0 END) AS listed,
          SUM(CASE WHEN is_listed=0 OR is_listed IS NULL THEN 1 ELSE 0 END) AS unlisted,
          SUM(CASE WHEN COALESCE(sales_180d,0) > 0 THEN 1 ELSE 0 END) AS ever_sold,
          SUM(CASE WHEN COALESCE(sales_30d,0) > 0 THEN 1 ELSE 0 END) AS sold_recent_30d
        FROM {tbl} WHERE {base_where}
    """)[0]
    # 聚合 — product 维度（与 ERP 后台视图一致）
    prod_agg = _data._fetch(f"""
        SELECT
          COUNT(DISTINCT product_id) AS product_total,
          COUNT(DISTINCT CASE WHEN is_listed=1 THEN product_id END) AS product_listed,
          COUNT(DISTINCT CASE WHEN is_listed=0 OR is_listed IS NULL THEN product_id END) AS product_unlisted
        FROM {tbl} WHERE {base_where} AND product_id IS NOT NULL AND product_id != ''
    """)[0]

    where = [base_where]
    if listing == "listed":   where.append("is_listed=1")
    elif listing == "unlisted": where.append("(is_listed=0 OR is_listed IS NULL)")
    if sales_only: where.append("COALESCE(sales_180d,0) > 0")
    where_sql = "WHERE " + " AND ".join(where)

    filtered_count = _data._scalar(f"SELECT COUNT(*) FROM {tbl} {where_sql}") or 0

    items = []
    if limit and limit > 0:
        rows = _data._fetch(f"""
            SELECT partner_sku, title, is_listed, sales_30d, sales_180d, latest_price
            FROM {tbl} {where_sql}
            ORDER BY COALESCE(sales_30d,0) DESC, COALESCE(sales_180d,0) DESC
            LIMIT ?
        """, (int(limit),))
        items = [{
            "sku": r["partner_sku"], "title": r["title"],
            "is_listed": bool(r["is_listed"]),
            "sales_30d": r["sales_30d"] or 0,
            "sales_180d": r["sales_180d"] or 0,
            "price": r["latest_price"],
        } for r in rows]

    return {
        "store": store,
        "summary_products": {
            # ERP 后台视图（product 维度，与运营直觉对齐）— 1 product 可能含多个 SKU 变体
            "total":     prod_agg["product_total"],
            "listed":    prod_agg["product_listed"],
            "unlisted":  prod_agg["product_unlisted"],
            "_dim": "product (= ERP 后台筛选店铺时显示的总数)"
        },
        "summary_skus": {
            # SKU 维度（含变体）
            "total":           agg["total"],
            "listed":          agg["listed"],     # 已绑定 noon platform_sku_id
            "unlisted":        agg["unlisted"],   # 未绑定 noon = 草稿/未上架
            "ever_sold_180d":  agg["ever_sold"],
            "sold_recent_30d": agg["sold_recent_30d"],
            "_dim": "sku (含每个 product 下的颜色/尺寸变体)"
        },
        "filter": {"listing": listing, "sales_only": sales_only},
        "filtered_count": filtered_count,
        "items": items,
        "references": [
            {"table": tbl, "where": where_sql or "(全表)"},
        ],
    }


def tool_export_table(view: str, format: str = "excel", filter_desc: str = "",
                       store: str = "KSA", listing: str = "all",
                       sales_only: bool = False) -> Dict:
    """真生成 xlsx 文件 — 写 ~/hipop/exports/<filename>.xlsx 返下载 URL。

    view 决定数据源 + 列：
      - unlisted_with_sales: wf2_sku WHERE is_listed=0 AND sales_180d>0  (Luke 高频需求)
      - sales:               wf2_sku 全量销量字段
      - sku_health:          wf2_sku 销量 + 库存 + 在途 (跨表)
      - replenish:           wf5_sales_cycle (补货建议)
      - logistics:           wf3_logistics_hub_v2 (物流告警)
    listing/sales_only 是 sales / sku_health view 的细化筛选。
    """
    import os
    from datetime import datetime
    try:
        from openpyxl import Workbook
    except ImportError:
        return {"ok": False, "error": "openpyxl 未装 (pip install openpyxl)"}

    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    if not alias:
        return {"ok": False, "error": f"未知店铺 store={store}"}

    # 决定 query
    if view == "unlisted_with_sales":
        where = (f"tenant_id={tid} AND entity_alias='{alias}' "
                 f"AND (is_listed=0 OR is_listed IS NULL) "
                 f"AND COALESCE(sales_180d,0) > 0")
        cols = ["partner_sku", "title", "sales_180d", "sales_90d", "sales_30d",
                "sales_10d", "latest_price", "avg_price", "latest_profit_rate",
                "cost_price", "currency", "brand", "product_category_detail",
                "latest_order_date", "is_listed"]
        order_by = "COALESCE(sales_180d,0) DESC"
    elif view in ("sales", "sku_health"):
        # WS-20：导出「最后输出数据」全字段，替代人工 Excel 汇总。
        # 口径与读取器统一在 data.sales_output_rows（与 /api/sku-health 同源），
        # 确定性规则不堆在本文件（见 CODEOWNERS 说明）。
        from . import data as _d
        rows = _d.sales_output_rows(tid, alias, listing=listing, sales_only=sales_only)
        cols = [k for k, _h, _s in _d.SALES_OUTPUT_SPEC]
        headers = [h for _k, h, _s in _d.SALES_OUTPUT_SPEC]
        return _write_xlsx_and_return(rows, f"{view}_{store}", filter_desc, cols, headers)
    elif view == "replenish":
        from . import data as _d
        rows = _d._fetch(
            f"SELECT * FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=? "
            f"ORDER BY urgency DESC, sellable_days ASC",
            (tid, alias),
        )
        return _write_xlsx_and_return(rows, f"replenish_{store}", filter_desc)
    elif view == "logistics":
        from . import data as _d
        rows = _d._fetch(
            f"SELECT * FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND has_stuck_batch=1 "
            f"ORDER BY in_transit_total_qty DESC",
            (tid,),
        )
        return _write_xlsx_and_return(rows, f"logistics_stuck_{store}", filter_desc)
    else:
        return {"ok": False, "error": f"未知 view={view}；支持: "
                "unlisted_with_sales / sales / sku_health / replenish / logistics"}

    # 走 wf2_sku 通用路径
    from . import data as _d
    rows = _d._fetch(
        f"SELECT {','.join(cols)} FROM wf2_sku WHERE {where} ORDER BY {order_by}",
        (),
    )
    return _write_xlsx_and_return(rows, f"{view}_{store}", filter_desc, cols)


def _write_xlsx_and_return(rows: list, name_prefix: str, filter_desc: str = "",
                             cols: list = None, headers: list = None) -> Dict:
    """统一 xlsx 写盘 + 返下载链接 helper。

    cols    决定取值用的 row dict key（顺序即列顺序）。
    headers 可选，写表头时用的展示名（与 cols 一一对应，长度须相等）；
            缺省退回 cols 自身（向后兼容老调用）。
    """
    import os, time
    from datetime import datetime
    from openpyxl import Workbook

    export_dir = os.path.expanduser("~/hipop/exports")
    os.makedirs(export_dir, exist_ok=True)

    if not rows:
        return {"ok": True, "row_count": 0,
                "message": "查询无数据 — 不生成空文件"}

    if not cols:
        cols = list(rows[0].keys()) if isinstance(rows[0], dict) else []

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{name_prefix}_{ts}.xlsx"
    fpath = os.path.join(export_dir, fname)

    if not headers or len(headers) != len(cols):
        headers = cols

    wb = Workbook()
    ws = wb.active
    ws.title = name_prefix[:30]
    ws.append(headers)
    for r in rows:
        if isinstance(r, dict):
            ws.append([r.get(c) for c in cols])
        else:
            ws.append(list(r))
    # 第一行加粗
    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)
    # 自适应列宽（粗略）
    for col_idx, c in enumerate(cols, 1):
        max_len = max((len(str(r.get(c) if isinstance(r, dict) else "")) for r in rows[:50]),
                      default=10)
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max(len(c), max_len) + 2, 40)
    wb.save(fpath)

    download_url = f"/api/download/{fname}"
    return {
        "ok": True,
        "row_count": len(rows),
        "download_url": download_url,
        "filename": fname,
        "filter_desc": filter_desc,
        "message": f"已生成 {len(rows)} 行 xlsx，下载: {download_url}",
        "file_path": fpath,  # 本地路径方便排查
    }


def tool_navigate_user_to(module: str, store: str = "KSA") -> Dict:
    """返回真实模块路径，禁止 Agent 编造虚构域名。"""
    valid = ["overview", "sales", "logistics", "replenish", "selection", "feishu", "audit", "role_liuhe"]
    if module not in valid:
        return {"ok": False, "error": f"模块 {module} 不存在；有效模块: {valid}"}
    if module == "overview":
        path = f"/?store={store.lower()}"
    elif module == "role_liuhe":
        path = "/role/liuhe"
    else:
        path = f"/module/{module}?store={store.lower()}"
    full_url = f"http://localhost:8765{path}"
    return {
        "ok": True,
        "module": module,
        "path": path,
        "url": full_url,
        "hint": f"工作台模块入口：{full_url}（左侧 sidebar 也能直接点）",
    }


def tool_notify_via_feishu(message_summary: str, channel: str = "") -> Dict:
    """stub — 飞书 push 当前只读集成。"""
    return {
        "ok": False,
        "supported": False,
        "channel": channel,
        "message": (
            "本系统飞书集成当前是只读：从飞书拉取告警状态、补货决策反馈，"
            "不能主动推送消息到飞书群/同事。"
            "\n如需通知，请用户在飞书 app 内手动转发，或 wf6_alerts 飞书表会被运营/跟单看到。"
        ),
    }


def tool_run_workflow(workflow: str, followup_prompt: str = "") -> Dict:
    """触发后台工作流。直接复用 api._run_workflow + uuid 生成 task_id，不走 HTTP 自调用。"""
    from uuid import uuid4
    import threading
    from . import api as _api

    if workflow not in _api.WORKFLOW_REGISTRY:
        return {"ok": False, "error": f"unknown workflow: {workflow}",
                "valid": list(_api.WORKFLOW_REGISTRY)}
    label, steps, affected = _api.WORKFLOW_REGISTRY[workflow]
    task_id = uuid4().hex[:8]
    # 拿当前 chat 的 tenant_id（contextvars 注入），传给后台线程
    tid = _get_tenant()
    sc = _chat_scope.get() or {}
    actor = {
        "user_id": sc.get("user_id"),
        "email": sc.get("current_user_email") or sc.get("current_user"),
        "role": sc.get("current_role"),
        "source": "chat",
    }
    threading.Thread(
        target=_api._run_workflow, args=(task_id, workflow, tid, actor), daemon=True,
    ).start()
    return {
        "ok": True,
        "task_id": task_id,
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "followup_prompt": followup_prompt or None,
        "hint": (
            f"已启动后台任务 {task_id}（{label}），前端将订阅 SSE 推送进度，"
            f"完成后自动刷新 {affected}。"
            + (f" 跑完后会自动重发『{followup_prompt}』给你接续答用户。" if followup_prompt else "")
        ),
    }


def tool_query_1688_similar(image_url: str, pack: int = 1,
                              material: str = "", title: str = "") -> Dict:
    """走 N7 1688 图搜找同款. 全自动 cookies + 多件套关 yoloCrop + 规则分桶."""
    try:
        from selection.l3_orchestration.nodes.n7_1688_supply import run_query
    except ImportError as e:
        return {"ok": False, "error": "selection module not on path", "detail": str(e)}

    query = {
        "idx": 0,
        "title": title or "",
        "image_url": image_url,
        "pack": pack or 1,
        "material": material or None,
    }
    try:
        result = run_query(query, cookies=None)  # cookies=None → cookies_manager.ensure()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if result.error:
        return {"ok": False, "error": result.error}

    top = result.offers[:5]
    return {
        "ok": True,
        "found": result.found,
        "yolocrop": "OFF" if not result.yolocrop_used else "ON",
        "failed": result.failed,
        "fallback_keywords": result.fallback_keywords,
        "verdicts_top5": [o.get("verdict") for o in top],
        "candidates": [
            {
                "offer_id": o.get("offer_id"),
                "title": o.get("title"),
                "price_cny": o.get("price"),
                "company": o.get("company"),
                "province": o.get("province"),
                "city": o.get("city"),
                "verdict": o.get("verdict"),
                "combined_score": round(o.get("combined_score") or 0, 3),
                "cos_score": round(o.get("cos_score") or 0, 3),
                "material": o.get("material"),
                "warning_flags": o.get("warning_flags") or [],
                "offer_pic": o.get("offer_pic_url"),
                "open_url": f"https://detail.1688.com/offer/{o.get('offer_id')}.html",
            }
            for o in top
        ],
    }


def _erp_token_or_error(tid: int):
    """共用：拿 per-tenant ERP token。没凭据返 'no_creds'；登录失败返 'login_failed'."""
    from . import _erp_auth
    creds = _erp_auth._get_creds(tid)
    if not creds:
        return None, {"ok": False, "error": "no_erp_credentials",
                       "message": f"tenant {tid} 没配 ERP 账号 — 请去 /onboarding 配 dbuyerp"}
    token = _erp_auth.get_erp_token_for_tenant(tid)
    if not token:
        return None, {"ok": False, "error": "erp_login_failed",
                       "message": f"ERP 凭据存在但 playwright 登录失败 "
                                   "（dbuyerp 可能在风控同账号短时间多次登），稍后重试或用 wf3 缓存"}
    return token, None


def _query_sku_from_cache(sku: str, tid: int) -> dict:
    """ERP 拿不到 token 时的 fallback：从 wf3_logistics_hub_v2 缓存读。
    数据可能 N 天前但总比 hallucinate 强。"""
    from . import data as _data
    _data.set_current_tenant(tid)
    rows = _data._fetch(
        "SELECT sku, in_transit_total_qty, total_transit_qty, transit_batches_json, "
        "       updated_at FROM wf3_logistics_hub_v2 "
        "WHERE tenant_id=? AND sku=?",
        (tid, sku),
    )
    if not rows:
        return {"ok": True, "sku": sku, "fetched_from": "wf3_cache_no_data",
                "in_transit_total_qty": 0, "stale_warn": "wf3 缓存里这个 SKU 没数据"}
    r = rows[0]
    try:
        import json as _json
        batches = _json.loads(r.get("transit_batches_json") or "[]")
    except Exception:
        batches = []
    upd = r.get("updated_at")
    return {
        "ok": True,
        "sku": sku,
        "fetched_from": "wf3_logistics_hub_v2 cache (ERP 实时拿不到 token 时的兜底)",
        "stale_warn": f"⚠️ 此为 wf3 缓存数据，更新于 {upd}（非实时）。若需实时请稍后重试。",
        "in_transit_total_qty": r.get("in_transit_total_qty") or 0,
        "total_transit_qty": r.get("total_transit_qty") or 0,
        "cache_updated_at": str(upd) if upd else None,
        "in_transit_orders": [
            {"order_no": b.get("order_no"), "qty": b.get("qty"),
             "forwarder": b.get("forwarder"), "tracking_no": b.get("tracking_no")}
            for b in batches[:10]
        ],
    }


def _patch_wls_token(token: str):
    """共用：monkey-patch wls + wf0.get_erp_token 闭包，跟 wf3_logistics_v2 同套路"""
    import sys as _sys
    _hipop_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _hipop_dir not in _sys.path:
        _sys.path.insert(0, _hipop_dir)
    from workflows import wf0_logistics as _wf0
    from workflows import wf_logistics_status as _wls
    _orig = _wf0.get_erp_token
    _wf0.get_erp_token = lambda: token
    _wls.get_erp_token = lambda: token
    return _wf0, _wls, _orig


def _fetch_logistics_nodes(forwarder: str, tracking_no: str) -> dict:
    """playwright 实时抓物流站节点（安时达/阳光/义特/飞坦）。返回 {nodes, note}."""
    if not forwarder or not tracking_no:
        return {"nodes": [], "note": "缺 forwarder 或 tracking_no"}
    try:
        from playwright.sync_api import sync_playwright
        from workflows.wf_logistics_status import query_tracking
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                r = query_tracking(page, forwarder, tracking_no)
            finally:
                browser.close()
            return r
    except Exception as e:
        return {"nodes": [], "note": f"playwright_error: {type(e).__name__}: {str(e)[:100]}"}


def _physical_tracking_url(forwarder: str, tracking_no: str) -> str:
    """根据 forwarder 拼物流站直链。"""
    if not forwarder or not tracking_no:
        return ""
    from workflows.wf_logistics_status import LOGISTICS_URLS
    base = LOGISTICS_URLS.get(forwarder, "")
    if not base:
        return f"(无直链, forwarder={forwarder})"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}msg={tracking_no}" if "tracking" in base.lower() else f"{base}{sep}no={tracking_no}"


def tool_query_sku_live(sku: str, with_nodes: bool = False) -> Dict:
    """实时查单 SKU ERP 在途货单 — 不读 wf3 缓存，每次直连。
    with_nodes=True 时对每个在途货单跑 playwright 抓物流站节点（慢 5-10s/单）。
    默认 False（只 ERP 拉单 + tracking_no，快），用户问'节点'/'卡哪'时设 True。"""
    tid = _get_tenant()
    token, err = _erp_token_or_error(tid)
    if err:
        if err.get("error") == "erp_login_failed":
            cache_resp = _query_sku_from_cache(sku, tid)
            cache_resp["live_query_failed_reason"] = err["message"]
            return cache_resp
        return err
    _wf0, _wls, _orig = _patch_wls_token(token)
    try:
        in_transit, completed = _wls.collect_sku_orders(sku, token)
    except Exception as e:
        return {"ok": False, "error": f"erp_fetch_error: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        _wf0.get_erp_token = _orig
        _wls.get_erp_token = _orig
    in_t_qty = sum((o.get("qty") or 0) for o in in_transit)
    in_transit_out = []
    for o in in_transit[:15]:
        item = {
            "order_no": o["order_no"],
            "qty": o.get("qty"),
            "forwarder": o.get("logistics_name"),
            "tracking_no": o.get("tracking_no"),
            "delivery_at": o.get("delivery_at"),
            "tracking_url": _physical_tracking_url(o.get("logistics_name"), o.get("tracking_no")),
        }
        if with_nodes and o.get("tracking_no"):
            n = _fetch_logistics_nodes(o.get("logistics_name"), o.get("tracking_no"))
            item["nodes"] = n.get("nodes", [])
            item["current_node"] = n["nodes"][-1] if n.get("nodes") else None
            item["nodes_note"] = n.get("note", "")
        in_transit_out.append(item)
    return {
        "ok": True,
        "sku": sku,
        "fetched_from": ("ERP realtime + 物流站节点抓取" if with_nodes else "ERP realtime"),
        "in_transit_count": len(in_transit),
        "in_transit_total_qty": in_t_qty,
        "completed_count": len(completed),
        "in_transit_orders": in_transit_out,
        "recent_completed": [
            {"order_no": o["order_no"], "forwarder": o.get("logistics_name"),
             "delivery_at": o.get("delivery_at")}
            for o in completed[:5]
        ],
        "references": [
            {"table": "ERP /delivery (realtime)", "where": f"keyword={sku}",
             "as_of_date": "now"}
        ],
    }


def tool_query_order_live(order_no: str) -> Dict:
    """实时查单货单 ERP 状态 + 物流站直链。"""
    tid = _get_tenant()
    token, err = _erp_token_or_error(tid)
    if err:
        if err.get("error") == "erp_login_failed":
            return {"ok": False, "error": "erp_login_failed_no_cache",
                     "message": f"ERP 实时查失败（{err['message']}），单货单查询没缓存兜底。"
                                 "请用 query_sku_live(sku) 查整 SKU 的缓存。"}
        return err
    _wf0, _wls, _orig = _patch_wls_token(token)
    try:
        # 用 erp_get /delivery?keyword=order_no 找货单
        from workflows.wf0_logistics import erp_get
        data = erp_get("/delivery", {"keyword": order_no, "page": 1, "page_size": 20}, token)
        if isinstance(data, dict):
            dd = data.get("data") or []
            items = dd if isinstance(dd, list) else dd.get("list", [])
        else:
            items = []
    except Exception as e:
        _wf0.get_erp_token = _orig; _wls.get_erp_token = _orig
        return {"ok": False, "error": f"erp_fetch_error: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        _wf0.get_erp_token = _orig
        _wls.get_erp_token = _orig
    # 找精确匹配
    match = [o for o in items if (o.get("delivery_order_no") or "").upper() == order_no.upper()]
    if not match:
        return {"ok": False, "error": "order_not_found_in_erp", "order_no": order_no}
    o = match[0]
    forwarder = (o.get("logistics") or {}).get("logistics_name", "")
    tracking = o.get("logistics_bill_no", "")
    # 抓物流站节点（playwright 5-10s）
    nodes_result = _fetch_logistics_nodes(forwarder, tracking) if tracking else {"nodes": [], "note": "无单号"}
    return {
        "ok": True,
        "order_no": order_no,
        "fetched_from": "ERP realtime + 物流站节点抓取",
        "status": o.get("status"),
        "store": (o.get("store") or {}).get("name", ""),
        "forwarder": forwarder,
        "tracking_no": tracking,
        "tracking_url": _physical_tracking_url(forwarder, tracking),
        "delivery_at": (o.get("delivery_at") or "")[:10],
        "in_storage_at": (o.get("latest_in_storage_at") or "")[:10],
        "nodes": nodes_result.get("nodes", []),
        "nodes_note": nodes_result.get("note", ""),
        "current_node": nodes_result["nodes"][-1] if nodes_result.get("nodes") else None,
        "references": [
            {"table": "ERP /delivery + 物流站节点 (realtime)",
             "where": f"order={order_no} tracking={tracking}",
             "as_of_date": "now"}
        ],
    }


def _tool_tenant_notes_get(section: str = "") -> Dict:
    from . import tenant_notes
    tid = _get_tenant()
    content = tenant_notes.get_notes(tid, section or None)
    return {
        "tenant_id": tid,
        "section": section or "(全文)",
        "content": content or "(尚无 NOTES，可用 tenant_notes_append 沉淀)",
        "sections": tenant_notes.list_sections(tid),
    }


def _tool_tenant_notes_append(note: str, section: str = "通用") -> Dict:
    from . import tenant_notes
    tid = _get_tenant()
    return tenant_notes.append_note(tid, note, section)


def _tool_confirm_proposal(proposal_id: str, user_decision: str) -> Dict:
    """confirm_proposal tool 实现 — 用 chat scope 当前 user 验签 + 走 governance.confirm_proposal."""
    from . import governance as _gov
    sc = _chat_scope.get() or {}
    actor = {
        "user_id": sc.get("user_id"),
        "email": sc.get("current_user_email") or sc.get("current_user"),
        "role": sc.get("current_role"),
        "tenant_id": sc.get("tenant_id") or _get_tenant(),
        "source": "chat",
    }
    return _gov.confirm_proposal(proposal_id, user_decision, actor, TOOL_FUNCS)


def tool_capture_feedback(content: str, scene: str = "", category: str = "需求") -> Dict:
    """把撞限/超范围时用户确认的需求真写入 feedback 表（WS-26）。

    报告即事实：写不进库就如实返 ok=False + error，**绝不返一个假的成功**。
    并在写入后**回读一次**确认真落库（钉死占位假数据）。
    """
    from . import data as _d
    tid = _get_tenant()
    sc = _chat_scope.get() or {}
    if not content or not str(content).strip():
        return {"ok": False, "error": "empty_content",
                "message": "没有可记录的需求内容 —— 请把诉求说清楚我再记。"}
    cat = category or "需求"
    try:
        fid = _d.write_feedback(
            content,
            trigger_scene=scene or None,
            category=cat,
            user=sc.get("current_user") or sc.get("current_user_email"),
            role=sc.get("current_role"),
            store=sc.get("store"),
            tenant_id=tid,
        )
    except Exception as e:
        return {"ok": False,
                "error": f"feedback_write_failed: {type(e).__name__}: {str(e)[:200]}",
                "message": "没记成（写库失败），等会儿再跟我说一次这个需求。"}
    # 回读确认真落库 —— 不信 write 的返回值，亲自查一次
    saved = _d._fetch("SELECT id FROM feedback WHERE tenant_id=? AND id=?", (tid, fid))
    if not saved:
        return {"ok": False, "error": "feedback_not_persisted",
                "message": "写库后回查不到，判定没记成；请稍后重试。"}
    return {
        "ok": True,
        "feedback_id": fid,
        "category": cat,
        "message": f"已记成需求 #{fid}，产品会看到。",
        "references": [{"table": "feedback", "where": f"tenant_id={tid} AND id={fid}"}],
    }


def tool_explain_status_enum(field: str = "alert_status") -> Dict:
    """告诉用户某个枚举字段的取值出处 + 是否能扩展。
    Luke 多次问『5 个状态哪里来的 / 能加状态吗』，Agent 必须能说清此事。
    """
    if field in ("alert_status", "ops_status", "update_alert_status"):
        import yaml as _yaml
        yaml_path = os.path.join(os.path.dirname(__file__), "governance_actions.yaml")
        try:
            with open(yaml_path) as f:
                spec = _yaml.safe_load(f).get("update_alert_status", {})
        except Exception as e:
            return {"ok": False, "error": f"读 yaml 失败: {e}"}
        return {
            "ok": True,
            "field": "ops_status (wf6_logistics_alerts_v2 表的告警处理状态)",
            "current_allowed": spec.get("allowed_statuses", []),
            "source": "hipop 自己定义在 hipop/server/governance_actions.yaml:update_alert_status.allowed_statuses",
            "from_erp_api": False,
            "explanation": (
                "这些状态不是 ERP/dbuyerp 软件内置的，是 hipop 工作流自己的运营字段。"
                "DB 字段 ops_status 是 TEXT free text 类型（非 ENUM），技术上可任意扩展，"
                "只是 chat agent 调用时会按 yaml 白名单校验。"
            ),
            "how_to_add_new_status": (
                "加新状态需 2 处改动：\n"
                "  1) hipop/server/governance_actions.yaml — allowed_statuses 加一行\n"
                "  2) hipop/server/agent.py — update_alert_status schema enum 加一项\n"
                "重启 uvicorn 后立即生效，数据库无需 migration。\n"
                "如新状态需算告警关闭，再改 wf_logistics_alerts.py TERMINAL_STATUSES。"
            ),
            "references": [
                {"table": "wf6_logistics_alerts_v2.ops_status", "type": "TEXT"},
                {"file": "hipop/server/governance_actions.yaml", "key": "update_alert_status.allowed_statuses"},
                {"file": "hipop/server/agent.py", "key": "TOOLS[update_alert_status].input_schema.status.enum"},
            ],
        }
    return {
        "ok": False,
        "error": f"unknown field={field}",
        "supported_fields": ["alert_status"],
    }


# ── Tool 派发 ─────────────────────────────────────────
TOOL_FUNCS = {
    "query_sku": tool_query_sku,
    "query_order": tool_query_order,
    "update_alert_status": tool_update_alert_status,
    "scope_overview": tool_scope_overview,
    "compute_replenishment": tool_compute_replenishment,
    "compute_air_freight_roi": tool_compute_air_freight_roi,
    "data_health_check": tool_data_health_check,
    "list_products": tool_list_products,
    "export_table": tool_export_table,
    "navigate_user_to": tool_navigate_user_to,
    "notify_via_feishu": tool_notify_via_feishu,
    "run_workflow": tool_run_workflow,
    "confirm_proposal": lambda proposal_id, user_decision: _tool_confirm_proposal(proposal_id, user_decision),
    "tenant_notes_get": lambda section="": _tool_tenant_notes_get(section),
    "tenant_notes_append": lambda note, section="通用": _tool_tenant_notes_append(note, section),
    "query_sku_live": tool_query_sku_live,
    "query_order_live": tool_query_order_live,
    "query_1688_similar": tool_query_1688_similar,
    "explain_status_enum": tool_explain_status_enum,
    "capture_feedback": tool_capture_feedback,
}


def _exec_tool(name: str, args: dict, user: dict = None) -> dict:
    """tool 执行前先过 RBAC + governance pipeline。

    - RBAC: user.role → tool 入口权限
    - Governance (Phase 0.2 半 MSCL): destructive tool 走
      ActionProposal → Decision (Haiku) → ExecToken → Execute → ExecutionRecord
      read-only tool 跳过 governance，直调

    ⚠️ INVARIANT (2026-05-26)：
    所有 LLM tool 调用必须经此函数。provider 层（_provider_anthropic /
    _provider_openai）禁止自己实现 _exec_tool —— 历史上 5/21 把 _exec_tool
    复制到 provider 文件只做 RBAC，导致 destructive tool 全部裸跑（governance
    pipeline 形同虚设）。新增 provider 时：from . import agent; agent._exec_tool(...).
    smoke_governance.py 会跑 inspect.getsource 检查 provider 没自定义 _exec_tool。
    """
    try:
        from . import rbac as _rbac
        if user and not _rbac.tool_allowed(user, name):
            return {
                "error": "permission_denied",
                "tool": name,
                "user_role": user.get("role"),
                "message": f"当前角色 {user.get('role')} 不能调用 {name}（请向 owner/manager 申请权限）",
            }
        if name not in TOOL_FUNCS:
            return {"error": f"unknown tool: {name}"}
        # ── Governance pipeline（仅 destructive） ──
        from . import governance as _gov
        if _gov.is_destructive(name):
            sc = _chat_scope.get() or {}
            actor = {
                "user_id": (user or {}).get("id") or sc.get("user_id"),
                "email": (user or {}).get("email") or sc.get("current_user_email"),
                "role": (user or {}).get("role") or sc.get("current_role"),
                "tenant_id": (user or {}).get("tenant_id") or sc.get("tenant_id") or _get_tenant(),
                "source": sc.get("source") or "chat",
            }
            return _gov.propose_and_execute(name, args, actor, sc, TOOL_FUNCS)
        # read-only 直调
        fn = TOOL_FUNCS[name]
        return fn(**args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── Chat 主入口 ──────────────────────────────────────
SYSTEM_PROMPT_LEGACY = """你是点购 Agent OS 的店铺协作 Agent，工作在共同空间内（5 个同事 + 1 个你）。

三大原则:
1. 共同工作空间：你和运营/跟单/组长在同一个 chat 里，所有结论和决策可被同事看到、采纳、回滚。
2. 任务泛化：用户可以问任何问题，你应当首选用工具拿到真实数据，再回答。
3. 自主决策：你应该主动给出判断 + 建议（不只是数字），并提供数据出处。

**用户原则**: 用户不应该在终端跑任何脚本。所有数据更新/查询都通过 chat 工具完成。
- 用户问问题 → 你先看现有数据是否够答
- 数据不够新 → 你自动 run_workflow 触发更新（带 followup_prompt 等跑完接续答），不要让用户去跑
- 数据够新 → 直接答 + 给数据出处

当前 scope（参考）:
{scope}

## 问题 → 工具映射（必须遵守）

| 用户问 | 你调的 tool |
|---|---|
| 我该补货吗 / 哪些货要补 / 补多少 / 本周必补 | compute_replenishment |
| <SKU> 卖得怎么样 / 库存够不够 / 趋势 | query_sku |
| 单 <SKU> 海空运怎么选 / 海运合算还是空运 | compute_air_freight_roi |
| 这单 <PDxxx> 怎么样 / 卡了几天 / 到哪了 | query_order |
| <PDxxx> 已确认丢货 / 已约仓 / 已结案 | update_alert_status |
| 店铺总共多少商品 / 多少 SKU / 多少未上架 | list_products |
| 店铺整体怎么样 / 概览 / 红色告警 | scope_overview |
| 数据是什么时候更新的 / 数据新鲜吗 | data_health_check |
| 跑一下 / 刷新 / 重算 X | run_workflow |
| **导出 / 下载 / 给我 Excel / 给我表格** | **export_table**（必调，不要自己编"已生成 Excel"）|
| **打开 X 页面 / 进 X 模块 / 看 X 看板** | **navigate_user_to**（必调，禁编虚构 URL）|
| **发飞书 / 通知刘鹤 / 推到群里 / @同事** | **notify_via_feishu**（必调，禁说"已发到飞书"）|

## 数据新鲜度自动判断（**所有问题**都遵守这个流程）

**核心**：每个用户问题都有上游依赖。回答之前先确认**所有依赖源**新鲜，不能只看终端表（如 wf5）。

### ⚠️ 强制规则（避免 hallucinate — 这些是会被运营当面骂"骗子"的事故）

**1. 任何业务数据回答前，必须先调 `data_health_check` 拿真实新鲜度**
   - 不要凭空猜"noon 销量是 X 天前"——必须从 tool 返回读
   - 不要假设字段值——所有数字/日期/SKU id 都要来自 tool 返回
   - 不要抄本 SYSTEM_PROMPT 的举例数字（举例里的"5 月 4 日"、"3 天前"全是占位符示意）

**2. 严禁宣称做了"未真正做"的事**
   - "✅ 已触发导出/同步/刷新/通知" → 必须真有对应 tool 调用并返回成功才能这么说
   - "已为你生成 Excel/链接" → **本系统没有 Excel 导出功能**，禁止编造
   - "已为你打开 X 页面" → 你不能打开页面，只能告诉用户"在工作台 sidebar 找 X 模块"
   - "我刚调用了 X 工具" → 当且仅当本轮真的调用了，否则不要写

**3. 严禁编造 URL / 域名 / 页面元素 / UI 按钮 / UI 操作路径**
   - 不要编 `https://agent.diangou.ai/...` 这种**虚构域名**
   - 不要描述前端不存在的 UI（"顶部 Tab 高亮"、"右上角导出按钮已激活"、"行末 🔍 按钮"）
   - **严禁编"在 sidebar/侧边栏 找到 X 按钮 → 点 Y"** —— sidebar 真实菜单只有：今日总览 / 数据获取 / 销售-库存 / 在途物流 / 补货决策 / 流量推广 / 选品+货源 / 营销活动 / 飞书沉淀 / 数据巡检 / 跟单跨店 + 系统块（Agent 操作记录 / 策略沉淀 / 数据刷新）。**绝不要描述"侧边栏的某某子菜单/路径/选项"，因为模型对实际 DOM 的猜测 80% 是错的**
   - 工作台真实的模块只有：overview / sales / logistics / replenish / selection / feishu / audit + role/liuhe，路径都是 localhost:8765/module/<name>
   - 真有的入口才能引导用户去；不确定就让用户"sidebar 看一下"

**3b. 用户问"刷新 / 跑工作流 / 同步数据 / 重算 X / 扫 ERP / 拉数据"时，必须本轮**真的**调 `run_workflow`，禁止只口头描述**
   - 你**有** run_workflow tool，能直接触发后台跑（前端会自动订 SSE 显进度）
   - "扫 / 拉 / 同步 / 刷新 / 重算 / 跑一下" 都是同一类动词 —— 必须 run_workflow，不能假装"再次触发"
   - **死规矩**：本轮你说出"已触发 / 已启动 / 已开始 / 再次触发 / 系统已经在后台跑了" 等表述 ⇔ 本轮 tool_use 块里必须有 `run_workflow` 实际调用。两者必须同时为真；只说不调 = 撒谎 = 事故
   - 用户连发两次同一指令时，**不要**假设"上次已触发了"（你不知道上次有没有真触发）—— 重新调 run_workflow 一次，最多重复了一次，比让用户以为任务在跑实际没跑要好
   - 禁说"这个需要组长/管理员账号才能触发" / "我没有权限" / "Agent 当前没有权限" —— 你已经被赋予 run_workflow，能跑就跑；只有 tool 真返回 `permission_denied` 才能这么回
   - 禁说"在工作台 sidebar 找到 X → 点 Y" —— 这种 UI 路径几乎必编错；直接 run_workflow 就对了

**3c. destructive tool 返回 action_type='plan' 时 — Explore→Plan→Implement 三段**
   - 高风险 destructive（update_alert_status 改物流告警 / 等）走治理 pipeline，第一次调返
     一个 dict 含 action_type='plan' + plan_text + proposal_id 字段
   - 你必须**原文转告 plan_text 给用户**，让用户回 OK / 不要 / 改
   - 用户回 "OK / 是 / 确认" → 本轮必须调 `confirm_proposal(proposal_id=..., user_decision='ok')`
   - 用户回 "不要 / 取消 / no" → 调 `confirm_proposal(proposal_id=..., user_decision='cancel')`
   - **绝不要**自己再次调原 destructive tool（governance 会拒）

**4. 用户报告状态变化时（如"我刷新了"、"我上传了"），必须重新调 tool 验证**
   - 不要直接信用户的报告就回"已确认更新"
   - 调 data_health_check / get_data_health 看真实 stale_days
   - 如果用户说更新了但实际没更新，要明确告诉用户"我看到的还是 X 天前，可能你的上传还没 ingest 完，或者文件没识别出来"

**5. 时间戳精度禁忌**
   - data_health_check 返回的日期都是 `YYYY-MM-DD` 粒度，**没有时分秒**
   - 严禁编造 `14:22:07Z` / UTC 偏移 / 沙特时间换算这种伪精确时间戳
   - 如果工具返回 `2026-05-05`，你只能说"5 月 5 日"，不能扩展成"2026-05-05T14:22:07Z UTC（沙特时间 17:22）"

**6. 表格字段必须用真实存在的列**
   - 现有 wf5 字段：`partner_sku / trend / daily_rate / urgency / weekly_total_replenish / current_pipeline / target_pipeline / ops_advice / risk_label / sellable_days / decision_days`
   - 现有 wf2 字段：`partner_sku / title / sales_10d / sales_30d / sales_60d / sales_90d / sales_180d / latest_price / latest_profit_rate / is_listed / sales_grade`
   - **严禁这些不存在的中文字段名**（已是反复事故源）：
     - ❌ "可撑天数" → 用 `sellable_days`（数据库真名）
     - ❌ "7 天销量" → 用 `sales_10d`（最近的真实窗口；没有 7 天）
     - ❌ "海运 ROI 预估" / "空运 ROI 预估" / "推荐物流方式" → 这些只能在调用了 `compute_air_freight_roi` 工具后才能引用
     - ❌ "可售周期" / "周转天数" / 任何 wf5 字段表里没有的中文名 → 不要用
   - 想要的字段如果工具返回里没有，直接说"这个字段我们目前不算"而不是编一个数

### 流程

1. **识别意图**（intent）：把用户问题映射到一种 intent
2. **拿依赖源**：调 `data_health_check` → `dependency_groups[intent]` → 列出该意图依赖的所有源
3. **检查每个源**：用 tool 返回的 `sources[<source>].stale_days` 和 `automation`
4. **行动**：
   - 全新鲜（< stale_threshold_days）→ 调对应查询 tool 直接答
   - **automation=auto 陈旧** → run_workflow(对应 workflow) + followup_prompt（用户原始问题）
   - **automation=needs_csv 陈旧** → **不要** run_workflow，给精确上传指引（path + csv_pattern），引导用户上传到工作台 📤 区
   - 混合 → 先列上传引导（needs_csv 部分），auto 部分一并 run_workflow

### 意图 → 依赖源 + 推荐 tool（必背）

| 用户说 | intent | 依赖源 | 数据齐了调什么 tool |
|---|---|---|---|
| 我该补货吗 / 哪些要补 / 补多少 / 本周必补 | `replenishment` | erp_sales + erp_stock + noon_orders + noon_stock + wf3_logistics + wf5_replenish | compute_replenishment |
| `<SKU>` 卖得怎么样 / 趋势 / 库存够不够 | `sku_health` | erp_sales + noon_orders + wf3_logistics + wf5_replenish | query_sku |
| 在途 / 物流追踪 / 货到哪了 | `logistics_track` | wf3_logistics | query_order 或 scope_overview |
| 告警 / 卡单 / 红色货单 / `<PDxxx>` | `alerts` | wf3_logistics + wf6_alerts | query_order |
| 单 SKU 海运空运怎么选 | `air_freight_roi` | erp_sales + noon_orders + wf5_replenish | compute_air_freight_roi |
| 店铺总共多少商品 / SKU 数 / 未上架 | `products_count` | erp_products | list_products |
| 店铺整体怎么样 / 概览 | `overview` | erp_sales + wf3_logistics + wf5_replenish + wf6_alerts | scope_overview |
| 销量 X 天卖了多少 | `sales_only` | erp_sales + noon_orders | query_sku 或 list_products |
| 库存够不够 / 还能撑几天 | `stock` | erp_stock + noon_stock | query_sku |
| 数据新鲜吗 / 什么时候更新的 | （直接答） | — | data_health_check |
| 跑/刷新/重算 X | （直接触发） | — | run_workflow |
| `<PDxxx>` 已确认丢货 / 已结案 | （写入） | — | update_alert_status（要确认意图） |

### 上传引导话术（needs_csv 陈旧时）

不要泛泛说"去上传 CSV"，要给精确指引（来自工具返回的 `sources[<src>].where` + `csv_pattern`）。

**模板（具体数字必须从工具返回值里取，不要凭空编造日期或数字）**：

> 你 [STORE] 的 [源中文名] 是 [N] 天前的（最新到 [日期]），我不能自动刷新这部分。
>
> 👉 请操作：
> 1. [where 字段的导出路径]，文件名形如 `[csv_pattern]`
> 2. 拖到工作台**顶部 📤 上传区**
>
> 上传完会自动 ingest + 重算，跑完我会接着告诉你『[用户原始问题]』的最终答案。

### 多个 needs_csv 源都陈旧时

合并指引（一次告诉用户全部要传的 CSV），不要分多次。

### 混合陈旧（auto + needs_csv）

例：用户问补货，noon_orders 陈旧 + erp_stock 陈旧。
- 告诉用户上传 noon CSV（needs_csv 部分要人工）
- 提一句"ERP 库存我会在你上传后顺便刷新"
- **不要先 run_workflow(wf1_stock)**，因为最终 wf5 还要等 noon 数据来才能正确算，单独跑 wf1 是浪费

### 已经触发后

run_workflow 调完后**不要**再 query 数据。前端会在跑完自动重发用户原始问题（followup_prompt），那时再用最新数据答。

### 用户坚持用旧数据时（关键场景）

用户可能不想等更新，要立刻拿当下数据。识别信号：
- 直接说："就用现在的" / "不用更新" / "先看看" / "凑合给个" / "粗略估" / "我现在就要"
- 拒绝上传 / 拒绝跑 workflow：在你给完上传指引或触发建议后，用户重复问同样问题或说"先告诉我"
- 上下文暗示赶时间："5 分钟后开会，告诉我"

**这种情况你应该**:
1. **不要**坚持要求更新，直接用旧数据答
2. **必须明确警示**：在答案开头一句话告诉用户具体哪些源陈旧多少天，结论可能因此偏向哪个方向（如"noon 销量数据是 4 天前的，最近一周的爆款会被低估，结论偏保守"）
3. 调对应查询 tool（compute_replenishment / query_sku / 等），照常给数据 + 出处
4. **结尾**附一句"如要更准的结论，跟我说『刷新数据』或上传最新 CSV"

**陈旧偏向参考**（用来给警示）:
- noon_orders 陈旧 → 漏掉最近订单 → 销量低估、利润率以历史为准、新爆款看不到
- noon_stock 陈旧 → 平台库存可能更紧张/更宽松 → 库存可撑天数有偏差
- erp_stock 陈旧 → 国内仓和海外仓库存数有偏差
- wf3_logistics 陈旧 → 在途到货时间预估不准
- wf5_replenish 陈旧 → 补货建议是上次跑的快照（如果 wf2/wf1/wf3 都新但 wf5 旧，可以 run_workflow(wf5_sales_cycle) 快速重算，不需要等）

**例子**：
- 用户："不用上传 CSV 了，就用现在的告诉我哪些要补"
- 你："好的。⚠️ noon 销量是 4 天前数据，结论偏保守（最近一周的爆款会被低估）。
       基于现有数据：本周必补 X 个 SKU... [给数据] 📎
       如需更准结论，上传最新 sales_noon_*_KSA_*.csv 后重问。"

## 回答风格

- 中文，简洁，2-4 句一段，不要罗列冗长字段
- 给判断（趋势 / 紧迫度）+ 简明建议（量化、可执行）
- 不知道时直说，不要瞎编
- 涉及写入（update_alert_status）需要用户确认意图后再调用
- run_workflow 不需要二次确认（页面有进度条），直接调
- 触发 run_workflow 后**不要再 query 数据**，等 followup_prompt 自动接续

## 输出风格 — 思考过程的"业务化简化"

**鼓励一句话的业务进度提示**（让用户感知 Agent 在做事）：
- ✅ "我先看看店铺整体情况"
- ✅ "我来查一下补货建议"
- ✅ "稍等，我对一下数据"

**绝不要暴露技术细节/内部字段名**：
- ❌ "这个问题属于 replenishment 意图"
- ❌ "依赖源 noon_orders.stale_days=3，automation=needs_csv"
- ❌ "我调用 data_health_check / compute_replenishment tool"
- ❌ "首先 X，然后 Y，接下来 Z" 的多步骤罗列

**陈旧警示也用业务语言**：
- ✅ "noon 销量是 4 天前，结论偏保守"
- ❌ "noon_orders source stale_days=4，automation=needs_csv"

## 例子对照

❌ 错（技术细节满天飞）：
> 这个问题属于 replenishment 意图，依赖 6 个源。
> 我先调 data_health_check 检查 stale_days...
> 看到 noon_orders.automation=needs_csv，stale_days=3，需要上传。

✅ 对（一句业务进度 + 直接结论）：
> 我来看看你的补货情况。
> noon 销量是 3 天前的，我不能自动刷新这部分。
> 👉 请到紫鸟 noon 后台 sales 页面 export 最近 180 天 CSV，拖到工作台 📤 上传区。
> 上传后我会接着告诉你哪些要补。
"""


# Phase 0.3 Context Engineering — SYSTEM_PROMPT 砍到 ~1500 token（4188→1500，节省 65%）
# 按 Anthropic Claude Code Best Practices："ruthlessly prune"、"too long → ignored"
# 多数细则已经被结构性约束做了（_safety hook / governance pipeline / smoke），
# prompt 只留 minimal essentials.
SYSTEM_PROMPT = """你是点购 Agent OS 的店铺协作 Agent。

scope: {scope}

## 工作流
1. 业务问题先调 data_health_check 看新鲜度 → 再调对应查询 tool 答
2. 数据陈旧 → run_workflow（auto 类）/ 给上传指引（noon CSV 类）
3. destructive tool 返回 action_type='plan' → 原文转告 plan_text 让用户回 OK → 调 confirm_proposal(pid,'ok')

## 关键：用户问"某 SKU/某货单当前在途 / 物流状态"时，**直接调 query_sku_live / query_order_live**
- 不要先 data_health_check 然后说"wf3 陈旧，等 ingest 完再答"——这是错的，应该跳过 wf3 缓存直接查 ERP
- 不要说"我可以查"然后不调 tool —— 必须本轮真调 query_sku_live(sku=...)
- 用户问多个 SKU → 对每个分别调 query_sku_live
- query_sku_live 慢（5-15s）但准（直连 ERP），值得
- query_sku_live 返回里有 `stale_warn` 或 `live_query_failed_reason` 时：必须明告用户「ERP 实时拉失败，给你的是 wf3 缓存（更新于 X），稍后可重试」，**不许**当作实时数据呈现

## tool 速查
| 用户问 | 调 |
|---|---|
| <SKU> 卖得 / 库存 / 趋势 | query_sku |
| <SKU> 当前在途多少 / 实时物流 / wf3 陈旧时 | **query_sku_live**（实时 ERP，5-15s）|
| <PDxxx> 状态 / 卡几天（hipop 缓存）| query_order |
| <PDxxx> 现在到哪了 / 实时状态 / 物流码 | **query_order_live**（实时 ERP）|
| <PDxxx> 已确认丢货/已结案 | update_alert_status |
| 我该补货吗 / 必补哪些 | compute_replenishment |
| 海运空运 ROI | compute_air_freight_roi |
| 店铺概览 / 红色告警 | scope_overview |
| 商品总数 / SKU 数 | list_products |
| 数据新鲜吗 | data_health_check |
| 跑/刷新/扫/拉/重算/同步 | run_workflow |
| 导出/下载/Excel/打成表格 | **export_table**（真生成 xlsx，禁说"系统只能返 N 个示例"——filtered_count 才是真总数；返完用 [文件名](download_url) markdown 给用户）|
| 状态/字段哪来 / 5 个状态出处 / 能加 X 状态吗 / 是 ERP 字段还是 hipop 字段 | **explain_status_enum**（不要凭空说"系统写死"，必须真调拿 source 引用）|
| 打开 X 页面 | navigate_user_to（禁编 URL）|
| 发飞书 / 通知群 | notify_via_feishu（禁说"已发"）|
| 撞到你做不了/超范围 → 用户回"记一下/提个需求/帮我记" | **capture_feedback**（真写 feedback 表；禁不调就说"已记下"；写失败如实报"没记成"）|

## 撞限即捕获需求（WS-26）
- 你回"做不到/超出范围"时，顺带 offer 一句"要我记成需求吗"（系统也会兜底补这句）。
- 用户确认（记一下/好/提个需求）→ 本轮必须调 capture_feedback(content=用户诉求原话)。
- capture_feedback 返 ok=False → 如实说"没记成，等会儿再说一次"，**绝不**假装记了。

## 死规矩（违反 = 事故）
1. **业务数据先调 data_health_check**，不要凭空猜"X 天前更新"
2. **禁说"已触发/启动/导出/发飞书"除非本轮真调了对应 tool**（_safety 后处理会拦你撒谎）
3. **禁说"之前触发的任务还没跑完 / 等 X 分钟 ingest 完 / 任务还在跑"** — 这是新型撒谎模式：用过去时绕开 hook 检测。**真要知道有没有任务在跑，调 data_health_check 看 stale_days，没有"还在跑"这种中间态。wf3 陈旧 → 用 query_sku_live / query_order_live 实时查 ERP（不要等 wf3 跑完）**
4. **禁编 URL / 字段名 / SKU id / 时间戳** — 数字必须来自 tool 返回
5. **用户报告状态变化（"我刷新了"/"我传了"）必须重新调 tool 验证**，不信用户报告
6. **真实字段**：wf2_sku（partner_sku/sales_*d/latest_profit_rate/is_listed/sales_grade）/ wf5（trend/daily_rate/urgency/sellable_days/weekly_total_replenish）/ wf3_hub_v2（in_transit_total_qty/has_stuck_batch）— 不在此列禁编
7. **destructive 不一步走完**：update_alert_status / run_workflow 返 plan → 原文转告 plan_text → 等用户回 OK → 调 confirm_proposal(pid, 'ok')。**绝不**自己再调原 destructive tool

## 长期偏好沉淀
- 用户明确说"以后都这么办" / "记住" / "默认 X" → 调 tenant_notes_append
- 高风险决策前可调 tenant_notes_get 看客户既定偏好（按需，不每次都拉）

## 回答风格
- 中文 2-4 句一段，给结论 + 简明建议
- 一句进度 OK（"我先看看"），不暴露技术细节（"调用 X tool"）
- run_workflow 后不再 query，等 followup_prompt 自动续

## 数据陈旧场景
- 用户说"就用现在的 / 不用更新" → 直接答 + 开头一句警示（"noon 销量是 X 天前，结论偏保守"）+ 末尾一句"如要更准，跟我说刷新"
- noon CSV 陈旧 → 给精确路径 + 拖工作台 📤 区（不是 run_workflow）
- ERP 陈旧 → run_workflow + followup_prompt 自动续答
"""


_JUDGE_SYSTEM_PROMPT = (
    "你是 Agent 回复质量评判官。基于用户问题、Agent 回复、调用的工具、系统检测到的幻觉信号，"
    "判断这个回复是否真回答了问题、有无编造、引用是否支撑结论。\n"
    "严格只返回 JSON（不要任何其他文字）：{\"confidence\": 0~1 浮点, \"verdict\": \"一句话评判\"}。\n"
    "工具调用多、有数据引用、无幻觉信号 → 高置信(0.8+)；凭空作答、有幻觉信号 → 低置信(0.4-)。"
)


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
            out.append({**m, "content": _strip_safety_banner(m["content"])})
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


def _deterministic_workflow_request(question: str) -> Optional[Dict[str, str]]:
    q = (question or "").lower()
    if any(x in q for x in ("不用刷新", "不要刷新", "无需刷新", "不用上传 不用刷新")):
        return None
    if not any(v in q for v in ("刷新", "同步", "重算", "跑一下", "拉一下", "扫", "刷一下")):
        return None
    if "物流" in q:
        return {"workflow": "wf3_logistics_v2", "label": "物流刷新"}
    if "库存" in q:
        return {"workflow": "wf1_stock_v2", "label": "库存刷新"}
    return None


def chat(messages: List[Dict], scope: Dict) -> Dict:
    """
    messages: [{role: 'user'|'assistant', content: '...'}]
    scope: {store, current_user, current_role, tenant_id, user_id, ...}
    返回: {reply, clean_reply, references, action_id, tag, workflow_task, tools_used, provider, confidence}

    走 _provider 抽象层，通过 LLM_PROVIDER env 切换 anthropic / qwen / deepseek / doubao。
    """
    from . import _provider

    # 把 scope.tenant_id 注入 contextvars，让所有 tool 函数（同线程）能拿到
    _chat_tenant.set(scope.get("tenant_id") or 1)
    _chat_scope.set(scope)
    _last_replenishment_stock_status.set(None)
    # 同时设给 data 层（PG RLS 用）
    _data.set_current_tenant(scope.get("tenant_id") or 1)

    question = messages[-1].get("content") if messages else ""
    if isinstance(question, list):  # content 可能是 blocks
        question = " ".join(b.get("text", "") for b in question if isinstance(b, dict))
    direct_workflow = _deterministic_workflow_request(question)
    if direct_workflow:
        tool_args = {"workflow": direct_workflow["workflow"], "followup_prompt": question}
        tool_result = _exec_tool("run_workflow", tool_args, user=scope)
        workflow_task = None
        if isinstance(tool_result, dict) and tool_result.get("ok"):
            workflow_task = {
                "task_id": tool_result["task_id"],
                "workflow": tool_result["workflow"],
                "label": tool_result["label"],
                "total_steps": tool_result["total_steps"],
                "affected_modules": tool_result["affected_modules"],
                "followup_prompt": tool_result.get("followup_prompt"),
            }
            reply = (
                f"已触发{direct_workflow['label']}（{direct_workflow['workflow']}）。"
                "跑完后我会按你的原问题继续回答。"
            )
        else:
            reply = (tool_result or {}).get("message") or (tool_result or {}).get("error") or "工作流触发失败。"
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["run_workflow"],
            "tag": "执行",
            "workflow_task": workflow_task,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_workflow_router",
            "hallucination_warnings": None,
        }

    sys_text = SYSTEM_PROMPT.format(scope=json.dumps(scope, ensure_ascii=False))
    result = _provider.chat_with_tools(
        messages=_clean_history(messages),   # 清掉历史里残留的 banner，断自激
        system=sys_text,
        tools=TOOLS,
        tool_funcs=TOOL_FUNCS,
        scope=scope,
    )
    clean_reply  = result["reply"]           # LLM 原文（无 banner）— 用于持久化 + 喂未来历史
    tool_log     = result["tool_log"]
    refs_collected = result["refs_collected"]
    workflow_task  = result.get("workflow_task")
    tools_used     = [t["name"] for t in tool_log]

    # Layer 3 hallucinate 后处理（上移自 api.py — 一处产生 warnings，既喂 confidence 又 sanitize）
    # final_text = 展示版（可能带 banner）；clean_reply = 持久化版（无 banner，防历史自激）
    from . import _safety
    final_text, hallu_warnings = _safety.sanitize_reply(clean_reply, tools_used, tool_log=tool_log)
    final_text = _maybe_append_stock_readiness_warning(final_text)
    clean_reply = _maybe_append_stock_readiness_warning(clean_reply)

    # judge + confidence 真逻辑（混合：启发式 + 低置信/destructive 触发 LLM judge）
    judge, confidence, judge_method = _compute_judge_confidence(
        question, final_text, tool_log, refs_collected, hallu_warnings)

    # 低置信自动在 reply 头部加提示（_safety 已加 banner 时不重复，避免双 banner）
    if confidence < 0.6 and not hallu_warnings:
        final_text = (
            f"⚠️ 我对这个回答的置信度较低（{int(confidence*100)}%），"
            "建议你核实关键数字，或换个更明确的问法。\n\n---\n\n"
        ) + final_text

    # WS-26: 撞限（做不到/超范围）回复确定性补一句『要记成需求吗』offer。
    # display 版 + 持久化版都补，保证下一轮用户回『记一下』时对话连贯。
    final_text  = _maybe_append_feedback_offer(final_text, tools_used)
    clean_reply = _maybe_append_feedback_offer(clean_reply, tools_used)

    # 写入 agent_actions（reference 系统）
    action_id = None
    if final_text and (refs_collected or tool_log):
        try:
            action_id = _data.write_agent_action(
                store=scope.get("store", "KSA"),
                module="chat",
                action_type="execute",
                subject=tool_log[0]["args"].get("sku") or tool_log[0]["args"].get("order_no") if tool_log else None,
                judge=judge,
                pill_text="执行" if _safety._is_substantive_action(tool_log) else ("查询" if tool_log else "信息"),
                pill="info",
                confidence=confidence,
                options=[],
                references=_dedup_refs(refs_collected),
                owner=scope.get("current_user", "Cherry"),
            )
        except Exception:
            pass

    return {
        "reply": final_text.strip() or "(无回复)",          # 展示版（带 banner）给前端当场看
        "clean_reply": (clean_reply or "").strip() or "(无回复)",  # 无 banner 版给持久化，防历史自激
        "references": _dedup_refs(refs_collected),
        "action_id": action_id,
        "tools_used": tools_used,
        "tag": ("hallucinate" if hallu_warnings else ("执行" if _safety._is_substantive_action(tool_log) else ("查询" if tool_log else None))),
        "workflow_task": workflow_task,
        "provider": _provider.get_provider(),
        "confidence": round(confidence, 2),
        "judge_method": judge_method,
        "hallucination_warnings": hallu_warnings or None,
    }


def _dedup_refs(refs: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for r in refs:
        k = (r.get("table"), r.get("where"))
        if k in seen: continue
        seen.add(k)
        out.append(r)
    return out

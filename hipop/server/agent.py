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
import os, sys, json, sqlite3, time, contextvars, re
from typing import List, Dict, Any, Optional

# ── chat 当前请求 context（tenant_id + scope）─────────────
# 由 chat() 入口 set，所有 tool 函数同线程读
_chat_tenant: contextvars.ContextVar[int] = contextvars.ContextVar("chat_tenant", default=1)
_chat_scope: contextvars.ContextVar[dict] = contextvars.ContextVar("chat_scope", default={})
_chat_question: contextvars.ContextVar[str] = contextvars.ContextVar("chat_question", default="")
_last_replenishment_stock_status: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "last_replenishment_stock_status", default=None
)
_last_sku_rate_stats: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "last_sku_rate_stats", default=None
)
# WS-145 肯定执行意图门:chat() 入口按本轮句式语气求出的门决策。
# _exec_tool 据此拒绝「非执行语气下偷偷 run_workflow」（LLM 不许绕）。
_chat_intent: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "chat_intent", default=None
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
from ._workflow_reply import _workflow_receipt_reply

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
        "name": "query_replenishment_sku",
        "description": (
            "查单个 SKU 的本周补货建议与可追溯证据。用于用户问某 SKU 的补货建议、"
            "pipeline、风险标签、紧急度。若缓存/聚合证据缺失、全 0、过期或与上架状态矛盾，"
            "必须使用实时/权威源；实时源失败时返回 blocked，不得把缓存 0 当作结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "SKU 码，如 TBU0010A"},
                "store": {"type": "string", "enum": ["KSA", "UAE"], "default": "KSA"},
            },
            "required": ["sku"],
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
            "用户问『近30天销量最高/销量排行/销量 TopN』时必须调用本工具并设置 limit=N；"
            "items 已按 wf2_sku.sales_30d DESC、sales_180d DESC 排序，limit=N 即近30天销量 TopN，"
            "不要由模型自行排序或补数字。"
            "is_listed=1 = 已绑定 noon 平台 SKU id（在线上能搜到/可下单）；is_listed=0 = 草稿/未挂平台。"
            "listing='listed'/'unlisted'/'all' 控制示例返回；sales_only=true 仅含 sales_180d>0；limit=0 时不返示例。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["KSA", "UAE"]},
                "listing": {"type": "string", "enum": ["all", "listed", "unlisted"], "default": "all"},
                "sales_only": {"type": "boolean", "default": False, "description": "true=仅含 180 天内有销量"},
                "limit": {
                    "type": "integer",
                    "default": 0,
                    "description": "返回示例 SKU 行数；0=只要聚合。问近30天销量 TopN 时传 N，items 即按 sales_30d DESC 的 TopN。",
                },
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
    {
        "name": "query_stock_split",
        "description": (
            "查单 SKU 四仓库存拆分 + 总量 + 来源时间戳。"
            "四仓：义乌(yiwu) / 沙特一号仓(overseas_saudi_1) / noon仓 / 在途(inbound)。"
            "数据超过 3 天 fail-closed 不出数；≤3天带警告提示用户确认。"
            "触发词：库存拆分 / 四仓 / 总库存 / yiwu / saudi / 义乌仓 / 沙特仓 / noon仓库存 / "
            "仓库明细 / 各仓 / <SKU>库存多少。"
            "单 SKU 询问（必须有 SKU 代码），不含 TopN 排行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "SKU 代码，如 TBP0169A"},
                "store": {"type": "string", "enum": ["KSA", "UAE"], "default": "KSA"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "total_stock_topn",
        "description": (
            "查 KSA/UAE 当前总库存（total_stock）最高的前 N 个 SKU。"
            "total_stock = 官方仓(noon) + 海外仓 + 国内仓(义乌+东莞) + 送仓未上架(pending_inbound)，"
            "口径含 pending_inbound，与 noon 可售(saleable) 不同。"
            "数据陈旧（>3天）时 fail-closed 不出数。"
            "触发词：总库存最高 / 库存最多的 SKU / 积压最多 / 库存 TopN / 当前库存量排行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["KSA", "UAE"], "default": "KSA"},
                "n": {"type": "integer", "default": 10,
                      "description": "返回前 N 个 SKU，默认 10"},
            },
            "required": [],
        },
    },
]


# ── 工具实现（v2 列存：按 tenant_id + entity_alias 过滤）──

# T03 门：SKU 销量问答必须走实时取数路径，不能以 wf2_sku 快照 as_of_date 是否新鲜为由绕过。
# injectable：测试注入 mock fn；生产使用 _erp_sku_stats_live 默认实现。
# 签名：(sku: str, nation_id: int, token: str | None) -> dict
#   ok=True:  {"sales_30d": int|None, "history_total": int|None, "fetched_at": str, "source": str}
#   ok=False: {"error": str, "message": str}
_sku_sales_live_fn: Optional[Any] = None


def _erp_sku_stats_live(sku: str, nation_id: int, token) -> dict:
    """ERP /product-order-statistics 实时拉单 SKU 30d 销量。token=None 时直接返回不可用。"""
    if token is None:
        return {"ok": False, "error": "no_erp_token",
                "message": "ERP 凭据不可用（未配置或登录失败），无法实时确认 SKU 销量"}
    import datetime as _dt
    import re as _re
    try:
        from workflows.wf0_logistics import erp_get as _erp_get
    except ImportError:
        return {"ok": False, "error": "erp_import_error",
                "message": "无法加载 ERP 模块"}
    today = _dt.date.today()
    start = today - _dt.timedelta(days=30)

    def _fmt(d):
        return f"{d.year}-{d.month}-{d.day}"

    params = {
        "nation_id": nation_id,
        "platform_id": 2,
        "keyword": sku,
        "keyword_type": 1,
        "ordered_time_section[]": [_fmt(start), _fmt(today)],
        "page": 1,
        "limit": 50,
    }
    try:
        resp = _erp_get("/product-order-statistics", params, token)
    except Exception as e:
        return {"ok": False, "error": f"erp_fetch: {type(e).__name__}",
                "message": str(e)[:200]}
    if resp.get("code") != 200:
        return {"ok": False, "error": "erp_api_error",
                "message": str(resp.get("msg") or "")[:200]}
    items = resp.get("data") or []
    sku_up = sku.upper()
    match = None
    for it in items:
        sku_obj = it.get("sku") or {}
        for psk in (sku_obj.get("platform_sku_ids") or []):
            if (psk.get("platform_sku_id") or "").upper() == sku_up:
                match = it
                break
        if not match and (it.get("sku_id") or "").upper() == sku_up:
            match = it
        if match:
            break
    if not match:
        return {"ok": False, "error": "sku_not_found_in_erp",
                "message": f"SKU {sku} 在 ERP 30d 窗口内无记录（API 可能不支持 keyword 过滤或该 SKU 无近期订单）"}

    _NATION_TO_COUNTRY = {1: "SA", 2: "AE"}
    country_code = _NATION_TO_COUNTRY.get(nation_id)
    if not country_code:
        return {"ok": False, "error": f"unknown_nation_id_{nation_id}",
                "message": f"nation_id={nation_id} 未知，无法确定目标国家前缀，拒绝返回其他国家数字"}

    def _parse_country(val, country):
        pat = _re.compile(rf"{country}\s*[:：]\s*(\d+)")
        for s in (val if isinstance(val, list) else [str(val or "")]):
            m = pat.match(str(s).strip())
            if m:
                return int(m.group(1))
        return None

    sales_30d = _parse_country(match.get("sales_count"), country_code)
    fetched_at = _dt.datetime.utcnow().isoformat() + "Z"
    return {
        "ok": True,
        "sku": sku,
        "sales_30d": sales_30d,
        "history_total": None,
        "fetched_at": fetched_at,
        "source": "ERP /product-order-statistics (realtime)",
        "window_days": 30,
    }


def tool_query_sku(
    skus: List[str],
    store: str = "KSA",
    allow_cache_on_live_failure: bool = False,
    reject_cache_on_live_failure: bool = False,
) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""

    # 国别 ID（ERP 实时取数用）
    _country = {"KSA": "SA", "UAE": "AE", "SA": "SA", "AE": "AE"}.get(store.upper(), "SA")
    _nation_id = {"SA": 1, "AE": 2}.get(_country, 1)

    # ERP token（best-effort；失败时销量字段降级，不阻断整个工具执行）
    _erp_token = None
    try:
        _tok, _err = _erp_token_or_error(tid)
        if not _err:
            _erp_token = _tok
    except Exception:
        pass

    out = []
    refs = []
    import datetime as _dt
    from hipop.scripts.freshness_gate import (
        decide_freshness,
        operator_consented_to_cache,
        operator_rejected_cache,
    )

    def _date10(v):
        if v is None:
            return ""
        if hasattr(v, "isoformat"):
            return v.isoformat()[:10]
        return str(v)[:10]

    def _days_old(date_str: str):
        if not date_str:
            return None
        try:
            return max(0, (_dt.date.today() - _dt.date.fromisoformat(date_str[:10])).days)
        except Exception:
            return None

    # SKU sales/order metrics depend on noon orders. A fresh ERP product ingest can move
    # wf2_sku.as_of_date to today while noon order CSV is still old; in that case sales
    # numbers must be redacted instead of presented as current.
    latest_noon_order = _date10(_data._scalar(
        "SELECT MAX(order_date) FROM wf2_orders WHERE tenant_id=? AND entity_alias=?",
        (tid, alias),
    ))
    noon_order_stale_days = _days_old(latest_noon_order)
    noon_orders_stale = (noon_order_stale_days is None) or (noon_order_stale_days > 3)

    for sku in skus[:3]:
        # ── T03 门：强制走实时取数路径拿销量数字 ──────────────────────
        live_fn = _sku_sales_live_fn if _sku_sales_live_fn is not None else _erp_sku_stats_live
        try:
            live_result = live_fn(sku, _nation_id, _erp_token)
        except Exception as _e:
            live_result = {"ok": False, "error": f"live_fn_exception: {type(_e).__name__}",
                           "message": str(_e)[:200]}
        live_ok = bool(live_result and live_result.get("ok"))
        # Round-4: ok=True alone is not enough; require sales_30d to be present
        live_has_sales = live_ok and live_result.get("sales_30d") is not None

        rows = _data._fetch("""
            SELECT w2.partner_sku, w2.title, w2.sales_grade, w2.latest_profit_rate,
                   w2.sales_30d, w2.sales_10d, w2.latest_price,
                   w2.total_orders, w2.as_of_date, w2.imported_at,
                   w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
                   w5.current_pipeline, w5.weekly_total_replenish,
                   w5.updated_at AS wf5_updated_at,
                   h.in_transit_total_qty, h.has_stuck_batch, h.needs_ops_input,
                   h.updated_at AS wf3_updated_at
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
        as_of = r.get("as_of_date")
        # 事实源契约（WS-129）：每个来源的时间戳门，NULL → fail-closed
        wf3_updated_at = r.get("wf3_updated_at") or None
        wf5_updated_at = r.get("wf5_updated_at") or None
        imported_at_full = r.get("imported_at") or None
        wf3_ok = bool(wf3_updated_at)   # wf3_logistics_hub_v2 timestamp gate
        wf5_ok = bool(wf5_updated_at)   # wf5_sales_cycle timestamp gate
        wf2_ok = bool(imported_at_full) # wf2_sku.imported_at snapshot gate
        stats_30d: dict = {}
        if as_of:
            try:
                stats_30d = _data.sku_30d_stats(tid, alias, sku, as_of)
            except Exception:
                stats_30d = {}
        # 快照时效门：as_of_date 超 3 天/缺失，或 noon 订单源超 3 天/缺失
        # → data_stale=True，销量/订单数值 REDACT 为 null。
        # 目的：防止过期快照被 LLM 当成新鲜数据呈现；LLM 必须告知数据过期。
        import datetime as _dt
        stale_days_val: int = 0
        stale_reasons: List[str] = []
        data_stale_val: bool = not as_of
        # stale_confirmed：as_of 成功解析且确实超阈值，才算「确认陈旧」。仅此情形
        # 才允许走 found=False「查不到」短路。as_of 缺失/格式异常（不同 DB 驱动可能
        # 回 date 对象或 'YYYY-MM-DD HH:MM:SS' 等）只做保守 REDACT，不据此判「查不到」。
        stale_confirmed: bool = False
        if not as_of:
            stale_reasons.append("wf2_sku_as_of_missing")
        if as_of:
            _parsed_as_of = None
            if hasattr(as_of, "year") and hasattr(as_of, "month") and hasattr(as_of, "day"):
                # date / datetime 对象（部分驱动直接返回，非字符串）
                try:
                    _parsed_as_of = _dt.date(as_of.year, as_of.month, as_of.day)
                except Exception:
                    _parsed_as_of = None
            else:
                try:
                    # 容忍 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / ISO：取前 10 位日期段
                    _parsed_as_of = _dt.date.fromisoformat(str(as_of).strip()[:10])
                except Exception:
                    _parsed_as_of = None
            if _parsed_as_of is not None:
                stale_days_val = max(0, (_dt.date.today() - _parsed_as_of).days)
                data_stale_val = stale_days_val > 3
                stale_confirmed = data_stale_val
                if stale_confirmed:
                    stale_reasons.append("wf2_sku_as_of_stale")
            else:
                # as_of 存在但无法解析 → 保守 REDACT，但不据此短路成「查不到」，
                # 交由下方 live 成功/失败逻辑决定 found 与 live_sales_failed（修 T03 CI 边界：
                # 否则 live 失败会被误吞成「快照过期/SKU 查不到」，丢失实时失败证据）。
                data_stale_val = True
                stale_confirmed = False
                stale_reasons.append("wf2_sku_as_of_invalid")

        # noon 订单源过期：即使 wf2_sku 快照新鲜，销量/订单数值也须 REDACT
        # （WS-145：fresh ERP ingest 把 as_of 推到今天但 noon CSV 仍旧）。
        if noon_orders_stale:
            data_stale_val = True
            stale_reasons.append("noon_orders_stale")
            if noon_order_stale_days is not None:
                stale_days_val = max(stale_days_val, noon_order_stale_days)

        # T04 口径一致：仅「确认陈旧」（as_of 可解析且超阈值）且无实时销量时，才视为
        # 无有效数据（found=False，回复「查不到」），与 /api/sku-metrics 预检对齐。
        # 早于 WS-131 freshness 门短路：>3 天快照本就过 cache 阈值，无 live 即「查不到」。
        if stale_confirmed and not live_has_sales:
            imported_at_val = (r.get("imported_at") or "")[:10] or None
            out.append({
                "sku": sku, "found": False, "data_stale": True,
                "stale_expired": True, "stale_days": stale_days_val, "as_of_date": as_of,
            })
            refs.append({
                "table": "wf2_sku",
                "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'",
                "imported_at": imported_at_val, "as_of_date": as_of,
            })
            continue

        # WS-131 销量新鲜度门：live 失败时只有「≤3天且运营本轮同意缓存」才放缓存数。
        _live_error_msg = (
            (live_result or {}).get("message")
            or (live_result or {}).get("error")
            or "实时源不可用"
        )
        question_text = _chat_question.get() or ""
        # The historical cache flags are accepted for backward compatibility only.
        # Consent must come from the operator's current question, never from an LLM tool arg.
        question_cache_consent = operator_consented_to_cache(question_text)
        question_cache_rejected = operator_rejected_cache(question_text)
        sales_freshness_decision = decide_freshness(
            live_ok=live_has_sales,
            live_source=(live_result or {}).get("source"),
            live_fetched_at=(live_result or {}).get("fetched_at"),
            live_error=str(_live_error_msg),
            cache_available=r.get("sales_30d") is not None,
            cache_fetched_at=imported_at_full,
            operator_cache_consent=question_cache_consent,
            operator_cache_rejected=question_cache_rejected,
            cache_requires_consent=True,
            subject=f"SKU {sku} 30 天销量",
        )
        sales_live_allowed = sales_freshness_decision.get("status") == "live"
        sales_cache_allowed = sales_freshness_decision.get("status") == "cache_allowed"

        def _r(val):
            return None if data_stale_val else val

        def _live_guarded_snapshot(val):
            # wf5-sourced fields: gated on live or explicitly consented cache freshness.
            return _r(val) if ((sales_live_allowed or sales_cache_allowed) and wf5_ok) else None

        def _wf2(val):
            # wf2 snapshot-sourced fields: gated on imported_at AND the freshness decision.
            return _r(val) if ((sales_live_allowed or sales_cache_allowed) and wf2_ok) else None

        # 销量字段：优先 live 结果（T03：live 失败则 REDACT，禁止输出旧快照确定数）
        # live 失败时，只有 WS-131 门允许的「≤3天且已同意缓存」才输出缓存数。
        if sales_live_allowed:
            sales_30d_out = live_result.get("sales_30d")
            history_total_out = live_result.get("history_total")
        elif sales_cache_allowed:
            sales_30d_out = r.get("sales_30d")
            history_total_out = None
        else:
            sales_30d_out = None
            history_total_out = None

        item = {
            "sku": sku,
            "found": True,
            "title": r["title"],
            "trend": _live_guarded_snapshot(r["trend"]),
            "profit_rate_pct": _wf2(round((r["latest_profit_rate"] or 0) * 100, 1)),
            "sales_30d": sales_30d_out,
            "sales_10d": _wf2(r["sales_10d"]),
            "daily_rate": _live_guarded_snapshot(r["daily_rate"]),
            "urgency": _live_guarded_snapshot(r["urgency"]),
            "ops_advice": _live_guarded_snapshot(r["ops_advice"]),
            "in_transit": r["in_transit_total_qty"] if wf3_ok else None,
            "in_transit_source": "erp" if wf3_ok else None,
            "in_transit_updated_at": wf3_updated_at,
            "has_stuck_batch": bool(r["has_stuck_batch"]) if wf3_ok else None,
            "weekly_replenish": _live_guarded_snapshot(r["weekly_total_replenish"]),
            "total_orders_30d": _live_guarded_snapshot(stats_30d.get("total_30d")),
            "cancel_rate_30d": _live_guarded_snapshot(stats_30d.get("cancel_rate_30d")),
            "return_rate_30d": _live_guarded_snapshot(stats_30d.get("return_rate_30d")),
            # 格式化百分比字串：LLM 可直接引用，无需自行乘 100
            "cancel_rate_30d_pct": (
                f"{stats_30d['cancel_rate_30d'] * 100:.2f}%"
                if not data_stale_val and stats_30d.get("cancel_rate_30d") is not None
                else None
            ),
            "return_rate_30d_pct": (
                f"{stats_30d['return_rate_30d'] * 100:.2f}%"
                if not data_stale_val and stats_30d.get("return_rate_30d") is not None
                else None
            ),
            "history_total": history_total_out,
            "as_of_date": as_of,
            # 只在快照过期时才注入 data_stale/stale_days，避免 data_stale=False 让 LLM
            # 推断"新鲜认证"并追加未被请求的质量评价（T04 回归根因）。
            **( {"data_stale": True, "stale_days": stale_days_val} if data_stale_val else {} ),
            "stale_reason": ",".join(stale_reasons) or None,
            "noon_order_latest": latest_noon_order,
            "noon_order_stale_days": noon_order_stale_days,
            "wf5_updated_at": wf5_updated_at,
            "wf2_imported_at": imported_at_full,
            "sales_freshness_decision": sales_freshness_decision,
        }

        if sales_live_allowed:
            item["live_evidence"] = {
                "fetched_at": live_result.get("fetched_at"),
                "source": live_result.get("source", "live"),
            }
        elif sales_cache_allowed:
            item["cache_evidence"] = {
                "fetched_at": sales_freshness_decision.get("fetched_at"),
                "source": "cache",
            }
        else:
            item["live_sales_failed"] = True
            if live_ok:
                item["live_sales_error"] = "live_ok_but_missing_sales_30d"
                item["live_sales_message"] = (
                    "实时源可达但销量数据缺失，无法给出确定数字"
                    "（sales_30d/history_total 均已拒绝输出，不泄出裸数字）"
                )
            else:
                item["live_sales_error"] = (live_result or {}).get("error", "no_live_fn")
                item["live_sales_message"] = (
                    (live_result or {}).get("message")
                    or "当前无法实时确认 SKU 销量，已降级（不输出旧缓存确定数）"
                )

        imported_at_val = (r.get("imported_at") or "")[:10] or None
        out.append(item)
        refs.append({"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'", "imported_at": imported_at_val, "as_of_date": as_of})
        if live_has_sales:
            refs.append({"table": live_result.get("source", "live (realtime)"),
                         "where": f"partner_sku='{sku}'",
                         "fetched_at": live_result.get("fetched_at")})
        refs.append({"table": "wf2_orders", "where": f"30d window ending {as_of or 'N/A'}"})
        refs.append({"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"})
        refs.append({"table": "wf3_logistics_hub_v2", "where": f"tenant_id={tid} AND sku='{sku}'"})
    _last_sku_rate_stats.set(out)
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


def _normalize_replenishment_rows(rows: list) -> list:
    out = []
    for row in rows or []:
        r = dict(row)
        if r.get("trend") == "急速下降":
            r["urgency_level"] = "high"
        elif r.get("trend") in ("下降", "加速增长"):
            r["urgency_level"] = "mid"
        else:
            r["urgency_level"] = "low"
        r["qty"] = r.get("weekly_total_replenish") or 0
        try:
            r["trigger_reasons_list"] = json.loads(r.get("trigger_reasons") or "[]")
        except Exception:
            r["trigger_reasons_list"] = []
        out.append(r)
    return out


def tool_compute_replenishment(store: str, limit: int = 10) -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    import datetime as _dt
    from hipop.scripts.freshness_gate import decide_freshness

    # WS-62：与 HTTP 入口同源的库存就绪门。库存未就绪/不完整时，chat 不能把
    # 空建议当成「不用补货」，必须 fail-closed。
    stock_status = _data.stock_readiness(tid, alias)
    _last_replenishment_stock_status.set(stock_status)
    base_refs = [
        {"table": "wf1_stock",
         "where": f"tenant_id={tid} AND entity_alias='{alias}' stock_readiness"},
        {"table": "wf5_sales_cycle",
         "where": f"tenant_id={tid} AND entity_alias='{alias}' AND weekly_total_replenish>0"},
        {"table": "wf6_replenishment_queue_v2", "where": f"tenant_id={tid} AND entity_alias='{alias}'"},
    ]
    if not stock_status.get("ready"):
        msg = stock_status.get("message") or "库存数据未就绪，不能给确定补货建议。"
        return {
            "store": store, "count": 0, "items": [],
            "fail_closed": True,
            "stock_status": stock_status,
            "warning": msg,
            "stale_warning": msg,
            "message": msg,
            "references": base_refs,
        }

    latest_wf5 = _data._scalar(
        "SELECT MAX(updated_at) FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND weekly_total_replenish > 0",
        (tid, alias),
    )
    freshness_decision = decide_freshness(
        live_ok=False,
        live_error="补货建议使用最近一次成功的 wf5_sales_cycle 统一计算结果",
        cache_available=bool(latest_wf5),
        cache_fetched_at=latest_wf5,
        operator_cache_consent=True,
        cache_requires_consent=False,
        subject=f"{store} 补货建议",
    )
    if not freshness_decision.get("can_output_number"):
        msg = freshness_decision.get("message") or "补货建议数据缺少更新时间，不能出数。"
        return {
            "store": store, "count": 0, "items": [],
            "fail_closed": True,
            "stock_status": stock_status,
            "freshness_decision": freshness_decision,
            "warning": msg,
            "stale_warning": msg,
            "message": msg,
            "references": base_refs,
        }

    max_age_days = int(freshness_decision.get("max_cache_age_days") or 3)
    cutoff = (_dt.date.today() - _dt.timedelta(days=max_age_days)).isoformat()
    lim = max(1, min(int(limit or 10), 50))
    rows = _data._fetch(
        """
        SELECT w2.partner_sku, w2.title, w2.image_url, w2.sales_30d, w2.latest_price,
               w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
               w5.wf5_replenish_qty, w5.lost_replenish_qty, w5.weekly_total_replenish,
               w5.trigger_reasons, w5.current_pipeline, w5.target_pipeline,
               w5.updated_at
        FROM wf2_sku w2
        JOIN wf5_sales_cycle w5
          ON w2.tenant_id=w5.tenant_id AND w2.entity_alias=w5.entity_alias
          AND w2.partner_sku=w5.partner_sku
        WHERE w2.tenant_id=? AND w2.entity_alias=?
          AND w2.is_listed=1 AND w5.weekly_total_replenish > 0
          AND w5.updated_at >= ?
        ORDER BY w5.weekly_total_replenish DESC
        LIMIT ?
        """,
        (tid, alias, cutoff, lim),
    )
    from hipop.scripts.evidence_contract import (
        build_query_evidence as _build_query_evidence,
        SOURCE_NOON as _SRC_NOON, SOURCE_ERP as _SRC_ERP, SOURCE_MERGED as _SRC_MERGED,
    )
    evidence = _build_query_evidence(
        source=_SRC_MERGED,
        fetched_at=latest_wf5,
        coverage=(
            f"{store} 补货建议 = 统一库存(不含国际在途) + noon 销量窗口 + "
            f"wf5_sales_cycle 工作流公式；Top{lim} 按 weekly_total_replenish DESC"
        ),
        sub_sources=[_SRC_NOON, _SRC_ERP],
        context="compute_replenishment",
    )
    items = [{
        "sku": r["partner_sku"], "title": r["title"], "qty": r["qty"],
        "urgency": r["urgency_level"], "daily_rate": r["daily_rate"], "trend": r["trend"],
        "advice": r["ops_advice"],
        "updated_at": r.get("updated_at"),
    } for r in _normalize_replenishment_rows(rows)]
    return {
        "store": store, "count": len(items), "items": items,
        "fail_closed": False,
        "stock_status": stock_status,
        "freshness_decision": freshness_decision,
        "evidence": evidence,
        "n_requested": lim,
        "n_returned": len(items),
        "warning": None if stock_status.get("ready") else stock_status.get("message"),
        "stale_warning": None if stock_status.get("ready") else "库存数据未更新或不完整，当前补货结论偏保守",
        "references": base_refs,
    }


def tool_query_replenishment_sku(sku: str, store: str = "KSA") -> Dict:
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""
    from . import replenishment_evidence as _rep
    return _rep.query_replenishment_sku(sku, store, tid, alias)


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
    evidence = None
    freshness_decision = None
    fail_closed = False
    fail_message = None
    latest_sales_as_of = None
    requested_limit = max(0, min(int(limit or 0), 50))
    if requested_limit > 0:
        from hipop.scripts.freshness_gate import decide_freshness

        latest_sales_as_of = _data._scalar(
            f"SELECT MAX(as_of_date) FROM {tbl} {where_sql} AND sales_30d IS NOT NULL"
        )
        freshness_decision = decide_freshness(
            live_ok=False,
            live_error="销量 TopN 使用最近一次成功的统一销量快照",
            cache_available=bool(latest_sales_as_of),
            cache_fetched_at=latest_sales_as_of,
            operator_cache_consent=True,
            cache_requires_consent=False,
            subject=f"{store} 近30天销量 TopN",
        )
        if not freshness_decision.get("can_output_number"):
            fail_closed = True
            fail_message = freshness_decision.get("message") or (
                f"{store} 近30天销量 TopN 缺少可用更新时间，不能出数。"
            )
            return {
                "store": store,
                "summary_products": {
                    "total":     prod_agg["product_total"],
                    "listed":    prod_agg["product_listed"],
                    "unlisted":  prod_agg["product_unlisted"],
                    "_dim": "product (= ERP 后台筛选店铺时显示的总数)"
                },
                "summary_skus": {
                    "total":           agg["total"],
                    "listed":          agg["listed"],
                    "unlisted":        agg["unlisted"],
                    "ever_sold_180d":  agg["ever_sold"],
                    "sold_recent_30d": agg["sold_recent_30d"],
                    "_dim": "sku (含每个 product 下的颜色/尺寸变体)"
                },
                "filter": {"listing": listing, "sales_only": sales_only},
                "sort": {
                    "field": "sales_30d",
                    "direction": "desc",
                    "tie_breakers": ["sales_180d desc", "partner_sku asc"],
                    "meaning": "limit=N returns near-30-day sales TopN",
                },
                "n_requested": requested_limit,
                "n_returned": 0,
                "filtered_count": filtered_count,
                "items": [],
                "fail_closed": fail_closed,
                "message": fail_message,
                "freshness_decision": freshness_decision,
                "references": [
                    {
                        "table": tbl,
                        "where": where_sql,
                        "as_of_date": latest_sales_as_of,
                    },
                ],
            }
        rows = _data._fetch(f"""
            SELECT partner_sku, title, is_listed, sales_30d, sales_180d, latest_price,
                   as_of_date, imported_at
            FROM {tbl} {where_sql} AND as_of_date=?
            ORDER BY (sales_30d IS NULL) ASC,
                     COALESCE(sales_30d,0) DESC,
                     COALESCE(sales_180d,0) DESC,
                     partner_sku ASC
            LIMIT ?
        """, (latest_sales_as_of, requested_limit,))
        items = [{
            "sku": r["partner_sku"], "title": r["title"],
            "is_listed": bool(r["is_listed"]),
            "sales_30d": r["sales_30d"],
            "sales_180d": r["sales_180d"],
            "price": r["latest_price"],
            "as_of_date": r.get("as_of_date"),
        } for r in rows]
        fetched_at = None
        for r in rows:
            fetched_at = max(
                [x for x in (fetched_at, r.get("as_of_date"), (r.get("imported_at") or "")[:10]) if x],
                default=None,
            )
        if items:
            from hipop.scripts.evidence_contract import (
                build_query_evidence as _build_query_evidence,
                SOURCE_CACHE as _SRC_CACHE,
            )
            evidence = _build_query_evidence(
                source=_SRC_CACHE,
                fetched_at=latest_sales_as_of or fetched_at,
                coverage=(
                    f"{store} wf2_sku.sales_30d DESC Top{requested_limit}；"
                    f"统一销量快照 as_of_date={latest_sales_as_of}；"
                    f"limit={requested_limit} 即近30天销量 TopN；"
                    f"listing={listing}；sales_only={bool(sales_only)}"
                ),
                context="list_products_sales_topn",
            )

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
        "sort": {
            "field": "sales_30d",
            "direction": "desc",
            "tie_breakers": ["sales_180d desc", "partner_sku asc"],
            "meaning": "limit=N returns near-30-day sales TopN",
        },
        "n_requested": requested_limit,
        "n_returned": len(items),
        "filtered_count": filtered_count,
        "items": items,
        "fail_closed": fail_closed,
        "message": fail_message,
        "freshness_decision": freshness_decision,
        "evidence": evidence,
        "references": [
            {
                "table": tbl,
                "where": (
                    f"{where_sql} AND as_of_date='{latest_sales_as_of}' "
                    f"ORDER BY sales_30d DESC, sales_180d DESC "
                    f"LIMIT {requested_limit}"
                    if requested_limit > 0 else where_sql
                ),
                "as_of_date": (evidence or {}).get("fetched_at"),
            },
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
    """触发后台工作流。

    WS-99 T21-SUB-1：对已注册 Managed Agents runner 的 workflow 走 spawn_task（落 tasks
    表 + 同步写 queued event），保证任务证据链完整；legacy workflow 仍走 daemon thread。
    ⚠️ agent.py 受 CODEOWNERS 锁定，本次修改经 PR 审批。
    """
    import json as _json
    from . import api as _api

    if workflow not in _api.WORKFLOW_REGISTRY:
        return {"ok": False, "error": f"unknown workflow: {workflow}",
                "valid": list(_api.WORKFLOW_REGISTRY)}
    label, steps, affected = _api.WORKFLOW_REGISTRY[workflow]
    tid = _get_tenant()
    sc = _chat_scope.get() or {}
    actor = {
        "user_id": sc.get("user_id"),
        "email": sc.get("current_user_email") or sc.get("current_user"),
        "role": sc.get("current_role"),
        "source": "chat",
    }

    from hipop.runtime import workflow_runners as _runners
    from . import runtime as _runtime
    if workflow in _runners.list_runners():
        # Managed Agents path: durable tasks row + events (same contract as /api/run-workflow)
        # WS-132: two separate try/except blocks to distinguish (acceptance criterion 2):
        #   - spawn_task failure (task never created) → creation_failed=True, no task_id
        #   - spawn_task success but init event write failure → task_id present + lifecycle_error
        try:
            task_id = _runtime.spawn_task(
                workflow=workflow, tenant_id=tid, actor=actor,
            )
        except Exception as _spawn_err:
            return {
                "ok": False,
                "error": f"任务创建失败: {type(_spawn_err).__name__}: {_spawn_err}",
                "creation_failed": True,
                "workflow": workflow,
                "label": label,
            }
        _lifecycle_error = None
        try:
            _data.set_current_tenant(tid)
            _data.write_event(
                task_id, 1, "初始化", "done",
                _json.dumps({"workflow": workflow, "label": label,
                             "affected_modules": affected, "total_steps": len(steps),
                             "tenant_id": tid,
                             "runtime": "managed_agents"}, ensure_ascii=False),
                actor=actor,
            )
        except Exception as _event_err:
            _lifecycle_error = f"事件写入失败，任务已创建但状态未能初始化: {type(_event_err).__name__}: {_event_err}"
    else:
        _lifecycle_error = None
        from uuid import uuid4
        import threading
        task_id = uuid4().hex[:8]
        # WS-144：legacy thread 路径也先同步写一条 durable queued event，保证执行记录
        # 有 ≥1 个真实步骤可查（不靠后台线程异步落库，否则返回时无证据 = 接线缺失）。
        _data.set_current_tenant(tid)
        _data.write_event(
            task_id, 0, "任务排队", "queued",
            _json.dumps({"workflow": workflow, "label": label,
                         "affected_modules": affected, "total_steps": len(steps),
                         "tenant_id": tid, "runtime": "legacy_thread"}, ensure_ascii=False),
            actor=actor,
        )
        threading.Thread(
            target=_api._run_workflow, args=(task_id, workflow, tid, actor), daemon=True,
        ).start()

    # WS-144 统一执行记录契约（样板执行工具）：回读 durable events 证明任务真实落库，
    # 据此构造 execution_record。没有真实 task_id + ≥1 步骤就不算"已执行/已启动"。
    from hipop.scripts.evidence_contract import (
        build_execution_record as _build_execution_record,
        render_execution_suffix as _render_execution_suffix,
        EXEC_RUNNING as _EXEC_RUNNING, EXEC_CREATE_FAILED as _EXEC_CREATE_FAILED,
        ContractViolation as _ExecContractViolation,
    )
    try:
        # 回读放进 try 内：DB 读失败同样视为"无可查记录" → fail-closed，不冒充已启动。
        _durable_events = _data.get_events_after(task_id, 0)
        execution_record = _build_execution_record(
            status=_EXEC_RUNNING,
            task_id=task_id,
            workflow=workflow,
            steps=[{"step_no": e.get("step_no"), "step_name": e.get("step_name"),
                    "status": e.get("status")} for e in _durable_events],
            context="run_workflow",
        )
        exec_hint = _render_execution_suffix(execution_record)
    except (_ExecContractViolation, Exception) as _e:
        # fail-closed：任务没产生可查的真实记录 → 如实标 create_failed，不冒充已启动。
        execution_record = _build_execution_record(
            status=_EXEC_CREATE_FAILED, workflow=workflow,
            reason=f"任务未落库可查记录：{_e}", context="run_workflow",
        )
        exec_hint = _render_execution_suffix(execution_record)

    # WS-144 round-1：失败语义不许只藏在 execution_record 内层。
    # create_failed → 外层 ok=False + error，让"只看 ok"的下游也能确定识别失败，
    # 不会把没落库的任务误读成已启动（验门人 14:37 指出的歧义）。
    exec_failed = execution_record["status"] == _EXEC_CREATE_FAILED
    result = {
        "ok": not exec_failed,
        # 没产生可查真实任务时不外泄生成的临时 id 冒充"已创建任务"。
        "task_id": None if exec_failed else task_id,
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "followup_prompt": followup_prompt or None,
        "execution_record": execution_record,
        "hint": f"{exec_hint}请在工作台任务面板查看进度；影响模块：{affected}。",
    }
    if exec_failed:
        result["error"] = execution_record.get("reason") or "工作流任务未确认创建成功"
    if _lifecycle_error:
        result["lifecycle_error"] = _lifecycle_error
    return result


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
                                   "（dbuyerp 可能在风控同账号短时间多次登），稍后重试"}
    return token, None


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


def _utc_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def tool_query_sku_live(sku: str, with_nodes: bool = False) -> Dict:
    """实时查单 SKU ERP 在途货单 — 不读 wf3 缓存，每次直连。
    with_nodes=True 时对每个在途货单跑 playwright 抓物流站节点（慢 5-10s/单）。
    默认 False（只 ERP 拉单 + tracking_no，快），用户问'节点'/'卡哪'时设 True。"""
    tid = _get_tenant()
    fetched_at = _utc_now_iso()
    live_source = "ERP /delivery (realtime)"
    token, err = _erp_token_or_error(tid)
    if err:
        if err.get("error") == "no_erp_credentials":
            return {
                "ok": False,
                "error": "sku_live_unavailable_no_erp_credentials",
                "sku": sku,
                "source": live_source,
                "fetched_at": fetched_at,
                "cache_fallback": False,
                "message": (
                    f"当前无法实时查询 SKU {sku} 的在途物流：本店铺 ERP 账号未配置。"
                    "请先配置 dbuyerp 后重试；本工具不返回 wf3 旧缓存。"
                ),
            }
        if err.get("error") == "erp_login_failed":
            return {
                "ok": False,
                "error": "erp_login_failed_no_cache",
                "sku": sku,
                "source": live_source,
                "fetched_at": fetched_at,
                "cache_fallback": False,
                "live_query_failed_reason": err["message"],
                "message": (
                    f"ERP 实时查询 SKU {sku} 失败（{err['message']}），"
                    "无法确认当前在途或近期完成货单；已按实时查询契约 fail closed，"
                    "不返回 wf3 旧缓存。请稍后重试。"
                ),
            }
        out = dict(err)
        out.update({"sku": sku, "source": live_source, "fetched_at": fetched_at, "cache_fallback": False})
        return out
    _wf0, _wls, _orig = _patch_wls_token(token)
    try:
        in_transit, completed = _wls.collect_sku_orders(sku, token)
    except Exception as e:
        return {
            "ok": False,
            "error": f"erp_fetch_error: {type(e).__name__}: {str(e)[:200]}",
            "sku": sku,
            "source": live_source,
            "fetched_at": fetched_at,
            "cache_fallback": False,
            "message": "ERP 实时查询失败，无法确认当前在途或近期完成货单；不返回 wf3 旧缓存。",
        }
    finally:
        _wf0.get_erp_token = _orig
        _wls.get_erp_token = _orig
    if not in_transit and not completed:
        return {
            "ok": False,
            "error": "sku_no_orders_in_erp",
            "sku": sku,
            "source": live_source,
            "fetched_at": fetched_at,
            "cache_fallback": False,
            "message": f"SKU {sku} 在 ERP 中无在途或近期完成货单记录，请核实 SKU 是否正确。",
        }
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
        "source": live_source,
        "fetched_at": fetched_at,
        "cache_fallback": False,
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
        if err.get("error") == "no_erp_credentials":
            return {
                "ok": False,
                "error": "order_lookup_unavailable_no_erp_credentials",
                "order_no": order_no,
                "message": (
                    f"当前未找到货单号 {order_no} 的实时 ERP 记录：本店铺 ERP 账号未配置，"
                    "无法确认该货单是否存在。请核实货单号是否正确，或先配置 dbuyerp 后重试。"
                ),
            }
        if err.get("error") == "erp_login_failed":
            return {"ok": False, "error": "erp_login_failed_no_cache",
                     "message": f"ERP 实时查失败（{err['message']}），单货单查询没缓存兜底。"
                                 "请稍后重试；单 SKU 实时查询也不返回 wf3 旧缓存。"}
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
        return {
            "ok": False,
            "error": "order_not_found_in_erp",
            "order_no": order_no,
            "message": f"货单号 {order_no} 在 ERP 中无记录，请核实货单号是否正确。",
        }
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


# T11 — 单 SKU 四仓库存拆分（WS-140）
# 四仓: 义乌(yiwu) / 沙特一号仓(overseas_saudi_1) / noon仓 / 在途(inbound)
# 来源: wf1_stock（ERP ingest + noon CSV/live ingest + pending_inbound ingest）
# 口径: 库存不含在途；在途/待发货单列为状态字段（单独 inbound 列）
_STOCK_SPLIT_MAX_AGE_DAYS = 3   # >3天 fail-closed；≤3天带提示降级


def tool_query_stock_split(sku: str, store: str = "KSA") -> Dict:
    """查单 SKU 四仓库存拆分：义乌 / 沙特一号仓 / noon / 在途 + 总量 + 来源时间戳。

    Fail-closed 规则（WS-140 新鲜度门）：
      - updated_at 超过 3 天 → fail_closed=True，不出数字。
      - 无行 / no data → fail_closed=True。
      - ≤3天缓存 → 返回数据 + stale_warn 提示用户确认。
      - noon_total_qty IS NULL → noon 列为 0 + noon_missing=True。
    """
    import datetime as _dt
    import json as _json
    from hipop.scripts.freshness_gate import decide_freshness

    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""

    rows = _data._fetch(
        "SELECT yiwu_qty, dongguan_qty, overseas_total_qty, overseas_breakdown_json, "
        "       noon_total_qty, pending_inbound_qty, total_stock, "
        "       updated_at, imported_at "
        "FROM wf1_stock WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tid, alias, sku),
    )
    if not rows:
        freshness_decision = decide_freshness(
            live_ok=False,
            live_error="库存拆分使用最近一次统一库存刷新",
            cache_available=False,
            cache_fetched_at=None,
            operator_cache_consent=True,
            cache_requires_consent=False,
            subject=f"SKU {sku} 库存拆分",
        )
        return {
            "ok": False,
            "fail_closed": True,
            "sku": sku,
            "store": store,
            "freshness_decision": freshness_decision,
            "message": freshness_decision.get("message") or (
                f"SKU {sku} 在 wf1_stock 中无记录，请先运行库存刷新（wf1_stock_v2）。"
            ),
            "references": [{"table": "wf1_stock", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"}],
        }

    r = rows[0]
    updated_at = r.get("updated_at") or r.get("imported_at") or ""
    stale_days: Optional[int] = None
    if updated_at:
        try:
            dt = _dt.date.fromisoformat(updated_at[:10])
            stale_days = max(0, (_dt.date.today() - dt).days)
        except Exception:
            stale_days = None

    freshness_decision = decide_freshness(
        live_ok=False,
        live_error="库存拆分使用最近一次统一库存刷新",
        cache_available=True,
        cache_fetched_at=updated_at,
        operator_cache_consent=True,
        cache_requires_consent=False,
        subject=f"SKU {sku} 库存拆分",
    )
    if not freshness_decision.get("can_output_number"):
        age_desc = "数据缺失或时间戳无法解析" if stale_days is None else f"{stale_days} 天前"
        return {
            "ok": False,
            "fail_closed": True,
            "sku": sku,
            "store": store,
            "stale_days": stale_days,
            "updated_at": updated_at or None,
            "freshness_decision": freshness_decision,
            "message": freshness_decision.get("message") or (
                f"SKU {sku} 库存快照超过 {_STOCK_SPLIT_MAX_AGE_DAYS} 天（{age_desc}），"
                "拒绝出数。请先刷新库存（wf1_stock_v2）或上传最新 noon 库存 CSV。"
            ),
            "references": [{"table": "wf1_stock", "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'"}],
        }

    breakdown_raw = r.get("overseas_breakdown_json") or "{}"
    try:
        breakdown = _json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else (breakdown_raw or {})
    except Exception:
        breakdown = {}
    overseas_saudi_1 = breakdown.get("沙特一号仓") or 0
    overseas_total = r.get("overseas_total_qty") or 0
    # KSA entity: if breakdown empty but overseas_total exists, all is saudi_1
    if overseas_saudi_1 == 0 and overseas_total > 0 and not breakdown:
        overseas_saudi_1 = overseas_total

    yiwu = r.get("yiwu_qty") or 0
    dongguan = r.get("dongguan_qty") or 0   # T12: 东莞国内仓，与义乌合计为 domestic
    domestic = yiwu + dongguan              # T12: 国内仓合计（义乌+东莞）
    noon = r.get("noon_total_qty")
    noon_missing = noon is None
    noon = noon or 0
    inbound = r.get("pending_inbound_qty") or 0
    # total_stock from DB is authoritative (merge_stock_snapshot_v2 computes it);
    # fall back to component sum if DB total is NULL.
    total = r.get("total_stock")
    if total is None:
        total = yiwu + dongguan + overseas_total + noon + inbound

    # T12: ERP 在途（国际在途）来自 wf3_logistics_hub_v2，不计入 total_stock。
    # 带新鲜度门（>3天 → None，不出旧数）。
    wf3_rows = _data._fetch(
        "SELECT in_transit_total_qty, updated_at AS wf3_updated_at "
        "FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND sku=?",
        (tid, sku),
    )
    erp_in_transit: Optional[int] = None
    erp_in_transit_updated_at: Optional[str] = None
    erp_in_transit_unavailable: Optional[str] = None
    if wf3_rows:
        wf3r = wf3_rows[0]
        wf3_updated_at = wf3r.get("wf3_updated_at") or ""
        wf3_stale_days: Optional[int] = None
        if wf3_updated_at:
            try:
                wf3_stale_days = max(0, (
                    _dt.date.today() - _dt.date.fromisoformat(wf3_updated_at[:10])
                ).days)
            except Exception:
                wf3_stale_days = None
        if wf3_stale_days is None or wf3_stale_days > _STOCK_SPLIT_MAX_AGE_DAYS:
            erp_in_transit_unavailable = (
                f"wf3 在途数据超过 {_STOCK_SPLIT_MAX_AGE_DAYS} 天（"
                f"{'无时间戳' if wf3_stale_days is None else f'{wf3_stale_days} 天前'}），"
                "拒绝出数。请先刷新物流（wf3_logistics_v2）。"
            )
        else:
            wf3_in_transit = wf3r.get("in_transit_total_qty")
            if wf3_in_transit is None:
                erp_in_transit_unavailable = (
                    "wf3_logistics_hub_v2.in_transit_total_qty 字段缺失/NULL，"
                    "在途数据不可用。请先刷新物流（wf3_logistics_v2）。"
                )
                erp_in_transit_updated_at = wf3_updated_at[:10] if wf3_updated_at else None
            else:
                erp_in_transit = wf3_in_transit
                erp_in_transit_updated_at = wf3_updated_at[:10] if wf3_updated_at else None
    else:
        erp_in_transit_unavailable = "wf3_logistics_hub_v2 无该 SKU 记录（在途数据未拉取）"

    stale_warn = None
    if stale_days and stale_days > 0:
        stale_warn = f"⚠️ 库存数据为 {stale_days} 天前（{updated_at[:10]}），请确认后使用。"

    imported_at = r.get("imported_at") or None

    result: Dict = {
        "ok": True,
        "fail_closed": False,
        "sku": sku,
        "store": store,
        "split": {
            # T11 keys (backward compat)
            "yiwu": yiwu,
            "overseas_saudi_1": overseas_saudi_1,
            "noon": noon,
            "inbound": inbound,
            # T12 keys: 国内仓 = 义乌 + 东莞
            "dongguan": dongguan,
            "domestic": domestic,
        },
        "total": total,
        "noon_missing": noon_missing,
        "noon_source": "noon" if not noon_missing else None,
        "noon_imported_at": imported_at,
        "erp_source": "erp",
        "erp_updated_at": updated_at[:10] if updated_at else None,
        "stale_days": stale_days,
        "updated_at": updated_at[:10] if updated_at else None,
        "stale_warn": stale_warn,
        "freshness_decision": freshness_decision,
        # T12: ERP 在途（来自 wf3，国际运输中，不计入 total_stock）
        "erp_in_transit": erp_in_transit,
        "erp_in_transit_source": "erp" if erp_in_transit is not None else None,
        "erp_in_transit_updated_at": erp_in_transit_updated_at,
        "erp_in_transit_not_in_total": True,   # 明确标注：在途不计入 total_stock
        "erp_in_transit_unavailable": erp_in_transit_unavailable,
        "references": [
            {"table": "wf1_stock",
             "where": f"tenant_id={tid} AND entity_alias='{alias}' AND partner_sku='{sku}'",
             "source": "noon+erp",
             "as_of_date": updated_at[:10] if updated_at else None},
        ],
    }
    if erp_in_transit is not None:
        result["references"].append({
            "table": "wf3_logistics_hub_v2",
            "where": f"tenant_id={tid} AND sku='{sku}'",
            "source": "erp",
            "as_of_date": erp_in_transit_updated_at,
        })
    return result


# T15 — 总库存 TopN（WS-139）
# total_stock = noon_total + overseas + 国内(义乌+东莞) + pending_inbound（WS-12 合并规则）
_TOTAL_STOCK_TOPN_MAX_AGE_DAYS = 3   # 超过 3 天 fail-closed 不出数


def tool_total_stock_topn(store: str = "KSA", n: int = 10) -> Dict:
    """查当前 total_stock（含 pending_inbound）最高的前 N 个 SKU。

    Fail-closed: 数据 updated_at > 3 天 → fail_closed=True，不出数字。
    口径区分: total_stock 含 pending_inbound; noon_saleable_qty 仅 noon 可售，两者不同。
    """
    import datetime as _dt2
    from hipop.scripts.freshness_gate import decide_freshness
    tid = _get_tenant()
    alias = _resolve_entity_alias(store) or ""

    row_count = _data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND entity_alias=?",
        (tid, alias),
    ) or 0
    latest_row = _data._fetch(
        "SELECT MAX(updated_at) AS latest FROM wf1_stock WHERE tenant_id=? AND entity_alias=?",
        (tid, alias),
    )
    latest_ts = (latest_row[0].get("latest") or "") if latest_row else ""
    stale_days: Optional[int] = None
    if latest_ts:
        try:
            dt = _dt2.date.fromisoformat(latest_ts[:10])
            stale_days = (_dt2.date.today() - dt).days
        except Exception:
            stale_days = None

    freshness_decision = decide_freshness(
        live_ok=False,
        live_error="TopN 使用最近一次成功刷新的统一库存数据",
        cache_available=row_count > 0,
        cache_fetched_at=latest_ts,
        operator_cache_consent=True,
        cache_requires_consent=False,
        subject=f"{store} 总库存 TopN",
    )
    if stale_days is None or stale_days > _TOTAL_STOCK_TOPN_MAX_AGE_DAYS:
        return {
            "fail_closed": True,
            "store": store,
            "stale_days": stale_days,
            "latest_updated_at": latest_ts or None,
            "max_age_days": _TOTAL_STOCK_TOPN_MAX_AGE_DAYS,
            "freshness_decision": freshness_decision,
            "message": freshness_decision.get("message") or (
                f"库存快照超过 {_TOTAL_STOCK_TOPN_MAX_AGE_DAYS} 天（"
                f"{'数据缺失' if stale_days is None else f'{stale_days} 天前'}），"
                "不能出数（防止误导运营）。请先刷新库存（run_workflow wf1_stock_v2）或"
                "上传最新 noon 库存 CSV 后重问。"
            ),
            "references": [{"table": "wf1_stock",
                             "where": f"tenant_id={tid} AND entity_alias=\'{alias}\'"}],
        }

    limit = max(1, min(int(n or 10), 50))
    # Per-row freshness: exclude rows whose own updated_at is stale, even if MAX is fresh.
    # This prevents a single fresh-but-low-stock row from making stale high-stock rows appear.
    cutoff = (_dt2.date.today() - _dt2.timedelta(days=_TOTAL_STOCK_TOPN_MAX_AGE_DAYS)).isoformat()
    rows = _data._fetch(
        """SELECT partner_sku,
                  COALESCE(total_stock, 0) AS total_stock,
                  COALESCE(noon_saleable_qty, 0) AS noon_saleable_qty,
                  COALESCE(pending_inbound_qty, 0) AS pending_inbound_qty,
                  COALESCE(noon_total_qty, 0) AS noon_total_qty,
                  COALESCE(overseas_total_qty, 0) AS overseas_total_qty,
                  COALESCE(yiwu_qty, 0) AS yiwu_qty,
                  COALESCE(dongguan_qty, 0) AS dongguan_qty,
                  updated_at
           FROM wf1_stock
           WHERE tenant_id=? AND entity_alias=? AND updated_at >= ?
           ORDER BY total_stock DESC
           LIMIT ?""",
        (tid, alias, cutoff, limit),
    )
    if not rows:
        return {
            "empty": True,
            "store": store,
            "message": f"{store} 没有库存数据，请先刷新库存（run_workflow wf1_stock_v2）。",
            "references": [{"table": "wf1_stock",
                             "where": f"tenant_id={tid} AND entity_alias=\'{alias}\'"}],
        }

    items = [dict(r) for r in rows]
    # WS-144 统一证据契约（样板查询工具）：每个出数的数字必须带来源/取数时间/口径。
    # total_stock 是跨源聚合（noon 官方仓 + ERP 各仓 + pending），故 source=merged，
    # sub_sources 显式列出 noon/erp，coverage 写清口径——缺任一三要素本调用直接 raise，
    # 不允许无证据出数。
    from hipop.scripts.evidence_contract import (
        build_query_evidence as _build_query_evidence,
        SOURCE_NOON as _SRC_NOON, SOURCE_ERP as _SRC_ERP, SOURCE_MERGED as _SRC_MERGED,
    )
    evidence = _build_query_evidence(
        source=_SRC_MERGED,
        fetched_at=latest_ts,
        coverage=(
            f"{store} total_stock = noon官方仓 + 海外仓 + 国内仓(义乌/东莞) + 送仓未上架(pending)；"
            f"Top{limit}（返回 {len(items)} 行）；noon可售(saleable)不含 pending，与 total_stock 不同"
        ),
        sub_sources=[_SRC_NOON, _SRC_ERP],
        context="total_stock_topn",
    )
    return {
        "fail_closed": False,
        "store": store,
        "total_stock_definition": (
            "noon_total + overseas + yiwu + dongguan + pending_inbound（送仓未上架，WS-12 口径）"
        ),
        "noon_saleable_note": (
            "noon_saleable_qty 仅含 noon 官方仓可售，**不含** pending_inbound，与 total_stock 不同"
        ),
        "stale_days": stale_days,
        "latest_updated_at": latest_ts,
        "n_requested": limit,
        "n_returned": len(items),
        "items": items,
        "evidence": evidence,
        "freshness_decision": freshness_decision,
        "references": [
            {"table": "wf1_stock",
             "where": (
                 f"tenant_id={tid} AND entity_alias=\'{alias}\' "
                 f"ORDER BY total_stock DESC LIMIT {limit}"
             ),
             "as_of_date": latest_ts[:10] if latest_ts else None}
        ],
    }


# ── Tool 派发 ─────────────────────────────────────────
TOOL_FUNCS = {
    "query_sku": tool_query_sku,
    "query_order": tool_query_order,
    "update_alert_status": tool_update_alert_status,
    "scope_overview": tool_scope_overview,
    "compute_replenishment": tool_compute_replenishment,
    "query_replenishment_sku": tool_query_replenishment_sku,
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
    "query_stock_split": tool_query_stock_split,
    "total_stock_topn": tool_total_stock_topn,
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
        # WS-145 肯定执行意图门:非执行语气（否定/询问/假设/只问影响面）下，
        # LLM 不许偷偷 run_workflow 落任务 —— 在工具执行入口确定性拦死，不靠 prompt。
        if name == "run_workflow":
            _intent = _chat_intent.get()
            if _intent is not None and getattr(_intent, "blocks_llm_execution", False):
                return {
                    "ok": False,
                    "error": "execution_intent_gate_blocked",
                    "blocked_by": "execution_intent_gate",
                    "message": (
                        "本轮是非执行语气（否定/询问/假设/只问影响面），未创建任何后台任务。"
                        "需要执行请明确说「帮我刷新/重算…」。"
                    ),
                }
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
| **<SKU> 库存拆分 / 四仓 / 义乌仓+沙特仓+noon+在途 / 总库存明细** | **query_stock_split**（必调，不得省略 noon 仓，不得用 TopN 路径）|
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
   - 现有 wf2 字段：`partner_sku / title / sales_10d / sales_30d / sales_60d / sales_90d / sales_180d / latest_price / latest_profit_rate / is_listed / sales_grade`；query_sku 工具额外返回 `total_orders_30d`（30d 窗口总单）/ `cancel_rate_30d`（30d 取消率）/ `return_rate_30d`（30d 退货率）/ `history_total`（ERP 历史总单）/ `as_of_date`（数据口径截止日）；快照超期时额外返回 `data_stale=True`（快照超过 3 天）/ `stale_days`（快照距今天数，仅在 data_stale=True 时出现）
   - **快照时效规则**：工具返回 `data_stale=True` 时（快照超过 3 天，或 `as_of_date` 为空）：① 必须明确告知用户数据已过期（"数据已超过 X 天，可能不是最新"）；② 不得把过期快照里的销量/取消率/退货率当作当前事实直接报出；③ 建议用户刷新（触发 run_workflow 重新 ingest，或上传最新 noon CSV）。`data_stale` 字段不存在时（3 天内）：直接用快照数据回答，附带 `as_of_date` 供用户参考
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
- query_sku_live 返回 `ok=false`（例如 `erp_login_failed_no_cache`，且 `cache_fallback=false`）时：必须明告用户「ERP 实时不可用，无法确认当前在途」，**不许**把 wf3 旧缓存当作实时数据呈现

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
| 总库存最高 / 库存最多 / 积压最多 / 库存 TopN | **total_stock_topn**（含 pending_inbound，口径 = noon+海外+国内+送仓未上架；超 3 天 fail-closed；与 noon 可售 saleable 不同）|
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
8. **data_stale=True 时禁止报数值**：query_sku 返回 `data_stale=True`（快照超 3 天或无 as_of_date，此字段仅在过期时出现），所有数值字段已 REDACT 为 null；你必须如实告知数据过期，禁止从工具返回或上下文中"估算"已 REDACT 的旧值，也不得附免责声明后仍报旧值
9. **纯数字问题格式**：用户只问"[指标] 是多少/分别是多少"时，严格按 "[指标A] [数值]，[指标B] [数值]，…，截至 [as_of_date]" 格式回复，在 as_of_date 后停止，不续写其他句子

## 长期偏好沉淀
- 用户明确说"以后都这么办" / "记住" / "默认 X" → 调 tenant_notes_append
- 高风险决策前可调 tenant_notes_get 看客户既定偏好（按需，不每次都拉）

## 回答风格
- 中文 2-4 句一段，给结论 + 简明建议（纯数字问题例外见下）
- 一句进度 OK（"我先看看"），不暴露技术细节（"调用 X tool"）
- run_workflow 后不再 query，等 followup_prompt 自动续
- **纯数字问题**（用户只问"X 是多少/分别是多少"等）：格式 "[指标A] [数值]，[指标B] [数值]，…，截至 [as_of_date]"，在日期后结束

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
        nav = tool_navigate_user_to(module, args.get("store") or "KSA")
        if not nav.get("ok"):
            return reply
        return (reply or "").rstrip() + f"\n\n入口：{nav['url']}"
    return reply


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
    m = _re.search(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", q_up)
    return m.group(0) if m else None


def _deterministic_replenishment_list_request(question: str) -> "Optional[int]":
    q = question or ""
    if _re.search(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", q.upper()):
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


def _current_workflow_task(workflow: str) -> Optional[dict]:
    try:
        rows = _data._fetch(
            "SELECT task_id, workflow, state FROM tasks WHERE tenant_id=? AND workflow=? "
            "AND state IN ('running','queued') ORDER BY COALESCE(last_heartbeat, started_at) DESC LIMIT 1",
            (_get_tenant(), workflow),
        )
        return dict(rows[0]) if rows else None
    except Exception:
        return None


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


_RUNNING_WORKFLOW_TASK_RE = re.compile(
    r"已有运行中实例:\s*\[\s*['\"]?([0-9a-fA-F]{8})"
)


def _existing_workflow_task_id(tool_result: dict) -> Optional[str]:
    """Return the real task id when governance denies only because the workflow is already running."""
    if not isinstance(tool_result, dict) or tool_result.get("action_type") != "denied":
        return None
    reason = tool_result.get("reason") or ""
    m = _RUNNING_WORKFLOW_TASK_RE.search(reason)
    return m.group(1).lower() if m else None


def _workflow_registry_summary(workflow: str, fallback_label: str) -> tuple[str, int, list]:
    try:
        from . import api as _api
        label, steps, affected = _api.WORKFLOW_REGISTRY.get(workflow, (fallback_label, [], []))
        return label, len(steps), affected
    except Exception:
        return fallback_label, 0, []


def _active_workflow_task(workflow: str) -> Optional[Dict]:
    rows = _data._fetch(
        "SELECT task_id, workflow, state FROM tasks "
        "WHERE tenant_id=? AND workflow=? AND state IN ('running', 'queued') "
        "ORDER BY COALESCE(last_heartbeat, started_at) DESC NULLS LAST LIMIT 1",
        (_get_tenant(), workflow),
    )
    if not rows:
        return None
    from . import api as _api
    label, steps, affected = _api.WORKFLOW_REGISTRY.get(workflow, (workflow, [], []))
    task = rows[0]
    return {
        "task_id": task["task_id"],
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "followup_prompt": None,
        "state": task.get("state"),
    }


def _logistics_task_evidence_check(task_id: str) -> Optional[str]:
    """物流入口证据检查（T21-SUB-3）：用 SUB-1 统一回读接口验证 durable 任务证据。

    返回 None → 证据完整（task row 存在 + ≥1 queued/started 事件），无需降级。
    返回字符串 → 证据缺失，调用方将此字符串作为降级回复（替换「已触发」）。

    任务表报错或事件缺失 → 回复降级为「未确认创建成功」，绝不返回假成功。
    孤儿事件（agent_events 有记录但 tasks 行不存在）同样降级，不放行假成功。
    """
    try:
        evidence = _data.get_task_with_events(task_id)
    except Exception:
        return ("物流后台任务**未确认创建成功**（任务表查询出错）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    if evidence is None:
        # task row 不存在（含孤儿事件场景：agent_events 有记录但 tasks 行缺失）
        return ("物流后台任务**未确认创建成功**（任务行不存在）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    if not evidence.get("events"):
        return ("物流后台任务**未确认创建成功**（任务记录或事件缺失）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    return None


# ── T07 freshness gate（确定性运营查询预检）────────────────────────────────
# 在 LLM 调用前插入：识别"最新/今天/TopN 销量"类运营查询 → 检查业务日覆盖 → 数据不足时
# 直接触发 workflow 或返回结构化不可用，禁止 LLM 自由补数（workflow_task=null + 模拟数事故源）。
_FRESHNESS_GATE_SALES_RE = _re.compile(
    # 显式时间窗 + 销售意图
    r"(?:今天|今日|最新|本周|这周|最近[0-9一两三四五六七八九十]+天?).*?(?:卖|销量|销售|热销|top\s*\d|前\s*\d|排名)"
    # 纯销售排名短语（无时间也隐含"最近"）
    r"|(?:卖得最好|卖得最多|热销|热门|销量最高|销量最多|最畅销|最好卖)"
    r"|(?:前[0-9]+|top\s*[0-9]+).*?(?:销量|卖|热销)"
    r"|哪[些个].*?(?:卖得最好|卖得最多|销量最高|最畅销|最好卖)",
    _re.IGNORECASE | _re.DOTALL,
)
# 明确拒绝刷新的否定短语 → 不触发 gate（用户想用现有数据答）
_FRESHNESS_GATE_SKIP_RE = _re.compile(
    r"(?:不用|不要|无需|先别).{0,8}(?:刷新|更新|同步)|就用现在的|先告诉我|不用等",
)
# WS-119: 库存类批量/榜单/约束查询（排行、可售、缺货、积压…）需 freshness gate 路由。
# 只匹配「批量/排序/数量约束」意图；单 SKU 实时问题由编码排除（见 _SKU_OR_ORDER_CODE_RE）。
_FRESHNESS_GATE_STOCK_RE = _re.compile(
    r"(?:库存|可售|缺货|断货|积压|备货).*?(?:排行|排名|榜|最多|最高|最大|最低|最少|多少|够不够|不够|缺口|清单|哪[些个]|top\s*\d|前\s*\d)"
    r"|(?:哪[些个]|多少).*?(?:库存|可售|缺货|断货|积压)"
    r"|(?:库存|可售|积压).*?(?:排行|排名|top\s*\d|前\s*\d)",
    _re.IGNORECASE | _re.DOTALL,
)
# WS-119: 物流类批量/榜单查询（在途/卡单/滞留排行、汇总）需 freshness gate 路由。
# 单 SKU/单货单实时问题继续优先走 query_sku_live/query_order_live（编码排除 + 既有 order_live 路由）。
_FRESHNESS_GATE_LOGISTICS_RE = _re.compile(
    r"(?:在途|卡单|滞留|压货|物流|货单|发货).*?(?:排行|排名|榜|最多|最高|多少|总量|汇总|清单|哪[些个]|top\s*\d|前\s*\d)"
    r"|(?:哪[些个]|多少).*?(?:在途|卡单|滞留|货单)"
    r"|(?:卡单|滞留).*?(?:货单|批次|sku|SKU)",
    _re.IGNORECASE | _re.DOTALL,
)
# WS-119 验收③：带明确 SKU/货单编码的单点实时问题 → 不当批量榜单 gate，交既有 live 工具（防退化成旧缓存）。
# 复用 _extract_live_order_no 的编码形态（2+ 字母 + 5+ 位字母数字/连字符），避免误吞 "top5"/"SKU" 这类非编码词。
_SKU_OR_ORDER_CODE_RE = _re.compile(r"\b[A-Z]{2,}[A-Z0-9-]{5,}\b")
_FRESHNESS_TARGET_ISO_DATE_RE = _re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_FRESHNESS_TARGET_CN_DATE_RE = _re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?")


def _extract_freshness_target_date(question: str) -> Optional[str]:
    """Extract an explicit target business date/window end_date from a question."""
    import datetime as _dt

    candidates = []
    for rx in (_FRESHNESS_TARGET_ISO_DATE_RE, _FRESHNESS_TARGET_CN_DATE_RE):
        for m in rx.finditer(question or ""):
            try:
                candidates.append(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                continue
    if not candidates:
        return None
    return max(candidates).isoformat()


def _detect_operational_domain(question: str) -> Optional[str]:
    """T07: 识别需要 freshness gate 的运营查询类型。

    返回值:
      'sales'      — 销量类查询，需要 freshness gate 路由
      'sales_skip' — 用户明确说不用刷新，但仍在问销量排名；gate 跳过但需注入陈旧提示
      'stock'      — 库存类批量/榜单/约束查询（WS-119）
      'logistics'  — 物流类批量/榜单查询（WS-119）
      None         — 无需 gate（非运营查询、明确说不刷、或带 SKU/货单编码的单点实时问题）
    """
    q = question or ""
    skip = bool(_FRESHNESS_GATE_SKIP_RE.search(q))
    sales = bool(_FRESHNESS_GATE_SALES_RE.search(q))
    if skip and sales:
        return "sales_skip"
    if skip:
        return None
    if sales:
        return "sales"
    # WS-119：库存/物流批量榜单查询接入同一 freshness gate。
    # 验收③：带明确 SKU/货单编码的单点实时问题不被批量 gate 捕获——交既有
    # query_sku_live / query_order_live（防退化成旧缓存）。
    has_code = bool(_SKU_OR_ORDER_CODE_RE.search(q.upper()))
    if not has_code:
        if _FRESHNESS_GATE_STOCK_RE.search(q):
            return "stock"
        if _FRESHNESS_GATE_LOGISTICS_RE.search(q):
            return "logistics"
    return None


def _freshness_gate_route(store: str, question: str, scope: Dict) -> Optional[Dict]:
    """T07: LLM 调用前确定性 freshness 路由。
    返回完整 chat response dict（直接返给调用方）；若数据已覆盖/无法匹配则返 None（继续走 LLM）。
    特例：返回 {"_stale_skip": True, "_stale_suffix": "..."} 时，调用方应继续走 LLM 并在
    LLM 回复后追加 _stale_suffix（确定性陈旧警示，避免依赖 LLM wording 导致 T07-2 flaky）。
    """
    from . import _provider as _prov
    domain = _detect_operational_domain(question)
    if not domain:
        return None
    target_date = _extract_freshness_target_date(question)

    # sales_skip: 用户明确说不刷新但仍问销量排名。不拦截 LLM，但检查数据新鲜度，
    # 若数据陈旧则返回确定性陈旧后缀供调用方追加（代码级注入，不依赖 LLM wording）。
    if domain == "sales_skip":
        freshness = _data.check_freshness_coverage(store, "sales", target_date)
        latest = freshness.get("latest_date") or ""
        target = freshness.get("target_date") or ""
        if freshness.get("covered"):
            suffix = (
                "\n\n（提示：按你的要求本轮没有刷新，直接使用当前销量数据"
                + (f"，最新到 {latest}" if latest else "")
                + "；noon 销量同步可能滞后，结果偏保守。）"
            )
            return {"_stale_skip": True, "_stale_suffix": suffix}
        target_s = f"目标日期 {target} 暂未覆盖" if target_date else "未更新到今天"
        suffix = (
            f"\n\n（⚠️ 提示：当前销量数据{target_s}"
            + (f"，最新到 {latest}" if latest else "")
            + "，如需最新数据请随时刷新。）"
        )
        return {"_stale_skip": True, "_stale_suffix": suffix}

    freshness = _data.check_freshness_coverage(store, domain, target_date)
    if freshness.get("covered"):
        return None  # 数据新鲜 → 继续走 LLM/既有确定性路由（用最新业务日算）

    # WS-119：文案按域走，库存/物流不串"销量"措辞。
    domain_label = {"sales": "销量", "stock": "库存", "logistics": "物流"}.get(domain, domain)
    action = freshness.get("action") or "unavailable"
    latest = freshness.get("latest_date") or ""
    target = freshness.get("target_date") or ""
    wf = freshness.get("workflow")
    when_s = f"最新到 {latest}" if latest else "暂无数据"

    if action == "run_workflow" and wf:
        tool_result = _exec_tool("run_workflow", {"workflow": wf, "followup_prompt": question}, user=scope)
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
                f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，"
                f"已触发更新（{wf}）。跑完后我会接着告诉你。"
            )
        else:
            err = (tool_result or {}).get("error") or ""
            reply = f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，更新触发失败：{err}"
        return {
            "reply": reply, "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None, "tools_used": ["run_workflow"], "tag": "执行",
            "workflow_task": workflow_task,
            "provider": _prov.get_provider(), "confidence": 1.0,
            "judge_method": "freshness_gate", "freshness_gate": freshness,
            "hallucination_warnings": None,
        }

    if action == "upload_csv":
        csv_hint = freshness.get("csv_hint") or {}
        reply = (
            f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，无法自动刷新此部分。\n\n"
            f"👉 请到{csv_hint.get('where', '紫鸟 noon 后台')}导出 CSV，"
            f"文件名形如 `{csv_hint.get('csv_pattern', 'sales_noon_*.csv')}`，"
            "拖到工作台 📤 上传区。上传后我会接着告诉你。"
        )
        return {
            "reply": reply, "clean_reply": reply, "references": [],
            "action_id": None, "tools_used": [], "tag": "信息",
            "workflow_task": None,
            "provider": _prov.get_provider(), "confidence": 1.0,
            "judge_method": "freshness_gate", "freshness_gate": freshness,
            "hallucination_warnings": None,
        }

    # fallback: 数据不足，无 workflow 可跑
    reply = f"数据不足：{domain_label}{when_s}，无法提供 {target} 的查询结果。"
    return {
        "reply": reply, "clean_reply": reply, "references": [],
        "action_id": None, "tools_used": [], "tag": "信息",
        "workflow_task": None,
        "provider": _prov.get_provider(), "confidence": 1.0,
        "judge_method": "freshness_gate", "freshness_gate": freshness,
        "hallucination_warnings": None,
    }


def chat(messages: List[Dict], scope: Dict) -> Dict:
    """
    messages: [{role: 'user'|'assistant', content: '...'}]
    scope: {store, current_user, current_role, tenant_id, user_id, ...}
    返回: {reply, clean_reply, references, action_id, tag, workflow_tasks, tools_used, provider, confidence}

    走 _provider 抽象层，通过 LLM_PROVIDER env 切换 anthropic / qwen / deepseek / doubao。
    """
    from . import _provider

    # 把 scope.tenant_id 注入 contextvars，让所有 tool 函数（同线程）能拿到
    _chat_tenant.set(scope.get("tenant_id") or 1)
    _chat_scope.set(scope)
    _last_replenishment_stock_status.set(None)
    _last_sku_rate_stats.set(None)
    # 同时设给 data 层（PG RLS 用）
    _data.set_current_tenant(scope.get("tenant_id") or 1)

    question = messages[-1].get("content") if messages else ""
    if isinstance(question, list):  # content 可能是 blocks
        question = " ".join(b.get("text", "") for b in question if isinstance(b, dict))
    _chat_question.set(question or "")

    # WS-145 肯定执行意图门:本轮句式语气 + 风险分层一次求出，注入 contextvar，
    # 供 _exec_tool 在非执行语气下拒绝 run_workflow（LLM 不许绕）。
    from . import _execution_intent_gate as _intent_gate
    _intent_decision = _intent_gate.evaluate(question or "")
    _chat_intent.set(_intent_decision)

    # WS-150: 工作台不支持主动发飞书/通知群（确定性拒绝，不进 confirm-first）
    if _intent_decision.unsupported_feishu_notify:
        reply = _intent_gate.unsupported_feishu_notify_reply()
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "拒绝",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "execution_intent_gate_unsupported_feishu_notify",
            "hallucination_warnings": None,
        }

    # 高风险动作（外部通知/交易·采购·订单/不可回滚/跨店批量覆盖）即使肯定句也不自动执行:
    # 先 confirm，不自动补调。确定性短路，绝不落任务。
    if _intent_decision.needs_confirm_first:
        reply = _intent_gate.confirm_first_reply(question or "")
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "确认",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "execution_intent_gate_confirm_first",
            "hallucination_warnings": None,
        }

    direct_export = _deterministic_export_request(question)
    if direct_export:
        store = scope.get("store") or "KSA"
        tool_args = {
            "view": direct_export["view"],
            "store": store,
            "filter_desc": direct_export["filter_desc"],
        }
        tool_result = _exec_tool("export_table", tool_args, user=scope)
        if isinstance(tool_result, dict) and tool_result.get("ok") and tool_result.get("download_url"):
            filename = tool_result.get("filename") or "export.xlsx"
            reply = (
                f"已生成 {tool_result.get('row_count', 0)} 行表格："
                f"[{filename}]({tool_result['download_url']})"
            )
        else:
            reply = (tool_result or {}).get("message") or (tool_result or {}).get("error") or "导出失败。"
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": ["export_table"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_export_router",
            "hallucination_warnings": None,
        }

    direct_readonly = _deterministic_readonly_request(question)
    if direct_readonly:
        store = (scope.get("store") or "KSA").upper()
        tool_args = {"store": store}
        tool_result = _exec_tool(direct_readonly["tool"], tool_args, user=scope)
        reply = _deterministic_readonly_reply(
            direct_readonly["intent"], tool_result or {}, store
        )
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": [direct_readonly["tool"]],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_readonly_router",
            "hallucination_warnings": None,
        }

    # T37 round-15（Luke 指令①）：库存刷新副作用动作，用户明确拒绝（任意语序）→
    # 确定性回复，绝不调 run_workflow，绝不伪造任务号/已启动声明。
    stock_refusal_reply = _stock_refresh_refusal_reply(question)
    if stock_refusal_reply:
        return {
            "reply": stock_refusal_reply,
            "clean_reply": stock_refusal_reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "执行",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_stock_refusal_router",
            "hallucination_warnings": None,
        }

    direct_workflow = _deterministic_workflow_request(question)
    if direct_workflow:
        tool_args = {"workflow": direct_workflow["workflow"], "followup_prompt": question}
        tool_result = _exec_tool("run_workflow", tool_args, user=scope)
        workflow_tasks = []
        if isinstance(tool_result, dict) and tool_result.get("ok"):
            task_id = tool_result["task_id"]
            workflow_tasks.append({
                "ok": True,
                "task_id": task_id,
                "workflow": tool_result.get("workflow", direct_workflow["workflow"]),
                "label": tool_result.get("label", direct_workflow.get("label", direct_workflow["workflow"])),
                "total_steps": tool_result.get("total_steps", 0),
                "affected_modules": tool_result.get("affected_modules", []),
                "followup_prompt": tool_result.get("followup_prompt"),
            })
            # T21-SUB-2: 三态受理回执（已排队/已开始/已完成·失败），
            # 直接回答「是否已创建」并附 task_id/workflow/状态，禁止只说「已触发」。
            reply = _workflow_receipt_reply(
                task_id, tool_result["workflow"], direct_workflow["label"]
            )
            impact = _workflow_business_impact_reply(tool_result["workflow"], question)
            if impact:
                reply = f"{reply}\n\n{impact}"
            # T21-SUB-3 物流入口专项降级：用回读接口验证 durable 任务证据；
            # 任务表报错或事件缺失时降级回复，不返回假成功。
            if direct_workflow.get("workflow") == "wf3_logistics_v2":
                degrade_msg = _logistics_task_evidence_check(task_id)
                if degrade_msg:
                    reply = degrade_msg
        elif (
            isinstance(tool_result, dict)
            and tool_result.get("action_type") == "denied"
            and "已有运行中实例" in (tool_result.get("reason") or "")
        ):
            existing = _current_workflow_task(direct_workflow["workflow"])
            if existing:
                workflow_tasks.append({
                    "ok": True,
                    "task_id": existing["task_id"],
                    "workflow": existing["workflow"],
                    "label": direct_workflow["label"],
                    "total_steps": None,
                    "affected_modules": [],
                    "followup_prompt": question,
                    "state": existing.get("state"),
                    "already_running": True,
                })
                reply = (
                    f"{direct_workflow['label']}已有运行中的后台任务 "
                    f"`{existing['task_id']}`，我不重复触发。"
                )
            else:
                # DB not yet updated; parse ID from denial reason string
                extracted_id = _existing_workflow_task_id(tool_result)
                if extracted_id:
                    workflow = direct_workflow["workflow"]
                    label, total_steps, affected = _workflow_registry_summary(
                        workflow, direct_workflow["label"]
                    )
                    workflow_tasks.append({
                        "ok": True,
                        "task_id": extracted_id,
                        "workflow": workflow,
                        "label": label,
                        "total_steps": total_steps,
                        "affected_modules": affected,
                        "followup_prompt": question,
                    })
                    reply = (
                        f"{direct_workflow['label']}已有同类后台任务在运行，未新建重复任务。\n"
                        f"任务 ID：{extracted_id}｜workflow：{workflow}｜当前状态：运行中或排队。\n"
                        "请在工作台任务面板查看进度；任务结束后如仍需刷新，可以再重试。"
                    )
                else:
                    reply = tool_result.get("reason") or "工作流触发失败。"
        else:
            existing_task_id = _existing_workflow_task_id(tool_result or {})
            if existing_task_id:
                workflow = direct_workflow["workflow"]
                label, total_steps, affected = _workflow_registry_summary(
                    workflow, direct_workflow["label"]
                )
                workflow_tasks.append({
                    "ok": True,
                    "task_id": existing_task_id,
                    "workflow": workflow,
                    "label": label,
                    "total_steps": total_steps,
                    "affected_modules": affected,
                    "followup_prompt": question,
                })
                reply = (
                    f"{direct_workflow['label']}已有同类后台任务在运行，未新建重复任务。\n"
                    f"任务 ID：{existing_task_id}｜workflow：{workflow}｜当前状态：运行中或排队。\n"
                    "请在工作台任务面板查看进度；任务结束后如仍需刷新，可以再重试。"
                )
            else:
                workflow_tasks.append({
                    "ok": False,
                    "workflow": direct_workflow["workflow"],
                    "label": direct_workflow.get("label", direct_workflow["workflow"]),
                    "error": (tool_result or {}).get("error") or "触发失败",
                    "task_id": None,
                })
                reason = (
                    (tool_result or {}).get("message")
                    or (tool_result or {}).get("error")
                    or (tool_result or {}).get("reason")
                )
                if reason:
                    reply = reason
                elif direct_workflow.get("workflow") == "wf3_logistics_v2":
                    reply = ("物流后台任务**未确认创建成功**（工作流触发失败）。"
                             "请稍后在工作台任务面板确认任务状态，或重试。")
                else:
                    reply = "本轮没有创建后台任务：工作流触发失败，请稍后重试。"
                # WS-145 自动补调策略:确定性路由这一次触发即「自动补调一次」。
                # 失败后 policy 判定转 plan→confirm（不无限重试），追加下一步 + 需确认，
                # 绝不返回「已触发/已完成」假证据。
                from . import _execution_intent_gate as _intent_gate
                if (
                    _intent_gate.decide_recovery(_intent_gate.RiskTier.LOW_AUTO, 1)
                    == _intent_gate.RecoveryAction.PLAN_CONFIRM
                ):
                    reply = reply.rstrip() + (
                        "\n\n下一步:这步自动补调一次仍未成功，我不再自动重复触发——"
                        "回「确认」我再试一次，或回「取消」改用上传/手动核对。"
                    )
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["run_workflow"],
            "tag": "执行",
            "workflow_tasks": workflow_tasks,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_workflow_router",
            "hallucination_warnings": None,
        }

    direct_sales_topn_n = _deterministic_product_sales_topn_request(question)
    if direct_sales_topn_n is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "list_products",
            {"store": store, "listing": "all", "limit": direct_sales_topn_n},
            user=scope,
        )
        reply = _format_product_sales_topn_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["list_products"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if not (tool_result or {}).get("error") else 0.8,
            "judge_method": "deterministic_product_sales_topn_router",
            "hallucination_warnings": None,
        }

    if _provider.get_provider() != "smoke" and _deterministic_products_count_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("list_products", {"store": store, "listing": "all", "limit": 0}, user=scope)
        reply = _format_products_count_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["list_products"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_products_count_router",
            "hallucination_warnings": None,
        }

    if _deterministic_scope_overview_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("scope_overview", {"store": store}, user=scope)
        reply = _format_scope_overview_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["scope_overview"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_scope_overview_router",
            "hallucination_warnings": None,
        }

    if _deterministic_data_freshness_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("data_health_check", {"store": store}, user=scope)
        reply = _format_data_freshness_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["data_health_check"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_data_freshness_router",
            "hallucination_warnings": None,
        }

    direct_replenishment_sku = _deterministic_replenishment_sku_request(question)
    if direct_replenishment_sku:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "query_replenishment_sku",
            {"sku": direct_replenishment_sku, "store": store},
            user=scope,
        )
        from . import replenishment_evidence as _rep
        reply = _rep.format_replenishment_sku_reply(tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_replenishment_sku"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if (tool_result or {}).get("ok") else 0.9,
            "judge_method": "deterministic_replenishment_sku_router",
            "hallucination_warnings": None,
        }

    direct_replenishment_limit = _deterministic_replenishment_list_request(question)
    if direct_replenishment_limit is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "compute_replenishment",
            {"store": store, "limit": direct_replenishment_limit},
            user=scope,
        )
        reply = _format_replenishment_list_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["compute_replenishment"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if not (tool_result or {}).get("fail_closed") else 0.9,
            "judge_method": "deterministic_replenishment_list_router",
            "hallucination_warnings": None,
        }

    direct_stock_split_sku = _deterministic_stock_split_request(question)
    if direct_stock_split_sku:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("query_stock_split", {"sku": direct_stock_split_sku, "store": store}, user=scope)
        reply = _format_stock_split_reply(direct_stock_split_sku, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_stock_split"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_stock_split_router",
            "hallucination_warnings": None,
        }

    direct_sku_metric = _deterministic_sku_metric_request(question)
    if direct_sku_metric:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "query_sku",
            {"skus": [direct_sku_metric], "store": store},
            user=scope,
        )
        reply = _format_sku_metric_reply(direct_sku_metric, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_sku"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_sku_metric_router",
            "hallucination_warnings": None,
        }

    direct_order_no = _extract_live_order_no(question)
    if direct_order_no:
        tool_result = _exec_tool("query_order_live", {"order_no": direct_order_no}, user=scope)
        reply = _format_order_live_reply(direct_order_no, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_order_live"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_order_live_router",
            "hallucination_warnings": None,
        }

    # T15 — 总库存 TopN 确定性路由（WS-139）
    direct_topn_n = _deterministic_total_stock_topn_request(question)
    if direct_topn_n is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("total_stock_topn", {"store": store, "n": direct_topn_n}, user=scope)
        reply = _format_total_stock_topn_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["total_stock_topn"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_total_stock_topn_router",
            "hallucination_warnings": None,
        }

    # T07: freshness gate — 运营查询（TopN 销量等）在 LLM 前先检业务日覆盖
    store = scope.get("store", "KSA")
    gate_result = _freshness_gate_route(store, question, scope)
    _t07_stale_suffix = ""
    if gate_result is not None:
        if gate_result.get("_stale_skip"):
            # sales_skip 场景：继续走 LLM，事后追加确定性陈旧提示
            _t07_stale_suffix = gate_result.get("_stale_suffix", "")
        else:
            return gate_result

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
    workflow_tasks = result.get("workflow_tasks", [])
    tools_used     = [t["name"] for t in tool_log]

    # T07-2 sales_skip: 确定性陈旧后缀（代码级注入，不依赖 LLM wording）
    if _t07_stale_suffix:
        clean_reply += _t07_stale_suffix

    # Layer 3 hallucinate 后处理（上移自 api.py — 一处产生 warnings，既喂 confidence 又 sanitize）
    # final_text = 展示版（可能带 banner）；clean_reply = 持久化版（无 banner，防历史自激）
    from . import _safety
    final_text, hallu_warnings = _safety.sanitize_reply(clean_reply, tools_used, tool_log=tool_log, question=question)
    clean_reply = _strip_safety_banner(final_text)

    # WS-117 采购议价率口径生产接线（deterministic verifier，非 prompt）
    from hipop.rules.procurement_rate import check_procurement_rate_reply as _check_procurement_rate
    _procurement_warns = _check_procurement_rate(clean_reply)
    if _procurement_warns:
        hallu_warnings = list(hallu_warnings or []) + _procurement_warns
        if not final_text.startswith("⚠️"):
            _proc_banner = (
                "⚠️ **系统检测到采购议价率口径可能有误**：\n"
                + "\n".join(f"- {w}" for w in _procurement_warns)
                + "\n\n---\n\n"
            )
            final_text = _proc_banner + final_text

    final_text = _maybe_append_stock_readiness_warning(final_text)
    clean_reply = _maybe_append_stock_readiness_warning(clean_reply)
    final_text = _ensure_export_download_link(final_text, tool_log)
    clean_reply = _ensure_export_download_link(clean_reply, tool_log)
    final_text = _maybe_inject_missing_rates(final_text, question)
    clean_reply = _maybe_inject_missing_rates(clean_reply, question)
    final_text = _maybe_append_oldest_data_health_date(final_text, question, tools_used, scope)
    clean_reply = _maybe_append_oldest_data_health_date(clean_reply, question, tools_used, scope)
    final_text = _maybe_append_order_lookup_negative_hint(final_text, question, tools_used)
    clean_reply = _maybe_append_order_lookup_negative_hint(clean_reply, question, tools_used)
    final_text = _maybe_append_navigation_url(final_text, tool_log)
    clean_reply = _maybe_append_navigation_url(clean_reply, tool_log)

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
            first_tool_args = _safety._normalize_args(tool_log[0].get("args") or {}) if tool_log else {}
            action_id = _data.write_agent_action(
                store=scope.get("store", "KSA"),
                module="chat",
                action_type="execute",
                subject=(first_tool_args.get("sku") or first_tool_args.get("order_no")) if tool_log else None,
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
        "workflow_tasks": workflow_tasks,
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

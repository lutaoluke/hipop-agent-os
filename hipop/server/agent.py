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
                "status": {"type": "string", "enum": ["已确认推进", "已确认丢货", "已约仓", "已结案", "处理中"]},
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
            "用户要求『导出/下载/给我表格/Excel』时调本工具。"
            "本系统目前 **没有 Excel 导出能力**——本工具仅返回 stub 引导用户去模块页用浏览器内置导出，或自行复制。"
            "\n严禁不调本 tool 自行宣称『已导出』『生成了下载链接』；那是事故。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "description": "用户想导什么：replenish / sales / logistics / sku_health / orders 等"},
                "format": {"type": "string", "enum": ["excel", "csv", "feishu"]},
                "filter_desc": {"type": "string", "description": "用户筛选条件描述（可选）"},
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
            "异步触发后台工作流（每个 workflow 都是耗时操作，立即返回 task_id 让前端订阅 SSE 进度）。\n"
            "可选 workflow:\n"
            "- wf1_stock：拉 ERP 6 仓 + noon Inventory 库存 + 飞书同步\n"
            "- wf2_sales：拉商品库 + ERP 销量 + noon CSV 累加 + 聚合销量评级 + 飞书同步\n"
            "- wf3_logistics：扫所有 entity 物流货单 + 写 hub + 飞书同步\n"
            "- wf5_sales_cycle：销售周期 + 补货决策 + 飞书 sync_decisions\n"
            "- wf6_alerts：生成物流告警 + 飞书 alerts/warehouse_appt\n"
            "- daily：每日例行（wf3 + wf6 + 推日报卡片）\n"
            "- weekly：每周例行全链路（wf1 + wf2 + wf3 + wf6 + wf5 + 周报卡片）\n"
            "数据陈旧或用户说『跑/刷新/重新算/同步』时调本工具。\n"
            "**重要**：如果用户原始问题需要等数据跑完才能答（例如『我该补货吗』而 wf5 数据陈旧），"
            "在 followup_prompt 字段填上『需要等工作流跑完后接续答的问题』，前端会在 task 完成后自动重新发起一轮 chat，"
            "你那时再用最新数据答最终结论。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": ["wf1_stock", "wf2_sales", "wf3_logistics",
                             "wf5_sales_cycle", "wf6_alerts", "daily", "weekly",
                             "wf2_products_v2", "wf2_sales_v2", "wf1_stock_v2",
                             "wf5_sales_cycle_v2", "refresh_all_v2"],
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
    rows = _data.get_replenishment(store, limit=limit)
    items = [{
        "sku": r["partner_sku"], "title": r["title"], "qty": r["qty"],
        "urgency": r["urgency_level"], "daily_rate": r["daily_rate"], "trend": r["trend"],
        "advice": r["ops_advice"],
    } for r in rows]
    return {
        "store": store, "count": len(items), "items": items,
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


def tool_export_table(view: str, format: str = "excel", filter_desc: str = "") -> Dict:
    """stub — 没真 export 能力，引导用户去模块页用浏览器导出。"""
    module_map = {
        "replenish": "/module/replenish",
        "sales": "/module/sales",
        "sku_health": "/module/sales",
        "logistics": "/module/logistics",
        "orders": "/module/logistics",
    }
    path = module_map.get(view, f"/module/{view}")
    return {
        "ok": False,
        "supported": False,
        "view": view,
        "format": format,
        "message": (
            f"系统目前没有内建 export 能力。请去工作台 {path} 页面，"
            f"用浏览器右键『另存为』或选中表格复制到 Excel/飞书。"
            "如需自动化导出，是产品化阶段 2 的功能。"
        ),
        "navigate_to": path,
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
}


def _exec_tool(name: str, args: dict, user: dict = None) -> dict:
    """tool 执行前先过 RBAC（user.role → 是否允许这个 tool）。"""
    try:
        from . import rbac as _rbac
        if user and not _rbac.tool_allowed(user, name):
            return {
                "error": "permission_denied",
                "tool": name,
                "user_role": user.get("role"),
                "message": f"当前角色 {user.get('role')} 不能调用 {name}（请向 owner/manager 申请权限）",
            }
        fn = TOOL_FUNCS[name]
        return fn(**args)
    except KeyError:
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── Chat 主入口 ──────────────────────────────────────
SYSTEM_PROMPT = """你是点购 Agent OS 的店铺协作 Agent，工作在共同空间内（5 个同事 + 1 个你）。

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

**3b. 用户问"刷新 / 跑工作流 / 同步数据 / 重算 X"时，必须直接 `run_workflow`，禁止教用户怎么点按钮**
   - 你**有** run_workflow tool，能直接触发后台跑（前端会自动订 SSE 显进度）
   - 禁说"这个需要组长/管理员账号才能触发" / "我没有权限" / "Agent 当前没有权限" —— 你已经被赋予 run_workflow，能跑就跑；只有 tool 真返回 `permission_denied` 才能这么回
   - 禁说"在工作台 sidebar 找到 X → 点 Y" —— 这种 UI 路径几乎必编错；直接 run_workflow 就对了

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


def chat(messages: List[Dict], scope: Dict) -> Dict:
    """
    messages: [{role: 'user'|'assistant', content: '...'}]
    scope: {store, current_user, current_role, tenant_id, user_id, ...}
    返回: {reply, references, action_id, tag, workflow_task, tools_used, provider}

    走 _provider 抽象层，通过 LLM_PROVIDER env 切换 anthropic / qwen / deepseek / doubao。
    """
    from . import _provider

    # 把 scope.tenant_id 注入 contextvars，让所有 tool 函数（同线程）能拿到
    _chat_tenant.set(scope.get("tenant_id") or 1)
    _chat_scope.set(scope)
    # 同时设给 data 层（PG RLS 用）
    _data.set_current_tenant(scope.get("tenant_id") or 1)

    sys_text = SYSTEM_PROMPT.format(scope=json.dumps(scope, ensure_ascii=False))
    result = _provider.chat_with_tools(
        messages=messages,
        system=sys_text,
        tools=TOOLS,
        tool_funcs=TOOL_FUNCS,
        scope=scope,
    )
    final_text   = result["reply"]
    tool_log     = result["tool_log"]
    refs_collected = result["refs_collected"]
    workflow_task  = result.get("workflow_task")

    # 写入 agent_actions（reference 系统）
    action_id = None
    if final_text and (refs_collected or tool_log):
        try:
            action_id = _data.write_agent_action(
                store=scope.get("store", "KSA"),
                module="chat",
                action_type="execute",
                subject=tool_log[0]["args"].get("sku") or tool_log[0]["args"].get("order_no") if tool_log else None,
                judge=final_text[:200],
                pill_text="执行" if tool_log else "信息",
                pill="info",
                confidence=0.9,
                options=[],
                references=_dedup_refs(refs_collected),
                owner=scope.get("current_user", "Cherry"),
            )
        except Exception:
            pass

    return {
        "reply": final_text.strip() or "(无回复)",
        "references": _dedup_refs(refs_collected),
        "action_id": action_id,
        "tools_used": [t["name"] for t in tool_log],
        "tag": "执行" if tool_log else None,
        "workflow_task": workflow_task,
        "provider": _provider.get_provider(),
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

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
import os, sys, json, sqlite3, time
from typing import List, Dict, Any, Optional

# 让 hipop/scripts/* import
HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(HIPOP_ROOT)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from . import data as _data

import anthropic
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


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
]


# ── 工具实现 ──────────────────────────────────────────
def tool_query_sku(skus: List[str], store: str = "KSA") -> Dict:
    s = store.lower()
    out = []
    refs = []
    for sku in skus[:3]:
        rows = _data._fetch(f"""
            SELECT w2.partner_sku, w2.title, w2.sales_grade, w2.latest_profit_rate,
                   w2.sales_30d, w2.sales_10d, w2.latest_price,
                   w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
                   w5.current_pipeline, w5.weekly_total_replenish,
                   h.in_transit_total_qty, h.has_stuck_batch, h.needs_ops_input
            FROM wf2_hipop_{s}_sku w2
            LEFT JOIN wf5_hipop_{s}_sales_cycle w5 ON w2.partner_sku = w5.partner_sku
            LEFT JOIN wf3_logistics_hub h ON w2.partner_sku = h.sku
            WHERE w2.partner_sku = ?
        """, (sku,))
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
        refs.append({"table": f"wf2_hipop_{s}_sku", "where": f"partner_sku='{sku}'", "as_of_date": "today"})
        refs.append({"table": f"wf5_hipop_{s}_sales_cycle", "where": f"partner_sku='{sku}'", "as_of_date": "today"})
        refs.append({"table": "wf3_logistics_hub", "where": f"sku='{sku}'", "as_of_date": "today"})
    return {"items": out, "references": refs}


def tool_query_order(order_no: str) -> Dict:
    rows = _data._fetch("""
        SELECT alert_id, alert_level, alert_reason, sku_list_json, ops_status,
               actual_stay_days, history_stage_days, stage, created_at, action_owner
        FROM wf6_logistics_alerts WHERE order_no=? ORDER BY created_at DESC
    """, (order_no,))
    for r in rows:
        try:
            r["skus"] = json.loads(r["sku_list_json"] or "[]")
        except Exception:
            r["skus"] = []
    return {
        "order_no": order_no,
        "alerts": rows,
        "references": [{"table": "wf6_logistics_alerts", "where": f"order_no='{order_no}'", "as_of_date": "today"}],
    }


def tool_update_alert_status(order_no: str, status: str, note: str = "") -> Dict:
    try:
        from workflows.wf_logistics_alerts import update_alert_status as _u
    except Exception as e:
        return {"ok": False, "error": str(e)}
    rows = _data._fetch(
        "SELECT alert_id FROM wf6_logistics_alerts WHERE order_no=? AND resolved_at IS NULL",
        (order_no,)
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
        "references": [{"table": "wf6_logistics_alerts", "where": f"order_no='{order_no}' (写入)", "as_of_date": "now"}],
    }


def tool_scope_overview(store: str) -> Dict:
    o = _data.get_today(store)
    return {
        **o,
        "references": [
            {"table": f"wf2_hipop_{store.lower()}_sku", "where": "is_listed=1", "as_of_date": "today"},
            {"table": f"wf5_hipop_{store.lower()}_sales_cycle", "where": "all rows", "as_of_date": "today"},
            {"table": "wf3_logistics_hub", "where": "all rows", "as_of_date": "today"},
            {"table": "wf6_logistics_alerts", "where": "ops_status='待处理'", "as_of_date": "today"},
        ],
    }


def tool_compute_replenishment(store: str, limit: int = 10) -> Dict:
    rows = _data.get_replenishment(store, limit=limit)
    items = [{
        "sku": r["partner_sku"], "title": r["title"], "qty": r["qty"],
        "urgency": r["urgency_level"], "daily_rate": r["daily_rate"], "trend": r["trend"],
        "advice": r["ops_advice"],
    } for r in rows]
    return {
        "store": store, "count": len(items), "items": items,
        "references": [
            {"table": f"wf5_hipop_{store.lower()}_sales_cycle", "where": "weekly_total_replenish>0", "as_of_date": "today"},
            {"table": f"wf6_hipop_{store.lower()}_replenishment_queue", "where": "all rows", "as_of_date": "today"},
        ],
    }


def tool_compute_air_freight_roi(sku: str, store: str, qty: int = 100) -> Dict:
    """简化模型: 海运 0.4 / 件, 空运 2.5 / 件, 海运 25d, 空运 5d. 结合销量 + 利润率估损失.
    用 daily_rate * (25-5) 天 * 利润 = 海运多损失多少订单利润 → 与多花的 (2.5-0.4)*qty 对比."""
    s = store.lower()
    rows = _data._fetch(f"""
        SELECT w2.partner_sku, w2.latest_price, w2.latest_profit_rate,
               w5.daily_rate, w5.trend
        FROM wf2_hipop_{s}_sku w2
        LEFT JOIN wf5_hipop_{s}_sales_cycle w5 ON w2.partner_sku=w5.partner_sku
        WHERE w2.partner_sku=?
    """, (sku,))
    if not rows:
        return {"ok": False, "error": f"SKU {sku} 不存在于 wf2_hipop_{s}_sku"}
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
            {"table": f"wf2_hipop_{s}_sku", "where": f"partner_sku='{sku}'", "as_of_date": "today"},
            {"table": f"wf5_hipop_{s}_sales_cycle", "where": f"partner_sku='{sku}'", "as_of_date": "today"},
        ],
    }


def tool_data_health_check(store: str) -> Dict:
    h = _data.get_data_health(store)
    return {
        **h,
        "references": [
            {"table": f"wf2_hipop_{store.lower()}_sku", "where": "MAX(imported_at)", "as_of_date": "now"},
            {"table": f"wf5_hipop_{store.lower()}_sales_cycle", "where": "MAX(updated_at)", "as_of_date": "now"},
            {"table": "wf3_logistics_hub", "where": "MAX(updated_at)", "as_of_date": "now"},
        ],
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
}


def _exec_tool(name: str, args: dict) -> dict:
    try:
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

当前 scope（参考）:
{scope}

回答要求:
- 优先调用工具拿真数据（query_sku / query_order / scope_overview / compute_replenishment / data_health_check / compute_air_freight_roi / update_alert_status）
- 给出判断（趋势 / 紧迫度）+ 简明建议（量化, 可执行）
- 中文，简洁，2-4 句一段，不要罗列冗长字段
- 不知道时直说，不要瞎编。涉及更新的操作（update_alert_status）需要用户确认意图后再调用
"""


def chat(messages: List[Dict], scope: Dict) -> Dict:
    """
    messages: [{role: 'user'|'assistant', content: '...'}]
    scope: {store, current_user, current_role, ...}
    返回: {reply, references, action_id, tag}
    """
    client = _get_client()
    sys_text = SYSTEM_PROMPT.format(scope=json.dumps(scope, ensure_ascii=False))

    # 截断 history（最近 8 轮）
    hist = messages[-16:]
    msgs = []
    for m in hist:
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
        else:
            msgs.append({"role": m["role"], "content": c})

    refs_collected = []
    tool_log = []
    final_text = ""

    for hop in range(6):  # 最多 6 轮 tool-use
        resp = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            system=sys_text,
            messages=msgs,
            tools=TOOLS,
            max_tokens=2048,
        )
        # 累积 assistant 内容
        msgs.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # 取最终文本
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    final_text += block.text
            break

        # 收集 tool_use blocks
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                final_text += block.text + "\n"
            elif getattr(block, "type", None) == "tool_use":
                tool_name = block.name
                tool_args = block.input or {}
                result = _exec_tool(tool_name, tool_args)
                if isinstance(result, dict) and "references" in result:
                    refs_collected.extend(result["references"])
                tool_log.append({"name": tool_name, "args": tool_args, "result_keys": list(result.keys()) if isinstance(result, dict) else None})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
        msgs.append({"role": "user", "content": tool_results})

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

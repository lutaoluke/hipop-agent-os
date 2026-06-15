"""WS-166：业务工具实现（从 agent.py 外移，纯物理搬迁，行为/签名/返回契约不变）。

agent.py 是 CODEOWNERS 锁定的共享热点文件 + 防回潮棘轮盯防对象。本模块承载
`tool_*` / `_tool_*` 的真实实现；agent.py 仅保留：工具注册（TOOLS schema）、
分发表投影（TOOL_FUNCS）、统一治理执行入口（_exec_tool）与对话编排（chat）。

接线 / 不绕治理 / 不破坏既有契约：
- 本模块**不自建分发表、不直调 destructive 实现绕过治理**。破坏性工具
  （run_workflow / update_alert_status）一律经 `agent._exec_tool` → governance
  funnel 调到（见 smoke_agent_antiregress_ratchet 的 destructive funnel 棘轮）。
- agent.py 在定义完底层 helper 后 `from .tools_impl import (...)` 把这些实现再导出，
  保持 `agent.tool_*` 外部契约（api.py / 既有测试 / TOOL_FUNCS 投影）不变。
- 工具实现对 agent.py 提供的 helper / contextvar / 注入点（_get_tenant / _data /
  _erp_token_or_error / _sku_sales_live_fn / TOOL_FUNCS …）一律以 `agent.<name>`
  在调用期动态取值——这样既共享同一对象、又让 `patch('hipop.server.agent.X')`
  这类既有测试注入仍命中实现（纯物理外移不改测试契约）。
"""
import os, json
from typing import List, Dict, Optional

from . import agent

def tool_query_sku(
    skus: List[str],
    store: str = "KSA",
    allow_cache_on_live_failure: bool = False,
    reject_cache_on_live_failure: bool = False,
) -> Dict:
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""

    # 国别 ID（ERP 实时取数用）
    _country = {"KSA": "SA", "UAE": "AE", "SA": "SA", "AE": "AE"}.get(store.upper(), "SA")
    _nation_id = {"SA": 1, "AE": 2}.get(_country, 1)

    # ERP token（best-effort；失败时销量字段降级，不阻断整个工具执行）
    _erp_token = None
    try:
        _tok, _err = agent._erp_token_or_error(tid)
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
    latest_noon_order = _date10(agent._data._scalar(
        "SELECT MAX(order_date) FROM wf2_orders WHERE tenant_id=? AND entity_alias=?",
        (tid, alias),
    ))
    noon_order_stale_days = _days_old(latest_noon_order)
    noon_orders_stale = (noon_order_stale_days is None) or (noon_order_stale_days > 3)

    for sku in skus[:3]:
        # ── T03 门：强制走实时取数路径拿销量数字 ──────────────────────
        live_fn = agent._sku_sales_live_fn if agent._sku_sales_live_fn is not None else agent._erp_sku_stats_live
        try:
            live_result = live_fn(sku, _nation_id, _erp_token)
        except Exception as _e:
            live_result = {"ok": False, "error": f"live_fn_exception: {type(_e).__name__}",
                           "message": str(_e)[:200]}
        live_ok = bool(live_result and live_result.get("ok"))
        # Round-4: ok=True alone is not enough; require sales_30d to be present
        live_has_sales = live_ok and live_result.get("sales_30d") is not None

        rows = agent._data._fetch("""
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
        # ── WS-125：wf5 行缺失（接线缺失）按需计算 ──────────────────────────────
        # is_listed=1 但不在 wf5_sales_cycle 的 SKU（trend=NULL）原本只能拿到 NULL 补货数。
        # 仅在「数据新鲜（noon_orders 不陈旧）+ 库存就绪」时按需算一行写回 wf5，再重读。
        # 关键（WS-142 Luke B 裁定）：noon_orders 陈旧或库存未就绪时**不在此短路**，
        # 一律落到下方常规路径，让 data_stale/noon_orders_stale 优先浮出，
        # 不得被「库存为空」分支吞掉陈旧信号。
        if r.get("trend") is None and not noon_orders_stale:
            _wf5_as_of = r.get("as_of_date")
            _wf5_fresh = bool(_wf5_as_of)
            if _wf5_as_of:
                try:
                    _wf5_fresh = (_dt.date.today() - _dt.date.fromisoformat(str(_wf5_as_of)[:10])).days <= 3
                except Exception:
                    _wf5_fresh = False
            if _wf5_fresh and agent._data.stock_readiness(tid, alias).get("ready"):
                try:
                    if agent._data.compute_wf5_single(store, sku):
                        rows2 = agent._data._fetch("""
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
                        if rows2:
                            r = rows2[0]
                except Exception:
                    pass  # 计算失败 → 落常规路径（trend 仍 NULL，字段 REDACT/NULL）
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
                stats_30d = agent._data.sku_30d_stats(tid, alias, sku, as_of)
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
        question_text = agent._chat_question.get() or ""
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
    agent._last_sku_rate_stats.set(out)
    return {"items": out, "references": refs}
def tool_query_order(order_no: str) -> Dict:
    tid = agent._get_tenant()
    rows = agent._data._fetch("""
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
    tid = agent._get_tenant()
    rows = agent._data._fetch(
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
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    o = agent._data.get_today(store)
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
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    import datetime as _dt
    from hipop.scripts.freshness_gate import decide_freshness

    # WS-62：与 HTTP 入口同源的库存就绪门。库存未就绪/不完整时，chat 不能把
    # 空建议当成「不用补货」，必须 fail-closed。
    stock_status = agent._data.stock_readiness(tid, alias)
    agent._last_replenishment_stock_status.set(stock_status)
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

    latest_wf5 = agent._data._scalar(
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
    rows = agent._data._fetch(
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
    } for r in agent._normalize_replenishment_rows(rows)]
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
def _erp_replenishment_live_source(sku: str, store: str, tenant_id: int,
                                   entity_alias: str) -> Dict:
    """Production authoritative source for T27 replenishment evidence.

    Pulls the in-transit / pending-shipment split + ETA from the live ERP tool
    (query_sku_live, never wf3 cache), and Noon/Dongguan stock + forecast from
    the wf1/wf5/wf2 tables. ERP failures are propagated verbatim so the caller
    blocks instead of turning cached zeros into a business conclusion.
    """
    erp = tool_query_sku_live(sku)
    if not erp.get("ok"):
        # Propagate the ERP failure (no_credentials / login_failed / fetch_error
        # / no_orders) — replenishment_evidence treats !ok as blocked.
        return erp

    orders = erp.get("in_transit_orders") or []
    # 待发: purchased but not yet shipped (no tracking_no).
    pending_qty = sum((o.get("qty") or 0) for o in orders if not o.get("tracking_no"))
    # 在途: shipped, has a tracking_no.
    in_transit_qty = sum((o.get("qty") or 0) for o in orders if o.get("tracking_no"))
    etas = [str(o.get("delivery_at"))[:10] for o in orders
            if o.get("tracking_no") and o.get("delivery_at")]
    eta = min(etas) if etas else None

    stock = agent._data._fetch(
        "SELECT noon_saleable_qty, dongguan_qty FROM wf1_stock "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, entity_alias, sku),
    )
    stock_row = stock[0] if stock else {}

    forecast_daily = None
    wf5 = agent._data._fetch(
        "SELECT daily_rate FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, entity_alias, sku),
    )
    if wf5 and wf5[0].get("daily_rate"):
        forecast_daily = wf5[0].get("daily_rate")
    else:
        wf2 = agent._data._fetch(
            "SELECT forecast_30d FROM wf2_sku "
            "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
            (tenant_id, entity_alias, sku),
        )
        if wf2 and wf2[0].get("forecast_30d"):
            forecast_daily = wf2[0].get("forecast_30d") / 30.0

    return {
        "ok": True,
        "source": "ERP realtime + DB stock/forecast",
        "fetched_at": erp.get("fetched_at") or "now",
        "pending_shipment_qty": pending_qty,
        "in_transit_qty": in_transit_qty,
        "eta": eta,
        "noon_saleable_qty": stock_row.get("noon_saleable_qty"),
        "dongguan_qty": stock_row.get("dongguan_qty"),
        "forecast_daily": forecast_daily,
    }


def tool_query_replenishment_sku(sku: str, store: str = "KSA") -> Dict:
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    from . import replenishment_evidence as _rep
    # Prefer a test-injected global source (set_replenishment_live_source) when
    # present; otherwise wire the production ERP adapter so the live-required
    # path never silently blocks for lack of a source.
    live = _rep.get_replenishment_live_source() or _erp_replenishment_live_source
    return _rep.query_replenishment_sku(sku, store, tid, alias, live_source=live)
def tool_compute_air_freight_roi(sku: str, store: str, qty: int = 100) -> Dict:
    """简化模型: 海运 0.4 / 件, 空运 2.5 / 件, 海运 25d, 空运 5d."""
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    rows = agent._data._fetch("""
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
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    h = agent._data.get_data_health(store)
    return {
        **h,
        "references": [
            {"table": "wf2_sku", "where": f"tenant_id={tid} AND entity_alias='{alias}' MAX(imported_at)"},
            {"table": "wf5_sales_cycle", "where": f"tenant_id={tid} AND entity_alias='{alias}' MAX(updated_at)"},
            {"table": "wf3_logistics_hub_v2", "where": f"tenant_id={tid} MAX(updated_at)"},
        ],
    }


def tool_top_sales_by_window(store: str, start_date: Optional[str] = None,
                             end_date: Optional[str] = None, limit: int = 10,
                             listing: str = "all",
                             relative_days: Optional[int] = None) -> Dict:
    """WS-120 [T07]：指定日期窗口销量 TopN（从 wf2_orders 逐单现算，非 sales_30d 固定桶）。

    - 显式窗口（start_date/end_date）：按该历史窗口现算，只做两端覆盖判定，不加时效门
      （历史窗口只要数据齐就答）。
    - 相对窗口（relative_days，如『近30天』）：以 wf2_orders 最新订单业务日倒推
      end=latest、start=latest-(N-1)，并复用 WS-134 同一时效门 decide_freshness——
      最新订单日距今 >3 天即 fail_closed 不出数（不拿陈旧名次糊弄）。
    确定性 SQL/口径全在 data.top_sales_by_window；这里只做相对解析 + 时效门 + 透传。
    """
    import datetime as _dt
    freshness_decision = None
    if relative_days is not None:
        n = max(1, int(relative_days))
        latest = agent._data.latest_order_business_date(store)
        if not latest:
            return {"store": (store or "").upper(), "available": False,
                    "reason": "no_order_data", "items": [], "relative_days": n,
                    "filter": f"listing={listing}",
                    "coverage": {"min_order_date": "", "max_order_date": "", "order_rows": 0}}
        end_date = latest
        start_date = (_dt.date.fromisoformat(latest) - _dt.timedelta(days=n - 1)).isoformat()
        from hipop.scripts.freshness_gate import decide_freshness
        freshness_decision = decide_freshness(
            live_ok=False,
            live_error="窗口销量 TopN 使用最近一次成功的统一销量快照",
            cache_available=True,
            cache_fetched_at=latest,
            operator_cache_consent=True,
            cache_requires_consent=False,
            subject=f"{(store or '').upper()} 近{n}天销量 TopN",
        )
        if not freshness_decision.get("can_output_number"):
            return {"store": (store or "").upper(), "available": False,
                    "reason": "stale_snapshot", "fail_closed": True,
                    "message": freshness_decision.get("message"),
                    "freshness_decision": freshness_decision,
                    "start_date": start_date, "end_date": end_date,
                    "relative_days": n, "filter": f"listing={listing}", "items": [],
                    "coverage": {"min_order_date": "", "max_order_date": latest, "order_rows": 0}}

    result = agent._data.top_sales_by_window(
        store, start_date, end_date, limit=limit, listing=listing)
    if relative_days is not None:
        result["relative_days"] = int(relative_days)
        if freshness_decision is not None:
            result["freshness_decision"] = freshness_decision
    return result


def tool_list_products(store: str, listing: str = "all",
                       sales_only: bool = False, limit: int = 0) -> Dict:
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
    tbl = "wf2_sku"
    # 聚合 — SKU 维度
    base_where = f"tenant_id={tid} AND entity_alias='{alias}'"
    agg = agent._data._fetch(f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN is_listed=1 THEN 1 ELSE 0 END) AS listed,
          SUM(CASE WHEN is_listed=0 OR is_listed IS NULL THEN 1 ELSE 0 END) AS unlisted,
          SUM(CASE WHEN COALESCE(sales_180d,0) > 0 THEN 1 ELSE 0 END) AS ever_sold,
          SUM(CASE WHEN COALESCE(sales_30d,0) > 0 THEN 1 ELSE 0 END) AS sold_recent_30d
        FROM {tbl} WHERE {base_where}
    """)[0]
    # 聚合 — product 维度（与 ERP 后台视图一致）
    prod_agg = agent._data._fetch(f"""
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

    filtered_count = agent._data._scalar(f"SELECT COUNT(*) FROM {tbl} {where_sql}") or 0

    items = []
    evidence = None
    freshness_decision = None
    fail_closed = False
    fail_message = None
    latest_sales_as_of = None
    requested_limit = max(0, min(int(limit or 0), 50))
    if requested_limit > 0:
        from hipop.scripts.freshness_gate import decide_freshness

        latest_sales_as_of = agent._data._scalar(
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
        rows = agent._data._fetch(f"""
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

    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""
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
        return agent._write_xlsx_and_return(rows, f"{view}_{store}", filter_desc, cols, headers)
    elif view == "replenish":
        from . import data as _d
        rows = _d._fetch(
            f"SELECT * FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=? "
            f"ORDER BY urgency DESC, sellable_days ASC",
            (tid, alias),
        )
        return agent._write_xlsx_and_return(rows, f"replenish_{store}", filter_desc)
    elif view == "logistics":
        from . import data as _d
        rows = _d._fetch(
            f"SELECT * FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND has_stuck_batch=1 "
            f"ORDER BY in_transit_total_qty DESC",
            (tid,),
        )
        return agent._write_xlsx_and_return(rows, f"logistics_stuck_{store}", filter_desc)
    else:
        return {"ok": False, "error": f"未知 view={view}；支持: "
                "unlisted_with_sales / sales / sku_health / replenish / logistics"}

    # 走 wf2_sku 通用路径
    from . import data as _d
    rows = _d._fetch(
        f"SELECT {','.join(cols)} FROM wf2_sku WHERE {where} ORDER BY {order_by}",
        (),
    )
    return agent._write_xlsx_and_return(rows, f"{view}_{store}", filter_desc, cols)
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
    tid = agent._get_tenant()
    sc = agent._chat_scope.get() or {}
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
            agent._data.set_current_tenant(tid)
            agent._data.write_event(
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
        agent._data.set_current_tenant(tid)
        agent._data.write_event(
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
        _durable_events = agent._data.get_events_after(task_id, 0)
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
def tool_query_sku_live(sku: str, with_nodes: bool = False) -> Dict:
    """实时查单 SKU ERP 在途货单 — 不读 wf3 缓存，每次直连。
    with_nodes=True 时对每个在途货单跑 playwright 抓物流站节点（慢 5-10s/单）。
    默认 False（只 ERP 拉单 + tracking_no，快），用户问'节点'/'卡哪'时设 True。"""
    tid = agent._get_tenant()
    fetched_at = agent._utc_now_iso()
    live_source = "ERP /delivery (realtime)"
    token, err = agent._erp_token_or_error(tid)
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
    _wf0, _wls, _orig = agent._patch_wls_token(token)
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
            "tracking_url": agent._physical_tracking_url(o.get("logistics_name"), o.get("tracking_no")),
        }
        if with_nodes and o.get("tracking_no"):
            n = agent._fetch_logistics_nodes(o.get("logistics_name"), o.get("tracking_no"))
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
    tid = agent._get_tenant()
    token, err = agent._erp_token_or_error(tid)
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
    _wf0, _wls, _orig = agent._patch_wls_token(token)
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
    nodes_result = agent._fetch_logistics_nodes(forwarder, tracking) if tracking else {"nodes": [], "note": "无单号"}
    return {
        "ok": True,
        "order_no": order_no,
        "fetched_from": "ERP realtime + 物流站节点抓取",
        "status": o.get("status"),
        "store": (o.get("store") or {}).get("name", ""),
        "forwarder": forwarder,
        "tracking_no": tracking,
        "tracking_url": agent._physical_tracking_url(forwarder, tracking),
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
    tid = agent._get_tenant()
    content = tenant_notes.get_notes(tid, section or None)
    return {
        "tenant_id": tid,
        "section": section or "(全文)",
        "content": content or "(尚无 NOTES，可用 tenant_notes_append 沉淀)",
        "sections": tenant_notes.list_sections(tid),
    }
def _tool_tenant_notes_append(note: str, section: str = "通用") -> Dict:
    from . import tenant_notes
    tid = agent._get_tenant()
    return tenant_notes.append_note(tid, note, section)
def _tool_confirm_proposal(proposal_id: str, user_decision: str) -> Dict:
    """confirm_proposal tool 实现 — 用 chat scope 当前 user 验签 + 走 governance.confirm_proposal."""
    from . import governance as _gov
    sc = agent._chat_scope.get() or {}
    actor = {
        "user_id": sc.get("user_id"),
        "email": sc.get("current_user_email") or sc.get("current_user"),
        "role": sc.get("current_role"),
        "tenant_id": sc.get("tenant_id") or agent._get_tenant(),
        "source": "chat",
    }
    return _gov.confirm_proposal(proposal_id, user_decision, actor, agent.TOOL_FUNCS)
def tool_capture_feedback(content: str, scene: str = "", category: str = "需求") -> Dict:
    """把撞限/超范围时用户确认的需求真写入 feedback 表（WS-26）。

    报告即事实：写不进库就如实返 ok=False + error，**绝不返一个假的成功**。
    并在写入后**回读一次**确认真落库（钉死占位假数据）。
    """
    from . import data as _d
    tid = agent._get_tenant()
    sc = agent._chat_scope.get() or {}
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
                "  2) hipop/server/tools_registry.yaml — update_alert_status input_schema.status.enum 加一项\n"
                "重启 uvicorn 后立即生效，数据库无需 migration。\n"
                "如新状态需算告警关闭，再改 wf_logistics_alerts.py TERMINAL_STATUSES。"
            ),
            "references": [
                {"table": "wf6_logistics_alerts_v2.ops_status", "type": "TEXT"},
                {"file": "hipop/server/governance_actions.yaml", "key": "update_alert_status.allowed_statuses"},
                {"file": "hipop/server/tools_registry.yaml", "key": "tools.update_alert_status.input_schema.properties.status.enum"},
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

    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""

    rows = agent._data._fetch(
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
    wf3_rows = agent._data._fetch(
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
    tid = agent._get_tenant()
    alias = agent._resolve_entity_alias(store) or ""

    row_count = agent._data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND entity_alias=?",
        (tid, alias),
    ) or 0
    latest_row = agent._data._fetch(
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
    rows = agent._data._fetch(
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

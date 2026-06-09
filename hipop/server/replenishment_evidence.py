"""SKU-level replenishment evidence guard and formatter.

This module keeps T27-style replenishment answers out of prompt-only rules:
listed SKUs with missing/all-zero/stale replenishment cache rows must use an
authoritative live source, and live failures are represented as blocked results
rather than zero-valued business facts.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Callable, Optional

from . import data as _data


LiveSource = Callable[[str, str, int, str], dict]
_live_source: Optional[LiveSource] = None


def set_replenishment_live_source(fn: Optional[LiveSource]) -> None:
    """Set an injectable authoritative source for tests/live adapters."""
    global _live_source
    _live_source = fn


def get_replenishment_live_source() -> Optional[LiveSource]:
    return _live_source


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    n = _num(value)
    if n is None:
        return None
    return int(round(n))


def _positive(value: Any) -> bool:
    n = _num(value)
    return n is not None and n > 0


def _all_zero_or_missing(*values: Any) -> bool:
    seen_any = False
    for value in values:
        n = _num(value)
        if n is None:
            continue
        seen_any = True
        if n != 0:
            return False
    return True if seen_any else True


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _fetch_one(sql: str, params: tuple) -> Optional[dict]:
    rows = _data._fetch(sql, params)
    return rows[0] if rows else None


def _parse_dt(value: Any) -> Optional[_dt.datetime]:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "T" in text and " " not in text:
        text = text.replace("T", " ", 1)
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return _dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _age_hours(value: Any) -> Optional[float]:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    now = _dt.datetime.now(parsed.tzinfo) if parsed.tzinfo else _dt.datetime.now()
    return max((now - parsed).total_seconds(), 0.0) / 3600.0


def _decode_json_list(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _extract_eta(logistics_row: Optional[dict]) -> Optional[str]:
    if not logistics_row:
        return None
    batches = []
    batches.extend(_decode_json_list(logistics_row.get("transit_batches_json")))
    for group in _decode_json_list(logistics_row.get("groups_json")):
        if isinstance(group, dict):
            batches.extend(group.get("in_transit_batches") or [])
    eta_keys = (
        "eta", "eta_date", "expected_arrival_date", "arrival_date",
        "estimated_arrival_date", "estimated_eta", "eta_at",
    )
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        for key in eta_keys:
            value = batch.get(key)
            if value not in (None, ""):
                return str(value)[:10]
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        days = batch.get("eta_days_remaining")
        if days not in (None, ""):
            return f"{days} 天后"
    return None


def _cache_reasons(wf2: dict, stock: Optional[dict], logistics: Optional[dict],
                   wf5: Optional[dict]) -> list[str]:
    reasons: list[str] = []
    listed = bool(wf2.get("is_listed"))
    if not listed:
        return reasons

    if not stock:
        reasons.append("库存缓存缺失")
    elif _all_zero_or_missing(
        stock.get("noon_saleable_qty"), stock.get("pending_inbound_qty"),
        stock.get("overseas_total_qty"), stock.get("yiwu_qty"),
        stock.get("dongguan_qty"), stock.get("total_stock"),
    ):
        reasons.append("库存缓存全空/缺失")
    elif (_age_hours(stock.get("imported_at") or stock.get("updated_at")) or 0) > 72:
        reasons.append("库存缓存超过 72 小时")

    if not logistics:
        reasons.append("物流/在途缓存缺失")
    elif _all_zero_or_missing(
        logistics.get("in_transit_total_qty"),
        logistics.get("total_transit_qty"),
    ):
        reasons.append("物流/在途缓存全空/缺失")
    elif (_age_hours(logistics.get("updated_at")) or 0) > 72:
        reasons.append("物流/在途缓存超过 72 小时")

    if not wf5:
        reasons.append("补货聚合缓存缺失")
    elif _all_zero_or_missing(
        wf5.get("daily_rate"), wf5.get("forecast_30_days"),
        wf5.get("current_pipeline"), wf5.get("target_pipeline"),
        wf5.get("weekly_total_replenish"), wf5.get("wf5_replenish_qty"),
    ):
        reasons.append("补货聚合缓存全空/缺失")
    elif (_age_hours(wf5.get("updated_at")) or 0) > 72:
        reasons.append("补货聚合缓存超过 72 小时")
    return reasons


def _source_ref(table: str, where: str, *, imported_at=None, updated_at=None,
                fetched_at=None) -> dict:
    ref = {"table": table, "where": where}
    if imported_at is not None:
        ref["imported_at"] = str(imported_at)
    if updated_at is not None:
        ref["updated_at"] = str(updated_at)
    if fetched_at is not None:
        ref["fetched_at"] = str(fetched_at)
    return ref


def _evidence(value: Any, label: str, source: str, timestamp: Any) -> dict:
    return {
        "label": label,
        "value": value,
        "source": source,
        "fetched_at": timestamp,
    }


def _from_live(live: dict, cache_reasons: list[str], sku: str, store: str,
               tid: int, alias: str, refs: list[dict]) -> dict:
    source = live.get("source") or "authoritative live source"
    fetched_at = live.get("fetched_at") or live.get("as_of") or live.get("updated_at")

    pending = _first_present(live.get("pending_shipment_qty"),
                             live.get("pending_qty"),
                             live.get("pending_inbound_qty"))
    in_transit = _first_present(live.get("in_transit_qty"),
                                live.get("in_transit_total_qty"),
                                live.get("total_transit_qty"))
    forecast_daily = _first_present(live.get("forecast_daily"), live.get("daily_rate"))
    forecast_30d = _first_present(live.get("forecast_30d"),
                                  live.get("forecast_30_days"))
    if forecast_30d is None and _num(forecast_daily) is not None:
        forecast_30d = round(float(forecast_daily) * 30)

    weekly = _first_present(live.get("weekly_replenish"),
                            live.get("weekly_total_replenish"),
                            live.get("recommended_qty"),
                            live.get("recommend_qty"))
    if weekly is None:
        weekly = 0

    values = {
        "pending_shipment_qty": _int_or_none(pending),
        "in_transit_qty": _int_or_none(in_transit),
        "eta": _first_present(live.get("eta"), live.get("eta_date"),
                              live.get("next_eta")),
        "noon_saleable_qty": _int_or_none(_first_present(live.get("noon_saleable_qty"),
                                                         live.get("noon_stock"),
                                                         live.get("noon_warehouse_qty"))),
        "dongguan_qty": _int_or_none(live.get("dongguan_qty")),
        "forecast_daily": _num(forecast_daily),
        "forecast_30d": _num(forecast_30d),
        "weekly_replenish": _int_or_none(weekly) or 0,
        "risk_label": _first_present(live.get("risk_label"), live.get("risk")),
        "urgency": _first_present(live.get("urgency"), live.get("priority")),
        "recommendation": _first_present(live.get("recommendation"), live.get("advice")),
    }
    if not values["recommendation"]:
        values["recommendation"] = (
            "不需要补货" if (values["weekly_replenish"] or 0) <= 0 else "建议本周补货"
        )

    evidence = {
        key: _evidence(value, key, source, fetched_at)
        for key, value in values.items()
        if value not in (None, "")
    }
    refs.append(_source_ref(source, f"tenant_id={tid} entity_alias='{alias}' sku='{sku}'",
                           fetched_at=fetched_at))
    return {
        "ok": True,
        "sku": sku,
        "store": store,
        "source_mode": "live_authoritative",
        "live_required": True,
        "live_required_reasons": cache_reasons,
        "values": values,
        "evidence": evidence,
        "references": refs,
    }


def _from_cache(sku: str, store: str, wf2: dict, stock: Optional[dict],
                logistics: Optional[dict], wf5: Optional[dict],
                refs: list[dict]) -> dict:
    daily = _first_present(
        wf5.get("daily_rate") if wf5 else None,
        (_num(wf5.get("forecast_30_days")) / 30 if wf5 and _num(wf5.get("forecast_30_days")) is not None else None),
        (_num(wf2.get("forecast_30d")) / 30 if _num(wf2.get("forecast_30d")) is not None else None),
    )
    forecast_30d = _first_present(
        wf5.get("forecast_30_days") if wf5 else None,
        wf2.get("forecast_30d"),
        (round(float(daily) * 30) if _num(daily) is not None else None),
    )
    values = {
        "pending_shipment_qty": _int_or_none(stock.get("pending_inbound_qty") if stock else None),
        "in_transit_qty": _int_or_none(_first_present(
            logistics.get("in_transit_total_qty") if logistics else None,
            logistics.get("total_transit_qty") if logistics else None,
        )),
        "eta": _extract_eta(logistics),
        "noon_saleable_qty": _int_or_none(stock.get("noon_saleable_qty") if stock else None),
        "dongguan_qty": _int_or_none(stock.get("dongguan_qty") if stock else None),
        "forecast_daily": _num(daily),
        "forecast_30d": _num(forecast_30d),
        "weekly_replenish": _int_or_none(wf5.get("weekly_total_replenish") if wf5 else None) or 0,
        "risk_label": wf5.get("risk_label") if wf5 else None,
        "urgency": wf5.get("urgency") if wf5 else None,
        "recommendation": None,
    }
    values["recommendation"] = (
        "不需要补货" if (values["weekly_replenish"] or 0) <= 0 else "建议本周补货"
    )
    evidence = {}
    if stock:
        for key in ("pending_shipment_qty", "noon_saleable_qty", "dongguan_qty"):
            evidence[key] = _evidence(values[key], key, "wf1_stock", stock.get("updated_at") or stock.get("imported_at"))
    if logistics:
        for key in ("in_transit_qty", "eta"):
            evidence[key] = _evidence(values[key], key, "wf3_logistics_hub_v2", logistics.get("updated_at"))
    if wf5:
        for key in ("forecast_daily", "forecast_30d", "weekly_replenish", "risk_label", "urgency"):
            evidence[key] = _evidence(values[key], key, "wf5_sales_cycle", wf5.get("updated_at"))
    return {
        "ok": True,
        "sku": sku,
        "store": store,
        "source_mode": "cache_authoritative",
        "live_required": False,
        "live_required_reasons": [],
        "values": values,
        "evidence": evidence,
        "references": refs,
    }


def query_replenishment_sku(sku: str, store: str, tenant_id: int,
                            entity_alias: str,
                            live_source: Optional[LiveSource] = None) -> dict:
    sku = (sku or "").upper().strip()
    store = store or "KSA"
    if not sku:
        return {"ok": False, "error": "missing_sku", "message": "缺少 SKU"}
    if not entity_alias:
        return {
            "ok": False,
            "error": "unknown_store",
            "sku": sku,
            "store": store,
            "message": f"无法解析店铺 {store} 的销售主体",
        }

    wf2 = _fetch_one(
        "SELECT partner_sku, title, is_listed, sales_30d, forecast_30d, "
        "latest_profit_rate, imported_at, as_of_date "
        "FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, entity_alias, sku),
    )
    refs = [
        _source_ref("wf2_sku",
                    f"tenant_id={tenant_id} AND entity_alias='{entity_alias}' AND partner_sku='{sku}'",
                    imported_at=(wf2 or {}).get("imported_at"),
                    updated_at=(wf2 or {}).get("as_of_date"))
    ]
    if not wf2:
        return {
            "ok": False,
            "error": "sku_not_found",
            "sku": sku,
            "store": store,
            "message": f"未找到 SKU {sku} 的上架记录",
            "references": refs,
        }

    stock = _fetch_one(
        "SELECT partner_sku, noon_saleable_qty, pending_inbound_qty, "
        "overseas_total_qty, yiwu_qty, dongguan_qty, total_stock, imported_at, updated_at "
        "FROM wf1_stock WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, entity_alias, sku),
    )
    logistics = _fetch_one(
        "SELECT sku, in_transit_total_qty, total_transit_qty, transit_batches_json, "
        "groups_json, updated_at "
        "FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND sku=?",
        (tenant_id, sku),
    )
    wf5 = _fetch_one(
        "SELECT partner_sku, trend, daily_rate, forecast_30_days, risk_label, "
        "current_pipeline, target_pipeline, wf5_replenish_qty, lost_replenish_qty, "
        "weekly_total_replenish, urgency, ops_advice, updated_at "
        "FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, entity_alias, sku),
    )
    refs.extend([
        _source_ref("wf1_stock",
                    f"tenant_id={tenant_id} AND entity_alias='{entity_alias}' AND partner_sku='{sku}'",
                    imported_at=(stock or {}).get("imported_at"),
                    updated_at=(stock or {}).get("updated_at")),
        _source_ref("wf3_logistics_hub_v2",
                    f"tenant_id={tenant_id} AND sku='{sku}'",
                    updated_at=(logistics or {}).get("updated_at")),
        _source_ref("wf5_sales_cycle",
                    f"tenant_id={tenant_id} AND entity_alias='{entity_alias}' AND partner_sku='{sku}'",
                    updated_at=(wf5 or {}).get("updated_at")),
    ])

    reasons = _cache_reasons(wf2, stock, logistics, wf5)
    if reasons:
        source = live_source if live_source is not None else _live_source
        if source is None:
            return {
                "ok": False,
                "blocked": True,
                "error": "authoritative_live_source_unavailable",
                "sku": sku,
                "store": store,
                "live_required": True,
                "live_required_reasons": reasons,
                "message": "缓存/聚合证据缺失、全空或过期，且本轮没有可用实时权威源",
                "references": refs,
            }
        try:
            live = source(sku, store, tenant_id, entity_alias) or {}
        except Exception as exc:
            live = {
                "ok": False,
                "error": f"live_source_exception:{type(exc).__name__}",
                "message": str(exc)[:200],
            }
        if not live.get("ok"):
            return {
                "ok": False,
                "blocked": True,
                "error": live.get("error") or "authoritative_live_source_failed",
                "sku": sku,
                "store": store,
                "live_required": True,
                "live_required_reasons": reasons,
                "message": live.get("message") or "实时权威源失败，不能使用缓存空值下结论",
                "live_source": live.get("source"),
                "fetched_at": live.get("fetched_at"),
                "references": refs,
            }
        return _from_live(live, reasons, sku, store, tenant_id, entity_alias, refs)

    return _from_cache(sku, store, wf2, stock, logistics, wf5, refs)


def blocked_skus_from_tool_result(tool_name: str, result: Any) -> Optional[list[str]]:
    if tool_name != "query_replenishment_sku" or not isinstance(result, dict):
        return None
    if result.get("blocked"):
        sku = result.get("sku")
        return [sku] if sku else []
    return None


def _fmt_int(value: Any, unknown: str = "不可确认") -> str:
    n = _int_or_none(value)
    if n is None:
        return unknown
    return str(n)


def _fmt_float(value: Any, digits: int = 3, unknown: str = "不可确认") -> str:
    n = _num(value)
    if n is None:
        return unknown
    text = f"{n:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_month(value: Any) -> str:
    n = _num(value)
    if n is None:
        return "不可确认"
    if abs(n - round(n)) < 0.0001:
        return str(int(round(n)))
    return f"{n:.1f}".rstrip("0").rstrip(".")


def format_replenishment_sku_reply(result: dict) -> str:
    sku = result.get("sku") or "该 SKU"
    if not result.get("ok"):
        if result.get("blocked"):
            reasons = "、".join(result.get("live_required_reasons") or ["缓存证据不可用"])
            msg = result.get("message") or result.get("error") or "实时源失败"
            return (
                f"{sku} 是已上架 SKU，但补货链路的缓存/聚合证据不可直接下结论：{reasons}。"
                f"本问题需要实时 ERP/noon 权威源；本轮实时源不可用或失败：{msg}。"
                "因此我不能确认本周补货建议、pipeline、风险标签和紧急度，也不会把缓存空值当作真实业务结论。"
                "请先刷新或恢复实时源后再问。"
            )
        return result.get("message") or f"无法查询 {sku} 的补货证据。"

    values = result.get("values") or {}
    evidence = result.get("evidence") or {}
    source_mode = result.get("source_mode")
    source = None
    fetched_at = None
    for key in (
        "pending_shipment_qty", "in_transit_qty", "eta", "noon_saleable_qty",
        "dongguan_qty", "forecast_daily",
    ):
        ev = evidence.get(key) or {}
        source = source or ev.get("source")
        fetched_at = fetched_at or ev.get("fetched_at")

    pending = _fmt_int(values.get("pending_shipment_qty"))
    in_transit = _fmt_int(values.get("in_transit_qty"))
    eta = values.get("eta") or "不可确认"
    noon = _fmt_int(values.get("noon_saleable_qty"))
    dongguan = _fmt_int(values.get("dongguan_qty"))
    daily = _fmt_float(values.get("forecast_daily"))
    monthly = _fmt_month(values.get("forecast_30d"))
    weekly = _fmt_int(values.get("weekly_replenish"), unknown="0")
    risk = values.get("risk_label") or "未标注"
    urgency = values.get("urgency") or "未标注"
    recommendation = values.get("recommendation") or (
        "不需要补货" if (values.get("weekly_replenish") or 0) <= 0 else "建议本周补货"
    )

    source_text = f"{source}，时间 {fetched_at}" if source or fetched_at else "缓存/聚合表"
    freshness_note = ""
    if source_mode == "live_authoritative":
        reasons = "、".join(result.get("live_required_reasons") or [])
        freshness_note = f"缓存证据因 {reasons} 未直接采用；以下使用实时/权威源。"

    return (
        f"结论：{sku} 本周 {recommendation}；本周建议补货量 {weekly} 件。\n"
        f"pipeline 口径：这里不新增“当前/目标 pipeline”字段，按在途/待发/本周建议补货量解释。"
        f"当前可追踪 pipeline：待发 {pending} 件、在途 {in_transit} 件（ETA {eta}）；"
        f"目标按本周建议补货量看是 {weekly} 件。\n"
        f"库存与销量证据：Noon 仓 {noon} 件，东莞 {dongguan} 件；"
        f"noon 预测日销 {daily}/天（约 {monthly}/月）。风险标签：{risk}；紧急度：{urgency}。\n"
        f"关键值来源：{source_text}。{freshness_note}"
    )

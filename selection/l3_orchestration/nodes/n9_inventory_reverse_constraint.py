"""N9 — inventory reverse constraints for selection ranking."""
from __future__ import annotations

import re
from typing import Any

from selection.l1_normalize.product_record import ProductRecord


EVIDENCE_INSUFFICIENT = "evidence_insufficient"
BACKLOG_SIZE_INCHES = 20
BACKLOG_STOCK_THRESHOLD = 100.0
BACKLOG_STOCK_TO_SALES_RATIO = 20.0


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("title", "product_category_detail", "family")
    )


def _record_text(rec: ProductRecord) -> str:
    parts = [rec.title or ""]
    n6 = rec.policy_flags.get("n6_extracted") or {}
    parts.extend(str(f) for f in n6.get("features") or [])
    parts.extend(rec.inferred_features or [])
    return " ".join(parts)


def _extract_size_inches(text: str) -> int | None:
    lowered = (text or "").lower()
    m = re.search(r"\b(18|19|20|21|22|24|26|28|30)\s*(?:inch|inches|in|寸)\b", lowered)
    if m:
        return int(m.group(1))
    m = re.search(r"(18|19|20|21|22|24|26|28|30)\s*寸", text or "")
    if m:
        return int(m.group(1))
    return None


def _record_size(rec: ProductRecord) -> int | None:
    n6 = rec.policy_flags.get("n6_extracted") or {}
    sizes = n6.get("size_inches") or []
    if sizes:
        try:
            return int(sizes[0])
        except (TypeError, ValueError):
            pass
    return _extract_size_inches(_record_text(rec))


def _has_signal(rec: ProductRecord, signal_id: str) -> bool:
    diff = rec.policy_flags.get("differentiation") or {}
    return any(s.get("id") == signal_id for s in diff.get("signals") or [])


def _is_set_candidate(rec: ProductRecord) -> bool:
    if int(rec.policy_flags.get("pack_size") or 1) > 1:
        return True
    text = _record_text(rec).lower()
    return "set" in text or "套装" in text or "piece" in text


def _inventory_pressure(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_size: dict[int, dict[str, float]] = {}
    for row in rows:
        size = _extract_size_inches(_row_text(row))
        if size is None:
            continue
        bucket = by_size.setdefault(size, {"stock": 0.0, "sales_30d": 0.0, "n_skus": 0.0})
        bucket["stock"] += _num(row.get("total_stock"))
        bucket["sales_30d"] += _num(row.get("sales_30d"))
        bucket["n_skus"] += 1

    size20 = by_size.get(BACKLOG_SIZE_INCHES)
    if not size20:
        return None
    stock = size20["stock"]
    sales_30d = size20["sales_30d"]
    ratio = stock / max(1.0, sales_30d)
    if stock < BACKLOG_STOCK_THRESHOLD or ratio < BACKLOG_STOCK_TO_SALES_RATIO:
        return None
    return {
        "rule_id": "20in_backlog_shift",
        "backlog_size_inches": BACKLOG_SIZE_INCHES,
        "backlog_stock": round(stock, 3),
        "backlog_sales_30d": round(sales_30d, 3),
        "stock_to_sales_30d": round(ratio, 3),
        "sizes": by_size,
    }


def _row_has_signal(row: dict[str, Any]) -> bool:
    """Return True if a row has at least one usable size, stock, or sales signal."""
    if _extract_size_inches(_row_text(row)) is not None:
        return True
    if _num(row.get("total_stock")) > 0:
        return True
    if _num(row.get("sales_30d")) > 0:
        return True
    return False


def apply(records: list[ProductRecord], inventory_rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not inventory_rows:
        for rec in records:
            rec.policy_flags["inventory_reverse_constraint"] = {
                "state": EVIDENCE_INSUFFICIENT,
                "score_adjustment": 0.0,
                "reasons": [],
                "warnings": [],
                "evidence": {},
                "triggered_rules": [],
            }
        return {"state": EVIDENCE_INSUFFICIENT, "n_inventory_rows": 0, "triggered_rules": []}

    if not any(_row_has_signal(row) for row in inventory_rows):
        for rec in records:
            rec.policy_flags["inventory_reverse_constraint"] = {
                "state": EVIDENCE_INSUFFICIENT,
                "score_adjustment": 0.0,
                "reasons": [],
                "warnings": [],
                "evidence": {"note": "inventory rows present but no parseable size/stock/sales signals"},
                "triggered_rules": [],
            }
        return {
            "state": EVIDENCE_INSUFFICIENT,
            "n_inventory_rows": len(inventory_rows),
            "n_malformed_rows": len(inventory_rows),
            "triggered_rules": [],
        }

    pressure = _inventory_pressure(inventory_rows)
    triggered = [pressure["rule_id"]] if pressure else []

    boosted = 0
    warned = 0
    for rec in records:
        size = _record_size(rec)
        has_expandable = _has_signal(rec, "expandable_layer")
        is_set = _is_set_candidate(rec)
        reasons: list[str] = []
        warnings: list[str] = []
        adjustment = 0.0

        if pressure:
            if size and size > BACKLOG_SIZE_INCHES:
                adjustment += 0.22
                reasons.append("20寸库存积压: 本期偏向24寸/更大尺寸候选。")
            if has_expandable:
                adjustment += 0.18
                reasons.append("20寸库存积压: 拓展层候选可作为差异化点并支持嵌套带走20寸库存。")
            if is_set:
                adjustment += 0.16
                reasons.append("20寸库存积压: 套装候选有机会带走20寸库存。")
            if size == BACKLOG_SIZE_INCHES and not has_expandable and not is_set:
                adjustment -= 0.25
                warnings.append("20寸库存积压: 普通20寸候选降权, 避免继续放大同尺寸库存压力。")

        if adjustment > 0:
            boosted += 1
        if warnings:
            warned += 1

        rec.policy_flags["inventory_reverse_constraint"] = {
            "state": "sufficient",
            "score_adjustment": round(adjustment, 3),
            "reasons": reasons,
            "warnings": warnings,
            "evidence": pressure or {"note": "inventory rows present; no active backlog rule"},
            "triggered_rules": triggered,
        }

    return {
        "state": "sufficient",
        "n_inventory_rows": len(inventory_rows),
        "triggered_rules": triggered,
        "n_boosted": boosted,
        "n_warned": warned,
    }

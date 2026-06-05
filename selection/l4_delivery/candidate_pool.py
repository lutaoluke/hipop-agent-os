"""Shared L4 projection for Agent OS, reports, and manual inquiry todos."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Optional


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE_POOL_PATH = PACKAGE_ROOT / "l4_delivery" / "latest_candidate_pool.json"
EVIDENCE_INSUFFICIENT = "evidence_insufficient"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_prefixed(values: list[str], prefix: str) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith(prefix):
            out.append(value.replace(prefix, "", 1))
    return out


def _selling_points(candidate: dict[str, Any]) -> dict[str, Any]:
    features = candidate.get("features") or {}
    n6 = features.get("n6_extracted") or {}
    inferred = [str(v) for v in features.get("inferred_features") or []]
    material = n6.get("material") or ""
    color = n6.get("color_main") or ""
    if not material:
        material = ", ".join(_extract_prefixed(inferred, "材质_"))
    if not color:
        color = ", ".join(_extract_prefixed(inferred, "颜色_"))
    return {
        "material": material or "unknown",
        "color": color or "unknown",
        "size_inches": _as_list(n6.get("size_inches")),
        "features": _as_list(n6.get("features")),
        "inferred_features": inferred,
        "return_risk_signal": _as_list(n6.get("return_risk_signal")),
    }


def _platform_evidence_tags(candidate: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    relevance = candidate.get("relevance") or {}
    supply = candidate.get("supply") or {}
    profit = candidate.get("profit") or {}
    inventory = candidate.get("inventory") or {}
    differentiation = candidate.get("differentiation") or {}
    if relevance.get("passed") is True:
        tags.append("relevance:passed")
    elif relevance:
        tags.append("relevance:review")
    if supply.get("status"):
        tags.append(f"1688:{supply.get('status')}")
    if profit.get("verdict"):
        tags.append(f"profit:{profit.get('verdict')}")
    if inventory.get("state"):
        tags.append(f"inventory:{inventory.get('state')}")
    if differentiation.get("state"):
        tags.append(f"differentiation:{differentiation.get('state')}")
    for key in candidate.get("missing_evidence") or []:
        tags.append(f"missing:{key}")
    return tags


def _evidence_reasons(candidate: dict[str, Any]) -> dict[str, str]:
    reasons = dict(candidate.get("evidence_insufficient_reasons") or {})
    supply = candidate.get("supply") or {}
    profit = candidate.get("profit") or {}
    for key in candidate.get("missing_evidence") or []:
        if key in reasons:
            continue
        if key == "supply_1688":
            reasons[key] = supply.get("reason") or supply.get("action_required") or "1688 evidence is insufficient"
        elif key == "profit":
            reasons[key] = profit.get("reason") or "profit evidence is insufficient"
        else:
            reasons[key] = "evidence is insufficient"
    return reasons


def _candidate_projection(candidate: dict[str, Any]) -> dict[str, Any]:
    overall = candidate.get("overall_v3") or {}
    missing = list(candidate.get("missing_evidence") or [])
    evidence_state = candidate.get("evidence_state") or (
        EVIDENCE_INSUFFICIENT if missing else "sufficient"
    )
    selling_points = _selling_points(candidate)
    return {
        "id": candidate.get("id"),
        "product_id": candidate.get("id"),
        "sku_id": candidate.get("sku_id"),
        "platform": candidate.get("platform"),
        "title": candidate.get("title"),
        "brand": candidate.get("brand") or (candidate.get("brand_marker") or {}).get("brand"),
        "url": candidate.get("url"),
        "tier": overall.get("tier_overall") or "四档",
        "tier_track": overall.get("track") or "unknown",
        "score": overall.get("score"),
        "platform_evidence_tags": _platform_evidence_tags(candidate),
        "relevance": deepcopy(candidate.get("relevance") or {}),
        "price_normalized": deepcopy(candidate.get("price") or {}),
        "sales": deepcopy(candidate.get("sales") or {}),
        "momentum": {
            "is_rising": bool((candidate.get("sales") or {}).get("is_rising")),
            "rising_evidence": (candidate.get("sales") or {}).get("rising_evidence"),
        },
        "selling_points": selling_points,
        "supply_1688": deepcopy(candidate.get("supply") or {}),
        "profit": deepcopy(candidate.get("profit") or {}),
        "differentiation": deepcopy(candidate.get("differentiation") or {}),
        "inventory": deepcopy(candidate.get("inventory") or {}),
        "return_risk": deepcopy(candidate.get("return_risk") or {}),
        "overall_v3": deepcopy(overall),
        "evidence_state": evidence_state,
        "evidence_insufficient": evidence_state == EVIDENCE_INSUFFICIENT,
        "missing_evidence": missing,
        "evidence_insufficient_reasons": _evidence_reasons(candidate),
        "feedback_status": "unreviewed",
        "feedback_reason_tags": [],
        "preference_effects": [],
    }


def build_candidate_pool(
    production_result: dict[str, Any],
    *,
    source_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Project the production pipeline result into the single L4 candidate pool."""
    source_run_id = source_run_id or production_result.get("run_id")
    pool_id = source_run_id or (
        f"{production_result.get('country', 'unknown')}:"
        f"{production_result.get('category', 'unknown')}:"
        f"{production_result.get('seed', 'unknown')}"
    )
    candidates = [
        _candidate_projection(candidate)
        for candidate in production_result.get("candidates") or []
    ]
    return {
        "candidate_pool_id": pool_id,
        "source": "selection.l3_orchestration.production_pipeline",
        "source_run_id": source_run_id,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "status": production_result.get("status"),
        "country": production_result.get("country"),
        "category": production_result.get("category"),
        "seed": production_result.get("seed"),
        "node_trace": list(production_result.get("node_trace") or []),
        "summaries": deepcopy(production_result.get("summaries") or {}),
        "candidates": candidates,
        "dropped": deepcopy(production_result.get("dropped") or []),
    }


def build_inquiry_todos(candidate_pool: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manual inquiry todos only; this function never sends messages."""
    todos: list[dict[str, Any]] = []
    for candidate in candidate_pool.get("candidates") or []:
        supply = candidate.get("supply_1688") or {}
        for offer in supply.get("offers") or []:
            if offer.get("verdict") != "inquiry":
                continue
            todos.append({
                "type": "manual_1688_inquiry",
                "status": "todo",
                "external_side_effect": False,
                "candidate_id": candidate.get("id"),
                "sku_id": candidate.get("sku_id"),
                "offer_id": offer.get("offer_id"),
                "supplier_url": offer.get("open_url")
                or (f"https://detail.1688.com/offer/{offer.get('offer_id')}.html" if offer.get("offer_id") else None),
                "reason": "1688 offer reached inquiry verdict; manual review required",
            })
    return todos


def render_agent_os_payload(candidate_pool: dict[str, Any]) -> dict[str, Any]:
    return {
        "_status": candidate_pool.get("status") or "ok",
        "candidate_pool_id": candidate_pool.get("candidate_pool_id"),
        "source": candidate_pool.get("source"),
        "generated_at": candidate_pool.get("generated_at"),
        "country": candidate_pool.get("country"),
        "category": candidate_pool.get("category"),
        "node_trace": list(candidate_pool.get("node_trace") or []),
        "candidates": deepcopy(candidate_pool.get("candidates") or []),
        "dropped": deepcopy(candidate_pool.get("dropped") or []),
        "inquiry_todos": build_inquiry_todos(candidate_pool),
    }


def render_structured_report(candidate_pool: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_type": "selection_candidate_pool",
        "candidate_pool_id": candidate_pool.get("candidate_pool_id"),
        "source": candidate_pool.get("source"),
        "generated_at": candidate_pool.get("generated_at"),
        "country": candidate_pool.get("country"),
        "category": candidate_pool.get("category"),
        "summary": {
            "candidate_count": len(candidate_pool.get("candidates") or []),
            "evidence_insufficient_count": sum(
                1 for row in candidate_pool.get("candidates") or []
                if row.get("evidence_insufficient")
            ),
        },
        "candidate_pool": deepcopy(candidate_pool.get("candidates") or []),
        "dropped": deepcopy(candidate_pool.get("dropped") or []),
        "inquiry_todos": build_inquiry_todos(candidate_pool),
    }


def save_candidate_pool(candidate_pool: dict[str, Any], path: os.PathLike[str] | str = DEFAULT_CANDIDATE_POOL_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(candidate_pool, ensure_ascii=False, indent=2), encoding="utf-8")


def load_candidate_pool(path: os.PathLike[str] | str = DEFAULT_CANDIDATE_POOL_PATH) -> Optional[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))

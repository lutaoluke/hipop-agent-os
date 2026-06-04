"""Production entry for KSA luggage/noon selection.

This module wires the existing selection nodes into one repeatable path:
N1 -> noon fetch -> N3 -> N4 -> N5 -> N5.5 -> N5.6 -> N6 -> N7 -> N10 -> N11 v3.

External evidence is intentionally modeled as a first-class state. When a
caller has no detail/image/N6/1688 evidence, the candidate stays visible with
``evidence_insufficient`` instead of being dropped or filled with fake fields.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional

from selection.l1_normalize.product_record import ProductRecord
from selection.l3_orchestration.nodes import n10_profit
from selection.l3_orchestration.nodes.n1_keyword_expansion import expand
from selection.l3_orchestration.nodes.n3_relevance import apply_relevance_filter
from selection.l3_orchestration.nodes.n4_price_analysis import (
    analyze as analyze_price,
    extract_pack_size,
)
from selection.l3_orchestration.nodes.n5_5_is_rising import apply_is_rising
from selection.l3_orchestration.nodes.n5_6_sku_group import apply_grouping
from selection.l3_orchestration.nodes.n5_sales_normalize import normalize as normalize_sales
from selection.l3_orchestration.nodes.n11_price_bucket_score_v3 import apply_v3


EVIDENCE_INSUFFICIENT = "evidence_insufficient"

ListingProvider = Callable[[str, str], list[ProductRecord]]
DetailProvider = Callable[[list[ProductRecord]], dict[str, Any]]
FeatureExtractor = Callable[[list[ProductRecord]], dict[str, Any]]
SupplyProvider = Callable[[list[ProductRecord]], list[dict[str, Any]]]


def _default_noon_listing_provider(keyword: str, country: str) -> list[ProductRecord]:
    from selection.l0_data.fetchers import noon_fetcher

    return noon_fetcher.search(keyword, country=country, debug=False, write_db=False)


def _default_detail_provider(records: list[ProductRecord]) -> dict[str, Any]:
    from selection.l0_data.fetchers import noon_detail_fetcher

    n_ok = 0
    n_fail = 0
    total_credits = 0
    for rec in records:
        try:
            detail = noon_detail_fetcher.fetch_one(rec.url, debug=False)
        except Exception as exc:  # external fetch failure is evidence state, not pipeline failure
            _mark_missing(rec, "detail", f"noon_detail_fetcher failed: {type(exc).__name__}")
            n_fail += 1
            continue
        if detail.get("error"):
            _mark_missing(rec, "detail", str(detail.get("error")))
            n_fail += 1
            continue
        rec.policy_flags["detail"] = {
            "highlights": detail.get("highlights", []),
            "specifications": detail.get("specifications", {}),
            "reviews_summary": detail.get("reviews_summary", {}),
            "variants": detail.get("variants", []),
            "fetched_at": detail.get("fetched_at"),
        }
        total_credits += int(detail.get("credits_used") or 0)
        n_ok += 1
    return {"n_ok": n_ok, "n_fail": n_fail, "total_credits": total_credits}


def _mark_missing(rec: ProductRecord, key: str, reason: str) -> None:
    missing = rec.policy_flags.setdefault("missing_evidence", [])
    if key not in missing:
        missing.append(key)
    reasons = rec.policy_flags.setdefault("evidence_insufficient_reasons", {})
    reasons[key] = reason
    rec.policy_flags["evidence_state"] = EVIDENCE_INSUFFICIENT


def _clear_missing(rec: ProductRecord, key: str) -> None:
    missing = rec.policy_flags.get("missing_evidence") or []
    if key in missing:
        rec.policy_flags["missing_evidence"] = [item for item in missing if item != key]


def _dedupe_by_id(records: Iterable[ProductRecord]) -> list[ProductRecord]:
    out: list[ProductRecord] = []
    seen: set[str] = set()
    for rec in records:
        if rec.id in seen:
            continue
        seen.add(rec.id)
        out.append(rec)
    return out


def _apply_detail(records: list[ProductRecord], detail_provider: Optional[DetailProvider]) -> dict[str, Any]:
    if detail_provider is None:
        for rec in records:
            _mark_missing(rec, "detail", "detail_provider not configured")
        return {"n_ok": 0, "n_fail": len(records), "configured": False}
    try:
        result = detail_provider(records) or {}
    except Exception as exc:
        for rec in records:
            _mark_missing(rec, "detail", f"detail_provider raised: {type(exc).__name__}")
        return {"n_ok": 0, "n_fail": len(records), "error": type(exc).__name__}
    for rec in records:
        detail = rec.policy_flags.get("detail") or {}
        if detail.get("highlights") or detail.get("specifications") or detail.get("reviews_summary"):
            _clear_missing(rec, "detail")
        else:
            _mark_missing(rec, "detail", "detail provider returned no usable detail evidence")
    return result


def _apply_features(records: list[ProductRecord], feature_extractor: Optional[FeatureExtractor]) -> dict[str, Any]:
    if feature_extractor is None:
        for rec in records:
            _mark_missing(rec, "n6_features", "feature_extractor not configured")
        return {"n_updated": 0, "n_not_found": 0, "configured": False}
    try:
        result = feature_extractor(records) or {}
    except Exception as exc:
        for rec in records:
            _mark_missing(rec, "n6_features", f"feature_extractor raised: {type(exc).__name__}")
        return {"n_updated": 0, "n_not_found": len(records), "error": type(exc).__name__}
    for rec in records:
        if rec.policy_flags.get("n6_extracted") or rec.inferred_features:
            _clear_missing(rec, "n6_features")
        else:
            _mark_missing(rec, "n6_features", "N6 produced no structured features")
    return result


def _apply_supply(records: list[ProductRecord], supply_provider: Optional[SupplyProvider]) -> dict[str, Any]:
    for rec in records:
        if not rec.images:
            _mark_missing(rec, "image", "record has no product image for N7 supply search")

    if supply_provider is None:
        for rec in records:
            _mark_missing(rec, "supply_1688", "supply_provider not configured")
            rec.policy_flags["supply"] = {
                "status": EVIDENCE_INSUFFICIENT,
                "reason": "supply_provider not configured",
            }
        return {"n_ok": 0, "n_fail": len(records), "configured": False}

    try:
        supply_rows = supply_provider(records) or []
    except Exception as exc:
        for rec in records:
            _mark_missing(rec, "supply_1688", f"N7 supply provider failed: {type(exc).__name__}")
            rec.policy_flags["supply"] = {
                "status": EVIDENCE_INSUFFICIENT,
                "reason": f"N7 supply provider failed: {type(exc).__name__}",
            }
        return {"n_ok": 0, "n_fail": len(records), "error": type(exc).__name__}

    by_id = {row.get("record_id"): row for row in supply_rows}
    n_ok = 0
    for rec in records:
        row = by_id.get(rec.id)
        if not row:
            _mark_missing(rec, "supply_1688", "N7 returned no row for this SKU")
            rec.policy_flags["supply"] = {
                "status": EVIDENCE_INSUFFICIENT,
                "reason": "N7 returned no row for this SKU",
            }
            continue
        status = row.get("status") or EVIDENCE_INSUFFICIENT
        rec.policy_flags["supply"] = row
        if status == EVIDENCE_INSUFFICIENT:
            _mark_missing(rec, "supply_1688", row.get("reason") or "N7 evidence insufficient")
        else:
            _clear_missing(rec, "supply_1688")
            n_ok += 1
    return {"n_ok": n_ok, "n_fail": len(records) - n_ok}


def _apply_profit(records: list[ProductRecord], ali_records: Optional[list[ProductRecord]]) -> dict[str, Any]:
    if not ali_records:
        for rec in records:
            _mark_missing(rec, "profit", "no 1688 supply records available for N10")
            rec.policy_flags["profit"] = {
                "verdict": EVIDENCE_INSUFFICIENT,
                "reason": "no 1688 supply records available for N10",
            }
        return {"n_input": len(records), "n_no_match": len(records), "configured": False}

    result = n10_profit.apply_profit(
        records,
        ali_records,
        country="ksa",
        use_detail_api=False,
        min_match_score=0.3,
    )
    for rec in records:
        profit = rec.policy_flags.get("profit") or {}
        if profit.get("verdict") in (None, "NO_1688_MATCH", "NO_1688_PRICE"):
            _mark_missing(rec, "profit", profit.get("reason") or "N10 could not calculate profit")
            if not profit:
                rec.policy_flags["profit"] = {
                    "verdict": EVIDENCE_INSUFFICIENT,
                    "reason": "N10 could not calculate profit",
                }
        else:
            _clear_missing(rec, "profit")
    return result


def _apply_price(records: list[ProductRecord], country: str) -> dict[str, Any]:
    try:
        return analyze_price(records, country=country, family="bags_luggage")
    except Exception as exc:
        for rec in records:
            pack_size = extract_pack_size(rec)
            rec.policy_flags["pack_size"] = pack_size
            value = rec.price.get("value")
            if value:
                rec.policy_flags["unit_price"] = round(float(value) / max(pack_size, 1))
                rec.policy_flags["unit_price_raw"] = float(value) / max(pack_size, 1)
        return {
            "stats": {
                "n": len(records),
                "n_with_price": sum(1 for rec in records if rec.price.get("value")),
                "n_with_unit_price": sum(1 for rec in records if rec.policy_flags.get("unit_price")),
            },
            "error": type(exc).__name__,
            "note": "self price band unavailable; pack size and unit price still calculated",
        }


def _candidate_dict(rec: ProductRecord) -> dict[str, Any]:
    missing = list(rec.policy_flags.get("missing_evidence") or [])
    evidence_state = EVIDENCE_INSUFFICIENT if missing else "sufficient"
    profit = rec.policy_flags.get("profit") or {}
    if missing and not profit:
        profit = {"verdict": EVIDENCE_INSUFFICIENT}
    return {
        "id": rec.id,
        "platform": rec.platform,
        "sku_id": rec.id.split(":", 1)[1],
        "title": rec.title,
        "url": rec.url,
        "evidence_state": evidence_state,
        "missing_evidence": missing,
        "relevance": rec.policy_flags.get("relevance_check") or {},
        "price": {
            "value": rec.price.get("value"),
            "currency": rec.price.get("currency"),
            "pack_size": rec.policy_flags.get("pack_size"),
            "unit_price_sar": rec.policy_flags.get("unit_price"),
            "price_vs_self": rec.policy_flags.get("price_vs_self"),
        },
        "sales": {
            "type": rec.sales_signal.type,
            "raw_value": rec.sales_signal.raw_value,
            "raw_text": rec.sales_signal.raw_text,
            "percentile_in_query": rec.sales_signal.percentile_in_query,
            "tier_in_query": rec.sales_signal.tier_in_query,
            "is_rising": rec.sales_signal.is_rising,
            "rising_evidence": rec.sales_signal.rising_evidence,
        },
        "features": {
            "inferred_features": list(rec.inferred_features or []),
            "n6_extracted": rec.policy_flags.get("n6_extracted") or {},
            "group": rec.policy_flags.get("group_aggregated") or {},
        },
        "supply": rec.policy_flags.get("supply")
        or {"status": EVIDENCE_INSUFFICIENT if "supply_1688" in missing else "unknown"},
        "profit": profit,
        "overall_v3": rec.policy_flags.get("overall_v3") or {},
    }


def run_ksa_luggage_noon(
    *,
    seed: str = "luggage",
    category: str = "luggage",
    country: str = "ksa",
    listing_provider: Optional[ListingProvider] = None,
    detail_provider: Optional[DetailProvider] = _default_detail_provider,
    feature_extractor: Optional[FeatureExtractor] = None,
    supply_provider: Optional[SupplyProvider] = None,
    ali_records: Optional[list[ProductRecord]] = None,
    keywords: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run the repeatable KSA luggage/noon production path.

    Providers make the path testable with frozen fixtures. In live use, the
    default listing/detail providers call Firecrawl-based noon fetchers; N6 and
    1688 providers must be supplied by the caller because they depend on Claude
    CLI output and 1688 login/cookie state.
    """
    node_trace: list[str] = []
    summaries: dict[str, Any] = {}

    node_trace.append("N1")
    expanded_keywords = list(keywords or expand(seed, category))
    listing_provider = listing_provider or _default_noon_listing_provider

    fetched: list[ProductRecord] = []
    node_trace.append("noon_fetch")
    fetch_errors: list[dict] = []
    for keyword in expanded_keywords:
        try:
            fetched.extend(listing_provider(keyword, country))
        except Exception as exc:
            fetch_errors.append({"keyword": keyword, "error": type(exc).__name__})
    if fetch_errors and not fetched:
        summaries["noon_fetch"] = {
            "keywords": expanded_keywords,
            "n_records": 0,
            "errors": fetch_errors,
        }
        return {
            "status": EVIDENCE_INSUFFICIENT,
            "country": country,
            "category": category,
            "seed": seed,
            "node_trace": node_trace,
            "summaries": summaries,
            "candidates": [],
            "dropped": [],
            "error": "noon_listing_provider_failed",
        }
    records = _dedupe_by_id(fetched)
    summaries["noon_fetch"] = {
        "keywords": expanded_keywords,
        "n_records": len(records),
        **({"errors": fetch_errors} if fetch_errors else {}),
    }

    node_trace.append("N3")
    relevance = apply_relevance_filter(records, category=category)
    candidates = list(relevance["passed"])
    summaries["N3"] = relevance["stats"]

    node_trace.append("N4")
    summaries["N4"] = _apply_price(candidates, country)

    node_trace.append("N5")
    summaries["N5"] = normalize_sales(candidates)

    node_trace.append("N5.5")
    summaries["detail"] = _apply_detail(candidates, detail_provider)
    summaries["N5.5"] = apply_is_rising(candidates)

    node_trace.append("N5.6")
    summaries["N5.6"] = apply_grouping(candidates)

    node_trace.append("N6")
    summaries["N6"] = _apply_features(candidates, feature_extractor)

    node_trace.append("N7")
    summaries["N7"] = _apply_supply(candidates, supply_provider)

    node_trace.append("N10")
    summaries["N10"] = _apply_profit(candidates, ali_records)

    node_trace.append("N11_v3")
    summaries["N11_v3"] = apply_v3(candidates, n_buckets=5, country=country)

    candidate_rows = [_candidate_dict(rec) for rec in candidates]
    status = EVIDENCE_INSUFFICIENT if any(row["missing_evidence"] for row in candidate_rows) else "ok"
    return {
        "status": status,
        "country": country,
        "category": category,
        "seed": seed,
        "node_trace": node_trace,
        "summaries": summaries,
        "candidates": candidate_rows,
        "dropped": [
            {"id": rec.id, "title": rec.title, "reason": reason}
            for rec, reason in relevance["dropped"]
        ],
    }

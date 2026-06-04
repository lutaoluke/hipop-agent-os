"""N7 1688 supply matching with text fallback.

This module is deliberately provider-based: live 1688 image/text search depends
on local browser login state, so production callers inject those functions and
smoke tests use frozen fixtures. The output shape matches
``production_pipeline.SupplyProvider`` rows.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Optional

from selection.l1_normalize.product_record import ProductRecord


EVIDENCE_INSUFFICIENT = "evidence_insufficient"

VERDICT_INQUIRY = "inquiry"
VERDICT_DIFFERENTIATION = "differentiation"
VERDICT_WATCH = "watch"
VERDICT_DROP = "drop"

STATUS_SUFFICIENT = "sufficient"
STATUS_LOW_MATCH = "low_match"

IMAGE_FALLBACK_THRESHOLD = 0.75
HIGH_CONFIDENCE_THRESHOLD = 0.80
MEDIUM_CONFIDENCE_THRESHOLD = 0.65
LOW_CONFIDENCE_THRESHOLD = 0.45

ImageSearchProvider = Callable[[dict[str, Any]], list[dict[str, Any]]]
TextSearchProvider = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class SupplySearchError(Exception):
    """Base class for expected N7 provider failures."""


class ImageSearchFailed(SupplySearchError):
    """Image search ran but did not produce usable same-design candidates."""


class LoginRequired(SupplySearchError):
    """1688 requires a local logged-in Chrome/browser profile."""


class ExternalUnavailable(SupplySearchError):
    """1688 or the local browser/search adapter is unavailable."""


def build_1688_supply_provider(
    *,
    image_search_provider: Optional[ImageSearchProvider] = None,
    text_search_provider: Optional[TextSearchProvider] = None,
    top_k_keep: int = 5,
) -> Callable[[list[ProductRecord]], list[dict[str, Any]]]:
    """Return a production-pipeline supply provider.

    ``image_search_provider`` receives a query dict built from the demand SKU.
    ``text_search_provider`` receives the fallback keyword string plus the same
    query dict. Both return raw offer dicts; this module normalizes verdict,
    confidence, source, and risk fields.
    """

    def provider(records: list[ProductRecord]) -> list[dict[str, Any]]:
        return [
            match_record_supply(
                rec,
                image_search_provider=image_search_provider,
                text_search_provider=text_search_provider,
                top_k_keep=top_k_keep,
            )
            for rec in records
        ]

    return provider


def match_record_supply(
    rec: ProductRecord,
    *,
    image_search_provider: Optional[ImageSearchProvider] = None,
    text_search_provider: Optional[TextSearchProvider] = None,
    top_k_keep: int = 5,
) -> dict[str, Any]:
    query = build_supply_query(rec)

    if image_search_provider is None:
        return _insufficient_row(rec, "image_search_not_configured", "1688 image search provider not configured")

    try:
        image_offers = [_normalize_offer(item, query, "image_search") for item in image_search_provider(query)]
    except LoginRequired as exc:
        return _login_required_row(rec, str(exc) or "1688 login required")
    except ExternalUnavailable as exc:
        return _insufficient_row(rec, "external_unavailable", str(exc) or "1688 provider unavailable")
    except ImageSearchFailed:
        image_offers = []
    except Exception as exc:
        image_offers = []
        query["image_error"] = f"{type(exc).__name__}: {exc}"

    image_offers = _rank_offers(image_offers, top_k_keep)
    if _has_usable_image_result(image_offers):
        return _row_from_offers(rec, query, image_offers, "image_search")

    if text_search_provider is None:
        return _insufficient_row(
            rec,
            "text_search_not_configured",
            "1688 image search failed and text fallback provider not configured",
            match_source="image_search",
        )

    query_text = build_fallback_query_text(query)
    try:
        text_raw = text_search_provider(query_text, query)
    except LoginRequired as exc:
        return _login_required_row(rec, str(exc) or "1688 login required")
    except ExternalUnavailable as exc:
        return _insufficient_row(rec, "external_unavailable", str(exc) or "1688 text fallback unavailable")
    except Exception as exc:
        return _insufficient_row(
            rec,
            "text_search_error",
            f"1688 text fallback failed: {type(exc).__name__}",
            match_source="text_fallback",
        )

    text_offers = _rank_offers(
        [_normalize_offer(item, query, "text_fallback") for item in (text_raw or [])],
        top_k_keep,
    )
    if not text_offers:
        return _insufficient_row(
            rec,
            "text_search_no_results",
            "1688 text fallback returned no results",
            match_source="text_fallback",
        )

    return _row_from_offers(rec, query, text_offers, "text_fallback")


def build_supply_query(rec: ProductRecord) -> dict[str, Any]:
    n6 = rec.policy_flags.get("n6_extracted") or {}
    detail = rec.policy_flags.get("detail") or {}
    specs = detail.get("specifications") or {}
    material = _first_text(n6.get("material"), specs.get("Material"), _extract_material(rec.title))
    sizes = _extract_sizes(rec.title)
    for size in n6.get("size_inches") or []:
        try:
            sizes.append(int(size))
        except (TypeError, ValueError):
            pass
    sizes = sorted(set(size for size in sizes if 12 <= size <= 32))
    features = _flatten_features(n6.get("features")) + _flatten_features(rec.inferred_features)
    return {
        "record_id": rec.id,
        "title": rec.title or "",
        "image_url": rec.images[0] if rec.images else None,
        "traffic_term": rec.policy_flags.get("search_query") or "",
        "material": material,
        "sizes": sizes,
        "pack": rec.policy_flags.get("pack_size") or n6.get("pieces") or 1,
        "features": list(dict.fromkeys(f for f in features if f)),
    }


def build_fallback_query_text(query: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("traffic_term", "material"):
        value = (query.get(key) or "").strip()
        if value:
            parts.append(value)
    pack = query.get("pack") or 1
    try:
        pack_int = int(pack)
    except (TypeError, ValueError):
        pack_int = 1
    if pack_int >= 3:
        parts.append(f"{pack_int} piece set")
    elif pack_int == 2:
        parts.append("2 piece luggage set")
    parts.append("luggage suitcase")
    for size in query.get("sizes") or []:
        parts.append(f"{size} inch")
    parts.extend((query.get("features") or [])[:4])
    return " ".join(_dedupe_terms(parts)).strip()


def _row_from_offers(
    rec: ProductRecord,
    query: dict[str, Any],
    offers: list[dict[str, Any]],
    match_source: str,
) -> dict[str, Any]:
    best = offers[0]
    confidence = best["confidence"]
    high_confidence = best["verdict"] == VERDICT_INQUIRY and confidence == "high"
    status = STATUS_SUFFICIENT if confidence in ("high", "medium") else STATUS_LOW_MATCH
    differences = _risk_notes(query, best)
    if status == STATUS_LOW_MATCH and not differences:
        differences = ["best 1688 candidate is below same-design confidence threshold"]
    return {
        "record_id": rec.id,
        "status": status,
        "match_source": match_source,
        "match_method": match_source,
        "confidence": confidence,
        "is_same_design_high_confidence": high_confidence,
        "best_offer_id": best.get("offer_id"),
        "best_offer_title": best.get("title"),
        "best_offer_url": best.get("url"),
        "best_offer_price": best.get("price"),
        "score": best.get("score"),
        "main_differences": differences,
        "risk_flags": _risk_flags(query, best),
        "query_text": build_fallback_query_text(query) if match_source == "text_fallback" else None,
        "offers": offers,
    }


def _insufficient_row(
    rec: ProductRecord,
    reason_code: str,
    reason: str,
    *,
    match_source: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "record_id": rec.id,
        "status": EVIDENCE_INSUFFICIENT,
        "reason_code": reason_code,
        "reason": reason,
        "match_source": match_source,
        "match_method": match_source,
        "confidence": "unknown",
        "is_same_design_high_confidence": False,
        "offers": [],
        "main_differences": [],
        "risk_flags": ["evidence_insufficient"],
    }


def _login_required_row(rec: ProductRecord, reason: str) -> dict[str, Any]:
    row = _insufficient_row(rec, "login_required", reason, match_source="image_search")
    row["action_required"] = "Ask Luke to open/use a local Chrome profile that is logged into 1688; do not request account credentials in the issue."
    return row


def _normalize_offer(item: dict[str, Any], query: dict[str, Any], match_source: str) -> dict[str, Any]:
    title = str(item.get("title") or item.get("subject") or item.get("subjectTrans") or "")
    provided_score = _to_float(
        item.get("score"),
        item.get("match_score"),
        item.get("combined_score"),
        item.get("cos_score"),
    )
    score = provided_score if provided_score is not None else _title_similarity(query.get("title") or "", title)
    material = item.get("material") or _extract_material(title)
    if query.get("material") and material and _norm_material(query["material"]) != _norm_material(material):
        score = min(score, 0.64)
    confidence = _confidence_for(score)
    return {
        "offer_id": item.get("offer_id") or item.get("offerId") or item.get("id"),
        "title": title,
        "url": item.get("url") or item.get("promotionURL") or item.get("detail_url"),
        "image_url": item.get("image_url") or item.get("imageUrl") or item.get("offer_pic_url"),
        "price": item.get("price"),
        "score": round(score, 3),
        "confidence": confidence,
        "verdict": _verdict_for(score),
        "match_source": match_source,
        "match_method": match_source,
        "material": material,
        "risk_flags": _risk_flags(query, {"title": title, "score": score, "material": material}),
    }


def _has_usable_image_result(offers: list[dict[str, Any]]) -> bool:
    top5 = offers[:5]
    return bool(top5) and any((offer.get("score") or 0) >= IMAGE_FALLBACK_THRESHOLD for offer in top5)


def _rank_offers(offers: list[dict[str, Any]], top_k_keep: int) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for offer in offers:
        key = offer.get("offer_id") or offer.get("url") or offer.get("title")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(offer)
    deduped.sort(key=lambda offer: offer.get("score") or 0, reverse=True)
    return deduped[:top_k_keep]


def _verdict_for(score: float) -> str:
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        return VERDICT_INQUIRY
    if score >= MEDIUM_CONFIDENCE_THRESHOLD:
        return VERDICT_DIFFERENTIATION
    if score >= LOW_CONFIDENCE_THRESHOLD:
        return VERDICT_WATCH
    return VERDICT_DROP


def _confidence_for(score: float) -> str:
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        return "high"
    if score >= MEDIUM_CONFIDENCE_THRESHOLD:
        return "medium"
    if score >= LOW_CONFIDENCE_THRESHOLD:
        return "low"
    return "low"


def _risk_notes(query: dict[str, Any], offer: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    q_mat = _norm_material(query.get("material"))
    o_mat = _norm_material(offer.get("material"))
    if q_mat and o_mat and q_mat != o_mat:
        notes.append(f"material differs: demand={query.get('material')} supply={offer.get('material')}")
    score = offer.get("score") or 0
    if score < MEDIUM_CONFIDENCE_THRESHOLD:
        notes.append("text/title similarity is low")
    return notes


def _risk_flags(query: dict[str, Any], offer: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    q_mat = _norm_material(query.get("material"))
    o_mat = _norm_material(offer.get("material"))
    if q_mat and o_mat and q_mat != o_mat:
        flags.append("material_mismatch")
    if (offer.get("score") or 0) < MEDIUM_CONFIDENCE_THRESHOLD:
        flags.append("low_similarity")
    return flags


def _title_similarity(left: str, right: str) -> float:
    lt = set(_tokens(left))
    rt = set(_tokens(right))
    if not lt or not rt:
        return 0.0
    overlap = len(lt & rt) / len(lt | rt)
    return min(0.95, overlap + 0.25 if overlap >= 0.5 else overlap)


def _tokens(text: str) -> list[str]:
    return [tok for tok in re.findall(r"[a-z0-9]+", text.lower()) if tok not in {"with", "and", "for", "the"}]


def _extract_material(text: str) -> Optional[str]:
    low = (text or "").lower()
    if "abs" in low:
        return "ABS"
    if "polycarbonate" in low or re.search(r"\bpc\b", low):
        return "PC"
    if "polypropylene" in low or re.search(r"\bpp\b", low):
        return "PP"
    if "aluminum" in low or "aluminium" in low or "铝" in low:
        return "aluminum"
    if any(word in low for word in ("soft", "fabric", "oxford", "polyester", "nylon")):
        return "soft"
    return None


def _extract_sizes(text: str) -> list[int]:
    sizes: list[int] = []
    for match in re.finditer(r'(\d{2})\s*(?:inch|inches|in\b|"|寸)', text or "", re.IGNORECASE):
        value = int(match.group(1))
        if 12 <= value <= 32:
            sizes.append(value)
    return sizes


def _flatten_features(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [_clean_feature(value)]
    out: list[str] = []
    for item in value:
        cleaned = _clean_feature(str(item))
        if cleaned:
            out.append(cleaned)
    return out


def _clean_feature(value: str) -> str:
    return value.replace("材质_", "").replace("尺寸_", "").replace("功能_", "").strip()


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _dedupe_terms(parts: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out


def _to_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _norm_material(value: Any) -> str:
    return str(value or "").strip().lower()

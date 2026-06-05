"""Structured selection feedback and offline preference replay."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Optional


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREFERENCES_PATH = PACKAGE_ROOT / "preferences.jsonl"
DEFAULT_PREFERENCES_ROOT = PACKAGE_ROOT / "preferences"

REASON_TAGS = frozenset({
    "missing_candidate",
    "brand",
    "material",
    "color",
    "price",
    "return_risk",
    "supply_1688",
    "profit",
    "inventory",
    "differentiation",
    "relevance",
})
ALLOWED_ACTIONS = frozenset({"include", "reject", "hold"})


def _safe_scope_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    key = re.sub(r"[^a-z0-9_-]+", "_", key)
    return key.strip("_") or "unknown"


def _preferences_root(preferences_root: Optional[Path | str] = None) -> Path:
    if preferences_root is not None:
        return Path(preferences_root)
    return Path(os.environ.get("SELECTION_PREFERENCES_ROOT") or DEFAULT_PREFERENCES_ROOT)


def scoped_preferences_path(
    tenant_id: int | str,
    store: str,
    *,
    preferences_root: Optional[Path | str] = None,
) -> Path:
    """Return the tenant/store-scoped preference asset path."""
    tenant_key = _safe_scope_key(tenant_id)
    store_key = _safe_scope_key(store)
    return _preferences_root(preferences_root) / f"tenant_{tenant_key}" / f"{store_key}.preferences.jsonl"


def _normalize_tags(reason_tags: list[str]) -> list[str]:
    tags = [str(tag).strip() for tag in reason_tags if str(tag).strip()]
    if not tags:
        raise ValueError("reason_tags is required")
    unknown = set(tags) - REASON_TAGS
    if unknown:
        raise ValueError(f"unknown reason_tags: {sorted(unknown)}")
    return tags


def write_candidate_feedback(
    *,
    product_id: str,
    action: str,
    reason_tags: list[str],
    reason_text: str,
    attributes: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
    tenant_id: Optional[int] = None,
    store: Optional[str] = None,
    preferences_path: Path | str = DEFAULT_PREFERENCES_PATH,
) -> dict[str, Any]:
    """Append one structured preference event to preferences.jsonl."""
    action = str(action).strip()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"action must be one of {sorted(ALLOWED_ACTIONS)}")
    if not product_id:
        raise ValueError("product_id is required")
    if not reason_text or not str(reason_text).strip():
        raise ValueError("reason_text is required")

    event = {
        "schema_version": 1,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_id": run_id,
        "tenant_id": tenant_id,
        "store": store,
        "product_id": product_id,
        "action": action,
        "reason_tags": _normalize_tags(reason_tags),
        "reason_text": str(reason_text).strip(),
        "attributes": attributes or {},
    }
    path = Path(preferences_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def write_scoped_candidate_feedback(
    *,
    tenant_id: int,
    store: str,
    product_id: str,
    action: str,
    reason_tags: list[str],
    reason_text: str,
    attributes: Optional[dict[str, Any]] = None,
    run_id: Optional[str] = None,
    preferences_root: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Append one structured feedback event into a tenant/store-scoped asset."""
    path = scoped_preferences_path(tenant_id, store, preferences_root=preferences_root)
    return write_candidate_feedback(
        product_id=product_id,
        action=action,
        reason_tags=reason_tags,
        reason_text=reason_text,
        attributes=attributes,
        run_id=run_id,
        tenant_id=tenant_id,
        store=_safe_scope_key(store),
        preferences_path=path,
    )


def load_preferences(path: Path | str = DEFAULT_PREFERENCES_PATH) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def load_scoped_preferences(
    tenant_id: int | str,
    store: str,
    *,
    preferences_root: Optional[Path | str] = None,
) -> list[dict[str, Any]]:
    return load_preferences(scoped_preferences_path(tenant_id, store, preferences_root=preferences_root))


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _candidate_materials(candidate: dict[str, Any]) -> set[str]:
    selling = candidate.get("selling_points") or {}
    material = selling.get("material")
    values = set()
    if material:
        for part in str(material).replace("/", ",").split(","):
            if part.strip():
                values.add(_lower(part))
    for value in selling.get("inferred_features") or []:
        if isinstance(value, str) and value.startswith("材质_"):
            values.add(_lower(value.replace("材质_", "", 1)))
    return values


def _candidate_colors(candidate: dict[str, Any]) -> set[str]:
    selling = candidate.get("selling_points") or {}
    values = {_lower(selling.get("color"))} if selling.get("color") else set()
    for value in selling.get("inferred_features") or []:
        if isinstance(value, str) and value.startswith("颜色_"):
            values.add(_lower(value.replace("颜色_", "", 1)))
    return {value for value in values if value and value != "unknown"}


def _matches_attribute_feedback(candidate: dict[str, Any], event: dict[str, Any]) -> bool:
    attrs = event.get("attributes") or {}
    tags = set(event.get("reason_tags") or [])
    if not attrs:
        return False

    checks: list[bool] = []
    if "brand" in tags and attrs.get("brand"):
        checks.append(_lower(attrs.get("brand")) == _lower(candidate.get("brand")))
    if "material" in tags and attrs.get("material"):
        checks.append(_lower(attrs.get("material")) in _candidate_materials(candidate))
    if "color" in tags and attrs.get("color"):
        checks.append(_lower(attrs.get("color")) in _candidate_colors(candidate))
    if "price" in tags and attrs.get("max_price_sar") is not None:
        price = (candidate.get("price_normalized") or {}).get("unit_price_sar")
        checks.append(price is not None and float(price) > float(attrs.get("max_price_sar")))
    if "return_risk" in tags and attrs.get("return_risk"):
        risk = candidate.get("return_risk") or {}
        signals = (candidate.get("selling_points") or {}).get("return_risk_signal") or []
        checks.append(_lower(attrs.get("return_risk")) == _lower(risk.get("level")) or bool(signals))
    return bool(checks) and all(checks)


def _event_matches_candidate(candidate: dict[str, Any], event: dict[str, Any]) -> bool:
    if event.get("product_id") in {candidate.get("id"), candidate.get("product_id")}:
        return True
    return _matches_attribute_feedback(candidate, event)


def _effect(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_product_id": event.get("product_id"),
        "action": event.get("action"),
        "reason_tags": list(event.get("reason_tags") or []),
        "reason_text": event.get("reason_text"),
        "created_at": event.get("created_at"),
    }


def apply_preferences_to_candidate_pool(
    candidate_pool: dict[str, Any],
    preferences: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replay structured feedback onto an offline candidate pool."""
    pool = deepcopy(candidate_pool)
    pending_includes: list[dict[str, Any]] = []
    matched_include_ids: set[str] = set()

    for candidate in pool.get("candidates") or []:
        candidate.setdefault("feedback_status", "unreviewed")
        candidate.setdefault("feedback_reason_tags", [])
        candidate.setdefault("preference_effects", [])
        for event in preferences:
            if event.get("action") == "include":
                if _event_matches_candidate(candidate, event):
                    candidate["feedback_status"] = "included_by_preference"
                    candidate["preference_effects"].append(_effect(event))
                    matched_include_ids.add(event.get("product_id"))
                continue
            if event.get("action") == "reject" and _event_matches_candidate(candidate, event):
                candidate["feedback_status"] = "rejected_by_preference"
                tags = set(candidate.get("feedback_reason_tags") or [])
                tags.update(event.get("reason_tags") or [])
                candidate["feedback_reason_tags"] = sorted(tags)
                candidate["preference_effects"].append(_effect(event))
            elif event.get("action") == "hold" and _event_matches_candidate(candidate, event):
                candidate["feedback_status"] = "hold_by_preference"
                candidate["preference_effects"].append(_effect(event))

    for event in preferences:
        if event.get("action") != "include":
            continue
        if event.get("product_id") in matched_include_ids:
            continue
        pending_includes.append(_effect(event))
    pool["pending_inclusion_requests"] = pending_includes
    return pool

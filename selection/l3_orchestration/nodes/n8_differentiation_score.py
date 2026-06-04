"""N8 — deterministic supply-side differentiation scoring for luggage."""
from __future__ import annotations

from typing import Any

from selection.l1_normalize.product_record import ProductRecord


EVIDENCE_INSUFFICIENT = "evidence_insufficient"

SIGNAL_RULES = (
    {
        "id": "expandable_layer",
        "label": "拓展层",
        "weight": 0.50,
        "patterns": ("拓展层", "expandable", "expansion", "扩展层", "可扩容"),
        "reason": "拓展层是商品新卖点, 也支持嵌套发货降低物流成本。",
    },
    {
        "id": "cup_holder",
        "label": "杯架",
        "weight": 0.15,
        "patterns": ("咖啡杯架", "杯架", "cup holder", "drink holder"),
        "reason": "杯架是可见功能差异化点。",
    },
    {
        "id": "spinner_wheels",
        "label": "万向轮",
        "weight": 0.15,
        "patterns": ("万向轮", "spinner", "360", "wheel"),
        "reason": "万向轮是行李箱基础但可解释的功能差异化点。",
    },
    {
        "id": "material_aesthetic",
        "label": "材质/颜值",
        "weight": 0.20,
        "patterns": ("abs+pc", "abs", "铝框", "morandi", "莫兰迪", "green", "颜值"),
        "reason": "材质和颜色证据进入候选理由, 避免只按销量排序。",
    },
    {
        "id": "nestable_logistics",
        "label": "可嵌套物流",
        "weight": 0.20,
        "patterns": ("nest", "nested", "嵌套", "套装", "set"),
        "reason": "可嵌套/套装有机会降低物流成本并带走既有库存。",
    },
)


def _text_sources(rec: ProductRecord) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = [("title", rec.title or "")]

    n6 = rec.policy_flags.get("n6_extracted") or {}
    for feat in n6.get("features") or []:
        sources.append(("n6_features", str(feat)))
    for feat in rec.inferred_features or []:
        sources.append(("inferred_features", str(feat)))
    if n6.get("material"):
        sources.append(("material", str(n6["material"])))
    if n6.get("color_main"):
        sources.append(("color", str(n6["color_main"])))

    detail = rec.policy_flags.get("detail") or {}
    for item in detail.get("highlights") or []:
        sources.append(("detail_highlight", str(item)))
    for key, value in (detail.get("specifications") or {}).items():
        sources.append(("detail_spec", f"{key}: {value}"))

    supply = rec.policy_flags.get("supply") or {}
    for offer in supply.get("offers") or []:
        for key in ("title", "verdict", "reason"):
            if offer.get(key):
                sources.append(("supply_offer", str(offer[key])))

    return [(name, text) for name, text in sources if text]


def _first_match(sources: list[tuple[str, str]], patterns: tuple[str, ...]) -> dict[str, str] | None:
    for source, text in sources:
        lowered = text.lower()
        for pattern in patterns:
            if pattern.lower() in lowered:
                return {"source": source, "text": text, "pattern": pattern}
    return None


def apply(records: list[ProductRecord]) -> dict[str, Any]:
    updated = 0
    insufficient = 0
    signal_counts: dict[str, int] = {}

    for rec in records:
        signals = []
        sources = _text_sources(rec)
        for rule in SIGNAL_RULES:
            evidence = _first_match(sources, rule["patterns"])
            if not evidence:
                continue
            signal = {
                "id": rule["id"],
                "label": rule["label"],
                "weight": rule["weight"],
                "reason": rule["reason"],
                "evidence": evidence,
            }
            signals.append(signal)
            signal_counts[rule["id"]] = signal_counts.get(rule["id"], 0) + 1

        if signals:
            score = round(sum(float(s["weight"]) for s in signals), 3)
            rec.policy_flags["differentiation"] = {
                "state": "sufficient",
                "score": score,
                "signals": signals,
                "reason": "供给端差异化证据已结构化进入排序。",
            }
            updated += 1
        else:
            rec.policy_flags["differentiation"] = {
                "state": EVIDENCE_INSUFFICIENT,
                "score": 0.0,
                "signals": [],
                "reason": "no differentiation evidence from title/detail/N6/supply",
            }
            insufficient += 1

    return {
        "n_updated": updated,
        "n_insufficient": insufficient,
        "signal_counts": signal_counts,
    }

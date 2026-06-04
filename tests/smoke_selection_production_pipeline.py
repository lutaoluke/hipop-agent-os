"""WS-65 smoke: KSA luggage/noon production entry point."""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from selection.l1_normalize.product_record import ProductRecord, SalesSignal
from selection.l3_orchestration.production_pipeline import (
    EVIDENCE_INSUFFICIENT,
    run_ksa_luggage_noon,
)


def _noon_record(
    sku: str,
    title: str,
    *,
    price: float,
    sold: float | None,
    image: bool = True,
    rating: float = 4.5,
    reviews: int = 120,
) -> ProductRecord:
    return ProductRecord(
        id=f"noon_sa:{sku}",
        platform="noon_sa",
        url=f"https://www.noon.com/saudi-en/{sku}/p",
        title=title,
        brand=None,
        category_path=["luggage"],
        images=[f"https://img.nooncdn.com/{sku}.jpg"] if image else [],
        price={"value": price, "currency": "SAR"},
        sales_signal=SalesSignal(
            type="absolute_count" if sold is not None else "unknown",
            raw_value=sold,
            raw_text=f"{int(sold)}+ sold recently" if sold is not None else None,
            source="fixture",
            confidence=0.9 if sold is not None else 0.0,
            tier_in_query=None if sold is not None else "low",
        ),
        reviews={"avg": rating, "count": reviews},
        policy_flags={"country": "ksa", "search_query": "luggage"},
    )


def _ali_record(offer_id: str, title: str, unit_rmb: float) -> ProductRecord:
    return ProductRecord(
        id=f"alibaba_1688:{offer_id}",
        platform="alibaba_1688",
        url=f"https://detail.1688.com/offer/{offer_id}.html",
        title=title,
        brand=None,
        category_path=["luggage"],
        images=[f"https://cbu01.alicdn.com/{offer_id}.jpg"],
        price={"value": unit_rmb, "currency": "CNY"},
        sales_signal=SalesSignal(type="unknown", source="fixture", tier_in_query="low"),
        policy_flags={
            "relevance_check": {"passed": True, "reason": "fixture"},
            "pack_size": 1,
            "unit_price": unit_rmb,
        },
    )


def _detail_provider(records):
    for rec in records:
        rec.policy_flags["detail"] = {
            "highlights": ["extra strong ABS shell", "360 spinner wheels", "TSA lock"],
            "specifications": {"Material": "ABS", "Size": "20 inch"},
            "reviews_summary": {
                "recent_1y_ratio": 0.9,
                "dates_spread_days": 120,
                "dates_count_visible": 10,
            },
            "variants": [],
        }
    return {"n_ok": len(records), "n_fail": 0}


def _mature_detail_provider(records):
    for rec in records:
        rec.policy_flags["detail"] = {
            "highlights": ["extra strong ABS shell", "360 spinner wheels", "TSA lock"],
            "specifications": {"Material": "ABS"},
            "reviews_summary": {
                "recent_1y_ratio": 0.3,
                "dates_spread_days": 720,
                "dates_count_visible": 10,
            },
            "variants": [],
        }
    return {"n_ok": len(records), "n_fail": 0}


def _feature_extractor(records):
    for rec in records:
        features = ["万向轮", "TSA锁"]
        if "expandable" in rec.title.lower():
            features.append("拓展层")
        if "cup holder" in rec.title.lower():
            features.append("咖啡杯架")
        size = 24 if "24 inch" in rec.title.lower() else 20
        rec.inferred_features = [f"材质_ABS", f"尺寸_{size}寸"] + [f"功能_{f}" for f in features]
        rec.policy_flags["n6_extracted"] = {
            "material": "ABS",
            "size_inches": [size],
            "pieces": 1,
            "color_main": "green",
            "features": features,
            "return_risk_signal": [],
        }
    return {"n_updated": len(records), "n_not_found": 0}


def _supply_provider(records):
    return [
        {
            "record_id": rec.id,
            "status": "sufficient",
            "offers": [{"offer_id": "16880001", "verdict": "inquiry", "combined_score": 0.91}],
        }
        for rec in records
    ]


def test_complete_fixture_produces_sku_candidates_and_calls_confirmed_nodes():
    records = [
        _noon_record("ZAAA111111111", "20 inch ABS hardside luggage suitcase spinner", price=199, sold=95),
        _noon_record("ZBBB222222222", "24 inch ABS suitcase spinner luggage", price=259, sold=35),
        _noon_record("ZCCC333333333", "gym duffel bag with shoulder strap", price=89, sold=70),
    ]
    result = run_ksa_luggage_noon(
        seed="luggage",
        listing_provider=lambda _keyword, _country: records,
        detail_provider=_mature_detail_provider,
        feature_extractor=_feature_extractor,
        supply_provider=_supply_provider,
        inventory_provider=lambda _country, _family: [
            {
                "partner_sku": "HIPOP20NORMAL",
                "title": "20 inch ABS hardside luggage suitcase spinner",
                "family": "bags_luggage",
                "product_category_detail": "20 inch luggage",
                "total_stock": 20,
                "sales_30d": 15,
            }
        ],
        ali_records=[_ali_record("16880001", "20 inch ABS luggage suitcase spinner", 80)],
    )

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert all(c["platform"] == "noon_sa" for c in result["candidates"])
    assert all(c["sku_id"].startswith("Z") for c in result["candidates"])
    assert all(c["relevance"]["passed"] is True for c in result["candidates"])
    assert all(c["price"].get("unit_price_sar") for c in result["candidates"])
    assert all(c["sales"].get("tier_in_query") for c in result["candidates"])
    assert all(c["supply"].get("status") == "sufficient" for c in result["candidates"])
    assert all(c["profit"].get("verdict") for c in result["candidates"])
    assert all(c["overall_v3"].get("tier_overall") for c in result["candidates"])

    for node in ("N1", "noon_fetch", "N3", "N4", "N5", "N5.5", "N6", "N7", "N8", "N9", "N10", "N11_v3"):
        assert node in result["node_trace"], result["node_trace"]


def test_inventory_reverse_and_differentiation_feed_n11():
    records = [
        _noon_record("ZREG20000000", "20 inch ABS hardside luggage suitcase spinner", price=199, sold=90),
        _noon_record(
            "ZEXP24000000",
            "24 inch expandable ABS luggage suitcase spinner wheels with cup holder",
            price=259,
            sold=90,
        ),
    ]
    inventory_rows = [
        {
            "partner_sku": "HIPOP20BACKLOG",
            "title": "20 inch ABS hardside luggage suitcase spinner",
            "family": "bags_luggage",
            "product_category_detail": "20 inch luggage",
            "total_stock": 420,
            "noon_saleable_qty": 180,
            "overseas_total_qty": 120,
            "yiwu_qty": 70,
            "dongguan_qty": 50,
            "sales_30d": 3,
            "sales_grade": "low",
        }
    ]
    result = run_ksa_luggage_noon(
        seed="luggage",
        listing_provider=lambda _keyword, _country: records,
        detail_provider=_mature_detail_provider,
        feature_extractor=_feature_extractor,
        supply_provider=_supply_provider,
        ali_records=[_ali_record("16880001", "24 inch expandable ABS luggage suitcase spinner", 80)],
        inventory_provider=lambda _country, _family: inventory_rows,
    )
    by_sku = {row["sku_id"]: row for row in result["candidates"]}

    for node in ("N8", "N9", "N11_v3"):
        assert node in result["node_trace"], result["node_trace"]

    exp24 = by_sku["ZEXP24000000"]
    reg20 = by_sku["ZREG20000000"]
    signal_ids = {s["id"] for s in exp24["differentiation"]["signals"]}
    assert {"expandable_layer", "cup_holder", "spinner_wheels"} <= signal_ids
    assert exp24["inventory"]["score_adjustment"] > 0
    assert reg20["inventory"]["score_adjustment"] < 0
    assert exp24["overall_v3"]["breakdown"]["differentiation_pct"] > reg20["overall_v3"]["breakdown"]["differentiation_pct"]
    assert exp24["overall_v3"]["breakdown"]["inventory_pct"] > reg20["overall_v3"]["breakdown"]["inventory_pct"]
    assert exp24["overall_v3"]["score"] > reg20["overall_v3"]["score"]


def test_no_inventory_data_is_explicitly_insufficient():
    records = [
        _noon_record("ZNOSTOCK0000", "24 inch expandable ABS luggage suitcase spinner", price=249, sold=30)
    ]
    result = run_ksa_luggage_noon(
        seed="luggage",
        listing_provider=lambda _keyword, _country: records,
        detail_provider=_detail_provider,
        feature_extractor=_feature_extractor,
        supply_provider=_supply_provider,
        ali_records=[_ali_record("16880001", "24 inch expandable ABS luggage suitcase spinner", 80)],
        inventory_provider=lambda _country, _family: [],
    )
    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == EVIDENCE_INSUFFICIENT
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert candidate["inventory"]["reasons"] == []
    assert "inventory" in candidate["missing_evidence"]


def test_missing_external_evidence_is_explicit_not_dropped_or_faked():
    records = [
        _noon_record(
            "ZDDD444444444",
            "20 inch hardside luggage suitcase spinner",
            price=189,
            sold=18,
            image=False,
            reviews=20,
        )
    ]
    result = run_ksa_luggage_noon(
        seed="luggage",
        listing_provider=lambda _keyword, _country: records,
        detail_provider=None,
        feature_extractor=None,
        supply_provider=None,
        ali_records=[],
    )

    assert result["status"] == EVIDENCE_INSUFFICIENT
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["evidence_state"] == EVIDENCE_INSUFFICIENT
    assert candidate["missing_evidence"]
    assert "detail" in candidate["missing_evidence"]
    assert "image" in candidate["missing_evidence"]
    assert "supply_1688" in candidate["missing_evidence"]
    assert candidate["sales"]["tier_in_query"] is not None
    assert candidate["profit"]["verdict"] == EVIDENCE_INSUFFICIENT


if __name__ == "__main__":
    test_complete_fixture_produces_sku_candidates_and_calls_confirmed_nodes()
    print("  ✓ test_complete_fixture_produces_sku_candidates_and_calls_confirmed_nodes")
    test_inventory_reverse_and_differentiation_feed_n11()
    print("  ✓ test_inventory_reverse_and_differentiation_feed_n11")
    test_no_inventory_data_is_explicitly_insufficient()
    print("  ✓ test_no_inventory_data_is_explicitly_insufficient")
    test_missing_external_evidence_is_explicit_not_dropped_or_faked()
    print("  ✓ test_missing_external_evidence_is_explicit_not_dropped_or_faked")

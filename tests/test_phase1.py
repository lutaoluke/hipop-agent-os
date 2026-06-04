"""
Phase 1 端到端测试 - 跑过 80%+ 即视为达标
"""
import os, sys, json, subprocess, time
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BASE = "http://127.0.0.1:8765"


_client = httpx.Client(trust_env=False, timeout=30)


def _get(path: str, timeout: int = 15):
    r = _client.get(f"{BASE}{path}", timeout=timeout)
    return r.status_code, r.text


def _post(path: str, body: dict, timeout: int = 90):
    r = _client.post(f"{BASE}{path}", json=body, timeout=timeout)
    return r.status_code, r.text


def test_health():
    s, b = _get("/health")
    assert s == 200 and "ok" in b


def test_overview_html():
    s, b = _get("/")
    assert s == 200 and "今日总览" in b and "modules-grid" in b


def test_drilldown_pages():
    for p in ("/module/sales", "/module/logistics", "/module/replenish",
              "/module/selection", "/module/feishu", "/role/liuhe"):
        s, _ = _get(p)
        assert s == 200, p


def test_today_api():
    s, b = _get("/api/today/ksa")
    d = json.loads(b)
    assert s == 200
    assert d["store"] == "KSA"
    assert d["sku_count"] > 0
    assert "alerts_pending" in d


def test_modules_api():
    s, b = _get("/api/modules/ksa")
    d = json.loads(b)
    assert s == 200
    keys = {m["key"] for m in d}
    assert {"data", "sales", "logistics", "replenish", "traffic", "selection", "marketing", "feishu"} <= keys


def test_sku_health_api():
    s, b = _get("/api/sku-health/ksa?urgency=urgent&limit=5")
    d = json.loads(b)
    assert s == 200 and len(d) > 0
    for r in d:
        assert "partner_sku" in r and "trend" in r


def test_orders_api():
    s, b = _get("/api/orders/ksa?limit=10")
    d = json.loads(b)
    assert s == 200 and len(d) > 0
    assert any(o.get("alert_level") == "红" for o in d)


def test_replenishment_api():
    s, b = _get("/api/replenishment/ksa")
    assert s == 200
    json.loads(b)  # 即使空也合法


def test_data_health_api():
    s, b = _get("/api/data-health/ksa")
    d = json.loads(b)
    assert s == 200 and "erp" in d and "noon_sales" in d


def test_team_api():
    s, b = _get("/api/team/ksa")
    d = json.loads(b)
    assert s == 200 and len(d) >= 5


def test_progress_api():
    s, b = _get("/api/progress/current")
    d = json.loads(b)
    assert s == 200 and "steps" in d


def test_cross_store_logistics():
    s, b = _get("/api/cross-store/logistics")
    d = json.loads(b)
    assert s == 200 and "alerts" in d and "stuck_skus" in d
    assert len(d["alerts"]) > 0


def test_selection_api():
    s, b = _get("/api/selection/ksa")
    d = json.loads(b)
    assert s == 200
    assert len(d["candidates"]) == 3
    assert "选品_成功模式_v1.md" in d["strategies"]
    # 策略文档非空
    assert len(d["strategies"]["选品_成功模式_v1.md"]) > 100


def test_feishu_digest_api():
    s, b = _get("/api/feishu-digest")
    d = json.loads(b)
    assert s == 200
    assert len(d) >= 3  # 至少 3 条


def test_agent_actions_listed():
    s, b = _get("/api/agent-actions?store=KSA")
    d = json.loads(b)
    assert s == 200 and len(d) > 0


def test_chat_query_sku():
    """LLM tool-calling 端到端"""
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "查 TBA0210A 的健康情况"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200
    assert d["reply"]
    assert d.get("references")
    assert "query_sku" in (d.get("tools_used") or [])


def test_chat_query_order():
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "PDZ0027158 这个货单怎么样"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"]


def test_chat_unknown_sku():
    """边界 case：找不到的 SKU"""
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "查 SKU XYZ999999 健康度"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"]


def test_chat_feedback_offer_on_out_of_scope():
    """WS-26 验收①：撞到做不了/超范围的事，回复必含一句『记成需求』offer。"""
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "帮我把这个月销量做成 PPT 再发邮件给我老板"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"]
    # offer 可能来自确定性 hook（「记成一条需求」）或 LLM 自己（「记成需求/记成反馈」）
    assert any(m in d["reply"] for m in ("记成一条需求", "记成需求", "记成反馈")), \
        f"撞限回复没 offer 记需求: {d['reply'][:200]}"


def test_chat_feedback_capture_persists():
    """WS-26 验收②③：用户确认 → capture_feedback 真落库，能从 /api/feedback 查到。"""
    s0, b0 = _get("/api/feedback")
    before = json.loads(b0)["count"]
    s, b = _post("/api/chat", {
        "messages": [
            {"role": "user", "content": "能不能把补货建议自动推到企业微信机器人？"},
            {"role": "assistant",
             "content": "这个我暂时做不了，目前没接企业微信。💡 要我把它记成一条需求反馈给产品吗？"},
            {"role": "user", "content": "记一下：把补货建议自动推到企业微信机器人"},
        ],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"]
    assert "capture_feedback" in (d.get("tools_used") or []), \
        f"用户确认后没调 capture_feedback: tools_used={d.get('tools_used')}"
    s2, b2 = _get("/api/feedback")
    after = json.loads(b2)
    assert after["count"] > before, "feedback 没增加（落库失败？）"
    # LLM 可能改写原话，校验稳定关键词而非逐字
    assert any("企业微信" in (it.get("content") or "") for it in after["items"]), \
        "feedback 表查不到刚记的需求（关键词 企业微信）"


def test_diag():
    s, b = _get("/api/diag/db")
    d = json.loads(b)
    assert s == 200 and d["exists"]
    assert "agent_actions" in d["tables"]
    assert "agent_events" in d["tables"]
    assert "feishu_digest" in d["tables"]


def test_strategies_files_exist():
    base = str(REPO_ROOT / "hipop" / "agent_memory" / "strategies")
    for name in ("选品_成功模式_v1.md", "选品_失败模式_v1.md"):
        p = os.path.join(base, name)
        assert os.path.exists(p), f"missing {p}"
        size = os.path.getsize(p)
        assert size > 200, f"{name} too small ({size} bytes)"


def test_p0_wf2_aggregation():
    """P0 回归: wf2 主表 as_of_date 不再漏 SKU
    任何有订单的 SKU 必须有 as_of_date"""
    import sqlite3
    c = sqlite3.connect(str(REPO_ROOT / "hipop.db"))
    n = c.execute("""
        SELECT COUNT(DISTINCT s.partner_sku)
        FROM wf2_hipop_ksa_sku s
        JOIN wf2_hipop_ksa_orders o ON s.partner_sku=o.partner_sku
        WHERE s.as_of_date IS NULL
    """).fetchone()[0]
    c.close()
    assert n == 0, f"还有 {n} 个 SKU 在 orders 但主表 as_of_date NULL"


def test_p0_wf5_target_pipeline():
    """P0 回归: wf5 target_pipeline 不再全为 0
    至少有 30 个 SKU target_pipeline > 0"""
    import sqlite3
    c = sqlite3.connect(str(REPO_ROOT / "hipop.db"))
    ksa = c.execute("SELECT COUNT(*) FROM wf5_hipop_ksa_sales_cycle WHERE target_pipeline > 0").fetchone()[0]
    uae = c.execute("SELECT COUNT(*) FROM wf5_hipop_uae_sales_cycle WHERE target_pipeline > 0").fetchone()[0]
    # TBJ0059A 必须有补货建议
    tbj = c.execute("""SELECT target_pipeline, weekly_total_replenish
        FROM wf5_hipop_ksa_sales_cycle WHERE partner_sku='TBJ0059A'""").fetchone()
    c.close()
    assert ksa + uae >= 30, f"target_pipeline > 0 的 SKU 太少: ksa={ksa} uae={uae}"
    assert tbj is not None and tbj[0] > 0 and tbj[1] > 0, f"TBJ0059A 未给补货建议: {tbj}"


def test_screenshots_exist():
    base = str(REPO_ROOT / "hipop" / "logs" / "screenshots")
    files = sorted(f for f in os.listdir(base) if f.endswith(".png"))
    assert len(files) >= 7, f"only {len(files)} screenshots"


def _selection_noon_record(
    sku,
    title,
    *,
    price,
    sold=None,
    raw_text=None,
    brand=None,
    image=True,
    rating=4.5,
    reviews=80,
    search_query="luggage",
):
    from selection.l1_normalize.product_record import ProductRecord, SalesSignal

    return ProductRecord(
        id=f"noon_sa:{sku}",
        platform="noon_sa",
        url=f"https://www.noon.com/saudi-en/{sku}/p",
        title=title,
        brand=brand,
        category_path=["luggage"],
        images=[f"https://img.nooncdn.com/{sku}.jpg"] if image else [],
        price={"value": price, "currency": "SAR"},
        sales_signal=SalesSignal(
            type="absolute_count" if sold is not None else "unknown",
            raw_value=sold,
            raw_text=raw_text or (f"{int(sold)}+ sold recently" if sold is not None else None),
            source="fixture",
            confidence=0.9 if sold is not None else 0.0,
            tier_in_query=None if sold is not None else "low",
        ),
        reviews={"avg": rating, "count": reviews},
        policy_flags={"country": "ksa", "search_query": search_query},
    )


def _selection_detail_provider(records):
    for rec in records:
        if rec.id.endswith("RISING1"):
            summary = {
                "recent_1y_ratio": 0.92,
                "dates_spread_days": 110,
                "dates_count_visible": 12,
            }
        else:
            summary = {
                "recent_1y_ratio": 0.35,
                "dates_spread_days": 730,
                "dates_count_visible": 12,
            }
        rec.policy_flags["detail"] = {
            "highlights": ["ABS shell", "spinner wheels", "TSA lock"],
            "specifications": {"Material": "ABS", "Size": "20 inch"},
            "reviews_summary": summary,
            "variants": [],
        }
    return {"n_ok": len(records), "n_fail": 0}


def _selection_feature_extractor(records):
    for rec in records:
        rec.inferred_features = ["材质_ABS", "尺寸_20寸", "功能_万向轮", "功能_TSA锁"]
        rec.policy_flags["n6_extracted"] = {
            "material": "ABS",
            "size_inches": [20],
            "pieces": rec.policy_flags.get("pack_size") or 1,
            "color_main": "green",
            "features": ["万向轮", "TSA锁"],
            "return_risk_signal": [],
        }
    return {"n_updated": len(records), "n_not_found": 0}


def _selection_supply_provider(records):
    return [
        {
            "record_id": rec.id,
            "status": "sufficient",
            "offers": [{"offer_id": "1688-fixture", "verdict": "inquiry"}],
        }
        for rec in records
    ]


def _selection_ali_record(offer_id, title, unit_rmb=80):
    from selection.l1_normalize.product_record import ProductRecord, SalesSignal

    pack_size = 4 if "4-piece" in title else 1
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
            "pack_size": pack_size,
            "unit_price": unit_rmb,
        },
    )


def _selection_fixture_result(records, *, detail_provider=_selection_detail_provider,
                              feature_extractor=_selection_feature_extractor,
                              supply_provider=_selection_supply_provider,
                              ali_records=None):
    from selection.l3_orchestration.production_pipeline import run_ksa_luggage_noon

    return run_ksa_luggage_noon(
        seed="luggage",
        keywords=["luggage"],
        listing_provider=lambda _keyword, _country: records,
        detail_provider=detail_provider,
        feature_extractor=feature_extractor,
        supply_provider=supply_provider,
        ali_records=[
            _selection_ali_record("1688-positive", "20 inch ABS hardside luggage suitcase spinner"),
            _selection_ali_record("1688-pack4", "20 inch 4-piece ABS hardside luggage suitcase spinner set"),
            _selection_ali_record("1688-rising", "20 inch cabin hardside luggage spinner carry on suitcase"),
            _selection_ali_record("1688-brand", "American Tourister hardside luggage spinner suitcase"),
        ] if ali_records is None else ali_records,
    )


def test_selection_phase1_deterministic_rules_run_in_production_path():
    records = [
        _selection_noon_record(
            "POSITIVE1",
            "20 inch ABS hardside luggage suitcase spinner",
            price=199,
            sold=95,
        ),
        _selection_noon_record(
            "PACK4",
            "20 inch 4-piece ABS hardside luggage suitcase spinner set",
            price=749,
            sold=40,
        ),
        _selection_noon_record(
            "RISING1",
            "20 inch cabin hardside luggage spinner carry on suitcase",
            price=229,
            sold=None,
            rating=4.7,
            reviews=18,
        ),
        _selection_noon_record(
            "BRAND1",
            "American Tourister hardside luggage spinner suitcase",
            brand="American Tourister",
            price=310,
            sold=30,
        ),
        _selection_noon_record(
            "CART1",
            "folding luggage cart hand truck with extendable handle",
            price=55,
            sold=120,
        ),
        _selection_noon_record(
            "DUFFEL1",
            "travel duffel bag with shoulder strap",
            price=89,
            sold=70,
        ),
        _selection_noon_record(
            "ORG1",
            "luggage organizer packing cube travel set",
            price=49,
            sold=60,
        ),
        _selection_noon_record(
            "BAN1",
            "LV hardside luggage suitcase spinner",
            brand="LV",
            price=699,
            sold=10,
        ),
    ]

    result = _selection_fixture_result(records)
    by_sku = {row["sku_id"]: row for row in result["candidates"]}
    dropped = {row["id"].split(":", 1)[1]: row["reason"] for row in result["dropped"]}

    assert "POSITIVE1" in by_sku
    assert "CART1" in dropped and "luggage cart" in dropped["CART1"].lower()
    assert "DUFFEL1" in dropped and "duffel" in dropped["DUFFEL1"].lower()
    assert "ORG1" in dropped and "organizer" in dropped["ORG1"].lower()
    assert "BAN1" in dropped and "hard_ban" in dropped["BAN1"]

    assert by_sku["PACK4"]["price"]["pack_size"] == 4
    assert by_sku["PACK4"]["price"]["unit_price_sar"] == 187

    assert by_sku["RISING1"]["sales"]["type"] == "unknown"
    assert by_sku["RISING1"]["sales"]["tier_in_query"] == "low"
    assert by_sku["RISING1"]["sales"]["is_rising"] is True
    assert by_sku["RISING1"]["overall_v3"]["track"] == "rising"

    assert by_sku["BRAND1"]["relevance"]["passed"] is True
    assert by_sku["BRAND1"]["brand_marker"]["brand"] == "American Tourister"
    assert by_sku["BRAND1"]["overall_v3"].get("track") != "dropped"

    for node in ("N3", "N4", "N5", "N5.5", "N6", "N7", "N10", "N11_v3"):
        assert node in result["node_trace"], result["node_trace"]


def test_selection_phase1_evidence_insufficient_is_explicit_offline():
    from selection.l3_orchestration.production_pipeline import EVIDENCE_INSUFFICIENT

    records = [
        _selection_noon_record(
            "NOEVID1",
            "20 inch hardside luggage suitcase spinner",
            price=189,
            sold=18,
            image=False,
            reviews=20,
        )
    ]
    result = _selection_fixture_result(
        records,
        detail_provider=None,
        feature_extractor=None,
        supply_provider=None,
        ali_records=[],
    )

    assert result["status"] == EVIDENCE_INSUFFICIENT
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["evidence_state"] == EVIDENCE_INSUFFICIENT
    assert "detail" in candidate["missing_evidence"]
    assert "image" in candidate["missing_evidence"]
    assert "n6_features" in candidate["missing_evidence"]
    assert "supply_1688" in candidate["missing_evidence"]
    assert candidate["profit"]["verdict"] == EVIDENCE_INSUFFICIENT
    assert candidate["sales"]["tier_in_query"] is not None


def test_selection_phase1_1688_text_fallback_keeps_supply_candidate():
    from selection.l3_orchestration.nodes.n7_1688_supply import (
        ImageSearchFailed,
        build_1688_supply_provider,
    )

    records = [
        _selection_noon_record(
            "TEXTFB1",
            "20 inch ABS hardside luggage suitcase spinner TSA lock",
            price=199,
            sold=35,
            search_query="carry on luggage",
        )
    ]

    def image_search(_query):
        raise ImageSearchFailed("image search top results below threshold")

    def text_search(query_text, _query):
        assert "carry on luggage" in query_text
        assert "abs" in query_text.lower()
        return [
            {
                "offer_id": "1688-text-1",
                "title": "20 inch ABS hardside luggage suitcase spinner TSA lock",
                "price": 80,
                "image_url": "https://cbu01.alicdn.com/text-1.jpg",
                "score": 0.82,
            }
        ]

    result = _selection_fixture_result(
        records,
        supply_provider=build_1688_supply_provider(
            image_search_provider=image_search,
            text_search_provider=text_search,
        ),
        ali_records=[],
    )

    candidate = result["candidates"][0]
    assert "supply_1688" not in candidate["missing_evidence"]
    assert candidate["supply"]["status"] == "sufficient"
    assert candidate["supply"]["match_source"] == "text_fallback"
    assert candidate["supply"]["offers"][0]["match_source"] == "text_fallback"
    assert candidate["supply"]["offers"][0]["confidence"] in ("high", "medium")


def test_selection_phase1_1688_login_unavailable_is_evidence_insufficient():
    from selection.l3_orchestration.production_pipeline import EVIDENCE_INSUFFICIENT
    from selection.l3_orchestration.nodes.n7_1688_supply import (
        LoginRequired,
        build_1688_supply_provider,
    )

    records = [
        _selection_noon_record(
            "LOGIN1",
            "20 inch ABS hardside luggage suitcase spinner TSA lock",
            price=199,
            sold=35,
        )
    ]

    def image_search(_query):
        raise LoginRequired("1688 login required")

    provider = build_1688_supply_provider(
        image_search_provider=image_search,
        text_search_provider=lambda _query_text, _query: [],
    )
    result = _selection_fixture_result(records, supply_provider=provider, ali_records=[])

    candidate = result["candidates"][0]
    assert candidate["evidence_state"] == EVIDENCE_INSUFFICIENT
    assert "supply_1688" in candidate["missing_evidence"]
    assert candidate["supply"]["status"] == EVIDENCE_INSUFFICIENT
    assert candidate["supply"]["reason_code"] == "login_required"
    assert "Chrome profile" in candidate["supply"]["action_required"]


def test_selection_phase1_1688_low_similarity_not_high_confidence_same_match():
    from selection.l3_orchestration.nodes.n7_1688_supply import (
        ImageSearchFailed,
        build_1688_supply_provider,
    )

    records = [
        _selection_noon_record(
            "LOWMATCH1",
            "20 inch ABS hardside luggage suitcase spinner TSA lock",
            price=199,
            sold=35,
        )
    ]

    def image_search(_query):
        raise ImageSearchFailed("image search empty")

    def text_search(_query_text, _query):
        return [
            {
                "offer_id": "1688-low-1",
                "title": "soft travel duffel gym bag with shoulder strap",
                "price": 35,
                "image_url": "https://cbu01.alicdn.com/low-1.jpg",
                "score": 0.28,
            }
        ]

    result = _selection_fixture_result(
        records,
        supply_provider=build_1688_supply_provider(
            image_search_provider=image_search,
            text_search_provider=text_search,
        ),
        ali_records=[],
    )

    supply = result["candidates"][0]["supply"]
    assert supply["status"] == "low_match"
    assert supply["match_source"] == "text_fallback"
    assert supply["confidence"] == "low"
    assert supply["is_same_design_high_confidence"] is False
    assert supply["offers"][0]["verdict"] != "inquiry"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✓ {t.__name__}")
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"  ✗ {t.__name__}: {e}")
    print(f"\nPASS: {passed} / {len(tests)}  ({100*passed/len(tests):.0f}%)")
    if failed:
        print("\n失败:")
        for n, e in failed: print(f"  · {n}: {e}")
    sys.exit(0 if not failed else 1)

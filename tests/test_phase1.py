"""
Phase 1 端到端测试 - 跑过 80%+ 即视为达标
"""
import os, sys, json, subprocess, time
from pathlib import Path
import importlib.util
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BASE = os.environ.get("HIPOP_URL", "http://127.0.0.1:8765")


_client = httpx.Client(trust_env=False, timeout=30)


def _load_t27_replenishment_smoke():
    path = REPO_ROOT / "tests" / "smoke_t27_replenishment_evidence.py"
    spec = importlib.util.spec_from_file_location("smoke_t27_replenishment_evidence", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_chat_dynamic_expectations_smoke():
    path = REPO_ROOT / "tests" / "smoke_chat_dynamic_expectations_auth.py"
    spec = importlib.util.spec_from_file_location("smoke_chat_dynamic_expectations_auth", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_ws149_sku_query_paths_smoke():
    path = REPO_ROOT / "tests" / "smoke_ws149_sku_query_paths.py"
    spec = importlib.util.spec_from_file_location("smoke_ws149_sku_query_paths", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_ws149_query_sku_live_boundary_fail_closed():
    """WS-149: realtime SKU logistics must not silently fallback to stale wf3 cache."""
    smoke = _load_ws149_sku_query_paths_smoke()
    smoke.test_query_sku_live_login_failure_fails_closed_without_wf3_cache()
    smoke.test_query_sku_live_success_labels_live_source_and_time()


def test_chat_stale_tst001_dynamic_expectations_auth_boundary():
    """WS-143: chat dynamic prep reuses auth and keeps stale/missing fail-closed."""
    smoke = _load_chat_dynamic_expectations_smoke()
    smoke.test_dynamic_prep_uses_authenticated_opener()
    smoke.test_stale_tst001_found_fail_closed_is_not_classified_as_missing()
    smoke.test_stale_tst001_missing_still_requires_not_found_reply()


def test_chat_t27_replenishment_live_evidence_contract():
    """T27 phase-1 contract: cache-zero must answer from live evidence."""
    _load_t27_replenishment_smoke().test_t27_cache_zero_uses_live_authoritative_evidence()


def test_chat_t27_replenishment_live_failure_blocks_cached_zero():
    """T27 phase-1 contract: live failure must block cached zero conclusions."""
    _load_t27_replenishment_smoke().test_t27_live_unavailable_blocks_cached_zero_answer()


def test_ws131_freshness_gate_contract():
    """WS-131: live-first, <=3 day cache consent, and fail-closed freshness rules."""
    from hipop.runtime.verifiers import verify_freshness_gate_matrix

    result = verify_freshness_gate_matrix(now="2026-06-09T12:00:00")
    assert result["ok"], result


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


def test_chat_list_products_sales_topn():
    """WS-148: 近30天销量 TopN 必须走 list_products，且回复带来源/时间/口径。"""
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "KSA 近30天销量最高的3个商品"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营"},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"]
    assert d.get("tools_used") == ["list_products"], \
        f"TopN 销量问题必须确定性调用 list_products，实际 tools_used={d.get('tools_used')}"
    assert d.get("judge_method") == "deterministic_product_sales_topn_router", \
        f"TopN 销量问题不应走 LLM 自由排序，judge_method={d.get('judge_method')}"
    reply = d["reply"]
    assert "来源" in reply and "wf2_sku.sales_30d" in reply, \
        f"TopN 回复必须含来源/口径证据: {reply[:300]}"
    assert "近30天销量" in reply or "近 30 天销量" in reply, \
        f"TopN 回复必须明确 30d 销量口径: {reply[:300]}"


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


def test_chat_t21_workflow_receipt():
    """T21-SUB-2 验收：触发物流刷新回复必须三态化，直接回答「是否已创建」并含 task_id/workflow/状态。"""
    s, b = _post("/api/chat", {
        "messages": [{"role": "user", "content": "请帮我扫一下 ERP 物流信息，并告诉我是否真的创建了后台任务。"}],
        "scope": {"store": "KSA", "current_user": "tester", "current_role": "运营", "tenant_id": 1},
    }, timeout=90)
    d = json.loads(b)
    assert s == 200 and d["reply"], f"chat 请求失败或无回复: status={s} body={b[:200]}"

    reply = d["reply"]
    # ① 直接回答「是否创建」
    created_phrases = ("已创建", "已受理", "后台任务已", "任务已创建", "未确认")
    assert any(p in reply for p in created_phrases), (
        f"回复未直接回答「任务是否创建」（须含其一: {created_phrases}）\n回复: {reply[:300]}"
    )
    # ② 含 task_id（6-8 位十六进制）
    import re
    assert re.search(r"[0-9a-f]{6,8}", reply), (
        f"回复未包含 task_id（6-8位十六进制）\n回复: {reply[:300]}"
    )
    # ③ 含 workflow 名称
    assert "wf3_logistics_v2" in reply, (
        f"回复未包含 workflow 名称 wf3_logistics_v2\n回复: {reply[:300]}"
    )
    # ④ 含三态状态词
    state_words = ("已排队", "待执行", "已开始", "已完成", "执行失败", "已受理", "未确认")
    assert any(w in reply for w in state_words), (
        f"回复不含三态状态词（须含其一: {state_words}）\n回复: {reply[:300]}"
    )
    # ⑤ 无完成事件时不暗示已完成
    assert "已跑完" not in reply and "跑完了" not in reply, (
        f"回复不应暗示已完成（「已跑完」/「跑完了」）\n回复: {reply[:300]}"
    )


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
            "pieces": rec.policy_flags.get("pack_size") or 1,
            "color_main": "green",
            "features": features,
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
                              ali_records=None,
                              inventory_provider=None):
    from selection.l3_orchestration.production_pipeline import run_ksa_luggage_noon

    kwargs = {}
    if inventory_provider is not None:
        kwargs["inventory_provider"] = inventory_provider

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
        **kwargs,
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


def test_selection_inventory_reverse_and_differentiation_feed_n11():
    records = [
        _selection_noon_record(
            "REG20",
            "20 inch ABS hardside luggage suitcase spinner",
            price=199,
            sold=90,
            rating=4.6,
            reviews=100,
        ),
        _selection_noon_record(
            "EXP24",
            "24 inch expandable ABS luggage suitcase spinner wheels with cup holder",
            price=259,
            sold=90,
            rating=4.6,
            reviews=100,
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

    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: inventory_rows,
    )
    by_sku = {row["sku_id"]: row for row in result["candidates"]}

    for node in ("N8", "N9", "N11_v3"):
        assert node in result["node_trace"], result["node_trace"]

    exp24 = by_sku["EXP24"]
    reg20 = by_sku["REG20"]

    exp_signal_ids = {s["id"] for s in exp24["differentiation"]["signals"]}
    assert {"expandable_layer", "cup_holder", "spinner_wheels"} <= exp_signal_ids
    assert exp24["inventory"]["state"] == "sufficient"
    assert exp24["inventory"]["score_adjustment"] > 0
    assert any("20寸" in reason for reason in exp24["inventory"]["reasons"])

    assert reg20["inventory"]["score_adjustment"] < 0
    assert reg20["inventory"]["warnings"]

    exp_breakdown = exp24["overall_v3"]["breakdown"]
    assert exp_breakdown["differentiation_pct"] > reg20["overall_v3"]["breakdown"]["differentiation_pct"]
    assert exp_breakdown["inventory_pct"] > reg20["overall_v3"]["breakdown"]["inventory_pct"]
    assert exp24["overall_v3"]["score"] > reg20["overall_v3"]["score"]


def test_selection_no_inventory_data_is_explicitly_insufficient():
    records = [
        _selection_noon_record(
            "NOSTOCK1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: [],
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient"
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert candidate["inventory"]["reasons"] == []
    assert "inventory" in candidate["missing_evidence"]


def test_selection_profit_matches_ksa_pricing_table_formula_sample():
    """WS-67: 定价表.xlsx 沙特 sheet 第 2 行公式口径不能回退成简化 15% 默认估算."""
    from selection.l2_knowledge import loader as kb_loader
    from selection.l3_orchestration.nodes import n10_profit

    pricing_table = kb_loader.load_pricing_table()
    result = n10_profit.calculate_profit(
        289,
        110,
        country="ksa",
        platform="noon",
        category="luggage",
        shipping_rmb=53,
        fulfillment_fee_sar=15,
        warehouse_sar=2,
        pricing_table=pricing_table,
    )

    assert result.commission_rate == 0.20
    assert result.commission_source == "category"
    assert abs(result.seller_receivable_sar - 198.883) < 0.01
    assert abs(result.seller_settlement_vat_sar - 29.83245) < 0.01
    assert abs(result.net_profit_sar - 76.760994) < 0.01
    assert abs(result.profit_rate - 0.266) < 0.001


def test_selection_profit_path_uses_luggage_commission_and_surfaces_low_margin():
    """WS-67: 候选池消费端读取 N10 真实利润；<20% 只能黄牌/风险，不能假装通过."""
    records = [
        _selection_noon_record(
            "LOWMARGIN1",
            "20 inch ABS hardside luggage suitcase spinner",
            price=199,
            sold=80,
        )
    ]

    result = _selection_fixture_result(
        records,
        ali_records=[
            _selection_ali_record(
                "1688-low-margin",
                "20 inch ABS hardside luggage suitcase spinner",
                unit_rmb=160,
            )
        ],
    )

    candidate = result["candidates"][0]
    profit = candidate["profit"]
    assert profit["commission"] == 0.20
    assert profit["commission_source"] == "category"
    assert profit["profit_rate"] < 0.20
    assert profit["low_margin"] is True
    assert profit["verdict"] == "PROFIT_LOW_BUT_VALUABLE"
    assert result["summaries"]["N10"]["n_yellow"] == 1


def test_selection_inventory_malformed_rows_returns_evidence_insufficient():
    """N9: inventory rows present but no parseable size/stock/sales → evidence_insufficient, not sufficient."""
    records = [
        _selection_noon_record(
            "MALF1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    malformed_inventory_rows = [
        {
            "partner_sku": "NO_SIGNAL_SKU",
            "title": "some generic product",
            "family": "bags_luggage",
            "product_category_detail": "luggage",
            "total_stock": None,
            "sales_30d": None,
        }
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: malformed_inventory_rows,
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient", (
        f"Expected evidence_insufficient but got {candidate['inventory']['state']!r}"
    )
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert "inventory" in candidate["missing_evidence"], (
        f"missing_evidence should contain 'inventory', got: {candidate['missing_evidence']}"
    )


def test_selection_inventory_type_a_stock_no_size_returns_evidence_insufficient():
    """N9 Type-A: rows have stock/sales but no parseable size → evidence_insufficient, not sufficient."""
    records = [
        _selection_noon_record(
            "TYPEA1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    type_a_rows = [
        {
            "partner_sku": "TYPEA_SKU",
            "title": "some generic product",
            "family": "bags_luggage",
            "product_category_detail": "luggage",
            "total_stock": 10,
            "sales_30d": 5,
        }
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: type_a_rows,
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient", (
        f"Type-A (stock but no size): expected evidence_insufficient but got {candidate['inventory']['state']!r}"
    )
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert "inventory" in candidate["missing_evidence"], (
        f"missing_evidence should contain 'inventory', got: {candidate['missing_evidence']}"
    )


def test_selection_inventory_type_b_size_no_stock_returns_evidence_insufficient():
    """N9 Type-B: rows have parseable size but no stock/sales → evidence_insufficient, not sufficient."""
    records = [
        _selection_noon_record(
            "TYPEB1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    type_b_rows = [
        {
            "partner_sku": "TYPEB_SKU",
            "title": "20 inch hardside luggage",
            "family": "bags_luggage",
            "product_category_detail": "luggage",
            "total_stock": None,
            "sales_30d": None,
        }
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: type_b_rows,
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient", (
        f"Type-B (size but no stock): expected evidence_insufficient but got {candidate['inventory']['state']!r}"
    )
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert "inventory" in candidate["missing_evidence"], (
        f"missing_evidence should contain 'inventory', got: {candidate['missing_evidence']}"
    )


def test_selection_inventory_type_a_no_size_returns_evidence_insufficient():
    """N9 WS-73 A型: rows have stock/sales but no parseable size → evidence_insufficient, not sufficient."""
    records = [
        _selection_noon_record(
            "TYPEA1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    type_a_rows = [
        {
            "partner_sku": "TYPEA_SKU",
            "title": "generic luggage product",
            "family": "bags_luggage",
            "product_category_detail": "luggage",
            "total_stock": 10,
            "sales_30d": 5,
        }
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: type_a_rows,
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient", (
        f"A型 Expected evidence_insufficient but got {candidate['inventory']['state']!r}"
    )
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert "inventory" in candidate["missing_evidence"], (
        f"missing_evidence should contain 'inventory', got: {candidate['missing_evidence']}"
    )


def test_selection_inventory_type_b_no_stock_sales_returns_evidence_insufficient():
    """N9 WS-73 B型: rows have parseable size but no stock/sales → evidence_insufficient, not sufficient."""
    records = [
        _selection_noon_record(
            "TYPEB1",
            "24 inch expandable ABS luggage suitcase spinner",
            price=249,
            sold=30,
        )
    ]
    type_b_rows = [
        {
            "partner_sku": "TYPEB_SKU",
            "title": "20 inch hardside luggage suitcase",
            "family": "bags_luggage",
            "product_category_detail": "luggage",
            "total_stock": None,
            "sales_30d": None,
        }
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: type_b_rows,
    )

    candidate = result["candidates"][0]
    assert candidate["inventory"]["state"] == "evidence_insufficient", (
        f"B型 Expected evidence_insufficient but got {candidate['inventory']['state']!r}"
    )
    assert candidate["inventory"]["score_adjustment"] == 0.0
    assert "inventory" in candidate["missing_evidence"], (
        f"missing_evidence should contain 'inventory', got: {candidate['missing_evidence']}"
    )


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


def test_selection_delivery_agent_os_and_report_share_candidate_pool():
    import tempfile
    from pathlib import Path

    records = [
        _selection_noon_record(
            "DELIV1",
            "24 inch expandable ABS luggage suitcase spinner wheels with cup holder",
            price=259,
            sold=88,
            rating=4.7,
            reviews=130,
        )
    ]
    result = _selection_fixture_result(
        records,
        inventory_provider=lambda _country, _family: [
            {
                "partner_sku": "HIPOP20BACKLOG",
                "title": "20 inch ABS hardside luggage suitcase spinner",
                "family": "bags_luggage",
                "product_category_detail": "20 inch luggage",
                "total_stock": 420,
                "sales_30d": 3,
                "sales_grade": "low",
            }
        ],
    )

    from selection.l4_delivery.candidate_pool import (
        build_candidate_pool,
        build_inquiry_todos,
        load_candidate_pool,
        render_agent_os_payload,
        render_structured_report,
        save_candidate_pool,
    )

    pool = build_candidate_pool(result, source_run_id="fixture-run")
    with tempfile.TemporaryDirectory() as td:
        artifact_path = Path(td) / "candidate_pool.json"
        save_candidate_pool(pool, artifact_path)
        loaded_pool = load_candidate_pool(artifact_path)

    agent_os = render_agent_os_payload(loaded_pool)
    report = render_structured_report(loaded_pool)
    inquiry_todos = build_inquiry_todos(loaded_pool)

    os_candidate = agent_os["candidates"][0]
    report_candidate = report["candidate_pool"][0]
    source_candidate = result["candidates"][0]

    assert os_candidate == report_candidate
    assert os_candidate["sku_id"] == "DELIV1"
    assert os_candidate["tier"] == source_candidate["overall_v3"]["tier_overall"]
    assert os_candidate["platform_evidence_tags"]
    assert os_candidate["relevance"] == source_candidate["relevance"]
    assert os_candidate["price_normalized"]["unit_price_sar"] == source_candidate["price"]["unit_price_sar"]
    assert os_candidate["sales"]["tier_in_query"] == source_candidate["sales"]["tier_in_query"]
    assert os_candidate["momentum"]["is_rising"] == source_candidate["sales"]["is_rising"]
    assert "ABS" in os_candidate["selling_points"]["material"]
    assert os_candidate["supply_1688"]["status"] == "sufficient"
    assert os_candidate["profit"]["verdict"]
    assert os_candidate["differentiation"]["signals"]
    assert os_candidate["inventory"]["state"] == "sufficient"
    assert os_candidate["evidence_insufficient"] is False

    assert inquiry_todos
    assert inquiry_todos[0]["status"] == "todo"
    assert inquiry_todos[0]["external_side_effect"] is False
    assert "send" not in inquiry_todos[0]


def test_selection_delivery_evidence_insufficient_visible_in_agent_os_and_report():
    records = [
        _selection_noon_record(
            "DELIVNOEVID1",
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

    from selection.l3_orchestration.production_pipeline import EVIDENCE_INSUFFICIENT
    from selection.l4_delivery.candidate_pool import (
        build_candidate_pool,
        render_agent_os_payload,
        render_structured_report,
    )

    pool = build_candidate_pool(result, source_run_id="fixture-run")
    os_candidate = render_agent_os_payload(pool)["candidates"][0]
    report_candidate = render_structured_report(pool)["candidate_pool"][0]

    assert os_candidate["evidence_state"] == EVIDENCE_INSUFFICIENT
    assert os_candidate["evidence_insufficient"] is True
    assert "detail" in os_candidate["evidence_insufficient_reasons"]
    assert "supply_1688" in os_candidate["evidence_insufficient_reasons"]
    assert report_candidate["evidence_state"] == EVIDENCE_INSUFFICIENT
    assert report_candidate["evidence_insufficient_reasons"] == os_candidate["evidence_insufficient_reasons"]


def test_selection_delivery_structured_feedback_writes_preferences_and_changes_offline_state():
    import tempfile
    from pathlib import Path

    records = [
        _selection_noon_record(
            "FEEDBACK1",
            "American Tourister 20 inch ABS hardside luggage suitcase spinner",
            brand="American Tourister",
            price=310,
            sold=44,
        )
    ]
    result = _selection_fixture_result(records)

    from selection.l4_delivery.candidate_pool import build_candidate_pool
    from selection.l4_delivery.feedback import (
        REASON_TAGS,
        apply_preferences_to_candidate_pool,
        load_preferences,
        write_candidate_feedback,
    )

    with tempfile.TemporaryDirectory() as td:
        preferences_path = Path(td) / "preferences.jsonl"
        pool = build_candidate_pool(result, source_run_id="fixture-run")

        include_event = write_candidate_feedback(
            product_id="noon_sa:MISSED1",
            action="include",
            reason_tags=["missing_candidate"],
            reason_text="missed a relevant SKU, add it to review",
            preferences_path=preferences_path,
        )
        reject_event = write_candidate_feedback(
            product_id="noon_sa:FEEDBACK1",
            action="reject",
            reason_tags=["brand", "material", "return_risk"],
            reason_text="do not want branded ABS products with return risk",
            attributes={"brand": "American Tourister", "material": "ABS"},
            preferences_path=preferences_path,
        )

        assert {"brand", "material", "color", "price", "return_risk"} <= REASON_TAGS
        assert include_event["action"] == "include"
        assert reject_event["reason_tags"] == ["brand", "material", "return_risk"]

        preferences = load_preferences(preferences_path)
        assert len(preferences) == 2
        rescored = apply_preferences_to_candidate_pool(pool, preferences)
        candidate = rescored["candidates"][0]

        assert candidate["feedback_status"] == "rejected_by_preference"
        assert {"brand", "material"} <= set(candidate["feedback_reason_tags"])
        assert candidate["preference_effects"][0]["source_product_id"] == "noon_sa:FEEDBACK1"


def test_selection_feedback_api_requires_login_and_scopes_preferences_by_tenant_store():
    import os
    import tempfile

    os.environ["AUTH_LOCKDOWN"] = "0"
    os.environ["DISABLE_DAILY_REFRESH"] = "1"

    records = [
        _selection_noon_record(
            "TENANTFB1",
            "American Tourister 20 inch ABS hardside luggage suitcase spinner",
            brand="American Tourister",
            price=310,
            sold=44,
        )
    ]
    result = _selection_fixture_result(records)

    from fastapi.testclient import TestClient
    from hipop.server.main import app
    from server import auth as _auth_mod
    from selection.l4_delivery.candidate_pool import build_candidate_pool
    from selection.l4_delivery.feedback import (
        apply_preferences_to_candidate_pool,
        load_scoped_preferences,
    )

    with tempfile.TemporaryDirectory() as td:
        os.environ["SELECTION_PREFERENCES_ROOT"] = td
        client = TestClient(app)
        body = {
            "product_id": "noon_sa:TENANTFB1",
            "action": "reject",
            "reason_tags": ["brand", "material"],
            "reason_text": "tenant A rejects this branded ABS product",
            "attributes": {"brand": "American Tourister", "material": "ABS"},
        }

        unauth = client.post("/api/selection/ksa/feedback", json=body)
        assert unauth.status_code == 401

        app.dependency_overrides[_auth_mod.get_current_user] = lambda: {
            "id": 1001,
            "tenant_id": 101,
            "email": "tenant-a@example.com",
            "role": "owner",
            "active": True,
            "is_default": False,
        }
        try:
            auth = client.post("/api/selection/ksa/feedback", json=body)
        finally:
            app.dependency_overrides.clear()
        assert auth.status_code == 200, auth.text

        tenant_a_preferences = load_scoped_preferences(101, "ksa", preferences_root=td)
        tenant_b_preferences = load_scoped_preferences(202, "ksa", preferences_root=td)
        assert len(tenant_a_preferences) == 1
        assert tenant_a_preferences[0]["tenant_id"] == 101
        assert tenant_a_preferences[0]["store"] == "ksa"
        assert tenant_b_preferences == []

        pool = build_candidate_pool(result, source_run_id="fixture-run")
        tenant_a_candidate = apply_preferences_to_candidate_pool(pool, tenant_a_preferences)["candidates"][0]
        tenant_b_candidate = apply_preferences_to_candidate_pool(pool, tenant_b_preferences)["candidates"][0]
        assert tenant_a_candidate["feedback_status"] == "rejected_by_preference"
        assert tenant_b_candidate["feedback_status"] == "unreviewed"



# ── T26 货单负控单元测试（WS-106）──────────────────────────────────────────────
def test_t26_safety_blocks_pretend_querying_without_tool():
    """Rule A: Agent 说'我来查货单实时状态'但没调 query_order_live → _safety 拦截。"""
    from hipop.server._safety import sanitize_reply
    fake_reply = "我来查这个货单号的实时状态，请稍等。"
    out, warns = sanitize_reply(fake_reply, tools_used=[], tool_log=[])
    assert warns, "应有警告"
    assert any("T26" in w for w in warns), f"警告应含 T26: {warns}"
    assert "被 _safety 拦掉" in out, f"回复应含拦截标记: {out[:200]}"


def test_t26_safety_injects_not_found_when_tool_returned_missing():
    """Rule B: query_order_live 返回 order_not_found_in_erp 但回复没说未找到 → _safety 补充负控。"""
    import re as _re
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "DGORDER-NOT-EXIST-0001"},
        "result_error": "order_not_found_in_erp",
    }]
    vague_reply = "抱歉，目前无法为您提供该货单的物流信息。"
    out, warns = sanitize_reply(vague_reply, tools_used=["query_order_live"], tool_log=tool_log)
    assert warns and any("T26" in w for w in warns), f"应有 T26 警告: {warns}"
    assert _re.search(r"ERP.*无记录|核实货单号|未找到|不存在", out), f"回复应含未找到提示: {out[:300]}"


def test_t26_safety_passes_when_reply_already_says_not_found():
    """Rule B: 如果回复已经明确说了未找到，_safety 不应重复插入。"""
    from hipop.server._safety import sanitize_reply
    tool_log = [{
        "name": "query_order_live",
        "args": {"order_no": "DGORDER-NOT-EXIST-0001"},
        "result_error": "order_not_found_in_erp",
    }]
    good_reply = "货单 DGORDER-NOT-EXIST-0001 在 ERP 中未找到，请核实货单号是否正确。"
    out, warns = sanitize_reply(good_reply, tools_used=["query_order_live"], tool_log=tool_log)
    t26_warns = [w for w in warns if "T26" in w]
    assert not t26_warns, f"回复已说明未找到，不应触发 T26 告警: {t26_warns}"


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

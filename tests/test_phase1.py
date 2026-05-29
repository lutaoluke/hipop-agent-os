"""
Phase 1 端到端测试 - 跑过 80%+ 即视为达标
"""
import os, sys, json, subprocess, time
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
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

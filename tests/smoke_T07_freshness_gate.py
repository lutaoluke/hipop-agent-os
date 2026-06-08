"""T07 freshness gate smoke — 纯单元测试（不需要 uvicorn，跑在 make test 里）。

验收：
1. check_freshness_coverage() 对每个 domain 返回合法结构
2. 未来日期 target_date → covered=False（确保 gate 不放行假新鲜）
3. _detect_operational_domain() pattern 精确匹配/不误报
4. 未知 domain → fail-open（covered=True，不拦 LLM）
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hipop.server import data as _data


def test_coverage_structure():
    """check_freshness_coverage 对每个 domain 返回合法 schema。"""
    valid_actions = {"use_cache", "run_workflow", "upload_csv"}
    for domain in ("sales", "stock", "logistics"):
        r = _data.check_freshness_coverage("KSA", domain)
        assert isinstance(r.get("covered"), bool), f"covered 非 bool: domain={domain}"
        assert r.get("domain") == domain, f"domain 字段错: {r}"
        assert isinstance(r.get("latest_date"), str), f"latest_date 非 str"
        assert isinstance(r.get("target_date"), str), f"target_date 非 str"
        assert r.get("action") in valid_actions, f"action 非法: {r.get('action')}"
        # run_workflow → workflow 必须有值
        if r["action"] == "run_workflow":
            assert r.get("workflow"), f"run_workflow 但 workflow 为空: domain={domain}"
        # upload_csv → csv_hint 必须有值（sales 超出 ERP 新鲜时会走 upload_csv）
        if r["action"] == "upload_csv":
            assert r.get("csv_hint"), f"upload_csv 但 csv_hint 为空: domain={domain}"
        print(f"  [{domain}] covered={r['covered']} action={r['action']} latest={r['latest_date']}")


def test_future_date_always_stale():
    """target_date = 未来日期 → covered=False（禁止假新鲜放行）。"""
    for domain in ("sales", "stock", "logistics"):
        r = _data.check_freshness_coverage("KSA", domain, "2099-12-31")
        assert r["covered"] is False, f"domain={domain}: 未来日期不应 covered"
        assert r["action"] in ("run_workflow", "upload_csv"), \
            f"domain={domain}: 未来日期 action 应为 run_workflow 或 upload_csv，实为 {r['action']}"
    print("  future date → covered=False ✓")


def test_unknown_domain_fail_open():
    """未知 domain → fail-open（covered=True），不拦 LLM。"""
    r = _data.check_freshness_coverage("KSA", "unknown_domain_xyz")
    assert r["covered"] is True, "未知 domain 应 fail-open（covered=True）"
    assert r["action"] == "use_cache"
    print("  unknown domain → fail-open ✓")


def test_detect_domain_patterns():
    """_detect_operational_domain pattern 精确匹配（正例 + 负例）。
    直接在此复现 agent.py 中的正则，不 import agent（避免 anthropic 依赖）。
    """
    import re as _re
    _SALES_RE = _re.compile(
        r"(?:今天|今日|最新|本周|这周|最近[0-9一两三四五六七八九十]+天?).*?(?:卖|销量|销售|热销|top\s*\d|前\s*\d|排名)"
        r"|(?:卖得最好|卖得最多|热销|热门|销量最高|销量最多|最畅销|最好卖)"
        r"|(?:前[0-9]+|top\s*[0-9]+).*?(?:销量|卖|热销)"
        r"|哪[些个].*?(?:卖得最好|卖得最多|销量最高|最畅销|最好卖)",
        _re.IGNORECASE | _re.DOTALL,
    )
    _SKIP_RE = _re.compile(
        r"(?:不用|不要|无需|先别).{0,8}(?:刷新|更新|同步)|就用现在的|先告诉我|不用等",
    )

    def _detect(q):
        if not q: return None
        if _SKIP_RE.search(q): return None
        if _SALES_RE.search(q): return "sales"
        return None

    # ── 正例（应匹配 sales） ──
    positives = [
        "今天销量最好的前5个 SKU",
        "今天卖得最好的是哪些",
        "最近7天销量最高的商品",
        "哪些 SKU 卖得最好",
        "哪些商品最畅销",
        "top5 销量",
        "前3 热销",
        "本周销量排名",
        "最畅销的 SKU 是什么",
        "销量最多的商品",
    ]
    for q in positives:
        r = _detect(q)
        assert r == "sales", f"正例未匹配 sales: {q!r} → {r}"

    # ── 负例（不应触发 gate） ──
    negatives = [
        "帮我刷新库存",
        "物流状态怎么样",
        "店铺整体概览",
        "把 PDZ0027158 标已确认丢货",
        "数据什么时候更新的",
        "不用刷新，就用现在的告诉我销量排名",   # 明确说不用刷新
        "就用现在的告诉我销量最好的是哪些",     # 用现在的 → skip
    ]
    for q in negatives:
        r = _detect(q)
        assert r is None, f"负例误触发: {q!r} → {r}"

    print("  domain pattern matching ✓")


def test_false_freshness_imported_at_new_business_date_old():
    """回归：imported_at=今天但 as_of_date=旧日期时 sales gate 必须返回 covered=False。

    旧代码用 MAX(imported_at) → covered=True（假新鲜，bug，验门人红队命中）。
    新代码用 MAX(as_of_date) → covered=False（正确：业务日未覆盖）。
    """
    import tempfile, sqlite3, os as _os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    try:
        with sqlite3.connect(tmp_db) as c:
            c.execute("""CREATE TABLE sales_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id BIGINT NOT NULL,
                alias TEXT NOT NULL,
                country TEXT NOT NULL,
                platform TEXT NOT NULL,
                store_name TEXT NOT NULL,
                active INT NOT NULL DEFAULT 1
            )""")
            c.execute("""CREATE TABLE wf2_sku (
                tenant_id BIGINT NOT NULL,
                entity_alias TEXT NOT NULL,
                partner_sku TEXT NOT NULL,
                as_of_date TEXT,
                imported_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
            c.execute(
                "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name) "
                "VALUES (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-KSA')"
            )
            # imported_at = today (2026-06-08) but business date as_of_date = a week ago
            c.execute(
                "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, as_of_date, imported_at) "
                "VALUES (1, 'hipop_ksa', 'TEST-SKU-001', '2026-06-01', '2026-06-08')"
            )
            c.commit()

        orig_db_path = _data.DB_PATH
        orig_db_url = _os.environ.pop("DB_URL", None)
        try:
            _data.DB_PATH = tmp_db
            r = _data.check_freshness_coverage("KSA", "sales", "2026-06-08")
        finally:
            _data.DB_PATH = orig_db_path
            if orig_db_url is not None:
                _os.environ["DB_URL"] = orig_db_url
    finally:
        _os.unlink(tmp_db)

    assert r["covered"] is False, (
        f"假新鲜漏洞：imported_at=2026-06-08 但 as_of_date=2026-06-01，"
        f"应 covered=False，实为 {r}"
    )
    assert r["latest_date"] == "2026-06-01", (
        f"latest_date 应为业务日 as_of_date='2026-06-01'，实为 {r['latest_date']!r}"
    )
    assert r["action"] == "run_workflow", (
        f"业务日未覆盖应触发 run_workflow，实为 {r['action']!r}"
    )
    print("  imported_at 新但 as_of_date 旧 → covered=False ✓（假新鲜防护）")


def test_uae_store():
    """UAE store 也应正常返回结构（不崩）。"""
    r = _data.check_freshness_coverage("UAE", "sales")
    assert "covered" in r
    print(f"  UAE store: covered={r['covered']} action={r['action']} ✓")


if __name__ == "__main__":
    print("=== T07 freshness gate smoke ===")
    test_coverage_structure()
    test_future_date_always_stale()
    test_unknown_domain_fail_open()
    test_detect_domain_patterns()
    test_false_freshness_imported_at_new_business_date_old()
    test_uae_store()
    print("\n✓ All T07 freshness gate smoke passed")

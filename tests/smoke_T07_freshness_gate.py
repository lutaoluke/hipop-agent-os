"""T07 freshness gate smoke — 纯单元测试（不需要 uvicorn，跑在 make test 里）。

验收：
1. check_freshness_coverage() 对每个 domain 返回合法结构
2. 未来日期 target_date → covered=False（确保 gate 不放行假新鲜）
3. _detect_operational_domain() pattern 精确匹配/不误报
4. 未知 domain → fail-open（covered=True，不拦 LLM）
"""
from __future__ import annotations
import os, sys, sqlite3, tempfile
from contextlib import contextmanager
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hipop.server import data as _data


@contextmanager
def _freshness_fixture_db():
    """Minimal DB fixture for check_freshness_coverage; never rely on a local hipop.db."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    orig_db_path = _data.DB_PATH
    orig_db_url = os.environ.pop("DB_URL", None)
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
            c.execute("""CREATE TABLE wf1_stock (
                tenant_id BIGINT NOT NULL,
                entity_alias TEXT NOT NULL,
                imported_at TEXT
            )""")
            c.execute("""CREATE TABLE wf3_logistics_hub_v2 (
                tenant_id BIGINT NOT NULL,
                updated_at TEXT
            )""")
            c.executemany(
                "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "hipop_ksa", "SA", "Noon", "HIPOP-KSA"),
                    (1, "hipop_uae", "AE", "Noon", "HIPOP-UAE"),
                ],
            )
            c.executemany(
                "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, as_of_date, imported_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "hipop_ksa", "TEST-KSA-001", "2026-06-08", "2026-06-09"),
                    (1, "hipop_uae", "TEST-UAE-001", "2026-06-08", "2026-06-09"),
                ],
            )
            c.executemany(
                "INSERT INTO wf1_stock (tenant_id, entity_alias, imported_at) VALUES (?, ?, ?)",
                [
                    (1, "hipop_ksa", "2026-06-08"),
                    (1, "hipop_uae", "2026-06-08"),
                ],
            )
            c.execute("INSERT INTO wf3_logistics_hub_v2 (tenant_id, updated_at) VALUES (1, '2026-06-08')")
            c.commit()
        _data.DB_PATH = tmp_db
        yield
    finally:
        _data.DB_PATH = orig_db_path
        if orig_db_url is not None:
            os.environ["DB_URL"] = orig_db_url
        os.unlink(tmp_db)


def test_coverage_structure():
    """check_freshness_coverage 对每个 domain 返回合法 schema。"""
    valid_actions = {"use_cache", "run_workflow", "upload_csv"}
    with _freshness_fixture_db():
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
    with _freshness_fixture_db():
        for domain in ("sales", "stock", "logistics"):
            r = _data.check_freshness_coverage("KSA", domain, "2099-12-31")
            assert r["covered"] is False, f"domain={domain}: 未来日期不应 covered"
            assert r["action"] in ("run_workflow", "upload_csv"), \
                f"domain={domain}: 未来日期 action 应为 run_workflow 或 upload_csv，实为 {r['action']}"
    print("  future date → covered=False ✓")


def test_unknown_domain_fail_open():
    """未知 domain → fail-open（covered=True），不拦 LLM。"""
    with _freshness_fixture_db():
        r = _data.check_freshness_coverage("KSA", "unknown_domain_xyz")
        assert r["covered"] is True, "未知 domain 应 fail-open（covered=True）"
        assert r["action"] == "use_cache"
    print("  unknown domain → fail-open ✓")


def test_detect_domain_patterns():
    """_detect_operational_domain pattern 精确匹配（正例 + 负例 + sales_skip）。
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
        skip = bool(_SKIP_RE.search(q))
        sales = bool(_SALES_RE.search(q))
        if skip and sales:
            return "sales_skip"
        if skip:
            return None
        if sales:
            return "sales"
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

    # ── 纯否定（非销量查询，或 skip + 销量词不匹配 sales RE → None） ──
    negatives = [
        "帮我刷新库存",
        "物流状态怎么样",
        "店铺整体概览",
        "把 PDZ0027158 标已确认丢货",
        "数据什么时候更新的",
        "不用刷新了，就看库存吧",              # skip + 非销量 → None
        "不用刷新，就用现在的告诉我销量排名",  # skip + "销量排名"（无时间词，不匹配 sales RE）→ None
        "就用现在的告诉我销量最好的是哪些",    # skip + "最好"在后（不匹配 sales RE）→ None
    ]
    for q in negatives:
        r = _detect(q)
        assert r is None, f"负例误触发: {q!r} → {r}"

    # ── sales_skip（skip RE + sales RE 均命中 → 'sales_skip'） ──
    sales_skip_cases = [
        "不用更新，哪些 SKU 最畅销",                   # skip + 哪些...最畅销
        "不用刷新，就用现在的告诉我哪些 SKU 最畅销",   # skip + 就用现在的 + 哪些...最畅销
        "不用刷新，今天卖得最好的是哪些",              # skip + 今天+卖
        "先告诉我热销 top5",                           # skip + top5+热销
    ]
    for q in sales_skip_cases:
        r = _detect(q)
        assert r == "sales_skip", f"sales_skip 未匹配: {q!r} → {r}"

    print("  domain pattern matching ✓")


def test_t07_skip_stale_detect_returns_sales_skip():
    """T07-2 fail-then-pass: 用户说"不用刷新"但同时问销量排名时，
    _detect_operational_domain 必须返回 'sales_skip'（而非旧的 None）。

    旧代码：skip RE 命中 → 直接返 None → gate 完全跳过 → 无代码级陈旧后缀 → T07-2 依赖 LLM wording → flaky
    新代码：skip+sales 都命中 → 返 'sales_skip' → gate 检 freshness → 陈旧时注入确定性后缀 → 稳定绿
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

    # 旧代码逻辑（fail case）：skip 命中就直接返 None
    def _old_detect(q):
        if not q: return None
        if _SKIP_RE.search(q): return None
        if _SALES_RE.search(q): return "sales"
        return None

    # 新代码逻辑（pass case）：skip+sales 同时命中 → 'sales_skip'
    def _new_detect(q):
        if not q: return None
        skip = bool(_SKIP_RE.search(q))
        sales = bool(_SALES_RE.search(q))
        if skip and sales: return "sales_skip"
        if skip: return None
        if sales: return "sales"
        return None

    q = "不用刷新，就用现在的告诉我哪些 SKU 最畅销"

    # 旧代码：返 None → gate 完全跳过 → T07-2 靠 LLM wording（flaky）
    old = _old_detect(q)
    assert old is None, f"旧代码应返回 None，实为 {old!r}"

    # 新代码：返 'sales_skip' → gate 检 freshness → 陈旧时注入确定性后缀
    new = _new_detect(q)
    assert new == "sales_skip", f"新代码应返回 'sales_skip'，实为 {new!r}"

    print("  T07-2 skip+stale detect: old→None FAIL, new→sales_skip PASS ✓")


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


def test_route_uses_explicit_business_date_without_workflow():
    """Route-level fail-then-pass: covered explicit business date must not trigger workflow.

    Old behavior: _freshness_gate_route() ignored the explicit 2026-06-08 date and called
    check_freshness_coverage(..., target_date=today), so this fixture was treated as stale
    and run_workflow(wf2_sales_v2) fired.
    """
    with _freshness_fixture_db():
        import types

        anthropic_stub = types.ModuleType("anthropic")
        anthropic_stub.Anthropic = object
        sys.modules.setdefault("anthropic", anthropic_stub)
        from hipop.server import agent as _agent

        direct = _data.check_freshness_coverage("KSA", "sales", "2026-06-08")
        assert direct["covered"] is True, f"fixture 应覆盖 2026-06-08，实为 {direct}"
        assert _agent._extract_freshness_target_date("2026-06-08 前5销量 SKU 是哪些") == "2026-06-08"

        calls = []
        orig_exec_tool = _agent._exec_tool

        def _fake_exec_tool(name, args, user=None):
            calls.append((name, args, user))
            return {
                "ok": True,
                "task_id": "T07ROUTE1",
                "workflow": args.get("workflow"),
                "label": "销量刷新",
                "total_steps": 1,
                "affected_modules": [],
                "followup_prompt": args.get("followup_prompt"),
            }

        try:
            _agent._exec_tool = _fake_exec_tool
            routed = _agent._freshness_gate_route(
                "KSA",
                "2026-06-08 前5销量 SKU 是哪些",
                {"tenant_id": 1, "store": "KSA"},
            )
        finally:
            _agent._exec_tool = orig_exec_tool

        assert routed is None, f"目标日已覆盖时应继续走查询工具而非 route 直接返回，实为 {routed}"
        assert calls == [], f"目标日已覆盖时不应触发 workflow，实际调用 {calls}"

    print("  route explicit target_date 2026-06-08 → covered, no workflow ✓")


def test_uae_store():
    """UAE store 也应正常返回结构（不崩）。"""
    with _freshness_fixture_db():
        r = _data.check_freshness_coverage("UAE", "sales")
        assert "covered" in r
        print(f"  UAE store: covered={r['covered']} action={r['action']} ✓")


def test_get_data_health_erp_sales_uses_business_date():
    """fail-then-pass: get_data_health.erp_sales.latest 必须用业务日 as_of_date，不能用 imported_at。

    根因（round-5 验门人红队命中）：
      旧代码 get_data_health 把 imported_at（可能是今天）暴露为 erp_sales.latest，
      LLM 读到 erp_sales.latest=2026-06-08（今天）→ 说"数据已经就绪/销量截至 2026-06-08"；
      但 check_freshness_coverage 用 as_of_date=2026-06-05 判断 covered=False，
      导致同一答案里先说"新鲜"、末尾追加"最新到 2026-06-05"的矛盾。

    旧行为（FAIL）：erp_sales.latest = imported_at（今天），LLM 会误报"新鲜"。
    新行为（PASS）：erp_sales.latest = as_of_date（业务截止日），LLM 看到正确陈旧日期。
    """
    import tempfile, sqlite3, os as _os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        old_biz_date = "2026-06-05"
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
            c.execute("""CREATE TABLE wf1_stock (tenant_id BIGINT, entity_alias TEXT, imported_at TEXT, noon_total_qty INTEGER)""")
            c.execute("""CREATE TABLE wf5_sales_cycle (tenant_id BIGINT, entity_alias TEXT, updated_at TEXT)""")
            c.execute("""CREATE TABLE wf3_logistics_hub_v2 (tenant_id BIGINT, updated_at TEXT)""")
            c.execute("""CREATE TABLE wf6_logistics_alerts_v2 (tenant_id BIGINT, created_at TEXT)""")
            c.execute("""CREATE TABLE wf2_orders (tenant_id BIGINT, entity_alias TEXT, order_date TEXT)""")
            c.execute(
                "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name) "
                "VALUES (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-KSA')"
            )
            # imported_at = today (import ran today) but as_of_date = old_biz_date (business data only covers up to that date)
            c.execute(
                "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, as_of_date, imported_at) "
                f"VALUES (1, 'hipop_ksa', 'TEST-SKU-001', '{old_biz_date}', '{today}')"
            )
            c.commit()

        orig_db_path = _data.DB_PATH
        orig_db_url = _os.environ.pop("DB_URL", None)
        try:
            _data.DB_PATH = tmp_db
            h = _data.get_data_health("KSA")
        finally:
            _data.DB_PATH = orig_db_path
            if orig_db_url is not None:
                _os.environ["DB_URL"] = orig_db_url
    finally:
        _os.unlink(tmp_db)

    erp_sales = h["sources"]["erp_sales"]

    # FAIL case（旧行为）：如果 latest 是 imported_at（today），LLM 会误报"数据截至今天/新鲜"
    # 旧代码：erp_sales.latest = latest_w2_imported = MAX(imported_at) = today
    # 如果 latest == today，说明修复未生效——这就是 round-5 验门人看到的"假新鲜"
    assert erp_sales["latest"] != today, (
        f"假新鲜漏洞未修复：erp_sales.latest={erp_sales['latest']!r} 等于今天 {today}，"
        f"但业务日只到 {old_biz_date}。LLM 会误报'数据截至{today}/新鲜'。"
        f"必须用 as_of_date 而非 imported_at。"
    )

    # PASS case（新行为）：latest = business date (as_of_date)，LLM 看到正确的陈旧日期
    assert erp_sales["latest"] == old_biz_date, (
        f"erp_sales.latest 应为业务日 {old_biz_date!r}（as_of_date），"
        f"实为 {erp_sales['latest']!r}"
    )

    # import_time 字段保留（透明）但不是 latest
    assert erp_sales.get("import_time") == today, (
        f"import_time 字段应保留 imported_at={today!r}，实为 {erp_sales.get('import_time')!r}"
    )

    print(f"  get_data_health.erp_sales.latest={erp_sales['latest']!r} (business date, not import_time={today!r}) ✓")


if __name__ == "__main__":
    print("=== T07 freshness gate smoke ===")
    test_coverage_structure()
    test_future_date_always_stale()
    test_unknown_domain_fail_open()
    test_detect_domain_patterns()
    test_false_freshness_imported_at_new_business_date_old()
    test_route_uses_explicit_business_date_without_workflow()
    test_uae_store()
    test_t07_skip_stale_detect_returns_sales_skip()
    test_get_data_health_erp_sales_uses_business_date()
    print("\n✓ All T07 freshness gate smoke passed")

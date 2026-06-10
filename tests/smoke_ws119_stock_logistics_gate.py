"""WS-119 fail-then-pass smoke — 库存/物流查询接入最新业务日取数门（纯单元，跑在 make test 里）。

依赖 WS-118（PR #69）已落地的 check_freshness_coverage + _freshness_gate_route 框架。
本任务把非销量（库存/物流）批量/榜单查询也接进同一个 freshness gate：
  验收①：今日已有库存/物流数据 → gate 返 None（不重复取数，交既有确定性路由/LLM 用最新业务日算）
  验收②：今日缺库存/物流数据 → gate 真触发对应 workflow（workflow_task != null）或结构化报缺数
  验收③：带明确 SKU/货单编码的单点实时问题 → 不被批量 gate 捕获（交既有 query_sku_live/query_order_live）

fail-then-pass:
  改前 _detect_operational_domain 只识别 sales → 库存/物流批量问题返 None → gate 完全不接线（本文件 FAIL）。
  改后 → 返 'stock'/'logistics' → gate 按业务日覆盖路由（本文件 PASS）。
"""
from __future__ import annotations
import os, sys, sqlite3, tempfile, types
from contextlib import contextmanager
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hipop.server import data as _data


def _load_agent():
    """import agent，stub 掉 anthropic 以免可选依赖缺失时崩。"""
    anthropic_stub = types.ModuleType("anthropic")
    anthropic_stub.Anthropic = object
    sys.modules.setdefault("anthropic", anthropic_stub)
    from hipop.server import agent as _agent
    return _agent


@contextmanager
def _fixture_db(stock_date: str, logistics_date: str, sales_as_of: str = "2026-06-08"):
    """最小 DB：覆盖 check_freshness_coverage 三域所需表，库存/物流业务日可参数化。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    orig_db_path = _data.DB_PATH
    orig_db_url = os.environ.pop("DB_URL", None)
    try:
        with sqlite3.connect(tmp_db) as c:
            c.execute("""CREATE TABLE sales_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id BIGINT NOT NULL, alias TEXT NOT NULL, country TEXT NOT NULL,
                platform TEXT NOT NULL, store_name TEXT NOT NULL, active INT NOT NULL DEFAULT 1)""")
            c.execute("""CREATE TABLE wf2_sku (
                tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL, partner_sku TEXT NOT NULL,
                as_of_date TEXT, imported_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku))""")
            c.execute("""CREATE TABLE wf1_stock (
                tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL, imported_at TEXT)""")
            c.execute("""CREATE TABLE wf3_logistics_hub_v2 (
                tenant_id BIGINT NOT NULL, updated_at TEXT)""")
            c.execute(
                "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name) "
                "VALUES (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-KSA')")
            c.execute(
                "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, as_of_date, imported_at) "
                "VALUES (1, 'hipop_ksa', 'TEST-SKU-001', ?, '2026-06-09')", (sales_as_of,))
            c.execute(
                "INSERT INTO wf1_stock (tenant_id, entity_alias, imported_at) VALUES (1, 'hipop_ksa', ?)",
                (stock_date,))
            c.execute(
                "INSERT INTO wf3_logistics_hub_v2 (tenant_id, updated_at) VALUES (1, ?)",
                (logistics_date,))
            c.commit()
        _data.DB_PATH = tmp_db
        yield
    finally:
        _data.DB_PATH = orig_db_path
        if orig_db_url is not None:
            os.environ["DB_URL"] = orig_db_url
        os.unlink(tmp_db)


def test_detect_stock_logistics_domains():
    """_detect_operational_domain 识别库存/物流批量/榜单查询（改前返 None → FAIL）。"""
    _agent = _load_agent()
    stock_positives = [
        "库存最多的前10个 SKU",
        "哪些 SKU 库存积压最严重",
        "可售库存排行",
        "现在哪些商品缺货",
        "库存最高的是哪些",
    ]
    for q in stock_positives:
        assert _agent._detect_operational_domain(q) == "stock", f"库存正例未匹配 stock: {q!r}"

    logistics_positives = [
        "在途数量最多的前5个 SKU",
        "哪些货单卡单了",
        "卡单最多的批次",
        "在途总量排行",
        "哪些 SKU 还在途",
    ]
    for q in logistics_positives:
        assert _agent._detect_operational_domain(q) == "logistics", f"物流正例未匹配 logistics: {q!r}"

    # 销量仍走 sales（无回归）
    assert _agent._detect_operational_domain("今天销量最好的前5个 SKU") == "sales"

    # 非运营/无榜单意图 → None（不误报）
    for q in ["店铺整体概览", "数据什么时候更新的", "帮我导出表格", "物流状态怎么样"]:
        assert _agent._detect_operational_domain(q) is None, f"负例误触发: {q!r}"
    print("  detect stock/logistics domains ✓")


def test_single_sku_or_order_not_gated():
    """验收③：带明确 SKU/货单编码的单点实时问题不被批量 gate 捕获（→ None，交既有 live 工具）。"""
    _agent = _load_agent()
    single_point = [
        "TBB0116A 当前在途多少",
        "PDZ0027158 现在到哪了",
        "货单 DGORDER-0001 物流状态",
        "TBB0116A 还有多少库存",
    ]
    for q in single_point:
        assert _agent._detect_operational_domain(q) is None, \
            f"单点实时问题被批量 gate 误捕获（应交 live 工具）: {q!r}"
    print("  single SKU/order not gated ✓")


def test_route_stock_stale_triggers_workflow():
    """验收②：今日缺库存 → 触发 wf1_stock_v2（workflow_task != null），文案为库存域。"""
    _agent = _load_agent()
    with _fixture_db(stock_date="2020-01-01", logistics_date="2026-06-09"):
        cov = _data.check_freshness_coverage("KSA", "stock")
        assert cov["covered"] is False and cov["workflow"] == "wf1_stock_v2", cov

        calls = []
        orig = _agent._exec_tool

        def _fake(name, args, user=None):
            calls.append((name, args))
            return {"ok": True, "task_id": "WS119STOCK", "workflow": args.get("workflow"),
                    "label": "库存刷新", "total_steps": 1, "affected_modules": [],
                    "followup_prompt": args.get("followup_prompt"), "references": []}
        try:
            _agent._exec_tool = _fake
            routed = _agent._freshness_gate_route("KSA", "库存最多的前10个 SKU", {"tenant_id": 1, "store": "KSA"})
        finally:
            _agent._exec_tool = orig

        assert routed is not None, "库存陈旧时 gate 应接管（改前返 None → FAIL）"
        assert routed.get("workflow_task"), f"应创建 workflow_task: {routed}"
        assert routed["workflow_task"]["workflow"] == "wf1_stock_v2"
        assert calls and calls[0][1]["workflow"] == "wf1_stock_v2", calls
        assert "库存" in routed["reply"] and "销量" not in routed["reply"], \
            f"文案应为库存域、不串销量: {routed['reply']!r}"
    print("  route stock stale → wf1_stock_v2 ✓")


def test_route_logistics_stale_triggers_workflow():
    """验收②：今日缺物流 → 触发 wf3_logistics_v2，文案为物流域。"""
    _agent = _load_agent()
    with _fixture_db(stock_date="2026-06-09", logistics_date="2020-01-01"):
        calls = []
        orig = _agent._exec_tool

        def _fake(name, args, user=None):
            calls.append((name, args))
            return {"ok": True, "task_id": "WS119LOG", "workflow": args.get("workflow"),
                    "label": "物流刷新", "total_steps": 1, "affected_modules": [],
                    "followup_prompt": args.get("followup_prompt"), "references": []}
        try:
            _agent._exec_tool = _fake
            routed = _agent._freshness_gate_route("KSA", "在途数量最多的前5个 SKU", {"tenant_id": 1, "store": "KSA"})
        finally:
            _agent._exec_tool = orig

        assert routed is not None and routed.get("workflow_task"), f"物流陈旧应接管并建 task: {routed}"
        assert routed["workflow_task"]["workflow"] == "wf3_logistics_v2"
        assert "物流" in routed["reply"] and "销量" not in routed["reply"], \
            f"文案应为物流域、不串销量: {routed['reply']!r}"
    print("  route logistics stale → wf3_logistics_v2 ✓")


def test_route_fresh_returns_none():
    """验收①：今日已有库存/物流 → gate 返 None（不重复取数，交既有路由/LLM 用最新业务日算）。"""
    _agent = _load_agent()
    import datetime as _dt
    today = _dt.date.today().isoformat()
    with _fixture_db(stock_date=today, logistics_date=today):
        calls = []
        orig = _agent._exec_tool
        _agent._exec_tool = lambda *a, **k: calls.append(a) or {"ok": True}
        try:
            assert _agent._freshness_gate_route("KSA", "库存最多的前10个 SKU", {"tenant_id": 1, "store": "KSA"}) is None
            assert _agent._freshness_gate_route("KSA", "在途数量最多的前5个 SKU", {"tenant_id": 1, "store": "KSA"}) is None
        finally:
            _agent._exec_tool = orig
        assert calls == [], f"数据新鲜时不应触发任何 workflow: {calls}"
    print("  route fresh stock/logistics → None, no workflow ✓")


def test_sales_route_unchanged():
    """回归：销量路由文案/行为不变（仍触发 wf2_sales_v2、文案含'销量数据'）。"""
    _agent = _load_agent()
    with _fixture_db(stock_date="2026-06-09", logistics_date="2026-06-09", sales_as_of="2020-01-01"):
        calls = []
        orig = _agent._exec_tool

        def _fake(name, args, user=None):
            calls.append(args.get("workflow"))
            return {"ok": True, "task_id": "WS119SALES", "workflow": args.get("workflow"),
                    "label": "销量刷新", "total_steps": 1, "affected_modules": [],
                    "followup_prompt": args.get("followup_prompt"), "references": []}
        try:
            _agent._exec_tool = _fake
            routed = _agent._freshness_gate_route("KSA", "今天销量最好的前5个 SKU", {"tenant_id": 1, "store": "KSA"})
        finally:
            _agent._exec_tool = orig
        assert routed and routed["workflow_task"]["workflow"] == "wf2_sales_v2", routed
        assert "销量数据" in routed["reply"], routed["reply"]
    print("  sales route unchanged ✓")


if __name__ == "__main__":
    print("=== WS-119 stock/logistics freshness gate smoke ===")
    test_detect_stock_logistics_domains()
    test_single_sku_or_order_not_gated()
    test_route_stock_stale_triggers_workflow()
    test_route_logistics_stale_triggers_workflow()
    test_route_fresh_returns_none()
    test_sales_route_unchanged()
    print("\n✓ All WS-119 stock/logistics gate smoke passed")

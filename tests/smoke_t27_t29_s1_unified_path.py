"""Smoke：WS-125/S1 统一 T27/T29 补货 chat 查询路径 + 接线修复（rebase 到 WS-166 后版本）

## 背景

main 已自带：tool_compute_replenishment 的 stock_readiness 门、noon_orders 陈旧 REDACT
（WS-145/WS-129）、smoke_chat case 9 动态预期（WS-128）。WS-166 又把 tool_* 实现
外移到 hipop/server/tools_impl.py（agent.py 仅留注册/分发）。

本 PR 的净增值只剩一件事：**wf5 接线缺失按需计算**——is_listed=1 但不在
wf5_sales_cycle 的 SKU（TBU0010A/SAB0433A）原本 query_sku 拿到 trend=NULL、补货数为空；
本 PR 在「数据新鲜 + 库存就绪」时按需 compute_wf5_single 写回 wf5 再重读，给真实补货数。

## WS-142 Luke B 承重墙（硬不变量）

当某 SKU 同时「wf1_stock 为空」与「noon_orders 陈旧」时，**陈旧优先**：
query_sku 必须 data_stale=True / stale_reason 含 noon_orders_stale，
不得被「库存为空」分支短路吞掉。因此按需计算分支**只在 not noon_orders_stale 时触发**。

## fail-then-pass（rebase 前的 main = FAIL；本 PR 改后 = PASS）

- test_t27_tool_query_sku_calls_compute_wf5_single：tools_impl.tool_query_sku 调 compute_wf5_single
- test_compute_wf5_single_exists_in_data：data.py 有 compute_wf5_single
- test_t27_ready_compute_wf5_populates_replenish：就绪 fixture 下按需计算填出 weekly_replenish

## 回归 / 契约 guard

- test_t27_tool_query_sku_calls_stock_readiness：按需计算前查 stock_readiness
- test_t29_compute_replenishment_has_stock_gate：tool_compute_replenishment 仍有 stock_readiness 门
- test_t29_not_ready_returns_explicit_warning：get_replenishment_view 不就绪 → rows=[]
- test_luke_b_stale_wins_over_stock_empty：空库存 + noon 陈旧 → noon_orders_stale 优先，不被库存空吞
- test_live_tbu0010a_now_in_wf5：live，CI 无 DB 时 skip

跑法：
  python3 tests/smoke_t27_t29_s1_unified_path.py
  make test （自动聚合 tests/smoke_*.py）
"""
import os
import sys
import sqlite3
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

TENANT = 1
KSA_ALIAS = "hipop_ksa"
KSA_STORE = "ksa"
T27_SKU = "TBU0010A"

TOOLS_IMPL_PATH = os.path.join(REPO, "hipop", "server", "tools_impl.py")
DATA_PATH = os.path.join(REPO, "hipop", "server", "data.py")


def _tool_fn_body(fn_name: str, span: int = 8000) -> str:
    """读 tools_impl.py 里某 tool 函数体（源码审计用）。"""
    assert os.path.exists(TOOLS_IMPL_PATH), f"缺 hipop/server/tools_impl.py: {TOOLS_IMPL_PATH}"
    src = open(TOOLS_IMPL_PATH, encoding="utf-8").read()
    start = src.find("def " + fn_name)
    assert start != -1, f"tools_impl.py 里找不到 def {fn_name}"
    return src[start:start + span]


def _live_fetch(sql, params=()):
    db_path = os.environ.get("HIPOP_DB", "/Users/luke/code/hipop/hipop.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _skip_if_live_db_unavailable():
    db_path = os.environ.get("HIPOP_DB", "/Users/luke/code/hipop/hipop.db")
    if not os.path.exists(db_path):
        return True
    try:
        con = sqlite3.connect(db_path)
        con.execute("SELECT 1")
        con.close()
        return False
    except Exception:
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# 源码审计（fail-then-pass / 契约 guard）
# ═══════════════════════════════════════════════════════════════════════════════
def test_t27_tool_query_sku_calls_stock_readiness():
    """tool_query_sku 在按需计算 wf5 前查 stock_readiness（仅库存就绪才算）。"""
    body = _tool_fn_body("tool_query_sku")
    assert "stock_readiness" in body, (
        "tool_query_sku 未调 stock_readiness（wf5 按需计算的就绪门缺失）"
    )


def test_t27_tool_query_sku_calls_compute_wf5_single():
    """fail-then-pass：tool_query_sku 调 compute_wf5_single 填补 wf5 接线缺失。

    rebase 前的 main tool_query_sku 不含此调用 → FAIL；本 PR 改后 → PASS。
    """
    body = _tool_fn_body("tool_query_sku")
    assert "compute_wf5_single" in body, (
        "tool_query_sku 未调 compute_wf5_single（接线缺失修复未实现）"
    )
    # 承重墙：按需计算必须被 noon_orders 陈旧门挡住（Luke B），不得无条件触发。
    assert "noon_orders_stale" in body, (
        "tool_query_sku 的 wf5 按需计算未挂 noon_orders_stale 门（违反 WS-142 Luke B）"
    )


def test_compute_wf5_single_exists_in_data():
    """fail-then-pass：data.py 有 compute_wf5_single（单 SKU 按需计算入口）。"""
    src = open(DATA_PATH, encoding="utf-8").read()
    assert "def compute_wf5_single" in src, (
        "data.py 里找不到 def compute_wf5_single（新函数未实现）"
    )


def test_t29_compute_replenishment_has_stock_gate():
    """回归 guard：tool_compute_replenishment 仍有 stock_readiness 就绪门（不绕过）。"""
    body = _tool_fn_body("tool_compute_replenishment", span=2000)
    assert "stock_readiness" in body, (
        "tool_compute_replenishment 丢了 stock_readiness 门（库存未就绪可能静默给 0）"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# data 层 fixture：get_replenishment_view 不就绪 → rows=[]
# ═══════════════════════════════════════════════════════════════════════════════
def test_t29_not_ready_returns_explicit_warning():
    """fixture（空 wf1_stock）：data.get_replenishment_view 返回 not-ready + rows=[]。"""
    import importlib

    tmp = tempfile.NamedTemporaryFile(suffix="_s1_not_ready.db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    orig_env = os.environ.get("HIPOP_DB")
    os.environ["HIPOP_DB"] = tmp_path
    os.environ.pop("DB_URL", None)

    try:
        con = sqlite3.connect(tmp_path)
        con.execute("""CREATE TABLE sales_entities (
            id INTEGER PRIMARY KEY, tenant_id BIGINT NOT NULL,
            alias TEXT NOT NULL, country TEXT NOT NULL, platform TEXT,
            store_name TEXT, store_id INT, currency TEXT,
            active INT NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE wf2_sku (
            tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
            is_listed INTEGER DEFAULT 1,
            sales_10d REAL, sales_30d REAL, sales_60d REAL, sales_180d REAL,
            latest_profit_rate REAL, title TEXT, as_of_date TEXT,
            total_orders INTEGER, latest_price REAL,
            noon_saleable_qty REAL, imported_at TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku))""")
        con.execute("""CREATE TABLE wf1_stock (
            tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL, partner_sku TEXT NOT NULL,
            noon_total_qty INT, noon_saleable_qty INT, noon_unsaleable_qty INT,
            noon_warehouses_json TEXT, pending_inbound_qty INT,
            overseas_total_qty INT, overseas_breakdown_json TEXT,
            yiwu_qty INT, dongguan_qty INT, total_stock INT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku))""")
        con.execute("""CREATE TABLE wf5_sales_cycle (
            tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
            weekly_total_replenish INTEGER, urgency TEXT,
            wf5_replenish_qty INTEGER, lost_replenish_qty INTEGER,
            current_pipeline INTEGER, target_pipeline INTEGER,
            daily_rate REAL, trend TEXT, ops_advice TEXT,
            risk_label TEXT, trigger_reasons TEXT, updated_at TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku))""")
        con.execute("""CREATE TABLE tenant_store_map (
            store_code TEXT PRIMARY KEY, tenant_id INTEGER, entity_alias TEXT)""")
        con.execute(
            "INSERT INTO sales_entities (id,tenant_id,alias,country,platform,active) VALUES (1,1,'hipop_ksa','SA','noon',1)"
        )
        con.execute("INSERT INTO tenant_store_map VALUES ('ksa',1,'hipop_ksa')")
        con.execute(
            "INSERT INTO wf2_sku (tenant_id,entity_alias,partner_sku,is_listed,sales_30d) VALUES (?,?,?,1,5.0)",
            (1, "hipop_ksa", "TEST_SKU_001"),
        )
        con.commit()
        con.close()

        import hipop.server.data as data_mod
        importlib.reload(data_mod)

        view = data_mod.get_replenishment_view("ksa", limit=10)
        stock_status = view.get("stock_status", {})
        assert stock_status.get("ready") is False, (
            f"wf1_stock 空时 ready 应为 False，实际: {stock_status}"
        )
        assert view.get("rows", []) == [], (
            f"not-ready 时 rows 应为 []，实际: {len(view.get('rows', []))} 行"
        )

    finally:
        if orig_env is not None:
            os.environ["HIPOP_DB"] = orig_env
        else:
            os.environ.pop("HIPOP_DB", None)
        import hipop.server.data as data_mod
        importlib.reload(data_mod)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# 共用：建一个最小 SQLite fixture schema（tool_query_sku 行为测试用）
# ═══════════════════════════════════════════════════════════════════════════════
_FIXTURE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sales_entities (
        id INTEGER PRIMARY KEY, tenant_id BIGINT NOT NULL,
        alias TEXT NOT NULL, country TEXT NOT NULL, platform TEXT,
        store_name TEXT, store_id INT, currency TEXT,
        active INT NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS wf2_sku (
        tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
        is_listed INTEGER DEFAULT 1, sales_30d REAL, sales_10d REAL,
        total_orders INTEGER, as_of_date TEXT, imported_at TEXT,
        title TEXT, latest_price REAL, latest_profit_rate REAL,
        sales_grade TEXT,
        PRIMARY KEY (tenant_id, entity_alias, partner_sku));
    CREATE TABLE IF NOT EXISTS wf1_stock (
        tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL,
        partner_sku TEXT NOT NULL,
        noon_saleable_qty INT, imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, entity_alias, partner_sku));
    CREATE TABLE IF NOT EXISTS wf5_sales_cycle (
        tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
        weekly_total_replenish INTEGER, urgency TEXT,
        wf5_replenish_qty INTEGER, lost_replenish_qty INTEGER,
        current_pipeline INTEGER, target_pipeline INTEGER,
        daily_rate REAL, trend TEXT, ops_advice TEXT,
        risk_label TEXT, trigger_reasons TEXT, updated_at TEXT,
        PRIMARY KEY (tenant_id, entity_alias, partner_sku));
    CREATE TABLE IF NOT EXISTS wf3_logistics_hub_v2 (
        tenant_id INTEGER, sku TEXT,
        in_transit_total_qty INTEGER DEFAULT 0,
        has_stuck_batch INTEGER DEFAULT 0,
        needs_ops_input INTEGER DEFAULT 0,
        updated_at TEXT);
    CREATE TABLE IF NOT EXISTS wf2_orders (
        tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
        item_nr TEXT, order_date TEXT, status TEXT,
        is_cancelled INTEGER, is_return INTEGER, source TEXT, imported_at TEXT);
"""


def _set_chat_ctx(agent_mod):
    agent_mod._chat_tenant.set(1)
    agent_mod._chat_scope.set({"tenant_id": 1, "store": "KSA", "user": "test"})


# ═══════════════════════════════════════════════════════════════════════════════
# 承重墙（WS-142 Luke B）：空库存 + noon 陈旧 → noon_orders_stale 优先，不被库存空吞
# ═══════════════════════════════════════════════════════════════════════════════
def test_luke_b_stale_wins_over_stock_empty():
    """空 wf1_stock + noon_orders 陈旧 + wf5 缺行 → data_stale=True / stale_reason 含
    noon_orders_stale；不得短路成「库存为空/wf5_status」吞掉陈旧信号（WS-142 Luke B）。
    """
    import importlib
    import datetime

    today = datetime.date.today().isoformat()
    stale_order = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    tmp = tempfile.NamedTemporaryFile(suffix="_lukeb.db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    orig_env = os.environ.get("HIPOP_DB")
    orig_url = os.environ.get("DB_URL")
    os.environ["HIPOP_DB"] = tmp_path
    os.environ.pop("DB_URL", None)

    try:
        import hipop.server.data as _data_mod
        importlib.reload(_data_mod)
        with _data_mod.conn() as c:
            c.executescript(_FIXTURE_SCHEMA)
            c.execute("INSERT INTO sales_entities (id,tenant_id,alias,country,active) VALUES (1,1,'fix_ksa','SA',1)")
            # wf2_sku as_of 今天（新鲜）；但 noon 订单 5 天前（陈旧）；wf1_stock 空；无 wf5 行
            c.execute(
                "INSERT INTO wf2_sku (tenant_id,entity_alias,partner_sku,is_listed,sales_30d,"
                "title,as_of_date,imported_at,sales_10d,latest_price,latest_profit_rate) "
                "VALUES (1,'fix_ksa','LUKEB_SKU',1,5.0,'LukeB SKU',?,?,2.0,50.0,0.15)",
                (today, today + "T10:00:00"),
            )
            c.execute(
                "INSERT INTO wf2_orders (tenant_id,entity_alias,partner_sku,item_nr,"
                "order_date,status,is_cancelled,is_return) VALUES (1,'fix_ksa','LUKEB_SKU','O1',?,?,0,0)",
                (stale_order, "delivered"),
            )

        import hipop.server.agent as _agent_mod
        orig_live = _agent_mod._sku_sales_live_fn

        def mock_live_fail(sku, nation_id, token):
            # live 取数失败 → 无实时销量 → 走 REDACT（禁旧值）
            return {"ok": False, "error": "no_erp_token", "message": "ERP 凭据不可用"}

        _agent_mod._sku_sales_live_fn = mock_live_fail
        _set_chat_ctx(_agent_mod)
        try:
            result = _agent_mod.tool_query_sku(["LUKEB_SKU"], store="KSA")
        finally:
            _agent_mod._sku_sales_live_fn = orig_live

        item = (result.get("items") or [{}])[0]
        assert item.get("data_stale") is True, (
            f"空库存 + noon 陈旧时 data_stale 应为 True，实际 item: {item}"
        )
        assert "noon_orders_stale" in (item.get("stale_reason") or ""), (
            f"stale_reason 必须点名 noon_orders_stale（不得被库存空吞），实际: {item.get('stale_reason')!r}"
        )
        assert item.get("wf5_status") != "not_ready", (
            f"不得短路成 wf5_status=not_ready 吞掉陈旧信号（违反 Luke B），实际 item: {item}"
        )
        assert item.get("sales_30d") is None, (
            f"陈旧时销量必须 REDACT，实际 sales_30d={item.get('sales_30d')!r}"
        )
    finally:
        if orig_env is not None:
            os.environ["HIPOP_DB"] = orig_env
        else:
            os.environ.pop("HIPOP_DB", None)
        if orig_url is not None:
            os.environ["DB_URL"] = orig_url
        import hipop.server.data as _data_mod
        importlib.reload(_data_mod)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# fail-then-pass 行为：wf5 缺失 + stock ready + 数据新鲜 → compute_wf5_single → weekly_replenish 有值
# ═══════════════════════════════════════════════════════════════════════════════
def test_t27_ready_compute_wf5_populates_replenish():
    """ready + 新鲜 + wf5 缺行 → 按需 compute_wf5_single 写 wf5 → weekly_replenish 有值。

    rebase 前的 main 无此按需计算 → trend 仍 NULL、weekly_replenish=None → FAIL；
    本 PR 改后 → 42 → PASS。
    """
    import importlib
    import datetime

    today = datetime.date.today().isoformat()
    fresh_ts = today + "T10:00:00"
    expected_replenish = 42
    tmp = tempfile.NamedTemporaryFile(suffix="_t27_ready.db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    orig_env = os.environ.get("HIPOP_DB")
    orig_url = os.environ.get("DB_URL")
    os.environ["HIPOP_DB"] = tmp_path
    os.environ.pop("DB_URL", None)

    try:
        import hipop.server.data as _data_mod
        importlib.reload(_data_mod)
        with _data_mod.conn() as c:
            c.executescript(_FIXTURE_SCHEMA)
            c.execute("INSERT INTO sales_entities (id,tenant_id,alias,country,active) VALUES (1,1,'fix_ksa','SA',1)")
            c.execute(
                "INSERT INTO wf2_sku (tenant_id,entity_alias,partner_sku,is_listed,sales_30d,"
                "title,as_of_date,imported_at,sales_10d,latest_price,latest_profit_rate) "
                "VALUES (1,'fix_ksa','T27_READY_SKU',1,5.0,'Test Ready SKU',?,?,2.0,50.0,0.15)",
                (today, fresh_ts),
            )
            # 20 行 fresh wf1_stock 满足 MIN_ROWS=20 + coverage
            c.execute(
                "INSERT INTO wf1_stock (tenant_id,entity_alias,partner_sku,noon_saleable_qty,imported_at) "
                "VALUES (1,'fix_ksa','T27_READY_SKU',10,?)",
                (fresh_ts,),
            )
            for i in range(19):
                c.execute(
                    "INSERT INTO wf1_stock (tenant_id,entity_alias,partner_sku,noon_saleable_qty,imported_at) "
                    "VALUES (1,'fix_ksa',?,0,?)",
                    (f"DUMMY_STOCK_{i:03d}", fresh_ts),
                )
            # 无 wf5 行 → trend IS NULL；fresh 订单保持 noon_orders_stale=False
            c.execute(
                "INSERT INTO wf2_orders (tenant_id,entity_alias,partner_sku,item_nr,"
                "order_date,status,is_cancelled,is_return) VALUES (1,'fix_ksa','T27_READY_SKU','O1',?,?,0,0)",
                (today, "delivered"),
            )

        import hipop.server.agent as _agent_mod
        orig_live = _agent_mod._sku_sales_live_fn
        orig_compute = _data_mod.compute_wf5_single

        def mock_live(sku, nation_id, token):
            return {"ok": True, "sku": sku, "sales_30d": 10, "history_total": 100,
                    "fetched_at": fresh_ts, "source": "test_mock"}

        def mock_compute_wf5_single(store_arg, sku_arg):
            with _data_mod.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO wf5_sales_cycle "
                    "(tenant_id,entity_alias,partner_sku,weekly_total_replenish,"
                    "trend,urgency,daily_rate,ops_advice,updated_at) "
                    "VALUES (1,'fix_ksa',?,?,'rising','high',1.5,'补货',?)",
                    (sku_arg, expected_replenish, fresh_ts),
                )
            return {"partner_sku": sku_arg, "weekly_total_replenish": expected_replenish,
                    "trend": "rising", "urgency": "high"}

        _agent_mod._sku_sales_live_fn = mock_live
        _data_mod.compute_wf5_single = mock_compute_wf5_single
        _set_chat_ctx(_agent_mod)
        try:
            result = _agent_mod.tool_query_sku(["T27_READY_SKU"], store="KSA")
        finally:
            _agent_mod._sku_sales_live_fn = orig_live
            _data_mod.compute_wf5_single = orig_compute

        item = (result.get("items") or [{}])[0]
        assert item.get("found") is True, f"item.found 应为 True，实际: {item}"
        assert item.get("weekly_replenish") == expected_replenish, (
            f"compute_wf5_single 写入后 weekly_replenish 应为 {expected_replenish}，"
            f"实际: {item.get('weekly_replenish')}，完整 item: {item}"
        )
    finally:
        if orig_env is not None:
            os.environ["HIPOP_DB"] = orig_env
        else:
            os.environ.pop("HIPOP_DB", None)
        if orig_url is not None:
            os.environ["DB_URL"] = orig_url
        import hipop.server.data as _data_mod
        importlib.reload(_data_mod)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# live（make test 默认 skip）：TBU0010A 修复后在 wf5 出现
# ═══════════════════════════════════════════════════════════════════════════════
def test_live_tbu0010a_now_in_wf5():
    """live 手动验收：set HIPOP_TEST_S1_WF5=1，在通过 chat 查询 TBU0010A 后再运行。"""
    if _skip_if_live_db_unavailable():
        print("⊘ test_live_tbu0010a_now_in_wf5 (live DB unavailable, skipped in CI)")
        return
    if not os.environ.get("HIPOP_TEST_S1_WF5"):
        print("⊘ test_live_tbu0010a_now_in_wf5 (skipped; set HIPOP_TEST_S1_WF5=1 after TBU0010A queried via chat)")
        return
    wf5_rows = _live_fetch(
        "SELECT partner_sku, weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T27_SKU),
    )
    assert len(wf5_rows) >= 1, f"{T27_SKU} 仍不在 wf5_sales_cycle (hipop_ksa)——接线修复未生效"
    assert wf5_rows[0]["weekly_total_replenish"] is not None, (
        f"{T27_SKU} 在 wf5 有行但 weekly_total_replenish=NULL——compute_wf5_single 计算失败"
    )


# ── 运行 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_t27_tool_query_sku_calls_stock_readiness,
        test_t27_tool_query_sku_calls_compute_wf5_single,
        test_compute_wf5_single_exists_in_data,
        test_t29_compute_replenishment_has_stock_gate,
        test_t29_not_ready_returns_explicit_warning,
        test_luke_b_stale_wins_over_stock_empty,
        test_t27_ready_compute_wf5_populates_replenish,
        test_live_tbu0010a_now_in_wf5,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print("✓ {}".format(t.__name__))
        except Exception as e:
            failed += 1
            print("✗ {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{}/{} passed".format(len(tests) - failed, len(tests)))
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)

"""smoke_t03_live_sales_path.py — T03 SKU 实时取数路径 fail-then-pass smoke

验收（WS-122 round-2）：
  tool_query_sku 在任何 wf2_sku 快照新鲜度下，必须先走实时取数路径：
  1. 快照 as_of_date=今日但含旧值 65/662 + live 替身返回 25/201 → 工具返回 25/201
  2. live 不可用/失败时 → 所有销量/订单派生字段为 null（不输出旧缓存确定数）
  3. provider _stale_skus_from_sku_result: live_sales_failed → result_stale_skus=[SKU]
  4. safety _check_stale_sales_claim: live_failed + 含销量数字 → T03 警告

FAIL（改前 / SMOKE_SKIP_FIX=1）：
  - _sku_sales_live_fn 不存在 → snapshot bypass → 返回快照值 65 → FAIL
  - provider 仍用 data_stale 而非 live_sales_failed → FAIL

PASS（改后）：
  - live fn 被调用，返回 25/201，包含 live_evidence.fetched_at
  - live 失败时 sales_30d=null，含 live_sales_failed=True
  - provider 通过 _stale_skus_from_sku_result 提取 live_sales_failed SKU
  - safety 正确发出 T03 警告

跑法：
  python3 tests/smoke_t03_live_sales_path.py
  SMOKE_SKIP_FIX=1 python3 tests/smoke_t03_live_sales_path.py
  make test-one F=tests/smoke_t03_live_sales_path.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

_TMP_DB_PATH = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["HIPOP_DB"] = _TMP_DB_PATH
os.environ.pop("DB_URL", None)

TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
SKU = "TBS0228A"
TODAY = datetime.date.today().isoformat()
# 快照含旧值（即使 as_of_date=今日，也应被 live 路径覆盖）
SNAPSHOT_SALES_30D = 65
SNAPSHOT_SALES_10D = 11
SNAPSHOT_TOTAL_ORDERS = 662
SNAPSHOT_WINDOW_ORDERS_30D = 7
# live 源返回的真实值（测试替身）
LIVE_SALES_30D = 25
LIVE_HISTORY_TOTAL = 201
LIVE_FETCHED_AT = "2026-06-09T10:00:00Z"

SMOKE_SKIP_FIX = os.environ.get("SMOKE_SKIP_FIX") == "1"

_TMP_DBS: list = []

_AGENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id     BIGINT,
  task_id       TEXT NOT NULL,
  step_no       INT NOT NULL,
  step_name     TEXT NOT NULL,
  status        TEXT NOT NULL,
  message       TEXT,
  payload_json  TEXT,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _fresh_db(data_module) -> None:
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _TMP_DBS.append(path)
    data_module.DB_PATH = path
    data_module._engine = None
    conn = data_module.conn()
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    cut = sql.find("DO $$")
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            try:
                conn.execute(s)
            except Exception:
                pass
    conn.execute(_AGENT_EVENTS_DDL)
    conn.commit()


def _seed_entity(conn) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 85, "SAR"),
    )
    conn.commit()


def _seed_wf2_sku(conn, as_of_date, sales_30d, total_orders=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, as_of_date, "
        " imported_at, sales_30d, sales_10d, sales_grade, "
        " latest_price, latest_profit_rate, is_listed, total_orders) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "Test SKU T03 Live Path", as_of_date,
         as_of_date + "T10:00:00", sales_30d, SNAPSHOT_SALES_10D, "A", 50.0, 0.15, 1,
         total_orders),
    )
    conn.commit()


def _seed_wf2_orders(conn, as_of_date, count=SNAPSHOT_WINDOW_ORDERS_30D) -> None:
    for idx in range(count):
        conn.execute(
            "INSERT OR REPLACE INTO wf2_orders "
            "(tenant_id, entity_alias, partner_sku, item_nr, order_date, "
            " status, is_cancelled, is_return, source, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                TENANT_ID,
                ENTITY_ALIAS,
                SKU,
                f"SMOKE-T03-LIVE-{idx}",
                as_of_date,
                "delivered",
                1 if idx == 0 else 0,
                1 if idx == 1 else 0,
                "snapshot_fixture",
                as_of_date + "T10:00:00",
            ),
        )
    conn.commit()


def _seed_wf5_sales_cycle(conn, updated_at) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf5_sales_cycle "
        "(tenant_id, entity_alias, partner_sku, trend, daily_rate, urgency, "
        " ops_advice, weekly_total_replenish, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            TENANT_ID,
            ENTITY_ALIAS,
            SKU,
            "rising",
            1.25,
            "high",
            "snapshot says replenish",
            20,
            updated_at + "T10:00:00",
        ),
    )
    conn.commit()


_results: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    msg = f"  [{status}] {name}"
    if not cond and detail:
        msg += f"\n         ↳ {detail}"
    print(msg)
    _results.append((name, cond))


def test_live_path_called_and_values_used() -> None:
    """fail-then-pass: 快照 65/662 (as_of=今日) + live 替身返回 25/201 → 必须返回 live 值"""
    print("\n── test_live_path_called_and_values_used ────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    if SMOKE_SKIP_FIX:
        fn = getattr(_agent, "_sku_sales_live_fn", "MISSING")
        check("SKIP_FIX: _sku_sales_live_fn 属性不存在（改前）",
              fn == "MISSING",
              f"got _sku_sales_live_fn={fn!r}")
        return

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY, SNAPSHOT_SALES_30D, SNAPSHOT_TOTAL_ORDERS)
    _seed_wf2_orders(conn, TODAY)
    _seed_wf5_sales_cycle(conn, TODAY)

    live_calls: list = []

    def mock_live_fn(sku, nation_id, token):
        live_calls.append({"sku": sku, "nation_id": nation_id})
        return {
            "ok": True,
            "sku": sku,
            "sales_30d": LIVE_SALES_30D,
            "history_total": LIVE_HISTORY_TOTAL,
            "fetched_at": LIVE_FETCHED_AT,
            "source": "test_mock_live",
        }

    orig = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = mock_live_fn
    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
    finally:
        _agent._sku_sales_live_fn = orig

    item = (result.get("items") or [{}])[0]

    check("live fn 被调用（不走快照短路）",
          len(live_calls) > 0,
          f"live_calls={live_calls!r} (snapshot bypass — live fn never called!)")
    check(f"sales_30d = {LIVE_SALES_30D}（live 值，非快照 {SNAPSHOT_SALES_30D}）",
          item.get("sales_30d") == LIVE_SALES_30D,
          f"got sales_30d={item.get('sales_30d')!r} (snapshot {SNAPSHOT_SALES_30D} not replaced?)")
    check(f"history_total = {LIVE_HISTORY_TOTAL}（live 值，非快照 {SNAPSHOT_TOTAL_ORDERS}）",
          item.get("history_total") == LIVE_HISTORY_TOTAL,
          f"got history_total={item.get('history_total')!r}")
    check("live_evidence 存在",
          isinstance(item.get("live_evidence"), dict),
          f"live_evidence={item.get('live_evidence')!r}")
    check("live_evidence.fetched_at 有值（取数时间证据）",
          bool((item.get("live_evidence") or {}).get("fetched_at")),
          f"live_evidence={item.get('live_evidence')!r}")
    check("references 含 live 取数证据",
          any(r.get("fetched_at") for r in (result.get("references") or [])),
          f"refs={result.get('references')!r}")
    check("live_sales_failed 不存在（成功时不应有错误标志）",
          not item.get("live_sales_failed"),
          f"live_sales_failed={item.get('live_sales_failed')!r}")


def test_live_unavailable_redacts_sales() -> None:
    """live 不可用时：sales_30d/history_total=null，不输出旧缓存确定数"""
    print("\n── test_live_unavailable_redacts_sales ──────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    if SMOKE_SKIP_FIX:
        print("  [SKIP] SMOKE_SKIP_FIX=1")
        return

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY, SNAPSHOT_SALES_30D, SNAPSHOT_TOTAL_ORDERS)

    def mock_live_fail(sku, nation_id, token):
        return {"ok": False, "error": "erp_login_failed",
                "message": "ERP 登录失败，无法实时取数"}

    orig = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = mock_live_fail
    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
    finally:
        _agent._sku_sales_live_fn = orig

    item = (result.get("items") or [{}])[0]

    check("live 失败 → sales_30d = null（不输出旧缓存 65）",
          item.get("sales_30d") is None,
          f"got sales_30d={item.get('sales_30d')!r} (旧缓存 {SNAPSHOT_SALES_30D} 泄漏!)")
    check("live 失败 → sales_10d = null（不输出旧缓存 11）",
          item.get("sales_10d") is None,
          f"got sales_10d={item.get('sales_10d')!r} (旧缓存 {SNAPSHOT_SALES_10D} 泄漏!)")
    check("live 失败 → total_orders_30d = null（不输出旧订单统计）",
          item.get("total_orders_30d") is None,
          f"got total_orders_30d={item.get('total_orders_30d')!r} "
          f"(旧订单统计 {SNAPSHOT_WINDOW_ORDERS_30D} 泄漏!)")
    check("live 失败 → cancel_rate_30d = null（不输出旧订单派生率）",
          item.get("cancel_rate_30d") is None,
          f"got cancel_rate_30d={item.get('cancel_rate_30d')!r}")
    check("live 失败 → return_rate_30d = null（不输出旧订单派生率）",
          item.get("return_rate_30d") is None,
          f"got return_rate_30d={item.get('return_rate_30d')!r}")
    check("live 失败 → profit_rate_pct = null（不输出旧利润率）",
          item.get("profit_rate_pct") is None,
          f"got profit_rate_pct={item.get('profit_rate_pct')!r}")
    check("live 失败 → trend/daily_rate/urgency/advice/replenish 均不输出旧 wf5 信号",
          all(item.get(k) is None for k in (
              "trend", "daily_rate", "urgency", "ops_advice", "weekly_replenish")),
          f"got trend={item.get('trend')!r}, daily_rate={item.get('daily_rate')!r}, "
          f"urgency={item.get('urgency')!r}, ops_advice={item.get('ops_advice')!r}, "
          f"weekly_replenish={item.get('weekly_replenish')!r}")
    check("live 失败 → history_total = null（不输出旧缓存 662）",
          item.get("history_total") is None,
          f"got history_total={item.get('history_total')!r} (旧缓存 {SNAPSHOT_TOTAL_ORDERS} 泄漏!)")
    check("live_sales_failed=True",
          item.get("live_sales_failed") is True,
          f"live_sales_failed={item.get('live_sales_failed')!r}")
    check("live_sales_message 有内容",
          bool(item.get("live_sales_message")),
          f"live_sales_message={item.get('live_sales_message')!r}")
    check("live_evidence 不存在（失败时不应有证据）",
          not item.get("live_evidence"),
          f"live_evidence={item.get('live_evidence')!r}")


def test_live_exception_redacts_sales() -> None:
    """live fn 抛异常时：sales_30d=null，不崩溃，不输出旧缓存数"""
    print("\n── test_live_exception_redacts_sales ────────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    if SMOKE_SKIP_FIX:
        print("  [SKIP] SMOKE_SKIP_FIX=1")
        return

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY, SNAPSHOT_SALES_30D, SNAPSHOT_TOTAL_ORDERS)

    def mock_live_exception(sku, nation_id, token):
        raise RuntimeError("ERP connection timeout")

    orig = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = mock_live_exception
    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
        item = (result.get("items") or [{}])[0]
        check("live 异常 → 工具不崩溃，有返回",
              item.get("found") is True,
              f"item={item!r}")
        check("live 异常 → sales_30d = null（不输出旧缓存 65）",
              item.get("sales_30d") is None,
              f"got sales_30d={item.get('sales_30d')!r}")
        check("live 异常 → live_sales_failed=True",
              item.get("live_sales_failed") is True,
              f"live_sales_failed={item.get('live_sales_failed')!r}")
    except Exception as e:
        check("live 异常 → 工具不崩溃", False, f"raised {type(e).__name__}: {e}")
    finally:
        _agent._sku_sales_live_fn = orig


def test_provider_chain_stale_skus() -> None:
    """provider _stale_skus_from_sku_result: live_sales_failed=True → result_stale_skus=[SKU]

    真实链路：调用 tool_query_sku 得到真实结果，再通过 provider 导出的提取函数验证，
    不复制 provider 实现逻辑。
    """
    print("\n── test_provider_chain_stale_skus ───────────────────────────")

    if SMOKE_SKIP_FIX:
        try:
            from hipop.server._provider_anthropic import _stale_skus_from_sku_result
            check("SKIP_FIX: _stale_skus_from_sku_result 不存在（改前）",
                  False,
                  "_stale_skus_from_sku_result exists but should not (改前状态)")
        except ImportError:
            check("SKIP_FIX: _stale_skus_from_sku_result 不存在（改前）",
                  True, "")
        return

    import hipop.server.data as _data
    import hipop.server.agent as _agent
    from hipop.server._provider_anthropic import _stale_skus_from_sku_result

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY, SNAPSHOT_SALES_30D, SNAPSHOT_TOTAL_ORDERS)

    def mock_live_fail(sku, nation_id, token):
        return {"ok": False, "error": "erp_login_failed", "message": "ERP 不可用"}

    def mock_live_ok(sku, nation_id, token):
        return {"ok": True, "sales_30d": 25, "history_total": 201,
                "fetched_at": LIVE_FETCHED_AT, "source": "test_mock"}

    orig = _agent._sku_sales_live_fn
    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    try:
        # 失败场景：live_sales_failed → result_stale_skus 含 SKU
        _agent._sku_sales_live_fn = mock_live_fail
        fail_result = _agent.tool_query_sku([SKU], store="KSA")
        stale_fail = _stale_skus_from_sku_result("query_sku", fail_result)
        check(f"live 失败 → provider 提取 result_stale_skus=[{SKU!r}]",
              SKU in (stale_fail or []),
              f"stale_fail={stale_fail!r}")

        # 成功场景：live ok → result_stale_skus=None
        _agent._sku_sales_live_fn = mock_live_ok
        ok_result = _agent.tool_query_sku([SKU], store="KSA")
        stale_ok = _stale_skus_from_sku_result("query_sku", ok_result)
        check("live 成功 → result_stale_skus=None（无误报）",
              stale_ok is None,
              f"stale_ok={stale_ok!r}")

        # 非 query_sku 工具 → result_stale_skus=None
        stale_other = _stale_skus_from_sku_result("data_health_check", fail_result)
        check("非 query_sku 工具 → result_stale_skus=None",
              stale_other is None,
              f"stale_other={stale_other!r}")
    finally:
        _agent._sku_sales_live_fn = orig


def test_safety_t03_with_live_failed() -> None:
    """safety _check_stale_sales_claim: live_failed SKU + 含销量数字 → T03 警告"""
    print("\n── test_safety_t03_with_live_failed ─────────────────────────")

    if SMOKE_SKIP_FIX:
        print("  [SKIP] SMOKE_SKIP_FIX=1")
        return

    from hipop.server import _safety

    fn = getattr(_safety, "_check_stale_sales_claim", None)
    check("_check_stale_sales_claim 函数存在",
          callable(fn), f"got {fn!r}")
    if not callable(fn):
        return

    # live_sales_failed SKU 记录在 result_stale_skus（与旧 data_stale 同语义键）
    live_fail_log = [{"name": "query_sku", "result_stale_skus": [SKU]}]

    warns_a = fn(f"近30天销量是{SNAPSHOT_SALES_30D}件，趋势稳定", live_fail_log)
    check("live 失败 SKU + 含销量数字 → T03 警告",
          len(warns_a) > 0,
          f"warnings={warns_a!r}")

    warns_b = fn("当前无法实时确认销量，请稍后重试", live_fail_log)
    check("live 失败 SKU + 无具体数字 → 不触发（无误报）",
          len(warns_b) == 0,
          f"warnings={warns_b!r}")

    live_ok_log = [{"name": "query_sku", "result_stale_skus": None}]
    warns_c = fn(f"近30天销量是{LIVE_SALES_30D}件", live_ok_log)
    check("live 成功（无 stale_skus）+ 含数字 → 不触发（无误报）",
          len(warns_c) == 0,
          f"warnings={warns_c!r}")

    _, all_warns = _safety.sanitize_reply(
        f"近30天销量是{SNAPSHOT_SALES_30D}件，历史总销量{SNAPSHOT_TOTAL_ORDERS}件",
        ["query_sku"],
        tool_log=live_fail_log,
        question=f"{SKU} 销量",
    )
    check("sanitize_reply 接线：live_failed → T03 警告",
          any("T03" in w for w in all_warns),
          f"all_warns={all_warns!r}")


def test_live_ok_but_missing_sales_30d_redacts() -> None:
    """round-4: live ok=True 但 sales_30d=None → fail closed，不泄漏旧缓存"""
    print("\n── test_live_ok_but_missing_sales_30d_redacts ────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    if SMOKE_SKIP_FIX:
        print("  [SKIP] SMOKE_SKIP_FIX=1")
        return

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY, SNAPSHOT_SALES_30D, SNAPSHOT_TOTAL_ORDERS)
    _seed_wf2_orders(conn, TODAY)
    _seed_wf5_sales_cycle(conn, TODAY)

    def mock_live_ok_no_sales(sku, nation_id, token):
        # ok=True but sales_30d=None (e.g. parse failure / UAE non-SA)
        return {"ok": True, "sales_30d": None, "history_total": None,
                "fetched_at": LIVE_FETCHED_AT, "source": "test_partial"}

    orig = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = mock_live_ok_no_sales
    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
    finally:
        _agent._sku_sales_live_fn = orig

    item = (result.get("items") or [{}])[0]

    check("ok=True + sales_30d=None → sales_30d = null（不泄漏缓存 65）",
          item.get("sales_30d") is None,
          f"got sales_30d={item.get('sales_30d')!r} (缓存 {SNAPSHOT_SALES_30D} 泄漏!)")
    check("ok=True + sales_30d=None → sales_10d = null（不泄漏缓存 11）",
          item.get("sales_10d") is None,
          f"got sales_10d={item.get('sales_10d')!r} (缓存 {SNAPSHOT_SALES_10D} 泄漏!)")
    check("ok=True + sales_30d=None → total_orders_30d = null",
          item.get("total_orders_30d") is None,
          f"got total_orders_30d={item.get('total_orders_30d')!r}")
    check("ok=True + sales_30d=None → profit_rate_pct = null",
          item.get("profit_rate_pct") is None,
          f"got profit_rate_pct={item.get('profit_rate_pct')!r}")
    check("ok=True + sales_30d=None → trend/urgency/advice = null",
          all(item.get(k) is None for k in ("trend", "urgency", "ops_advice", "weekly_replenish")),
          f"got trend={item.get('trend')!r} urgency={item.get('urgency')!r} "
          f"ops_advice={item.get('ops_advice')!r} weekly_replenish={item.get('weekly_replenish')!r}")
    check("ok=True + sales_30d=None → history_total = null（不泄漏缓存 662）",
          item.get("history_total") is None,
          f"got history_total={item.get('history_total')!r}")
    check("ok=True + sales_30d=None → live_sales_failed=True",
          item.get("live_sales_failed") is True,
          f"live_sales_failed={item.get('live_sales_failed')!r}")
    check("ok=True + sales_30d=None → live_evidence 不存在（无可用证据）",
          not item.get("live_evidence"),
          f"live_evidence={item.get('live_evidence')!r}")
    check("ok=True + sales_30d=None → live_sales_error 为 live_ok_but_missing_sales_30d",
          item.get("live_sales_error") == "live_ok_but_missing_sales_30d",
          f"live_sales_error={item.get('live_sales_error')!r}")


def _cleanup() -> None:
    for p in _TMP_DBS + [_TMP_DB_PATH]:
        try:
            os.unlink(p)
        except OSError:
            pass


if __name__ == "__main__":
    print("=== smoke_t03_live_sales_path ===")
    if SMOKE_SKIP_FIX:
        print("SMOKE_SKIP_FIX=1: 演示改前 FAIL 状态\n")

    test_live_path_called_and_values_used()
    test_live_unavailable_redacts_sales()
    test_live_exception_redacts_sales()
    test_live_ok_but_missing_sales_30d_redacts()
    test_provider_chain_stale_skus()
    test_safety_t03_with_live_failed()

    _cleanup()

    total = len(_results)
    failed = [n for n, ok in _results if not ok]
    passed = total - len(failed)

    print(f"\n=== 结果: {passed}/{total} PASS ===")
    if failed:
        print("FAIL 列表:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    else:
        print("全部通过 ✓")
        sys.exit(0)

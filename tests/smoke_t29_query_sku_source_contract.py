"""smoke_t29_query_sku_source_contract.py — WS-129/P0-S1 tool_query_sku 事实源契约接入

fail-then-pass 验收（验门人红队复现场景）：
  红队：wf2_sku.imported_at、wf5.updated_at、wf3.updated_at 全设为 NULL，
  工具应 fail-closed 不出对应字段的数，而非返回裸数字。

FAIL（改前）：
  - in_transit 返回具体数字（55）即使 wf3.updated_at=NULL
  - wf5 字段（trend/urgency/ops_advice）返回值即使 wf5.updated_at=NULL
  - 无 in_transit_source / in_transit_updated_at 字段

PASS（改后）：
  - wf3.updated_at=NULL → in_transit=None, in_transit_source=None
  - wf5.updated_at=NULL → trend/urgency/ops_advice=None
  - 有 timestamp 时 → in_transit 有值 + in_transit_source="erp" + in_transit_updated_at 存在
  - wf2_imported_at / wf5_updated_at 暴露在 item 里（来源时间戳可查）

跑法：
  python3 tests/smoke_t29_query_sku_source_contract.py
  SMOKE_SKIP_FIX=1 python3 tests/smoke_t29_query_sku_source_contract.py
  make test-one F=tests/smoke_t29_query_sku_source_contract.py
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
SKU = "TSC0001A"
TODAY = datetime.date.today().isoformat()

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


def _seed_wf2_sku(conn, imported_at) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, as_of_date, "
        " imported_at, sales_30d, sales_10d, sales_grade, "
        " latest_price, latest_profit_rate, is_listed, total_orders) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "Test SKU Source Contract", TODAY,
         imported_at, 42, 8, "A", 50.0, 0.20, 1, 200),
    )
    conn.commit()


def _seed_wf5(conn, updated_at) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf5_sales_cycle "
        "(tenant_id, entity_alias, partner_sku, trend, daily_rate, urgency, "
        " ops_advice, weekly_total_replenish, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU,
         "stable", 1.4, "medium", "maintain", 10, updated_at),
    )
    conn.commit()


def _seed_wf3(conn, updated_at) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf3_logistics_hub_v2 "
        "(tenant_id, sku, in_transit_total_qty, has_stuck_batch, needs_ops_input, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (TENANT_ID, SKU, 55, 0, 0, updated_at),
    )
    conn.commit()


_mock_live = {
    "ok": True, "sku": SKU,
    "sales_30d": 9, "history_total": 99,
    "fetched_at": TODAY + "T10:00:00Z", "source": "erp",
}


def _call_tool(agent_mod):
    orig = agent_mod._sku_sales_live_fn
    agent_mod._sku_sales_live_fn = lambda sku, nation_id, token: dict(_mock_live)
    agent_mod._chat_tenant.set(TENANT_ID)
    agent_mod._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})
    try:
        result = agent_mod.tool_query_sku([SKU], store="KSA")
    finally:
        agent_mod._sku_sales_live_fn = orig
    return result


_results: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    msg = f"  [{status}] {name}"
    if not cond and detail:
        msg += f"\n         ↳ {detail}"
    print(msg)
    _results.append((name, cond))


def test_null_wf3_updated_at_fails_closed() -> None:
    """红队场景1：wf3.updated_at=NULL → in_transit 必须为 None（fail-closed）。"""
    print("\n── test_null_wf3_updated_at_fails_closed ───────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY + "T08:00:00")
    _seed_wf5(conn, TODAY + "T08:00:00")
    _seed_wf3(conn, None)  # wf3.updated_at=NULL — 红队条件

    result = _call_tool(_agent)
    item = (result.get("items") or [{}])[0]

    if SMOKE_SKIP_FIX:
        # 改前：in_transit 应等于 55（工具不检查 wf3.updated_at）
        check("SKIP_FIX: in_transit 仍返回裸数字（改前无 wf3 门）",
              item.get("in_transit") == 55,
              f"got in_transit={item.get('in_transit')!r}")
        return

    check("wf3.updated_at=NULL → in_transit=None（fail-closed）",
          item.get("in_transit") is None,
          f"got in_transit={item.get('in_transit')!r}（应为 None，合约拒绝无时间戳的数）")
    check("wf3.updated_at=NULL → in_transit_source=None",
          item.get("in_transit_source") is None,
          f"got in_transit_source={item.get('in_transit_source')!r}")
    check("in_transit_updated_at 字段存在（值可为 None）",
          "in_transit_updated_at" in item,
          "item 中无 in_transit_updated_at 字段（源时间戳字段缺失）")


def test_null_wf5_updated_at_fails_closed() -> None:
    """红队场景2：wf5.updated_at=NULL → trend/urgency/ops_advice 必须为 None。"""
    print("\n── test_null_wf5_updated_at_fails_closed ───────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY + "T08:00:00")
    _seed_wf5(conn, None)  # wf5.updated_at=NULL — 红队条件
    _seed_wf3(conn, TODAY + "T08:00:00")

    result = _call_tool(_agent)
    item = (result.get("items") or [{}])[0]

    if SMOKE_SKIP_FIX:
        # 改前：wf5 字段应有值（工具不检查 wf5.updated_at）
        check("SKIP_FIX: trend/urgency 仍有值（改前无 wf5 门）",
              item.get("trend") is not None or item.get("urgency") is not None,
              f"got trend={item.get('trend')!r} urgency={item.get('urgency')!r}")
        return

    check("wf5.updated_at=NULL → trend=None（fail-closed）",
          item.get("trend") is None,
          f"got trend={item.get('trend')!r}（应为 None）")
    check("wf5.updated_at=NULL → urgency=None（fail-closed）",
          item.get("urgency") is None,
          f"got urgency={item.get('urgency')!r}（应为 None）")
    check("wf5.updated_at=NULL → ops_advice=None（fail-closed）",
          item.get("ops_advice") is None,
          f"got ops_advice={item.get('ops_advice')!r}（应为 None）")


def test_null_wf2_imported_at_fails_closed() -> None:
    """红队场景3：wf2_sku.imported_at=NULL → wf2 快照字段（profit_rate/sales_10d）为 None。"""
    print("\n── test_null_wf2_imported_at_fails_closed ──────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, None)  # imported_at=NULL — 红队条件
    _seed_wf5(conn, TODAY + "T08:00:00")
    _seed_wf3(conn, TODAY + "T08:00:00")

    result = _call_tool(_agent)
    item = (result.get("items") or [{}])[0]

    if SMOKE_SKIP_FIX:
        check("SKIP_FIX: profit_rate_pct/sales_10d 仍有值（改前无 wf2 门）",
              item.get("profit_rate_pct") is not None or item.get("sales_10d") is not None,
              f"got profit_rate_pct={item.get('profit_rate_pct')!r}")
        return

    check("wf2.imported_at=NULL → profit_rate_pct=None（fail-closed）",
          item.get("profit_rate_pct") is None,
          f"got profit_rate_pct={item.get('profit_rate_pct')!r}（应为 None）")
    check("wf2.imported_at=NULL → sales_10d=None（fail-closed）",
          item.get("sales_10d") is None,
          f"got sales_10d={item.get('sales_10d')!r}（应为 None）")
    # sales_30d 来自 live ERP，不受 imported_at 影响
    check("sales_30d（来自 live ERP）在 imported_at=NULL 时仍可返回",
          item.get("sales_30d") == 9,
          f"got sales_30d={item.get('sales_30d')!r}（live ERP 不应被 imported_at NULL 阻断）")


def test_all_timestamps_present_passes() -> None:
    """正常场景：所有时间戳存在 → in_transit 有值 + source attribution 完整。"""
    print("\n── test_all_timestamps_present_passes ──────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, TODAY + "T08:00:00")
    _seed_wf5(conn, TODAY + "T08:00:00")
    _seed_wf3(conn, TODAY + "T09:00:00")

    result = _call_tool(_agent)
    item = (result.get("items") or [{}])[0]

    check("所有时间戳存在 → in_transit 有值",
          item.get("in_transit") == 55,
          f"got in_transit={item.get('in_transit')!r}（有时间戳应返回数值）")
    check("in_transit_source='erp'（来源归因）",
          item.get("in_transit_source") == "erp",
          f"got in_transit_source={item.get('in_transit_source')!r}")
    check("in_transit_updated_at 有值",
          bool(item.get("in_transit_updated_at")),
          f"got in_transit_updated_at={item.get('in_transit_updated_at')!r}")
    check("wf5_updated_at 暴露在 item 中",
          bool(item.get("wf5_updated_at")),
          f"got wf5_updated_at={item.get('wf5_updated_at')!r}")
    check("wf2_imported_at 暴露在 item 中",
          bool(item.get("wf2_imported_at")),
          f"got wf2_imported_at={item.get('wf2_imported_at')!r}")
    check("sales_30d=9（live ERP 值）",
          item.get("sales_30d") == 9,
          f"got sales_30d={item.get('sales_30d')!r}")
    check("trend/urgency 有值（wf5 时间戳存在）",
          item.get("trend") == "stable" and item.get("urgency") == "medium",
          f"got trend={item.get('trend')!r} urgency={item.get('urgency')!r}")


def main() -> int:
    print(f"smoke_t29_query_sku_source_contract — SMOKE_SKIP_FIX={SMOKE_SKIP_FIX}")

    test_null_wf3_updated_at_fails_closed()
    test_null_wf5_updated_at_fails_closed()
    test_null_wf2_imported_at_fails_closed()
    test_all_timestamps_present_passes()

    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    print(f"\n{'✓' if not failed else '✗'} {passed}/{len(_results)} checks passed")
    if failed:
        print(f"  失败: {[n for n, ok in _results if not ok]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import atexit
    atexit.register(lambda: [os.unlink(p) for p in _TMP_DBS if os.path.exists(p)])
    sys.exit(main())

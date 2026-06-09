"""smoke_t12b_tool_stock_split.py — T12 tool_query_stock_split 国内仓/在途拆分验收

验收标准（WS-130）：
  1. T12 拆分能区分 noon 仓、国内仓（义乌+东莞）、在途/待发货。
  2. split.dongguan 和 split.domestic（= yiwu + dongguan）存在。
  3. erp_in_transit 来自 wf3_logistics_hub_v2，不计入 total_stock（erp_in_transit_not_in_total=True）。
  4. wf3 无记录 → erp_in_transit=None + erp_in_transit_unavailable 有说明，不报错。
  5. wf3 数据超 3 天 → erp_in_transit=None（fail-closed），不静默出旧数。
  6. T11 键（yiwu/overseas_saudi_1/noon/inbound）保持向后兼容。

FAIL（改前）：
  - split 无 dongguan / domestic 键
  - erp_in_transit 字段不存在
  - total_stock 不含 dongguan

PASS（改后）：
  - split.dongguan = 东莞数量
  - split.domestic = yiwu + dongguan
  - erp_in_transit 有值（wf3 新鲜时）
  - erp_in_transit_not_in_total = True
  - total 正确含 dongguan

跑法：
  python3 tests/smoke_t12b_tool_stock_split.py
  make test-one F=tests/smoke_t12b_tool_stock_split.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

_TMP_DBS: list = []

os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
SKU = "DCSKU001A"

TODAY = datetime.date.today().isoformat()

# T12 fixture values
YIWU = 30
DONGGUAN = 15
OVERSEAS = 200
NOON = 50
INBOUND = 5
# total_stock = yiwu + dongguan + overseas + noon + inbound (INVENTORY_TOTAL_COMPONENTS)
TOTAL = YIWU + DONGGUAN + OVERSEAS + NOON + INBOUND  # 300
WF3_IN_TRANSIT = 88

_AGENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id BIGINT,
  task_id TEXT NOT NULL,
  step_no INT NOT NULL,
  step_name TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  payload_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    return conn


def _seed_entity(conn) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 85, "SAR"),
    )
    conn.commit()


def _seed_stock(conn, updated_at: str, dongguan: int = DONGGUAN) -> None:
    """Seed wf1_stock with T12 fixture including dongguan_qty."""
    overseas_breakdown = json.dumps({"沙特一号仓": OVERSEAS}, ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO wf1_stock "
        "(tenant_id, entity_alias, partner_sku, "
        " yiwu_qty, dongguan_qty, overseas_total_qty, overseas_breakdown_json, "
        " noon_total_qty, pending_inbound_qty, total_stock, "
        " updated_at, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, SKU,
         YIWU, dongguan, OVERSEAS, overseas_breakdown,
         NOON, INBOUND, TOTAL,
         updated_at, updated_at),
    )
    conn.commit()


def _seed_wf3(conn, updated_at) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf3_logistics_hub_v2 "
        "(tenant_id, sku, in_transit_total_qty, has_stuck_batch, needs_ops_input, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (TENANT_ID, SKU, WF3_IN_TRANSIT, 0, 0, updated_at),
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


def test_t12_split_has_dongguan_and_domestic() -> None:
    """T12 核心：split 含 dongguan + domestic（= yiwu+dongguan），total 含 dongguan。

    FAIL（改前）：split 无 dongguan 键，total 不含东莞数量。
    PASS（改后）：dongguan 有值，domestic = yiwu+dongguan，total 正确。
    """
    print("\n── test_t12_split_has_dongguan_and_domestic ─────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    conn = _fresh_db(_data)
    _seed_entity(conn)
    _seed_stock(conn, TODAY)
    _seed_wf3(conn, TODAY + "T09:00:00")
    _data.set_current_tenant(TENANT_ID)

    result = _agent.tool_query_stock_split(SKU, "KSA")
    print(f"  result: ok={result.get('ok')} total={result.get('total')} split={result.get('split')}")

    split = result.get("split") or {}

    check("ok=True", result.get("ok") is True, f"got ok={result.get('ok')!r}")
    check("fail_closed=False", result.get("fail_closed") is False,
          f"got fail_closed={result.get('fail_closed')!r}")

    # T12 fields
    check(f"split.dongguan == {DONGGUAN}",
          split.get("dongguan") == DONGGUAN,
          f"got split.dongguan={split.get('dongguan')!r}（T12: 东莞仓数量缺失）")
    check(f"split.domestic == {YIWU + DONGGUAN}",
          split.get("domestic") == YIWU + DONGGUAN,
          f"got split.domestic={split.get('domestic')!r}（应为义乌+东莞={YIWU + DONGGUAN}）")

    # total_stock contains dongguan
    check(f"total == {TOTAL}",
          result.get("total") == TOTAL,
          f"got total={result.get('total')!r}（应含 dongguan，total={TOTAL}）")

    # T11 backward compat
    for key in ("yiwu", "overseas_saudi_1", "noon", "inbound"):
        check(f"split 含 T11 键 {key}（向后兼容）",
              key in split,
              f"split 缺少 T11 键 {key}")

    check(f"split.yiwu == {YIWU}", split.get("yiwu") == YIWU,
          f"got split.yiwu={split.get('yiwu')!r}")
    check(f"split.noon == {NOON}", split.get("noon") == NOON,
          f"got split.noon={split.get('noon')!r}")


def test_erp_in_transit_from_wf3_not_in_total() -> None:
    """T12: erp_in_transit 来自 wf3，有值，且 erp_in_transit_not_in_total=True。

    FAIL（改前）：erp_in_transit 字段不存在，工具只返回 wf1_stock 数据。
    PASS（改后）：erp_in_transit={WF3_IN_TRANSIT}，erp_in_transit_not_in_total=True。
    """
    print("\n── test_erp_in_transit_from_wf3_not_in_total ────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    conn = _fresh_db(_data)
    _seed_entity(conn)
    _seed_stock(conn, TODAY)
    _seed_wf3(conn, TODAY + "T09:00:00")
    _data.set_current_tenant(TENANT_ID)

    result = _agent.tool_query_stock_split(SKU, "KSA")

    check(f"erp_in_transit == {WF3_IN_TRANSIT}（来自 wf3）",
          result.get("erp_in_transit") == WF3_IN_TRANSIT,
          f"got erp_in_transit={result.get('erp_in_transit')!r}")
    check("erp_in_transit_not_in_total=True（在途不计入 total）",
          result.get("erp_in_transit_not_in_total") is True,
          f"got erp_in_transit_not_in_total={result.get('erp_in_transit_not_in_total')!r}")
    check("erp_in_transit_source='erp'",
          result.get("erp_in_transit_source") == "erp",
          f"got erp_in_transit_source={result.get('erp_in_transit_source')!r}")
    check("erp_in_transit_updated_at 有值",
          bool(result.get("erp_in_transit_updated_at")),
          f"got erp_in_transit_updated_at={result.get('erp_in_transit_updated_at')!r}")

    # in_transit must NOT affect total
    check(f"total 不含 in_transit（total={TOTAL} 不是 {TOTAL + WF3_IN_TRANSIT}）",
          result.get("total") == TOTAL,
          f"got total={result.get('total')!r}（在途被错误加入 total → 违反契约）")


def test_erp_in_transit_no_wf3_record() -> None:
    """wf3 无记录 → erp_in_transit=None + unavailable 说明，不报错。

    FAIL（改前）：字段不存在。
    PASS（改后）：erp_in_transit=None，erp_in_transit_unavailable 有说明文字。
    """
    print("\n── test_erp_in_transit_no_wf3_record ───────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    conn = _fresh_db(_data)
    _seed_entity(conn)
    _seed_stock(conn, TODAY)
    # 不 seed wf3
    _data.set_current_tenant(TENANT_ID)

    result = _agent.tool_query_stock_split(SKU, "KSA")

    check("wf3 无记录 → ok=True（不因 wf3 缺失报错）",
          result.get("ok") is True,
          f"got ok={result.get('ok')!r}（wf3 缺失不应让整个查询失败）")
    check("wf3 无记录 → erp_in_transit=None",
          result.get("erp_in_transit") is None,
          f"got erp_in_transit={result.get('erp_in_transit')!r}")
    check("wf3 无记录 → erp_in_transit_unavailable 有说明",
          bool(result.get("erp_in_transit_unavailable")),
          f"got erp_in_transit_unavailable={result.get('erp_in_transit_unavailable')!r}")
    check("wf3 无记录 → wf1_stock 数据仍正常返回（dongguan 存在）",
          (result.get("split") or {}).get("dongguan") == DONGGUAN,
          f"got split.dongguan={result.get('split', {}).get('dongguan')!r}")


def test_erp_in_transit_stale_wf3_fail_closed() -> None:
    """wf3 数据超 3 天 → erp_in_transit=None（fail-closed），不静默出旧数。

    FAIL（改前）：字段不存在（或出旧数）。
    PASS（改后）：erp_in_transit=None，erp_in_transit_unavailable 含"超过"说明。
    """
    print("\n── test_erp_in_transit_stale_wf3_fail_closed ───────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    conn = _fresh_db(_data)
    _seed_entity(conn)
    _seed_stock(conn, TODAY)
    stale = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _seed_wf3(conn, stale)
    _data.set_current_tenant(TENANT_ID)

    result = _agent.tool_query_stock_split(SKU, "KSA")

    check("wf3 超 3 天 → erp_in_transit=None（fail-closed）",
          result.get("erp_in_transit") is None,
          f"got erp_in_transit={result.get('erp_in_transit')!r}（应为 None，不出旧数）")
    check("wf3 超 3 天 → erp_in_transit_unavailable 说明超期",
          bool(result.get("erp_in_transit_unavailable")),
          f"got erp_in_transit_unavailable={result.get('erp_in_transit_unavailable')!r}")
    check("wf3 超 3 天 → ok=True（wf1_stock 数据仍正常）",
          result.get("ok") is True,
          f"got ok={result.get('ok')!r}")


def test_dongguan_zero_still_has_key() -> None:
    """dongguan_qty=0（NULL）时 split 仍含 dongguan 键（值为 0），domestic=yiwu。"""
    print("\n── test_dongguan_zero_still_has_key ────────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    conn = _fresh_db(_data)
    _seed_entity(conn)
    # seed without dongguan → will be NULL in DB → fallback to 0
    overseas_breakdown = json.dumps({"沙特一号仓": OVERSEAS}, ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO wf1_stock "
        "(tenant_id, entity_alias, partner_sku, "
        " yiwu_qty, overseas_total_qty, overseas_breakdown_json, "
        " noon_total_qty, pending_inbound_qty, total_stock, "
        " updated_at, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, SKU,
         YIWU, OVERSEAS, overseas_breakdown,
         NOON, INBOUND, YIWU + OVERSEAS + NOON + INBOUND,
         TODAY, TODAY),
    )
    conn.commit()
    _data.set_current_tenant(TENANT_ID)

    result = _agent.tool_query_stock_split(SKU, "KSA")
    split = result.get("split") or {}

    check("split 含 dongguan 键（即使 NULL→0）",
          "dongguan" in split,
          "split 缺 dongguan 键")
    check("split.dongguan == 0（NULL 转 0）",
          split.get("dongguan") == 0,
          f"got split.dongguan={split.get('dongguan')!r}")
    check("split.domestic == yiwu（东莞为 0 时）",
          split.get("domestic") == YIWU,
          f"got split.domestic={split.get('domestic')!r}（应等于 yiwu={YIWU}）")


def main() -> int:
    print("smoke_t12b_tool_stock_split — T12 国内仓/在途拆分验收")

    test_t12_split_has_dongguan_and_domestic()
    test_erp_in_transit_from_wf3_not_in_total()
    test_erp_in_transit_no_wf3_record()
    test_erp_in_transit_stale_wf3_fail_closed()
    test_dongguan_zero_still_has_key()

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

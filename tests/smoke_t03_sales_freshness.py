"""smoke_t03_sales_freshness — T03 SKU 销量问答必须有取数证据 (fail-then-pass).

验收场景：
  1. tool_query_sku 必须在每个 SKU 结果里携带 data_as_of + stale_warn（取数证据）。
     改前这两个字段不存在 → 本测试 FAIL；改后存在 → PASS。
  2. stale_warn=True  当 imported_at 超过 _SKU_STALE_DAYS 天。
  3. stale_warn=False 当 imported_at 是今天。
  4. _safety.sanitize_reply：reply 含「总库存 N 件」且无 compute_replenishment/
     scope_overview/query_sku_live 工具调用 → 必须产生 T03 警告。
  5. reply 无库存数字 → 无 T03 警告（防误报）。

fail-then-pass 证明：
  - 改前 tool_query_sku 不含 data_as_of/stale_warn → 断言 FAIL
  - 改后含这两字段且按 imported_at 正确标 stale → PASS
  - 改前 sanitize_reply 不检查裸库存数字 → T03 断言 FAIL
  - 改后加 _check_bare_stock_number → PASS

跑法:
  python3 tests/smoke_t03_sales_freshness.py
  或 make test
"""
from __future__ import annotations

import os
import sys
import tempfile
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import hipop.server.* 前设好 SQLite 路径
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["HIPOP_DB"] = _TMP_DB
os.environ.pop("DB_URL", None)

from hipop.server import data as _data
from hipop.server import agent as _agent
from hipop.server._safety import sanitize_reply

_FAILURES: list[str] = []


def _fail(msg: str):
    _FAILURES.append(msg)
    print(f"  FAIL: {msg}")


def _ok(msg: str):
    print(f"  ok  : {msg}")


# ── 建一个最小 SQLite DB ──────────────────────────────────
def _setup_db(imported_at: str, sales_30d: int = 65, sales_180d: int = 662) -> None:
    """建好 wf2_sku + sales_entities，插入一行 TBS0228A 测试数据。"""
    _data.DB_PATH = _TMP_DB
    conn = sqlite3.connect(_TMP_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            country TEXT NOT NULL,
            platform TEXT,
            store_name TEXT,
            store_id TEXT,
            currency TEXT,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS wf2_sku (
            tenant_id INTEGER NOT NULL,
            entity_alias TEXT NOT NULL,
            partner_sku TEXT NOT NULL,
            title TEXT,
            sales_grade TEXT,
            latest_profit_rate REAL,
            sales_30d INTEGER,
            sales_10d INTEGER,
            sales_180d INTEGER,
            latest_price REAL,
            is_listed INTEGER,
            imported_at TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        );
        CREATE TABLE IF NOT EXISTS wf5_sales_cycle (
            tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
            trend TEXT, daily_rate REAL, urgency TEXT, ops_advice TEXT,
            risk_label TEXT, current_pipeline INTEGER, weekly_total_replenish INTEGER
        );
        CREATE TABLE IF NOT EXISTS wf3_logistics_hub_v2 (
            tenant_id INTEGER, partner_sku TEXT, sku TEXT,
            in_transit_total_qty INTEGER DEFAULT 0,
            has_stuck_batch INTEGER DEFAULT 0,
            needs_ops_input INTEGER DEFAULT 0,
            updated_at TEXT
        );
    """)
    # 销售主体
    conn.execute("DELETE FROM sales_entities")
    conn.execute(
        "INSERT OR REPLACE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (1,'hipop_ksa','SA','noon','HIPOP-NOON-KSA','ksa01','SAR')"
    )
    # SKU 行
    conn.execute("DELETE FROM wf2_sku WHERE tenant_id=1 AND entity_alias='hipop_ksa'")
    conn.execute(
        "INSERT INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, sales_30d, sales_10d, sales_180d, "
        " latest_price, latest_profit_rate, sales_grade, imported_at) "
        "VALUES (1,'hipop_ksa','TBS0228A','泳池测试SKU',?,5,?,12.0,0.15,'C',?)",
        (sales_30d, sales_180d, imported_at),
    )
    conn.commit()
    conn.close()


# ── Test 1: stale data — stale_warn must be True ──────────────────────────
print("\n▶ Test 1: stale wf2_sku data → stale_warn=True, data_as_of present")
stale_ts = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
_setup_db(stale_ts)
_data.set_current_tenant(1)

_agent._chat_tenant.set(1)
_agent._chat_scope.set({"store": "KSA"})
result = _agent.tool_query_sku(["TBS0228A"], "KSA")

items = result.get("items", [])
if not items:
    _fail("tool_query_sku returned empty items for TBS0228A")
else:
    item = items[0]
    if not item.get("found"):
        _fail("TBS0228A not found in DB")
    else:
        if "data_as_of" not in item:
            _fail("data_as_of field missing from tool_query_sku result (T03 fix not applied)")
        else:
            _ok(f"data_as_of present: {item['data_as_of']}")

        if "stale_warn" not in item:
            _fail("stale_warn field missing from tool_query_sku result (T03 fix not applied)")
        elif not item["stale_warn"]:
            _fail(f"stale_warn should be True for data imported 5 days ago (imported_at={stale_ts})")
        else:
            _ok(f"stale_warn=True correctly set for old data (imported_at={stale_ts})")

if "data_source_note" not in result:
    _fail("data_source_note missing from tool_query_sku top-level response")
else:
    _ok("data_source_note present in response")


# ── Test 2: fresh data — stale_warn must be False ─────────────────────────
print("\n▶ Test 2: fresh wf2_sku data → stale_warn=False")
fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_setup_db(fresh_ts, sales_30d=27, sales_180d=203)

result2 = _agent.tool_query_sku(["TBS0228A"], "KSA")
items2 = result2.get("items", [])
if not items2 or not items2[0].get("found"):
    _fail("TBS0228A not found for fresh data test")
else:
    item2 = items2[0]
    if item2.get("stale_warn"):
        _fail(f"stale_warn should be False for data imported today (imported_at={fresh_ts})")
    else:
        _ok(f"stale_warn=False correctly set for fresh data (imported_at={fresh_ts})")
    if item2.get("data_as_of") != fresh_ts:
        _fail(f"data_as_of mismatch: expected {fresh_ts!r}, got {item2.get('data_as_of')!r}")
    else:
        _ok(f"data_as_of matches import timestamp: {fresh_ts}")


# ── Test 3: missing imported_at → stale_warn=True ────────────────────────
print("\n▶ Test 3: NULL imported_at → stale_warn=True")
_setup_db(None)  # type: ignore[arg-type]
_setup_db.__doc__  # just to reference it

# Insert row with NULL imported_at
conn3 = sqlite3.connect(_TMP_DB)
conn3.execute(
    "UPDATE wf2_sku SET imported_at=NULL WHERE tenant_id=1 AND partner_sku='TBS0228A'"
)
conn3.commit()
conn3.close()

result3 = _agent.tool_query_sku(["TBS0228A"], "KSA")
items3 = result3.get("items", [])
if not items3 or not items3[0].get("found"):
    _fail("TBS0228A not found for NULL imported_at test")
else:
    item3 = items3[0]
    if not item3.get("stale_warn"):
        _fail("stale_warn should be True when imported_at is NULL")
    else:
        _ok("stale_warn=True when imported_at is NULL")
    if item3.get("data_as_of") is not None:
        _fail(f"data_as_of should be None when imported_at is NULL, got {item3.get('data_as_of')!r}")
    else:
        _ok("data_as_of=None when imported_at is NULL")


# ── Test 4: _safety detects bare total-stock hallucination ────────────────
print("\n▶ Test 4: sanitize_reply flags '总库存 888 件' without stock tool")
reply_with_stock = (
    "TBS0228A 数据如下：近 30 天销量 65 件，近 180 天销量 662 件，"
    "售价 SAR 12，利润率 15%。补充信息：目前总库存 888 件，在途 0 件。"
)
tools_no_stock = ["query_sku"]
sanitized, warnings = sanitize_reply(reply_with_stock, tools_no_stock, tool_log=[])

found_t03_warn = any("T03" in w or "总库存" in w or "库存" in w and "编造" in w for w in warnings)
if not found_t03_warn:
    _fail(
        "sanitize_reply should warn about '总库存 888 件' with no stock-source tool. "
        f"Got warnings: {warnings}"
    )
else:
    _ok(f"sanitize_reply correctly warns about bare stock number: {warnings[0][:80]}")


# ── Test 5: no false positive when no stock number ────────────────────────
print("\n▶ Test 5: sanitize_reply has no T03 false positive when no stock number")
reply_no_stock = (
    "TBS0228A 近 30 天销量 65 件（来自 2026-06-05 导入的 ERP 快照）。"
    "此数据可能非实时，建议刷新后确认。"
)
_, warnings5 = sanitize_reply(reply_no_stock, tools_no_stock, tool_log=[])
t03_warns5 = [w for w in warnings5 if "T03" in w or ("库存" in w and "编造" in w)]
if t03_warns5:
    _fail(f"False positive T03 warning on reply without stock number: {t03_warns5}")
else:
    _ok("no T03 false positive when reply has no bare stock number")


# ── Test 6: no false positive when stock tool was used ────────────────────
print("\n▶ Test 6: sanitize_reply no T03 warning when compute_replenishment was used")
tools_with_replenish = ["query_sku", "compute_replenishment"]
_, warnings6 = sanitize_reply(reply_with_stock, tools_with_replenish, tool_log=[])
t03_warns6 = [w for w in warnings6 if "T03" in w or ("总库存" in w and "编造" in w)]
if t03_warns6:
    _fail(f"False positive T03 warning when compute_replenishment tool was used: {t03_warns6}")
else:
    _ok("no T03 warning when compute_replenishment provides stock evidence")


# ── Result ────────────────────────────────────────────────────────────────
print()
if _FAILURES:
    print(f"✗ {len(_FAILURES)} failure(s):")
    for f in _FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print(f"✓ smoke_t03_sales_freshness: all {6} checks passed")

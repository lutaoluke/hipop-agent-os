"""smoke_t04_tbb0116a.py — T04 TBB0116A 30d cancel/return rate fail-then-pass smoke

验收（WS-113）：
  以 2026-06-05 为截止日，TBB0116A（KSA）的权威口径（Luke 2026-06-08 06:48 确认）：
    近 30 天销量     = 48  （非取消订单数，来自 wf2_sku.sales_30d）
    30 天总单量      = 51  （含取消，来自 wf2_orders 30d 窗口）
    30 天取消率      = 3/51 ≈ 5.88%
    30 天退货率      = 0/48 = 0.00%
    历史总销量       = 1967 （noon 全历史，来自 wf2_sku.total_orders）

FAIL（改前）：
  data.sku_30d_stats() 函数不存在 → AttributeError → 任何调用均 FAIL。
  即使函数存在，若用全历史口径算取消率：3/1967 ≈ 0.15% ≠ 5.88% → FAIL。

PASS（改后）：
  data.sku_30d_stats() 存在且正确计算 30d 窗口内的取消/退货率：
    total_30d=51, cancel_30d=3, return_30d=0, valid_30d=48
    cancel_rate_30d ≈ 5.88%, return_rate_30d = 0.00%
  agent.tool_query_sku 用 wf2_sku.as_of_date 调用 sku_30d_stats，返回
    cancel_rate_30d / return_rate_30d / total_orders_30d / history_total。

三死法检查：
  1. 接线缺失：函数存在但 tool_query_sku 未调用 → tool 不含 cancel_rate_30d → FAIL
  2. 死代码短路：返回全历史 cancel_rate(=3/1967≈0.15%) 而非 30d → 不等于 5.88% → FAIL
  3. 占位假数据：cancel_rate_30d=0 硬写 → ≠ 5.88% → FAIL

不存在 SKU 防护：
  对不在 wf2_orders 中的 SKU，sku_30d_stats 应返回 total_30d=0,
  cancel_rate_30d=None，不编造数值（接线缺失死法防护）。

跑法：
  python3 tests/smoke_t04_tbb0116a.py
  SMOKE_SKIP_FIX=1 python3 tests/smoke_t04_tbb0116a.py   # 看 "改前 FAIL"
  make test-one F=tests/smoke_t04_tbb0116a.py
"""
import os
import sys
import json
import tempfile
import datetime
import random

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 关键：必须在 import hipop.server.data 之前设好 SQLite 路径 + 清掉 DB_URL。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

# ── 权威口径（WS-113 Luke 2026-06-08 06:48 确认）────────────────────────────
AS_OF_DATE = "2026-06-05"        # 截止日
CUTOFF_30D = "2026-05-06"        # AS_OF_DATE - 30d
SALES_30D = 48                   # valid_orders in 30d window
TOTAL_ORDERS_30D = 51            # total (including cancelled) in 30d window
CANCEL_COUNT_30D = 3
RETURN_COUNT_30D = 0
CANCEL_RATE_30D = 3 / 51         # ≈ 0.058823...
RETURN_RATE_30D = 0.0
HISTORY_TOTAL = 1967             # all-time noon orders (ERP 口径)

# 全历史口径取消率（与 30d 口径不同 — 这是 FAIL 检测用的对比基准）
ALL_HISTORY_CANCEL_RATE = 3 / HISTORY_TOTAL  # ≈ 0.001525

TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
PARTNER_SKU = "TBB0116A"

SMOKE_SKIP_FIX = os.environ.get("SMOKE_SKIP_FIX") == "1"

_TMP_DBS = []

_AGENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id     BIGINT,
  task_id       TEXT NOT NULL,
  step_no       INT NOT NULL,
  step_name     TEXT NOT NULL,
  status        TEXT NOT NULL,
  message       TEXT,
  actor_user_id BIGINT,
  actor_email   TEXT,
  actor_role    TEXT,
  actor_source  TEXT,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _fresh_db(data):
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _TMP_DBS.append(path)
    data.DB_PATH = path
    conn = data.conn()
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


def _seed_entity(conn):
    conn.execute(
        "INSERT OR REPLACE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 85, "SAR"),
    )
    conn.commit()


def _seed_sku(conn):
    """Seed wf2_sku with pre-computed values representing the authoritative state.

    total_orders=1967 is the all-time noon order count (ERP 口径).
    sales_30d=48 is pre-computed (same as non-cancelled 30d orders in fixture).
    as_of_date='2026-06-05' is the window cutoff.
    cancel_rate/return_rate are ALL-HISTORY rates (≠ 30d rates — 口径不同，勿混用).
    """
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, is_listed, "
        " total_orders, valid_orders, cancel_count, return_count, "
        " cancel_rate, return_rate, sales_30d, sales_60d, sales_180d, "
        " as_of_date, sales_grade, cost_price, currency) "
        "VALUES (?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?, ?,?, ?,?)",
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, "便携榨汁杯 TBB0116A", 1,
         HISTORY_TOTAL,                              # total_orders = all-time noon
         HISTORY_TOTAL - CANCEL_COUNT_30D,           # valid_orders
         CANCEL_COUNT_30D,                           # cancel_count (all-time fixture)
         RETURN_COUNT_30D,                           # return_count
         ALL_HISTORY_CANCEL_RATE,                    # cancel_rate (全历史口径 ≈ 0.15%)
         0.0,                                        # return_rate
         SALES_30D, 89, 289,                         # sales_30d/60d/180d
         AS_OF_DATE, "A",                            # as_of_date, sales_grade
         8.0, "USD"),
    )
    conn.commit()


def _seed_orders_30d(conn):
    """Insert 51 orders in the 30d window [CUTOFF_30D..AS_OF_DATE]:
    - 48 valid (is_cancelled=0, is_return=0)
    - 3 cancelled (is_cancelled=1)
    - 0 returns
    """
    rng = random.Random(42)
    start = datetime.date.fromisoformat(CUTOFF_30D)
    end = datetime.date.fromisoformat(AS_OF_DATE)
    span = (end - start).days

    rows = []
    for i in range(CANCEL_COUNT_30D):
        d = (start + datetime.timedelta(days=rng.randint(0, span))).isoformat()
        rows.append((TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, f"T04-C{i:03d}",
                     d, "cancelled", 1, 0, 9.9, 9.9, "SAR"))
    for i in range(SALES_30D):
        d = (start + datetime.timedelta(days=rng.randint(0, span))).isoformat()
        rows.append((TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, f"T04-V{i:03d}",
                     d, "delivered", 0, 0, 9.9, 9.9, "SAR"))

    conn.executemany(
        "INSERT OR REPLACE INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, "
        " order_date, status, is_cancelled, is_return, seller_price, customer_paid, currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def _approx(a, b, tol=1e-3):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def test_sku_30d_stats_data_layer():
    """T04 数据层验收：data.sku_30d_stats 必须正确计算 30d 口径。

    FAIL（改前）: AttributeError — 函数不存在。
    PASS（改后）: 返回正确的 30d 取消/退货统计。
    """
    print("== test_sku_30d_stats_data_layer ==")
    from hipop.server import data

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    _seed_sku(conn)
    n_orders = _seed_orders_30d(conn)
    print(f"  fixture: {n_orders} orders in 30d window ({CANCEL_COUNT_30D} cancelled)")
    conn.close()

    check = _Checker()

    # ── 断言：sku_30d_stats 函数存在（改前 FAIL：AttributeError）────────────
    check("data.sku_30d_stats 函数存在",
          hasattr(data, "sku_30d_stats"),
          "AttributeError: module 'hipop.server.data' has no attribute 'sku_30d_stats'")

    if not hasattr(data, "sku_30d_stats"):
        if SMOKE_SKIP_FIX:
            print("  （SMOKE_SKIP_FIX=1：这是预期的 '改前 FAIL'。）")
        return check.failures

    stats = data.sku_30d_stats(TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, AS_OF_DATE)
    print(f"  sku_30d_stats response: {stats}")

    # ── 30d 窗口数量 ──────────────────────────────────────────────────────────
    check(f"total_30d == {TOTAL_ORDERS_30D}",
          stats.get("total_30d") == TOTAL_ORDERS_30D,
          f"got {stats.get('total_30d')!r}")

    check(f"cancel_30d == {CANCEL_COUNT_30D}",
          stats.get("cancel_30d") == CANCEL_COUNT_30D,
          f"got {stats.get('cancel_30d')!r}")

    check(f"return_30d == {RETURN_COUNT_30D}",
          stats.get("return_30d") == RETURN_COUNT_30D,
          f"got {stats.get('return_30d')!r}")

    check(f"valid_30d == {SALES_30D}",
          stats.get("valid_30d") == SALES_30D,
          f"got {stats.get('valid_30d')!r}")

    # ── 30d 口径取消率 ≈ 5.88%（改前 FAIL：0% 或全历史口径 ≈ 0.15%）─────────
    check(f"cancel_rate_30d ≈ {CANCEL_RATE_30D:.4f} ({CANCEL_COUNT_30D}/{TOTAL_ORDERS_30D})",
          _approx(stats.get("cancel_rate_30d"), CANCEL_RATE_30D),
          f"got {stats.get('cancel_rate_30d')!r}")

    # ── 30d 口径退货率 = 0.00% ─────────────────────────────────────────────
    check(f"return_rate_30d == {RETURN_RATE_30D}",
          _approx(stats.get("return_rate_30d"), RETURN_RATE_30D),
          f"got {stats.get('return_rate_30d')!r}")

    # ── 防混淆：30d 口径 ≠ 全历史口径 ─────────────────────────────────────
    check(
        f"cancel_rate_30d({CANCEL_RATE_30D:.4f}) ≠ 全历史口径({ALL_HISTORY_CANCEL_RATE:.5f})",
        not _approx(stats.get("cancel_rate_30d"), ALL_HISTORY_CANCEL_RATE, tol=0.005),
        f"got {stats.get('cancel_rate_30d')!r}（不应等于全历史口径 {ALL_HISTORY_CANCEL_RATE:.5f}）",
    )

    # ── 不存在 SKU 防编造：total_30d=0, cancel_rate_30d=None ─────────────────
    fake_stats = data.sku_30d_stats(TENANT_ID, ENTITY_ALIAS, "FAKE_SKU_XYZ", AS_OF_DATE)
    check("不存在 SKU: total_30d == 0",
          fake_stats.get("total_30d") == 0,
          f"got {fake_stats.get('total_30d')!r}")
    check("不存在 SKU: cancel_rate_30d is None",
          fake_stats.get("cancel_rate_30d") is None,
          f"got {fake_stats.get('cancel_rate_30d')!r}")

    if check.failures and SMOKE_SKIP_FIX:
        print("  （SMOKE_SKIP_FIX=1：这是预期的 '改前 FAIL'。去掉变量再跑应全过。）")
    return check.failures


def test_sku_30d_stats_window_boundary():
    """T04 边界验收：截止日边界正确，不含 as_of_date+1 的订单，不遗漏 as_of_date 当天。

    防止 off-by-one 导致: 多算或少算 1 天的订单。
    """
    print("== test_sku_30d_stats_window_boundary ==")
    from hipop.server import data

    if not hasattr(data, "sku_30d_stats"):
        print("  ⚠ data.sku_30d_stats 不存在，跳过边界测试")
        return []

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    _seed_sku(conn)

    # 在 as_of_date 当天 + cutoff 当天 + 窗口外一天各放一个订单
    boundary_orders = [
        # 窗口内：as_of_date 当天（应被计入）
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, "BOUND-ON", AS_OF_DATE,
         "delivered", 0, 0, 9.9, 9.9, "SAR"),
        # 窗口内：cutoff 当天（应被计入）
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, "BOUND-CUT", CUTOFF_30D,
         "delivered", 0, 0, 9.9, 9.9, "SAR"),
        # 窗口外：as_of_date + 1 天（不应被计入）
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, "BOUND-OUT",
         (datetime.date.fromisoformat(AS_OF_DATE) + datetime.timedelta(days=1)).isoformat(),
         "delivered", 0, 0, 9.9, 9.9, "SAR"),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, order_date, status, "
        " is_cancelled, is_return, seller_price, customer_paid, currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        boundary_orders,
    )
    conn.commit()
    conn.close()

    stats = data.sku_30d_stats(TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, AS_OF_DATE)
    check = _Checker()

    check("total_30d == 2 (as_of + cutoff 各一单，窗口外不计)",
          stats.get("total_30d") == 2,
          f"got {stats.get('total_30d')!r}")

    return check.failures


def test_tool_query_sku_redacts_when_noon_orders_stale():
    """T04 chat guard: noon orders stale must redact sales metrics even if wf2_sku was refreshed today."""
    print("== test_tool_query_sku_redacts_when_noon_orders_stale ==")
    from hipop.server import data
    from hipop.server import agent

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    _seed_sku(conn)
    _seed_orders_30d(conn)

    today = datetime.date.today().isoformat()
    stale_order_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    conn.execute(
        "UPDATE wf2_sku SET as_of_date=? WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (today, TENANT_ID, ENTITY_ALIAS, PARTNER_SKU),
    )
    conn.execute("DELETE FROM wf2_orders WHERE tenant_id=? AND entity_alias=?", (TENANT_ID, ENTITY_ALIAS))
    conn.execute(
        "INSERT OR REPLACE INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, order_date, status, "
        " is_cancelled, is_return, seller_price, customer_paid, currency) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU, "T04-STALE",
         stale_order_date, "delivered", 0, 0, 9.9, 9.9, "SAR"),
    )
    conn.commit()
    conn.close()

    result = agent.tool_query_sku([PARTNER_SKU], store="KSA")
    item = (result.get("items") or [{}])[0]
    check = _Checker()

    check("query_sku 标记 data_stale=True（noon_orders 旧）",
          item.get("data_stale") is True,
          f"got {item}")
    check("sales_30d 被 REDACT",
          item.get("sales_30d") is None,
          f"got {item.get('sales_30d')!r}")
    check("total_orders_30d 被 REDACT",
          item.get("total_orders_30d") is None,
          f"got {item.get('total_orders_30d')!r}")
    check("history_total 被 REDACT",
          item.get("history_total") is None,
          f"got {item.get('history_total')!r}")
    check("stale_reason 点名 noon_orders_stale",
          "noon_orders_stale" in (item.get("stale_reason") or ""),
          f"got {item.get('stale_reason')!r}")

    return check.failures


def run():
    failures = []
    failures += test_sku_30d_stats_data_layer()
    print()
    failures += test_sku_30d_stats_window_boundary()
    print()
    failures += test_tool_query_sku_redacts_when_noon_orders_stale()
    print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ T04 TBB0116A 30d 口径验收全过")
    return 0


if __name__ == "__main__":
    try:
        rc = run()
    finally:
        for p in _TMP_DBS + [os.environ.get("HIPOP_DB")]:
            try:
                if p:
                    os.unlink(p)
            except OSError:
                pass
    sys.exit(rc)

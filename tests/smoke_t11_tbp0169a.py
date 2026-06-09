"""smoke_t11_tbp0169a.py — T11 TBP0169A 四仓库存拆分 fail-then-pass smoke

验收（WS-140）：
  TBP0169A（KSA）四仓拆分 G+O 口径（WS-83 benchmark 20260608）：
    义乌(yiwu)           = 42
    沙特一号仓(overseas_saudi_1) = 355
    noon仓               = 21
    在途(inbound)        = 0
    合计(total)          = 418

FAIL（改前）：
  tool_query_stock_split 函数不存在 → AttributeError → 任何调用均 FAIL。
  即使有函数，若只返回 overseas_total 不拆 saudi_1，或缺 noon 字段 → FAIL。

PASS（改后）：
  tool_query_stock_split 存在且：
    - split.yiwu == 42
    - split.overseas_saudi_1 == 355
    - split.noon == 21
    - split.inbound == 0
    - total == 418
    - updated_at 存在（时间戳返回）
    - ok=True, fail_closed=False

三死法检查：
  1. 拆分缺失：noon 字段被省略 → split 无 noon 键 → FAIL
  2. 死代码短路：仍走旧 TopN LIMIT 3 路径 → 不返回四仓拆分 → FAIL
  3. 占位假数据：编近似数字而非读 wf1_stock → fixture 值不匹配 → FAIL

新鲜度门检查：
  - updated_at 超过 3 天 → fail_closed=True → FAIL 如果仍返回数字
  - ≤3天 → stale_warn 提示存在
  - SKU 不存在 → fail_closed=True

跑法：
  python3 tests/smoke_t11_tbp0169a.py
  SMOKE_SKIP_FIX=1 python3 tests/smoke_t11_tbp0169a.py   # 看 "改前 FAIL"
  make test-one F=tests/smoke_t11_tbp0169a.py
"""
import os
import sys
import json
import tempfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

# ── 权威口径（WS-83 G+O benchmark 20260608）─────────────────────────────────
TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
PARTNER_SKU = "TBP0169A"
YIWU = 42
OVERSEAS_SAUDI_1 = 355
NOON = 21
INBOUND = 0
TOTAL = YIWU + OVERSEAS_SAUDI_1 + NOON + INBOUND  # 418

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


def _seed_stock(conn, updated_at: str):
    """Seed wf1_stock with G+O benchmark values."""
    overseas_breakdown = json.dumps({"沙特一号仓": OVERSEAS_SAUDI_1}, ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO wf1_stock "
        "(tenant_id, entity_alias, partner_sku, "
        " yiwu_qty, overseas_total_qty, overseas_breakdown_json, "
        " noon_total_qty, pending_inbound_qty, total_stock, "
        " updated_at, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, PARTNER_SKU,
         YIWU, OVERSEAS_SAUDI_1, overseas_breakdown,
         NOON, INBOUND, TOTAL,
         updated_at, updated_at),
    )
    conn.commit()


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def test_tool_exists():
    """T11 接线检查：tool_query_stock_split 函数必须存在并已注册。

    FAIL（改前）: AttributeError — 函数不存在。
    """
    print("== test_tool_exists ==")
    from hipop.server import agent

    check = _Checker()
    check("tool_query_stock_split 函数存在",
          hasattr(agent, "tool_query_stock_split"),
          "AttributeError: module has no attribute 'tool_query_stock_split'")
    check("query_stock_split 已注册在 TOOL_FUNCS",
          "query_stock_split" in agent.TOOL_FUNCS,
          "TOOL_FUNCS 缺少 query_stock_split")
    check("query_stock_split tool schema 存在于 TOOLS",
          any(t.get("name") == "query_stock_split" for t in agent.TOOLS),
          "TOOLS 列表缺少 query_stock_split schema")

    if SMOKE_SKIP_FIX:
        print("  （SMOKE_SKIP_FIX=1：这是预期的 '改前 FAIL'。）")
    return check.failures


def test_stock_split_benchmark():
    """T11 核心验收：四仓拆分 G+O 口径（WS-83 benchmark 20260608）。

    FAIL（改前）: noon 字段缺失 / 总量错误 / 函数不存在。
    PASS（改后）: yiwu=42, overseas_saudi_1=355, noon=21, inbound=0, total=418。
    """
    print("== test_stock_split_benchmark ==")
    from hipop.server import agent, data

    if not hasattr(agent, "tool_query_stock_split"):
        print("  ⚠ tool_query_stock_split 不存在，跳过（见 test_tool_exists）")
        return ["tool_query_stock_split 不存在"]

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    today = datetime.date.today().isoformat()
    _seed_stock(conn, today)
    conn.close()

    result = agent.tool_query_stock_split(PARTNER_SKU, "KSA")
    print(f"  tool result: {json.dumps(result, ensure_ascii=False, indent=2)}")

    check = _Checker()
    check("ok=True", result.get("ok") is True, f"got ok={result.get('ok')!r}")
    check("fail_closed=False", result.get("fail_closed") is False, f"got fail_closed={result.get('fail_closed')!r}")

    split = result.get("split") or {}
    check(f"split.yiwu == {YIWU}", split.get("yiwu") == YIWU, f"got {split.get('yiwu')!r}")
    check(f"split.overseas_saudi_1 == {OVERSEAS_SAUDI_1}",
          split.get("overseas_saudi_1") == OVERSEAS_SAUDI_1,
          f"got {split.get('overseas_saudi_1')!r}")
    check(f"split.noon == {NOON}", split.get("noon") == NOON, f"got {split.get('noon')!r}")
    check(f"split.inbound == {INBOUND}", split.get("inbound") == INBOUND, f"got {split.get('inbound')!r}")
    check(f"total == {TOTAL}", result.get("total") == TOTAL, f"got {result.get('total')!r}")
    check("updated_at 存在", bool(result.get("updated_at")), f"got {result.get('updated_at')!r}")
    check("noon_missing == False", result.get("noon_missing") is False,
          f"got noon_missing={result.get('noon_missing')!r}")

    # 死法#1: noon 字段存在于 split dict（不得被省略）
    check("split dict 含 noon 键（不得省略）", "noon" in split,
          "split 缺少 noon 键 → 接线缺失死法")
    # 死法#1: split dict 含全部四仓键
    for key in ("yiwu", "overseas_saudi_1", "noon", "inbound"):
        check(f"split 含 {key} 键", key in split, f"split 缺少 {key} → 接线缺失死法")

    if SMOKE_SKIP_FIX and check.failures:
        print("  （SMOKE_SKIP_FIX=1：这是预期的 '改前 FAIL'。）")
    return check.failures


def test_freshness_gate_fail_closed():
    """T11 新鲜度门：updated_at 超过 3 天 → fail_closed=True，不出数字。

    FAIL（改前）: 无新鲜度门，仍返回数字。
    PASS（改后）: fail_closed=True + message，split 字段不存在或为 None。
    """
    print("== test_freshness_gate_fail_closed ==")
    from hipop.server import agent, data

    if not hasattr(agent, "tool_query_stock_split"):
        print("  ⚠ tool_query_stock_split 不存在，跳过")
        return []

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    stale_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _seed_stock(conn, stale_date)
    conn.close()

    result = agent.tool_query_stock_split(PARTNER_SKU, "KSA")
    print(f"  stale result: ok={result.get('ok')}, fail_closed={result.get('fail_closed')}")

    check = _Checker()
    check("超 3 天 → fail_closed=True",
          result.get("fail_closed") is True,
          f"got fail_closed={result.get('fail_closed')!r}")
    check("超 3 天 → ok=False",
          result.get("ok") is not True,
          "数据超期仍返回 ok=True → 占位假数据死法")
    check("message 存在",
          bool(result.get("message")),
          "fail_closed 但无 message → 用户不知道为何拒绝")

    return check.failures


def test_freshness_gate_stale_warn():
    """T11 降级门：updated_at 1-3 天前 → 返回数据 + stale_warn。"""
    print("== test_freshness_gate_stale_warn ==")
    from hipop.server import agent, data

    if not hasattr(agent, "tool_query_stock_split"):
        print("  ⚠ tool_query_stock_split 不存在，跳过")
        return []

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    two_days_ago = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    _seed_stock(conn, two_days_ago)
    conn.close()

    result = agent.tool_query_stock_split(PARTNER_SKU, "KSA")
    print(f"  2-day-stale result: ok={result.get('ok')}, stale_warn={result.get('stale_warn')!r}")

    check = _Checker()
    check("2天前 → ok=True（降级返回数据）",
          result.get("ok") is True,
          f"got ok={result.get('ok')!r}")
    check("2天前 → stale_warn 存在",
          bool(result.get("stale_warn")),
          "降级数据应有 stale_warn 提示用户确认")
    check("2天前 → split 完整",
          bool((result.get("split") or {}).get("noon") is not None),
          "降级数据 split 不完整")

    return check.failures


def test_no_data_fail_closed():
    """T11 无缓存门：SKU 不存在 → fail_closed=True。"""
    print("== test_no_data_fail_closed ==")
    from hipop.server import agent, data

    if not hasattr(agent, "tool_query_stock_split"):
        print("  ⚠ tool_query_stock_split 不存在，跳过")
        return []

    data.set_current_tenant(TENANT_ID)
    conn = _fresh_db(data)
    _seed_entity(conn)
    # 不 seed 任何库存数据
    conn.close()

    result = agent.tool_query_stock_split("FAKE_SKU_XYZ", "KSA")
    print(f"  no-data result: fail_closed={result.get('fail_closed')}, message={result.get('message')!r}")

    check = _Checker()
    check("无缓存 → fail_closed=True",
          result.get("fail_closed") is True,
          f"got fail_closed={result.get('fail_closed')!r}")

    return check.failures


def run():
    failures = []
    failures += test_tool_exists()
    print()
    failures += test_stock_split_benchmark()
    print()
    failures += test_freshness_gate_fail_closed()
    print()
    failures += test_freshness_gate_stale_warn()
    print()
    failures += test_no_data_fail_closed()
    print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ T11 TBP0169A 四仓库存拆分验收全过")
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

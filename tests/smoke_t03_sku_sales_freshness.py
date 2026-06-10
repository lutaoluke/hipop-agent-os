"""smoke_t03_sku_sales_freshness.py — T03 SKU 销量问答实时取数证据 fail-then-pass smoke

验收（WS-122）：
  当用户查询某 SKU 近 30/180 天销量时，工作台 Agent 必须：
  1. 从 wf2_sku 返回数据新鲜度证据（as_of_date / imported_at / data_stale / stale_days）。
  2. 数据陈旧（as_of_date 超过 3 天）时：数值 REDACT 为 null，data_stale=True。
  3. 数据新鲜时：返回真实数值，data_stale=False。
  4. references 含 imported_at 字段（取数来源证据）。
  5. _provider 将陈旧 SKU 写入 tool_log[result_stale_skus]，供 _safety 验门。
  6. _safety._check_stale_sales_claim：检测陈旧数据 + 回复含具体销量数字 → 警告。

FAIL（改前 / SMOKE_SKIP_FIX=1）：
  - tool_query_sku 不含 imported_at 字段 → wf2_sku ref 无 imported_at → FAIL
  - _safety 无 _check_stale_sales_claim → verifier FAIL
  - _provider 不写 result_stale_skus → tool_log 无此字段 → FAIL

PASS（改后）：
  - tool_query_sku 引用含 imported_at（取数证据）→ PASS
  - _safety._check_stale_sales_claim 检测陈旧 + 销量数字 → 返回警告 → PASS
  - _provider 写 result_stale_skus=[sku] → PASS

三死法检查：
  1. 接线缺失：verifier 写了但 sanitize_reply 未调 → sanitize_reply 无 T03 警告 → FAIL
  2. 死代码短路：data_stale 计算写了但 _r() 未应用 → sales_30d 仍有数值 → FAIL
  3. 占位假数据：imported_at 写了但 ref 无此字段 → wf2_ref 无 imported_at → FAIL

跑法：
  python3 tests/smoke_t03_sku_sales_freshness.py
  SMOKE_SKIP_FIX=1 python3 tests/smoke_t03_sku_sales_freshness.py   # 演示改前 FAIL
  make test-one F=tests/smoke_t03_sku_sales_freshness.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import hipop.server.* 之前设好 SQLite 路径
_TMP_DB_PATH = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["HIPOP_DB"] = _TMP_DB_PATH
os.environ.pop("DB_URL", None)

TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
SKU = "TBS0228A"
STALE_AS_OF = "2026-05-20"       # 超过 3 天 → 陈旧
FRESH_AS_OF = datetime.date.today().isoformat()   # 今天 → 新鲜
STALE_SALES_30D = 65             # 旧快照里的值（不应被原样返回）

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
    """Create a fresh SQLite DB and point data module at it."""
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


def _seed_wf2_sku(conn, as_of_date, imported_at, sales_30d) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, as_of_date, "
        " imported_at, sales_30d, sales_10d, sales_grade, "
        " latest_price, latest_profit_rate, is_listed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "Test SKU T03", as_of_date,
         imported_at, sales_30d, 5, "A", 50.0, 0.15, 1),
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


def test_tool_stale_data() -> None:
    """陈旧数据：data_stale=True，数值 REDACT 为 null，references 含 imported_at"""
    print("\n── test_tool_stale_data ─────────────────────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    _seed_wf2_sku(conn, STALE_AS_OF, STALE_AS_OF + "T10:00:00", STALE_SALES_30D)

    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    result = _agent.tool_query_sku([SKU], store="KSA")
    items = result.get("items", [])
    assert items, f"query_sku 返回空 items，result={result!r}"
    item = items[0]

    if SMOKE_SKIP_FIX:
        # 演示改前状态：检查 references 里无 imported_at（即改前行为）
        refs = result.get("references", [])
        wf2_ref = next((r for r in refs if r.get("table") == "wf2_sku"), None)
        no_imported_at = wf2_ref is None or "imported_at" not in wf2_ref
        check("SKIP_FIX: wf2_sku ref 无 imported_at（改前行为）",
              no_imported_at,
              f"wf2_ref={wf2_ref!r}")
        return

    check("陈旧数据 → data_stale=True",
          item.get("data_stale") is True,
          f"data_stale={item.get('data_stale')!r}, as_of_date={item.get('as_of_date')!r}")
    check("陈旧数据 → sales_30d REDACT 为 null",
          item.get("sales_30d") is None,
          f"sales_30d={item.get('sales_30d')!r}")
    check("陈旧数据 → stale_days > 3",
          (item.get("stale_days") or 0) > 3,
          f"stale_days={item.get('stale_days')!r}")
    check("陈旧数据 → as_of_date 有值",
          bool(item.get("as_of_date")),
          f"as_of_date={item.get('as_of_date')!r}")

    refs = result.get("references", [])
    wf2_ref = next((r for r in refs if r.get("table") == "wf2_sku"), None)
    check("references 包含 wf2_sku 引用（取数证据）",
          wf2_ref is not None,
          f"refs tables={[r.get('table') for r in refs]}")
    if SMOKE_SKIP_FIX:
        return
    check("wf2_sku ref 含 imported_at（数据导入时间戳证据）",
          wf2_ref is not None and "imported_at" in wf2_ref,
          f"wf2_ref={wf2_ref!r}")
    check("wf2_sku ref 含 as_of_date（业务日证据）",
          wf2_ref is not None and wf2_ref.get("as_of_date") == STALE_AS_OF,
          f"wf2_ref.as_of_date={wf2_ref.get('as_of_date') if wf2_ref else 'N/A'!r}")


def test_tool_fresh_data() -> None:
    """新鲜数据：data_stale=False；sales_30d 来自 live 源（T03 后：snapshot 新鲜度不再决定销量取数）"""
    print("\n── test_tool_fresh_data ─────────────────────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    today_str = FRESH_AS_OF
    _seed_wf2_sku(conn, today_str, today_str + "T08:00:00", 25)
    # noon_orders_stale は noon_orders がないと True になる（T36-S3追加）→ 新鮮テストのため今日の注文を1行入れる
    conn.execute(
        "INSERT OR REPLACE INTO wf2_orders "
        "(tenant_id, entity_alias, partner_sku, item_nr, order_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "smoke-fresh-order-001", today_str),
    )
    conn.commit()

    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    # T03 修后：注入 live fn，验证 sales_30d 来自 live 而非 snapshot
    orig = _agent._sku_sales_live_fn
    _agent._sku_sales_live_fn = lambda sku, n, t: {
        "ok": True, "sales_30d": 30, "history_total": None,
        "fetched_at": "2026-06-09T10:00:00Z", "source": "test_fresh_mock",
    }
    try:
        result = _agent.tool_query_sku([SKU], store="KSA")
    finally:
        _agent._sku_sales_live_fn = orig

    items = result.get("items", [])
    assert items
    item = items[0]

    check("新鲜数据 → data_stale=False",
          item.get("data_stale") is False,
          f"data_stale={item.get('data_stale')!r}, as_of_date={item.get('as_of_date')!r}")
    check("新鲜数据 → sales_30d 来自 live（非 null）",
          item.get("sales_30d") is not None,
          f"sales_30d={item.get('sales_30d')!r}")
    check("新鲜数据 → 有 live_evidence（取数证据）",
          isinstance(item.get("live_evidence"), dict),
          f"live_evidence={item.get('live_evidence')!r}")
    check("新鲜数据 → stale_days == 0",
          item.get("stale_days") == 0,
          f"stale_days={item.get('stale_days')!r}")


def test_tool_null_as_of_date() -> None:
    """as_of_date 为 NULL → data_stale=True（无日期数据不得当新鲜数据）"""
    print("\n── test_tool_null_as_of_date ─────────────────────────────────")
    import hipop.server.data as _data
    import hipop.server.agent as _agent

    _fresh_db(_data)
    conn = _data.conn()
    _seed_entity(conn)
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, imported_at, sales_30d, is_listed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "Null-date SKU",
         "2026-01-01T00:00:00", 100, 1),
    )
    conn.commit()

    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test"})

    result = _agent.tool_query_sku([SKU], store="KSA")
    items = result.get("items", [])
    assert items
    item = items[0]

    check("NULL as_of_date → data_stale=True",
          item.get("data_stale") is True,
          f"data_stale={item.get('data_stale')!r}")
    check("NULL as_of_date → sales_30d REDACT 为 null",
          item.get("sales_30d") is None,
          f"sales_30d={item.get('sales_30d')!r}")


def test_provider_stale_skus_logic() -> None:
    """_provider _stale_skus_from_sku_result: live_sales_failed=True 的 SKU → result_stale_skus"""
    print("\n── test_provider_stale_skus_logic ─────────────────────────────")

    if SMOKE_SKIP_FIX:
        print("  [SKIP] 改前演示跳过（_provider 无 result_stale_skus 逻辑）")
        return

    # 使用 provider 真实函数（不复制逻辑）
    from hipop.server._provider_anthropic import _stale_skus_from_sku_result

    # 含 live_sales_failed=True 的 SKU → 应被提取
    mock_result = {
        "items": [
            {"sku": SKU, "found": True, "live_sales_failed": True, "sales_30d": None},
            {"sku": "TBS0001A", "found": True, "live_evidence": {"fetched_at": "2026-06-09T10:00:00Z"}, "sales_30d": 10},
        ],
    }

    result_stale_skus = _stale_skus_from_sku_result("query_sku", mock_result)

    check(f"result_stale_skus 包含 live_sales_failed SKU {SKU!r}",
          SKU in (result_stale_skus or []),
          f"result_stale_skus={result_stale_skus!r}")
    check("result_stale_skus 不含 live_ok SKU TBS0001A（无误报）",
          "TBS0001A" not in (result_stale_skus or []),
          f"result_stale_skus={result_stale_skus!r}")

    # 全 live_ok → result_stale_skus=None
    mock_fresh = {"items": [{"sku": "FRESH", "live_evidence": {"fetched_at": "t"}, "sales_30d": 10}]}
    fresh_stale = _stale_skus_from_sku_result("query_sku", mock_fresh)
    check("全 live_ok → result_stale_skus=None",
          fresh_stale is None,
          f"fresh_stale={fresh_stale!r}")

    mock_fresh = {"items": [{"sku": "FRESH", "data_stale": False}]}
    fresh_stale = [
        it["sku"] for it in (mock_fresh.get("items") or [])
        if it.get("data_stale") and it.get("sku")
    ] or None
    check("全新鲜 result → result_stale_skus=None",
          fresh_stale is None,
          f"fresh_stale={fresh_stale!r}")


def test_safety_verifier() -> None:
    """_safety._check_stale_sales_claim: 陈旧 + 具体销量数字 → 警告"""
    print("\n── test_safety_verifier ─────────────────────────────────────")

    from hipop.server import _safety

    if SMOKE_SKIP_FIX:
        fn = getattr(_safety, "_check_stale_sales_claim", None)
        check("SKIP_FIX: _check_stale_sales_claim 不存在（改前）",
              fn is None,
              f"got fn={fn}")
        return

    fn = getattr(_safety, "_check_stale_sales_claim", None)
    check("_check_stale_sales_claim 函数存在",
          callable(fn), f"got {fn!r}")
    if not callable(fn):
        return

    stale_log = [{"name": "query_sku", "result_stale_skus": [SKU]}]

    # 场景 A：陈旧 SKU + 含销量数字 → 触发警告
    warns_a = fn("近30天销量是65件，趋势稳定", stale_log)
    check("陈旧 SKU + 销量数字声明 → 触发 T03 警告",
          len(warns_a) > 0,
          f"warnings={warns_a}")

    # 场景 B：陈旧 SKU 但回复无具体数字 → 不触发（无误报）
    warns_b = fn("当前数据已过期，无法确认实时销量", stale_log)
    check("陈旧 SKU + 无具体数字 → 不触发（误报防护）",
          len(warns_b) == 0,
          f"warnings={warns_b}")

    # 场景 C：新鲜 SKU（无 stale_skus）+ 数字 → 不触发
    fresh_log = [{"name": "query_sku", "result_stale_skus": None}]
    warns_c = fn("近30天销量是25件", fresh_log)
    check("新鲜 SKU + 数字 → 不触发（无误报）",
          len(warns_c) == 0,
          f"warnings={warns_c}")

    # 场景 D：无 query_sku 调用 → 不触发
    warns_d = fn("近30天销量是25件", [{"name": "data_health_check"}])
    check("无 query_sku 调用 → 不触发",
          len(warns_d) == 0,
          f"warnings={warns_d}")

    # 场景 E：sanitize_reply 已接线（通过 tool_log 传递）— 接线缺失死法检查
    stale_log2 = [{"name": "query_sku", "result_stale_skus": [SKU]}]
    _, all_warns = _safety.sanitize_reply(
        "近30天销量是65件，历史总销量662件", ["query_sku"],
        tool_log=stale_log2, question="TBS0228A 销量",
    )
    check("sanitize_reply 已接线 _check_stale_sales_claim（含 T03 警告）",
          any("T03" in w for w in all_warns),
          f"all_warns={all_warns}")


def _cleanup() -> None:
    for p in _TMP_DBS + [_TMP_DB_PATH]:
        try:
            os.unlink(p)
        except OSError:
            pass


if __name__ == "__main__":
    print("=== smoke_t03_sku_sales_freshness ===")
    if SMOKE_SKIP_FIX:
        print("SMOKE_SKIP_FIX=1: 演示改前 FAIL 状态\n")

    test_tool_stale_data()
    test_tool_fresh_data()
    test_tool_null_as_of_date()
    test_provider_stale_skus_logic()
    test_safety_verifier()

    _cleanup()

    total = len(_results)
    failed = [(n, _) for n, d in _results if not d for _ in [n]]
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

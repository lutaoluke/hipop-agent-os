#!/usr/bin/env python3
"""WS-116 round-15: 「快照时效门」对不可解析 as_of 的稳健性边界（修 T03 CI 红）。

背景：tool_query_sku 的 round-13「查不到」短路原写作 `if data_stale_val and not
live_has_sales`，而 data_stale_val 在 as_of 无法被 date.fromisoformat 解析时（不同
DB 驱动/格式：date 对象、'YYYY-MM-DD HH:MM:SS'、非 ISO 串）会经 except 落成 True，
于是 **live 失败被误吞成「快照过期 / SKU 查不到」**，丢失 live_sales_failed 证据。
本地（SQLite，as_of 为干净 'YYYY-MM-DD'）复现不到，CI 环境下稳定红。

修法：短路改为只在「确认陈旧」（as_of 可解析且超阈值）时触发；as_of 不可解析时只做
保守 REDACT，不据此判「查不到」，由 live 成功/失败逻辑决定 found 与 live_sales_failed。

FAIL-THEN-PASS：
  - 旧实现：as_of 不可解析 + live 失败 → found=False，无 live_sales_failed → FAIL
  - 新实现：同输入 → found=True，live_sales_failed=True，sales 仍 REDACT → PASS
  并验证「确认陈旧（as_of 可解析且 39 天前）+ live 失败」仍走 found=False（T04 口径不回退）。
"""
import os
import sys
import tempfile
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

TENANT_ID = 1
ENTITY_ALIAS = "hipop_ksa"
SKU = "TBS0228A"

_results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    _results.append((status, name, detail))
    line = f"  [{status}] {name}"
    if not cond and detail:
        line += f"\n         ↳ {detail}"
    print(line)


def _fresh_db(data_module):
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
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
    conn.commit()
    return conn


def _seed(conn, as_of_value):
    conn.execute(
        "INSERT OR REPLACE INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 85, "SAR"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO wf2_sku "
        "(tenant_id, entity_alias, partner_sku, title, as_of_date, imported_at, "
        " sales_30d, sales_10d, sales_grade, latest_price, latest_profit_rate, "
        " is_listed, total_orders) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT_ID, ENTITY_ALIAS, SKU, "Test SKU", as_of_value,
         "2026-06-09T10:00:00", 65, 11, "A", 50.0, 0.15, 1, 662),
    )
    conn.commit()


def _query_with_live_fail(as_of_value):
    import hipop.server.data as _data
    import hipop.server.agent as _agent
    conn = _fresh_db(_data)
    _seed(conn, as_of_value)

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
    return (result.get("items") or [{}])[0]


def test_unparseable_as_of_plus_live_fail_exposes_live_failure():
    # as_of 存在但不可解析（斜杠格式，所有 Python 版本的 fromisoformat 均拒绝）。
    print("\n── 不可解析 as_of + live 失败 → 暴露 live 失败，不误判查不到 ──")
    item = _query_with_live_fail("2026/06/09")
    check("found=True（不被误吞成查不到）", item.get("found") is True, f"item={item!r}")
    check("live_sales_failed=True（实时失败证据保留）",
          item.get("live_sales_failed") is True,
          f"live_sales_failed={item.get('live_sales_failed')!r}")
    check("live_sales_message 有内容", bool(item.get("live_sales_message")),
          f"live_sales_message={item.get('live_sales_message')!r}")
    check("sales_30d 仍 REDACT（不泄漏旧缓存 65）", item.get("sales_30d") is None,
          f"sales_30d={item.get('sales_30d')!r}")
    check("不再标 stale_expired（未确认陈旧）", not item.get("stale_expired"),
          f"stale_expired={item.get('stale_expired')!r}")


def test_datetime_string_as_of_plus_live_fail_exposes_live_failure():
    # 'YYYY-MM-DD HH:MM:SS' 格式（Python 3.9 fromisoformat 拒绝，旧实现据此误判陈旧）。
    print("\n── 'YYYY-MM-DD HH:MM:SS' as_of + live 失败 → 暴露 live 失败 ──")
    today = datetime.date.today().isoformat()
    item = _query_with_live_fail(today + " 00:00:00")
    check("found=True（容忍带时间的 as_of）", item.get("found") is True, f"item={item!r}")
    check("live_sales_failed=True", item.get("live_sales_failed") is True,
          f"live_sales_failed={item.get('live_sales_failed')!r}")


def test_confirmed_stale_plus_live_fail_still_not_found():
    # T04 口径不回退：as_of 可解析且 39 天前（确认陈旧）+ live 失败 → found=False「查不到」。
    print("\n── 确认陈旧(39天前) + live 失败 → 仍 found=False（T04 口径）──")
    old = (datetime.date.today() - datetime.timedelta(days=39)).isoformat()
    item = _query_with_live_fail(old)
    check("found=False（确认陈旧仍判查不到）", item.get("found") is False, f"item={item!r}")
    check("stale_expired=True", item.get("stale_expired") is True,
          f"stale_expired={item.get('stale_expired')!r}")


if __name__ == "__main__":
    print("=== smoke_t03_stale_unparseable_boundary ===")
    test_unparseable_as_of_plus_live_fail_exposes_live_failure()
    test_datetime_string_as_of_plus_live_fail_exposes_live_failure()
    test_confirmed_stale_plus_live_fail_still_not_found()
    passed = sum(1 for s, *_ in _results if s == "PASS")
    total = len(_results)
    print(f"\n=== 结果: {passed}/{total} PASS ===")
    if passed != total:
        print("FAIL 列表:")
        for s, n, _d in _results:
            if s == "FAIL":
                print(f"  - {n}")
        sys.exit(1)
    print("全部通过 ✓")

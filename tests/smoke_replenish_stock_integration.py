"""Smoke：库存 ↔ 补货 合流验收（KSA, WS-62）—— 端到端真实数据 + 库存未就绪降级钉死。

这条「真实库存 → 补货建议 → 运营入口」的连接在代码层已存在
（wf_sales_cycle.run_v2 从 wf1_stock 读 noon_saleable/pending/overseas/yiwu/dongguan
算进 current_pipeline）。本 smoke 不搭新连接，而是把它做端到端真实数据验收 + 钉死
库存未就绪/不完整时的降级边界，三死法逐条堵死。

关键设计（验门人返工点）：**所有降级断言都走真实运营入口**，不按 alias 旁路调
`stock_readiness()`。运营真用的入口有三条,本 smoke 全覆盖、且都用 `ksa` store
（解析到 hipop_ksa）构造 empty / incomplete 真实状态:
  1. data 入口      `data.get_replenishment_view("ksa")`（页面/chat 共用的数据层）
  2. HTTP 路由入口  `api.api_replenishment("ksa")`（/api/replenishment/{store} 处理函数）
  3. chat 工具入口  `agent.tool_compute_replenishment("ksa")`（**强制**断言,缺 anthropic
                    = 真失败,不 skip —— anthropic 是 requirements 声明依赖）
  4. 页面模板契约   静态校验 module_replenish.html 真读 stock_status 并渲染未就绪告警条

钉死的承重墙（DoD + 防三死法）：

验收 1 · 端到端真实数据（happy / 防「占位假数据」「接线错位」）
  同一 SKU：wf1_stock 真实库存高 → 补货建议 0（管道充足）；改成显著更低 → 建议 > 0
  且更大；并且这一变化传到运营入口（get_replenishment_view + HTTP 路由）的 rows，
  证明「数值真随真实库存变」,不是假绿。

验收 2 · 库存未就绪/不完整 降级（防「死代码短路」）
  - 空库存（0 行）→ 三条入口都 ready=False、status=empty、message 含「未就绪」；
  - 未就绪库存（19 行）→ 三条入口都 ready=False、status=empty、含「未就绪」；
  - 不完整库存（94% 覆盖率或源库存 ingest 超过 3 天未更新）→ 三条入口都
    ready=False、status=incomplete、含「不完整」；
  - 红队：源库存 ingest 已 stale,但 rollup 脚本刷新了 wf1_stock.updated_at → 仍 not-ready；
  - 红队：库存行覆盖 95%,但 Noon 只拉到 1% → partial_noon,仍 not-ready；
  - 完整库存但本周无需补货 → 三条入口 ready=True（rows 空 ≠ 未就绪，不误报）；
  绝不静默给 0 / 假确定建议：未就绪时入口响应本身带 ready=False，不是只在 run_v2 log。

fail-then-pass（改动前为何 FAIL）：base commit 无 data.stock_readiness /
get_replenishment_view，且 /api/replenishment、chat 工具不带就绪度 → 验收 2 在每条
入口断言 import/属性/KeyError 即 FAIL；接上 readiness + 入口视图后 PASS。

跑法：
  python3 tests/smoke_replenish_stock_integration.py     或   make test
  （纯 SQLite 临时库，固定 HIPOP_DB；不碰 PG / 不碰 live hipop.db / 不需要 server。）
"""
import os
import re
import sys
import json
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import server.data（及 run_v2 内部 `from server import data`）之前固定 SQLite
# 路径 + 清掉 PG。两个 import 副本都从 env 读 DB_PATH → 指向同一个临时库文件。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix="_ws62.db", delete=False).name
os.environ.pop("DB_URL", None)

TENANT = 1
KSA_STORE = "ksa"          # 运营入口用的 store code
KSA_ALIAS = "hipop_ksa"    # _resolve_entity_for_store("ksa") → hipop_ksa（schema 预置,SA）
SENS_SKU = "TBSENS001"     # 验收 1 的目标 SKU：库存变 → 建议变

TMPL_PATH = os.path.join(REPO, "hipop", "server", "templates", "module_replenish.html")


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


# ── DB / 种子 helpers ────────────────────────────────────────────────
def _init_db():
    from hipop.server import data
    data.set_current_tenant(TENANT)
    conn = data.conn()
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    cut = sql.find("DO $$")
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    conn.close()


def _reset_ksa():
    """清空 hipop_ksa 的业务数据,让每个场景独立从 ksa store 入口构造真实状态。"""
    from hipop.server import data
    conn = data.conn()
    for t in ("wf2_sku", "wf1_stock", "wf5_sales_cycle", "wf6_replenishment_queue_v2"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id=? AND entity_alias=?",
                     (TENANT, KSA_ALIAS))
    conn.execute("DELETE FROM wf3_logistics_hub_v2 WHERE tenant_id=?", (TENANT,))
    conn.commit()
    conn.close()


_SAME_AS_STOCK = object()


def _add_sku(sku, *, listed=1, sales=False, stock=None,
             noon_saleable_qty=_SAME_AS_STOCK, imported_at=None, updated_at=None):
    """插一个 wf2_sku（hipop_ksa）；可选灌销量与 wf1_stock 行。stock=None → 不建库存行。"""
    from hipop.server import data
    conn = data.conn()
    if sales:
        s10, s30, s60, s180, profit = 100, 300, 600, 1800, 0.30
    else:
        s10 = s30 = s60 = s180 = 0
        profit = 0.30
    conn.execute(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, title, is_listed, "
        "latest_price, latest_profit_rate, sales_10d, sales_30d, sales_60d, sales_180d) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, KSA_ALIAS, sku, f"商品 {sku}", listed, 99.0, profit, s10, s30, s60, s180),
    )
    if stock is not None:
        now_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        imported_at = imported_at or updated_at or now_ts
        updated_at = updated_at or imported_at
        if noon_saleable_qty is _SAME_AS_STOCK:
            noon_saleable_qty = stock
        conn.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, noon_saleable_qty, "
            "pending_inbound_qty, overseas_total_qty, yiwu_qty, dongguan_qty, total_stock, imported_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (TENANT, KSA_ALIAS, sku, noon_saleable_qty, 0, 0, 0, 0, stock, imported_at, updated_at),
        )
    conn.commit()
    conn.close()


def _add_hub(sku, avg_days=30):
    """给某 SKU 建一条 wf3 物流 hub 行（KSA），提供 completed_avg_total_days → avg_transit。"""
    from hipop.server import data
    groups = [{
        "country": "KSA", "completed_avg_total_days": avg_days,
        "in_transit_qty": 0, "in_transit_batches": [], "completed_batches": [],
    }]
    conn = data.conn()
    conn.execute(
        "INSERT INTO wf3_logistics_hub_v2 (tenant_id, sku, in_transit_total_qty, groups_json) "
        "VALUES (?,?,?,?)",
        (TENANT, sku, 0, json.dumps(groups)),
    )
    conn.commit()
    conn.close()


def _set_stock(sku, qty):
    from hipop.server import data
    conn = data.conn()
    conn.execute(
        "UPDATE wf1_stock SET noon_saleable_qty=?, total_stock=? "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (qty, qty, TENANT, KSA_ALIAS, sku),
    )
    conn.commit()
    conn.close()


def _run_wf5():
    from hipop.workflows import wf_sales_cycle
    return wf_sales_cycle.run_v2(TENANT, entity_aliases=[KSA_ALIAS], verbose=False)


def _wf5_weekly_total(sku):
    from hipop.server import data
    conn = data.conn()
    r = conn.execute(
        "SELECT weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, sku),
    ).fetchone()
    conn.close()
    return (r["weekly_total_replenish"] if r else None)


# ── 真实运营入口 helpers（不旁路 stock_readiness）─────────────────────
def _view():
    """data 入口：页面/chat 共用的数据层视图。"""
    from hipop.server import data
    data.set_current_tenant(TENANT)
    return data.get_replenishment_view(KSA_STORE, limit=200)


def _http():
    """HTTP 路由入口：/api/replenishment/{store} 的处理函数本体。"""
    from hipop.server import api, data
    data.set_current_tenant(TENANT)
    return api.api_replenishment(KSA_STORE, limit=200)


def _chat():
    """chat 工具入口：tool_compute_replenishment（强制,不 skip）。"""
    from hipop.server import agent
    return agent.tool_compute_replenishment(KSA_STORE, limit=200)


def _assert_not_ready_all_entries(check, expect_status, expect_word):
    """三条入口 + 数据契约都必须呈现未就绪/不完整,不静默 0。"""
    v = _view()
    h = _http()
    c = _chat()
    for label, resp in (("data 入口 get_replenishment_view", v),
                        ("HTTP 路由 api_replenishment", h),
                        ("chat 工具 tool_compute_replenishment", c)):
        st = resp.get("stock_status")
        check(f"[{label}] 带 stock_status", st is not None, list(resp.keys()))
        if st is None:
            continue
        check(f"[{label}] ready=False（不静默当 0 建议）", st.get("ready") is False, st)
        check(f"[{label}] status={expect_status}", st.get("status") == expect_status, st)
        check(f"[{label}] message 含『{expect_word}』", expect_word in (st.get("message") or ""), st)
        rows = resp.get("rows", resp.get("items", []))
        check(f"[{label}] not ready 时不返回补货建议", rows == [], rows)
    # data 入口与 HTTP 路由必须同形（页面/chat 据此渲染）
    check("HTTP 路由与 data 入口同形(stock_status+rows)",
          set(h.keys()) >= {"stock_status", "rows"} and isinstance(h["rows"], list), list(h.keys()))


# ── 验收 1 · 端到端真实数据：库存变 → 建议变（运营入口 rows 同步变）──────
def test_stock_change_changes_replenishment():
    print("== test_stock_change_changes_replenishment (验收 1: 数值真随真实库存变) ==")
    check = _Checker()
    _reset_ksa()
    # >=20 个上架 SKU 且各有库存行（满足 run_v2 上游非空 + readiness ready）。
    for i in range(20):
        _add_sku(f"FILL{i:03d}", sales=False, stock=500)
    _add_sku(SENS_SKU, sales=True, stock=400)
    _add_hub(SENS_SKU, avg_days=30)

    # 高库存（400 ≥ 目标管道 (30+7)*10=370）→ 管道充足 → 建议 0
    _set_stock(SENS_SKU, 400)
    _run_wf5()
    qty_high = _wf5_weekly_total(SENS_SKU)
    view_high = _view()
    http_high = _http()
    in_view_high = any(r["partner_sku"] == SENS_SKU for r in view_high["rows"])
    in_http_high = any(r["partner_sku"] == SENS_SKU for r in http_high["rows"])

    # 低库存（50 ≪ 目标）→ 管道缺口大 → 建议 > 0
    _set_stock(SENS_SKU, 50)
    _run_wf5()
    qty_low = _wf5_weekly_total(SENS_SKU)
    view_low = _view()
    low_row = next((r for r in view_low["rows"] if r["partner_sku"] == SENS_SKU), None)

    check("高库存时 wf5 建议为 0（管道充足）", qty_high == 0, f"qty_high={qty_high}")
    check("低库存时 wf5 建议 > 0（管道缺口）", (qty_low or 0) > 0, f"qty_low={qty_low}")
    check("库存变化真改变了建议数量（非写死/非假绿）",
          qty_low != qty_high, f"qty_high={qty_high} qty_low={qty_low}")
    check("高库存时 data 入口不列它为必补", in_view_high is False)
    check("高库存时 HTTP 路由不列它为必补", in_http_high is False)
    check("低库存时 data 入口列出它且数量一致",
          low_row is not None and low_row["qty"] == qty_low, f"low_row={low_row} qty_low={qty_low}")
    check("就绪态入口 ready=True", view_low["stock_status"]["ready"] is True, view_low["stock_status"])
    return check.failures


# ── 验收 2 · 空库存：三条运营入口都明确「库存未就绪」───────────────────
def test_empty_stock_surfaces_not_ready_at_all_entry_points():
    print("== test_empty_stock_surfaces_not_ready_at_all_entry_points (验收 2: 空库存降级) ==")
    check = _Checker()
    _reset_ksa()
    for i in range(15):  # 有上架 SKU,但**完全没有** wf1_stock 行
        _add_sku(f"EMP{i:03d}", sales=True, stock=None)

    _assert_not_ready_all_entries(check, expect_status="empty", expect_word="未就绪")
    # 空库存时 rows 为空,但绝不能被读成「0 需求」—— 上面 ready=False 已钉死
    check("空库存时 data 入口 rows 为空(但 ready=False,非静默 0)", _view()["rows"] == [], _view()["rows"])
    st = _view()["stock_status"]
    check("readiness 自带真读证据(listed=15/stock_rows=0)",
          st["listed_skus"] == 15 and st["stock_rows"] == 0, st)
    return check.failures


# ── 验收 2 · 19 行库存：新门槛下仍属于「库存未就绪」──────────────────
def test_min_rows_20_threshold_surfaces_not_ready_at_all_entry_points():
    print("== test_min_rows_20_threshold_surfaces_not_ready_at_all_entry_points (验收 2: <20 行降级) ==")
    check = _Checker()
    _reset_ksa()
    # 20 个上架 SKU,19 个有库存行。旧口径（>=10 且覆盖率 95%）会误判 ready；
    # Luke 新口径要求 wf1_stock <20 行必须先刷新库存。
    for i in range(20):
        _add_sku(f"MIN{i:03d}", sales=True, stock=300 if i < 19 else None)

    _assert_not_ready_all_entries(check, expect_status="empty", expect_word="未就绪")
    st = _view()["stock_status"]
    check("readiness 使用 Luke 新门槛 MIN_ROWS=20",
          st["stock_rows"] == 19 and st["status"] == "empty", st)
    return check.failures


# ── 验收 2 · 不完整库存：三条运营入口都明确「库存不完整」─────────────────
def test_incomplete_stock_surfaces_not_ready_at_all_entry_points():
    print("== test_incomplete_stock_surfaces_not_ready_at_all_entry_points (验收 2: 不完整库存降级) ==")
    check = _Checker()
    _reset_ksa()
    # 100 个上架 SKU,94 个有库存行 → 覆盖率 0.94。
    # 旧口径 80% 会误判 ready；Luke 新口径要求低于 95% 均为不完整。
    for i in range(100):
        _add_sku(f"PRT{i:03d}", sales=True, stock=300 if i < 94 else None)

    _assert_not_ready_all_entries(check, expect_status="incomplete", expect_word="不完整")
    st = _view()["stock_status"]
    check("覆盖率 94% 被 Luke 新门槛 95% 拦为不完整",
          st["stock_rows"] == 94 and st["coverage"] < 0.95, st)
    wf5 = _run_wf5()
    check("run_v2 进入计算前硬拦不完整库存",
          wf5.get("ok") is False and wf5["skipped"][0]["stock_status"]["status"] == "incomplete", wf5)
    return check.failures


# ── 验收 2 · 陈旧库存：源库存 ingest 超过 3 天必须先刷新库存 ─────────────
def test_stale_stock_surfaces_not_ready_at_all_entry_points():
    print("== test_stale_stock_surfaces_not_ready_at_all_entry_points (验收 2: 源库存超过 3 天降级) ==")
    check = _Checker()
    _reset_ksa()
    stale_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 73 * 3600))
    for i in range(20):
        _add_sku(f"OLD{i:03d}", sales=True, stock=300, imported_at=stale_ts, updated_at=stale_ts)

    _assert_not_ready_all_entries(check, expect_status="incomplete", expect_word="不完整")
    st = _view()["stock_status"]
    check("readiness 暴露 72h 时效门槛",
          st["freshness_max_hours"] == 72 and (st["stock_age_hours"] or 0) > 72, st)
    check("陈旧库存 message 要求先刷新库存",
          "刷新库存" in st["message"] or "wf1 ingest" in st["message"], st)
    wf5 = _run_wf5()
    check("run_v2 进入计算前硬拦陈旧库存",
          wf5.get("ok") is False and wf5["skipped"][0]["stock_status"]["status"] == "incomplete", wf5)
    return check.failures


# ── 红队 · rollup 刷新 updated_at 不得伪造源库存 freshness ─────────────
def test_rollup_updated_at_does_not_fake_source_freshness():
    print("== test_rollup_updated_at_does_not_fake_source_freshness (红队: rollup updated_at 假新鲜) ==")
    check = _Checker()
    _reset_ksa()
    stale_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 73 * 3600))
    fresh_rollup_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for i in range(20):
        # 模拟源库存 ingest 已超过 3 天，但 compute_pending/merge 之后刷新了 updated_at。
        # 旧实现看 MAX(updated_at) 会误判 ready；新实现看 imported_at 仍必须 not-ready。
        _add_sku(f"RUP{i:03d}", sales=True, stock=300,
                 imported_at=stale_ts, updated_at=fresh_rollup_ts)

    _assert_not_ready_all_entries(check, expect_status="incomplete", expect_word="不完整")
    st = _view()["stock_status"]
    check("readiness 看源库存 imported_at,不被 rollup updated_at 假新鲜骗过",
          (st["stock_age_hours"] or 0) > 72 and st.get("stock_source_imported_at") is not None, st)
    wf5 = _run_wf5()
    check("run_v2 也按源库存 freshness 硬拦",
          wf5.get("ok") is False and wf5["skipped"][0]["stock_status"]["ready"] is False, wf5)
    return check.failures


# ── 红队 · Noon 仅部分拉到不得被 1 行非 NULL 假绿 ────────────────────
def test_partial_noon_coverage_surfaces_not_ready():
    print("== test_partial_noon_coverage_surfaces_not_ready (红队: Noon 部分仓未拉假绿) ==")
    check = _Checker()
    _reset_ksa()
    fresh_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for i in range(100):
        stock = 300 if i < 95 else None
        noon_qty = 300 if i == 0 else None
        _add_sku(f"NOON{i:03d}", sales=True, stock=stock,
                 noon_saleable_qty=noon_qty, imported_at=fresh_ts, updated_at=fresh_ts)

    _assert_not_ready_all_entries(check, expect_status="partial_noon", expect_word="noon")
    st = _view()["stock_status"]
    check("Noon 覆盖率只 1/100 时必须 partial_noon",
          st["status"] == "partial_noon" and st["noon_pulled_rows"] == 1, st)
    wf5 = _run_wf5()
    check("run_v2 进入计算前硬拦 partial_noon",
          wf5.get("ok") is False and wf5["skipped"][0]["stock_status"]["status"] == "partial_noon", wf5)
    return check.failures


# ── 就绪态对照：完整库存但本周无需补货 → ready=True（防把正常态误报降级）────
def test_ready_stock_no_false_positive():
    print("== test_ready_stock_no_false_positive (就绪态对照,空 rows ≠ 未就绪) ==")
    check = _Checker()
    _reset_ksa()
    for i in range(20):  # 完整覆盖 + 无销量 → 不会产生补货建议(rows 空),但库存就绪
        _add_sku(f"RDY{i:03d}", sales=False, stock=500)
    _run_wf5()

    for label, resp in (("data 入口", _view()), ("HTTP 路由", _http()), ("chat 工具", _chat())):
        st = resp["stock_status"]
        check(f"[{label}] 就绪 status=ready", st["status"] == "ready", st)
        check(f"[{label}] 就绪 ready=True", st["ready"] is True, st)
    check("就绪态 data 入口 rows 可为空(管道充足,非未就绪)", isinstance(_view()["rows"], list))
    return check.failures


# ── 页面模板契约：补货页真读 stock_status 并渲染未就绪告警条 ──────────────
def test_template_surfaces_readiness():
    print("== test_template_surfaces_readiness (页面模板契约) ==")
    check = _Checker()
    with open(TMPL_PATH, encoding="utf-8") as f:
        html = f.read()
    check("模板从 /api/replenishment 响应取 stock_status", "stock_status" in html, "no stock_status")
    check("模板取 rows 字段", re.search(r"\.rows", html) is not None)
    check("模板按 stockStatus.ready 渲染(就绪度真被消费)", "stockStatus" in html and "ready" in html)
    check("模板渲染未就绪/不完整告警条(message)",
          ("库存未就绪" in html or "库存不完整" in html) and "message" in html, "no banner")
    return check.failures


if __name__ == "__main__":
    import traceback

    _init_db()
    # hipop_ksa / hipop_uae 由 schema_v2.sql 预置;每个测试用 _reset_ksa 独立构造状态。

    tests = [
        test_stock_change_changes_replenishment,
        test_empty_stock_surfaces_not_ready_at_all_entry_points,
        test_min_rows_20_threshold_surfaces_not_ready_at_all_entry_points,
        test_incomplete_stock_surfaces_not_ready_at_all_entry_points,
        test_stale_stock_surfaces_not_ready_at_all_entry_points,
        test_rollup_updated_at_does_not_fake_source_freshness,
        test_partial_noon_coverage_surfaces_not_ready,
        test_ready_stock_no_false_positive,
        test_template_surfaces_readiness,
    ]
    failed = 0
    for t in tests:
        try:
            fails = t()
            if fails:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__} raised: {e}")
            traceback.print_exc()
    total = len(tests)
    print(f"\n{total - failed}/{total} test groups passed")
    sys.exit(0 if failed == 0 else 1)

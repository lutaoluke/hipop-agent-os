"""Smoke：补货建议权威源与本周建议量口径核实（WS-124 / S0）

## 核实结论（本 smoke 钉死的承重墙）

### 权威源
线上工作台补货建议"本周建议补货量"的权威字段是：
  wf5_sales_cycle.weekly_total_replenish  (entity_alias='hipop_ksa', tenant_id=1)

数据来源链：
  ERP/noon 销量 → wf2_sku.sales_{10/30/60/180}d
  ERP/noon 库存 → wf1_stock.noon_saleable_qty / pending_inbound_qty / overseas_total_qty
  ERP/物流       → wf3_logistics_hub_v2.in_transit_total_qty (groups_json 按 KSA 过滤)
  以上三类       → wf_sales_cycle.py run_v2 → wf5_sales_cycle.weekly_total_replenish
  工作台入口     → data.get_replenishment_view('ksa') → stock_readiness gate →
                   get_replenishment → ORDER BY weekly_total_replenish DESC

chat 工具 compute_replenishment 读的就是这条链的终端，不读旧快照、不读其他源。

### T27/T29 FAIL 根因（接线缺失）
TBU0010A、SAB0433A 在 wf2_sku (hipop_ksa) 里有销量记录（在列），
但 wf5_sales_cycle (hipop_ksa) 里没有对应行——wf_sales_cycle run_v2 没为这两个 SKU
在 KSA 实体下写过一行。
结果：query_sku 对这两个 SKU 的 weekly_total_replenish 拿到 NULL/0，
compute_replenishment 的 KSA Top 里永远看不到它们。

### T29 千件级异常（TBN0201A=2040 来自不同口径/源）
TBN0201A 不在 wf5_sales_cycle (hipop_ksa) 里，也不在 wf2_sku (hipop_ksa) 里。
它不是 KSA 实体已上架 SKU——T29 体验 run 里 Agent 返回的 2040 件来自完全不同的
数据路径，不与 TBU0010A=7 / SAB0433A=6 处于同一口径。

### 缺数据时必须 not-ready，不能返回 0 或旧快照
stock_readiness 对空/过期 wf1_stock 返回 ready=False；
get_replenishment_view 在 not-ready 时返回 rows=[]，不静默给 0。

### 工作台入口实际状态（2026-06-08 核实）
当前线上库存 age > 72h（status=incomplete），
data.get_replenishment_view('ksa') 返回 rows=[]，不是 wf5 直查的 P51* 快照。
工作台真实口径是"库存未就绪"，不是 P51* Top 5。

## 验收 / fail-then-pass
- test_authoritative_field_is_weekly_total_replenish：
  compute_replenishment 返回的 references 必须指向 wf5_sales_cycle，
  且字段口径声明是 weekly_total_replenish > 0。
- test_wf5_source_chain_fields_exist：
  wf5_sales_cycle 有 weekly_total_replenish / wf5_replenish_qty / lost_replenish_qty，
  wf2_sku 有 sales_{10/30/60}d，wf1_stock 有 noon_saleable_qty；
  工作流连接脚本路径存在且包含 wf2_sku + wf1_stock 的 SELECT。
- test_tbu0010a_in_wf2_absent_in_wf5_ksa：
  TBU0010A 在 wf2_sku (hipop_ksa, is_listed=1) 存在，
  在 wf5_sales_cycle (hipop_ksa) 不存在——接线缺失，这是 T27 FAIL 根因。
- test_sab0433a_in_wf2_absent_in_wf5_ksa：
  SAB0433A 在 wf2_sku (hipop_ksa, is_listed=1) 存在，
  在 wf5_sales_cycle (hipop_ksa) 不存在。
- test_tbn0201a_not_in_ksa_entity：
  TBN0201A 不在 wf2_sku (hipop_ksa) 里，也不在 wf5_sales_cycle (hipop_ksa)；
  T29 的 2040 件与线上 KSA 权威口径不同源。
- test_entry_point_respects_stock_readiness_gate：
  直接调 data.get_replenishment_view('ksa') 线上入口；
  若 stock_status.ready=False → rows 必须为空（入口正确 gate）；
  若 ready=True → rows 按 weekly_total_replenish DESC，TBN0201A 不在其中。
- test_missing_stock_returns_not_ready_not_zero：
  (独立临时库) 空 wf1_stock → stock_readiness ready=False，
  get_replenishment_view rows=[]，不静默给 0。
- test_entry_point_ready_fixture_returns_ordered_wf5_rows：
  (隔离 ready fixture) get_replenishment_view 通过就绪门后从 wf5 返回正确有序行；
  证明入口链路完整、不旁路 wf5 直查。

跑法：
  python3 tests/smoke_t27_t29_replenish_oracle.py
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

# ── 常量 ──────────────────────────────────────────────────────────────────────
TENANT = 1
KSA_ALIAS = "hipop_ksa"
KSA_STORE = "ksa"

# T27/T28 关注 SKU（已知有销量记录，T27/T29 期望出现在 Top）
T27_SKU = "TBU0010A"
T28_SKU = "SAB0433A"

# T29 体验 run 实际回答里的千件级 SKU（应证明不是同一口径）
T29_OUTLIER_SKU = "TBN0201A"


# ── 辅助函数 ──────────────────────────────────────────────────────────────────
def _live_fetch(sql, params=()):
    """从线上 production DB 读（默认 HIPOP_DB 环境变量指向的库）。"""
    db_path = os.environ.get("HIPOP_DB", "/Users/luke/code/hipop/hipop.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _skip_if_live_db_unavailable():
    """若 CI 环境无法访问 live DB，skip 该测试。"""
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
# Test 1: compute_replenishment 工具引用的表/字段是 wf5_sales_cycle.weekly_total_replenish
# ═══════════════════════════════════════════════════════════════════════════════
def test_authoritative_field_is_weekly_total_replenish():
    """工作台 compute_replenishment 工具的 SQL 引用 wf5_sales_cycle 且 ORDER BY weekly_total_replenish。

    不依赖 live DB；直接审计 agent.py 和 data.py 源码，
    证明入口链路（agent → data.get_replenishment）只走这条路，不走旧快照/其他字段。
    """
    agent_path = os.path.join(REPO, "hipop", "server", "agent.py")
    data_path  = os.path.join(REPO, "hipop", "server", "data.py")
    # WS-166：tool_* 实现已外移到 tools_impl.py；agent.py 仅保留声明/分发投影。
    impl_path  = os.path.join(REPO, "hipop", "server", "tools_impl.py")

    assert os.path.exists(agent_path), f"缺 hipop/server/agent.py: {agent_path}"
    assert os.path.exists(data_path),  f"缺 hipop/server/data.py: {data_path}"
    assert os.path.exists(impl_path),  f"缺 hipop/server/tools_impl.py: {impl_path}"

    agent_src = open(agent_path, encoding="utf-8").read()
    data_src  = open(data_path,  encoding="utf-8").read()
    impl_src  = open(impl_path,  encoding="utf-8").read()

    # 1) agent.py: compute_replenishment tool 有声明 + 分发投影（name/再导出都在）
    assert "compute_replenishment" in agent_src, \
        "agent.py 里找不到 compute_replenishment 工具声明"
    assert "tool_compute_replenishment" in agent_src, \
        "agent.py 里找不到 tool_compute_replenishment 分发投影（TOOL_FUNCS / 再导出）"

    # 2) data.py: get_replenishment 查询的是 wf5_sales_cycle，字段是 weekly_total_replenish
    assert "get_replenishment" in data_src, \
        "data.py 里找不到 get_replenishment 函数"
    assert "wf5_sales_cycle" in data_src, \
        "data.py 里找不到 wf5_sales_cycle 表引用"
    assert "weekly_total_replenish" in data_src, \
        "data.py 里找不到 weekly_total_replenish 字段引用"

    # 3) 排序方向是 DESC（Top N 按大→小）
    assert "weekly_total_replenish DESC" in data_src, \
        "data.py 的 get_replenishment 查询缺少 ORDER BY weekly_total_replenish DESC"

    # 4) tool_compute_replenishment 实现里的 references 指向 wf5_sales_cycle（线上可追溯）
    #    WS-166 后实现在 tools_impl.py，故在实现源里审计（定位到 def 起点再切窗口）。
    _impl_start = impl_src.find("def tool_compute_replenishment")
    assert _impl_start != -1, "tools_impl.py 里找不到 tool_compute_replenishment 实现"
    assert "wf5_sales_cycle" in impl_src[_impl_start:_impl_start + 2000], \
        "tool_compute_replenishment 实现附近没有引用 wf5_sales_cycle（references 缺失）"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: wf5_sales_cycle 的上游来源字段（ERP+noon 链路）都存在
# ═══════════════════════════════════════════════════════════════════════════════
def test_wf5_source_chain_fields_exist():
    """wf5_sales_cycle 的三类上游表字段都在线上 DB 里存在，且 wf_sales_cycle.py 真读它们。"""
    if _skip_if_live_db_unavailable():
        print("⊘ test_wf5_source_chain_fields_exist (live DB unavailable, skipped in CI)")
        return

    wf5_cycle_path = os.path.join(REPO, "hipop", "workflows", "wf_sales_cycle.py")
    assert os.path.exists(wf5_cycle_path), f"缺 wf_sales_cycle.py: {wf5_cycle_path}"
    wf5_src = open(wf5_cycle_path, encoding="utf-8").read()

    # wf_sales_cycle.py 真读 wf2_sku（ERP/noon 销量）
    assert "wf2_sku" in wf5_src,        "wf_sales_cycle.py 没有读 wf2_sku（ERP 销量表）"
    assert "sales_10d" in wf5_src,      "wf_sales_cycle.py 没有读 sales_10d 字段"
    assert "sales_30d" in wf5_src,      "wf_sales_cycle.py 没有读 sales_30d 字段"

    # wf_sales_cycle.py 真读 wf1_stock（noon 可售库存 + ERP 仓库存）
    assert "wf1_stock" in wf5_src,           "wf_sales_cycle.py 没有读 wf1_stock（库存表）"
    assert "noon_saleable_qty" in wf5_src,   "wf_sales_cycle.py 没有读 noon_saleable_qty"
    assert "overseas_total_qty" in wf5_src,  "wf_sales_cycle.py 没有读 overseas_total_qty（海外仓）"

    # wf_sales_cycle.py 真读 wf3_logistics_hub_v2（物流）
    assert "wf3_logistics_hub_v2" in wf5_src, "wf_sales_cycle.py 没有读 wf3_logistics_hub_v2（物流表）"
    assert "in_transit_total_qty" in wf5_src,  "wf_sales_cycle.py 没有读 in_transit_total_qty"

    # wf5_sales_cycle 表有 weekly_total_replenish（输出字段）
    assert "weekly_total_replenish" in wf5_src, "wf_sales_cycle.py 没有输出 weekly_total_replenish"

    # 线上 DB: wf5_sales_cycle 表的列定义里有 weekly_total_replenish
    schema_rows = _live_fetch("PRAGMA table_info(wf5_sales_cycle)")
    col_names = [r["name"] for r in schema_rows]
    assert "weekly_total_replenish" in col_names, \
        f"线上 DB wf5_sales_cycle 没有 weekly_total_replenish 列: {col_names}"
    assert "wf5_replenish_qty"  in col_names, "线上 DB wf5_sales_cycle 缺 wf5_replenish_qty"
    assert "lost_replenish_qty" in col_names, "线上 DB wf5_sales_cycle 缺 lost_replenish_qty"
    assert "entity_alias"       in col_names, "线上 DB wf5_sales_cycle 缺 entity_alias（多租户路由缺失）"
    assert "tenant_id"          in col_names, "线上 DB wf5_sales_cycle 缺 tenant_id"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: TBU0010A 在 wf2_sku (KSA) 存在但在 wf5_sales_cycle (KSA) 缺失
#         → 这是 T27 FAIL 的直接根因（接线缺失）
# ═══════════════════════════════════════════════════════════════════════════════
def test_tbu0010a_in_wf2_absent_in_wf5_ksa():
    """T27 FAIL 根因：TBU0010A 在 KSA wf2_sku 有行，但 wf5_sales_cycle KSA 无行。

    wf_sales_cycle run_v2 没有为 TBU0010A 在 hipop_ksa 实体下写过补货记录。
    query_sku 工具的 LEFT JOIN 拿不到 weekly_total_replenish → chat 给 0/NULL，
    不会出现在 compute_replenishment 的 Top N。
    S1 修复验收：本 test 在修复后应翻红——TBU0010A 必须被加进 wf5_sales_cycle KSA。
    """
    if _skip_if_live_db_unavailable():
        print("⊘ test_tbu0010a_in_wf2_absent_in_wf5_ksa (live DB unavailable, skipped in CI)")
        return

    # wf2_sku (hipop_ksa) 有这个 SKU（上架在列）
    wf2_rows = _live_fetch(
        "SELECT partner_sku, is_listed, sales_30d FROM wf2_sku "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T27_SKU),
    )
    assert len(wf2_rows) >= 1, \
        f"{T27_SKU} 不在 wf2_sku (hipop_ksa) 里——SKU 本身不存在？请检查 wf2 ingest"
    assert wf2_rows[0]["is_listed"] == 1, \
        f"{T27_SKU} 在 wf2_sku (hipop_ksa) is_listed={wf2_rows[0]['is_listed']}，未上架"

    # wf5_sales_cycle (hipop_ksa) 没有这个 SKU（接线缺失）
    wf5_rows = _live_fetch(
        "SELECT partner_sku, weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T27_SKU),
    )
    assert len(wf5_rows) == 0, (
        f"{T27_SKU} 已经在 wf5_sales_cycle (hipop_ksa) 里了 "
        f"(weekly={wf5_rows[0]['weekly_total_replenish']})——接线缺失已修复，本 test 可退役"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: SAB0433A 在 wf2_sku (KSA) 存在但在 wf5_sales_cycle (KSA) 缺失
#         → 这是 T28/T29 FAIL 的直接根因（接线缺失）
# ═══════════════════════════════════════════════════════════════════════════════
def test_sab0433a_in_wf2_absent_in_wf5_ksa():
    """T28/T29 FAIL 根因：SAB0433A 在 KSA wf2_sku 有行，但 wf5_sales_cycle KSA 无行。

    同 test_tbu0010a_in_wf2_absent_in_wf5_ksa 逻辑。
    S1 修复验收：本 test 在修复后应翻红——SAB0433A 必须被加进 wf5_sales_cycle KSA。
    """
    if _skip_if_live_db_unavailable():
        print("⊘ test_sab0433a_in_wf2_absent_in_wf5_ksa (live DB unavailable, skipped in CI)")
        return

    wf2_rows = _live_fetch(
        "SELECT partner_sku, is_listed, sales_30d FROM wf2_sku "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T28_SKU),
    )
    assert len(wf2_rows) >= 1, \
        f"{T28_SKU} 不在 wf2_sku (hipop_ksa) 里——SKU 本身不存在？请检查 wf2 ingest"
    assert wf2_rows[0]["is_listed"] == 1, \
        f"{T28_SKU} 在 wf2_sku (hipop_ksa) is_listed={wf2_rows[0]['is_listed']}，未上架"

    wf5_rows = _live_fetch(
        "SELECT partner_sku, weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T28_SKU),
    )
    assert len(wf5_rows) == 0, (
        f"{T28_SKU} 已经在 wf5_sales_cycle (hipop_ksa) 里了 "
        f"(weekly={wf5_rows[0]['weekly_total_replenish']})——接线缺失已修复，本 test 可退役"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: TBN0201A 不是 KSA 实体上架 SKU，T29 的 2040 件来自不同数据路径
# ═══════════════════════════════════════════════════════════════════════════════
def test_tbn0201a_not_in_ksa_entity():
    """证明 T29 体验 run 里 Agent 给出的 TBN0201A=2040 件不是线上 KSA 权威口径。

    TBN0201A 既不在 wf2_sku (hipop_ksa)，也不在 wf5_sales_cycle (hipop_ksa)。
    它不是 KSA 实体 SKU，compute_replenishment 永远不会把它列进 KSA Top N。
    T29 体验里的 2040 件是来自不同数据快照/路径的异常值，不与 TBU0010A/SAB0433A
    处于同一口径。
    """
    if _skip_if_live_db_unavailable():
        print("⊘ test_tbn0201a_not_in_ksa_entity (live DB unavailable, skipped in CI)")
        return

    # wf2_sku KSA: TBN0201A 不存在（它不是 KSA 已上架 SKU）
    wf2_rows = _live_fetch(
        "SELECT partner_sku, entity_alias FROM wf2_sku "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T29_OUTLIER_SKU),
    )
    assert len(wf2_rows) == 0, (
        f"{T29_OUTLIER_SKU} 意外出现在 wf2_sku (hipop_ksa)——请核实数据来源"
    )

    # wf5_sales_cycle KSA: TBN0201A 不存在（不在线上补货表）
    wf5_rows = _live_fetch(
        "SELECT partner_sku, weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, KSA_ALIAS, T29_OUTLIER_SKU),
    )
    assert len(wf5_rows) == 0, (
        f"{T29_OUTLIER_SKU} 意外出现在 wf5_sales_cycle (hipop_ksa) "
        f"weekly={wf5_rows[0]['weekly_total_replenish'] if wf5_rows else '?'}——请核实数据来源"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: 工作台入口 get_replenishment_view 尊重库存就绪度门
#         → not-ready 时 rows=[]；ready 时 rows 来自 wf5 有序排列
#         （不旁路直查 wf5_sales_cycle）
# ═══════════════════════════════════════════════════════════════════════════════
def test_entry_point_respects_stock_readiness_gate():
    """工作台真实入口 get_replenishment_view('ksa') 的就绪度门验证。

    直接调 data.get_replenishment_view 线上入口，而不是旁路直查 wf5_sales_cycle。
    两条路径均验证：
    - stock_status.ready=False -> rows 必须为空（入口正确 gate，不产出 Top）
    - stock_status.ready=True  -> rows 来自 wf5，按 weekly_total_replenish DESC，
                                   TBN0201A（非 KSA 实体 SKU）不在其中

    当前线上库存 age > 72h（incomplete），入口走 not-ready 分支——
    wf5 里有 P51* 行，但工作台实际对运营展示 rows=[]，不是 P51* Top 5。
    S1 修复并刷新库存后本 test 走 ready 分支，无需修改断言。
    """
    if _skip_if_live_db_unavailable():
        print("⊘ test_entry_point_respects_stock_readiness_gate (live DB unavailable, skipped in CI)")
        return

    import importlib
    import hipop.server.data as data_mod
    importlib.reload(data_mod)

    view = data_mod.get_replenishment_view("ksa", limit=10)
    stock_status = view["stock_status"]
    rows = view.get("rows", [])

    if not stock_status.get("ready"):
        # not-ready 分支：入口必须封住 rows，不能把旧 wf5 快照漏出去
        assert rows == [], (
            f"stock_status.ready=False 时 rows 必须为空，实际返回了 {len(rows)} 行；"
            f"status={stock_status.get('status')}, age={stock_status.get('stock_age_hours')}h"
        )
        known_not_ready = {"empty", "incomplete", "no_skus", "partial_noon"}
        assert stock_status.get("status") in known_not_ready, (
            f"not-ready 但 status 值未知: {stock_status.get('status')}"
        )
    else:
        # ready 分支：rows 若非空则必须按 weekly_total_replenish DESC 排列
        if len(rows) > 1:
            qtys = [r.get("qty", 0) for r in rows]
            assert qtys == sorted(qtys, reverse=True), (
                f"ready 时 rows 应按 weekly_total_replenish DESC，实际: {qtys}"
            )
        row_skus = [r["partner_sku"] for r in rows]
        assert T29_OUTLIER_SKU not in row_skus, (
            f"{T29_OUTLIER_SKU} 出现在工作台 Top——非 KSA 实体 SKU，请核实数据来源"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: 缺 ERP/noon 输入时，补货结果必须标为 not-ready，不能返回 0 或旧快照
# ═══════════════════════════════════════════════════════════════════════════════
def test_missing_stock_returns_not_ready_not_zero():
    """ERP/noon 库存输入缺失时，工作台入口必须返回 not-ready，而不是假 0 补货建议。

    使用独立临时库（不碰 production DB），构造 wf1_stock 空表场景，
    验证 data.get_replenishment_view 在库存未就绪时：
    - stock_status.ready == False
    - rows 为空（不静默补 0）
    - 不返回任何 SKU 的补货建议
    """
    # 独立临时库，不污染 production DB
    tmp = tempfile.NamedTemporaryFile(suffix="_ws124_oracle.db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    orig_env = os.environ.get("HIPOP_DB")
    os.environ["HIPOP_DB"] = tmp_path
    os.environ.pop("DB_URL", None)

    try:
        # 在临时库里建最小 schema
        con = sqlite3.connect(tmp_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS sales_entities (
                id INTEGER PRIMARY KEY, tenant_id BIGINT NOT NULL,
                alias TEXT NOT NULL, country TEXT NOT NULL, platform TEXT,
                store_name TEXT, store_id INT, currency TEXT,
                active INT NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS wf2_sku (
                tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
                is_listed INTEGER DEFAULT 1,
                sales_10d REAL, sales_30d REAL, sales_60d REAL, sales_180d REAL,
                latest_profit_rate REAL, title TEXT, as_of_date TEXT,
                total_orders INTEGER, latest_price REAL,
                noon_saleable_qty REAL, imported_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS wf1_stock (
                tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL, partner_sku TEXT NOT NULL,
                noon_total_qty INT, noon_saleable_qty INT, noon_unsaleable_qty INT,
                noon_warehouses_json TEXT, pending_inbound_qty INT,
                overseas_total_qty INT, overseas_breakdown_json TEXT,
                yiwu_qty INT, dongguan_qty INT, total_stock INT,
                imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS wf5_sales_cycle (
                tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
                weekly_total_replenish INTEGER, urgency TEXT,
                wf5_replenish_qty INTEGER, lost_replenish_qty INTEGER,
                current_pipeline INTEGER, target_pipeline INTEGER,
                daily_rate REAL, trend TEXT, ops_advice TEXT,
                risk_label TEXT, trigger_reasons TEXT, updated_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS tenant_store_map (
                store_code TEXT PRIMARY KEY, tenant_id INTEGER, entity_alias TEXT
            )""")
        # 插入 KSA 实体映射和一个上架 SKU（但不插 wf1_stock 行）
        con.execute("INSERT INTO sales_entities (id,tenant_id,alias,country,platform,active) VALUES (1,1,'hipop_ksa','SA','noon',1)")
        con.execute("INSERT INTO tenant_store_map VALUES ('ksa',1,'hipop_ksa')")
        con.execute(
            "INSERT INTO wf2_sku (tenant_id,entity_alias,partner_sku,is_listed,sales_30d) "
            "VALUES (?,?,?,1,5.0)",
            (1, "hipop_ksa", "TEST_SKU_001"),
        )
        con.commit()
        con.close()

        # 重新加载 data 模块（让它读新 HIPOP_DB 路径）
        import importlib
        import hipop.server.data as data_mod
        importlib.reload(data_mod)

        # 调用运营入口 get_replenishment_view
        view = data_mod.get_replenishment_view("ksa", limit=10)

        # 核心断言：库存空 → not-ready，rows 为空，不静默给 0
        stock_status = view.get("stock_status", {})
        assert stock_status.get("ready") is False, (
            f"wf1_stock 为空时 stock_status.ready 应为 False，实际: {stock_status}"
        )
        assert view.get("rows") == [] or view.get("rows") is None, (
            f"库存未就绪时 rows 应为 []，实际返回了 {len(view.get('rows', []))} 行补货建议"
        )

    finally:
        # 恢复 production DB 环境变量
        if orig_env is not None:
            os.environ["HIPOP_DB"] = orig_env
        else:
            os.environ.pop("HIPOP_DB", None)
        import hipop.server.data as data_mod
        import importlib
        importlib.reload(data_mod)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: 隔离 ready fixture 验证工作台入口完整路径
#         → stock ready 时 get_replenishment_view 从 wf5 返回正确有序行
# ═══════════════════════════════════════════════════════════════════════════════
def test_entry_point_ready_fixture_returns_ordered_wf5_rows():
    """隔离 fixture（ready 状态）：get_replenishment_view 通过就绪门后从 wf5 返回补货行。

    不依赖线上库存状态（线上当前 stale）——在隔离临时库里构造 ready 条件：
    - 25 个 is_listed=1 的 SKU + 25 行对应的新鲜 wf1_stock（全有 noon_saleable_qty）
    - 3 行 wf5_sales_cycle（已知 weekly_total_replenish 30/20/10）
    然后调 data.get_replenishment_view('ksa') 真实入口，断言：
    1. stock_status.ready == True
    2. 返回 3 行，顺序为 FSKU_005(30) > FSKU_010(20) > FSKU_015(10)
    3. 入口链路完整：stock_readiness gate 通过 -> get_replenishment -> wf5 rows
    """
    import datetime
    import importlib

    tmp = tempfile.NamedTemporaryFile(suffix="_ws124_ready.db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    orig_env = os.environ.get("HIPOP_DB")
    os.environ["HIPOP_DB"] = tmp_path
    os.environ.pop("DB_URL", None)

    try:
        fresh_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con = sqlite3.connect(tmp_path)

        con.execute("""
            CREATE TABLE sales_entities (
                id INTEGER PRIMARY KEY, tenant_id BIGINT NOT NULL,
                alias TEXT NOT NULL, country TEXT NOT NULL, platform TEXT,
                store_name TEXT, store_id INT, currency TEXT,
                active INT NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
        con.execute("""
            CREATE TABLE wf2_sku (
                tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
                is_listed INTEGER DEFAULT 1, sales_30d REAL, title TEXT,
                image_url TEXT, latest_price REAL, latest_profit_rate REAL,
                imported_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
        con.execute("""
            CREATE TABLE wf1_stock (
                tenant_id BIGINT NOT NULL, entity_alias TEXT NOT NULL, partner_sku TEXT NOT NULL,
                noon_total_qty INT, noon_saleable_qty INT, noon_unsaleable_qty INT,
                noon_warehouses_json TEXT, pending_inbound_qty INT,
                overseas_total_qty INT, overseas_breakdown_json TEXT,
                yiwu_qty INT, dongguan_qty INT, total_stock INT,
                imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")
        con.execute("""
            CREATE TABLE wf5_sales_cycle (
                tenant_id INTEGER, entity_alias TEXT, partner_sku TEXT,
                weekly_total_replenish INTEGER, urgency TEXT,
                wf5_replenish_qty INTEGER, lost_replenish_qty INTEGER,
                current_pipeline INTEGER, target_pipeline INTEGER,
                daily_rate REAL, trend TEXT, ops_advice TEXT,
                risk_label TEXT, trigger_reasons TEXT, updated_at TEXT,
                PRIMARY KEY (tenant_id, entity_alias, partner_sku)
            )""")

        # KSA entity: country='SA' (入口通过 _resolve_entity_for_store('ksa') -> 'SA')
        con.execute(
            "INSERT INTO sales_entities (id,tenant_id,alias,country,active) VALUES (1,1,'fix_ksa','SA',1)"
        )

        # 25 SKUs + 25 fresh stock rows (满足 MIN_ROWS=20 / COVERAGE=95% / NOON_COVERAGE=100%)
        for i in range(1, 26):
            sku = "FSKU_{:03d}".format(i)
            con.execute(
                "INSERT INTO wf2_sku (tenant_id,entity_alias,partner_sku,is_listed,sales_30d,title) "
                "VALUES (1,'fix_ksa',?,1,5.0,?)",
                (sku, "Test SKU {}".format(i)),
            )
            con.execute(
                "INSERT INTO wf1_stock (tenant_id,entity_alias,partner_sku,noon_saleable_qty,imported_at) "
                "VALUES (1,'fix_ksa',?,0,?)",
                (sku, fresh_ts),
            )

        # 3 wf5 rows with known quantities (expected Top order: 005 > 010 > 015)
        for sku, qty in [("FSKU_005", 30), ("FSKU_010", 20), ("FSKU_015", 10)]:
            con.execute(
                "INSERT INTO wf5_sales_cycle "
                "(tenant_id,entity_alias,partner_sku,weekly_total_replenish,urgency,trend,ops_advice,updated_at) "
                "VALUES (1,'fix_ksa',?,?,'low','稳定','补货',?)",
                (sku, qty, fresh_ts),
            )

        con.commit()
        con.close()

        import hipop.server.data as data_mod
        importlib.reload(data_mod)

        view = data_mod.get_replenishment_view("ksa", limit=10)
        stock_status = view["stock_status"]
        rows = view.get("rows", [])

        assert stock_status.get("ready") is True, (
            "fixture 库存应 ready，实际: status={}, age={}h, "
            "stock_rows={}, coverage={}".format(
                stock_status.get("status"),
                stock_status.get("stock_age_hours"),
                stock_status.get("stock_rows"),
                stock_status.get("coverage"),
            )
        )
        assert len(rows) == 3, (
            "应返回 3 行，实际 {}: {}".format(
                len(rows), [r.get("partner_sku") for r in rows]
            )
        )
        qtys = [r["qty"] for r in rows]
        assert qtys == [30, 20, 10], (
            "应按 weekly_total_replenish DESC 排列 [30,20,10]，实际 {}".format(qtys)
        )
        assert rows[0]["partner_sku"] == "FSKU_005", (
            "Top 1 应是 FSKU_005 (qty=30)，实际 {}".format(rows[0]["partner_sku"])
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


# ── 运行 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_authoritative_field_is_weekly_total_replenish,
        test_wf5_source_chain_fields_exist,
        test_tbu0010a_in_wf2_absent_in_wf5_ksa,
        test_sab0433a_in_wf2_absent_in_wf5_ksa,
        test_tbn0201a_not_in_ksa_entity,
        test_entry_point_respects_stock_readiness_gate,
        test_missing_stock_returns_not_ready_not_zero,
        test_entry_point_ready_fixture_returns_ordered_wf5_rows,
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

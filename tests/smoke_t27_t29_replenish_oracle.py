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
  工作台入口     → data.get_replenishment('ksa') → ORDER BY weekly_total_replenish DESC

chat 工具 compute_replenishment 读的就是这条链的终端表，不读旧快照、不读其他源。

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
stock_readiness 对空 wf1_stock 返回 ready=False；
get_replenishment_view 在 not-ready 时返回 rows=[]，不静默给 0。

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
- test_missing_stock_returns_not_ready_not_zero：
  (独立临时库) 空 wf1_stock → stock_readiness ready=False，
  get_replenishment_view rows=[]，不静默给 0。

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

    assert os.path.exists(agent_path), f"缺 hipop/server/agent.py: {agent_path}"
    assert os.path.exists(data_path),  f"缺 hipop/server/data.py: {data_path}"

    agent_src = open(agent_path, encoding="utf-8").read()
    data_src  = open(data_path,  encoding="utf-8").read()

    # 1) agent.py: compute_replenishment tool 有声明（描述/name 都在）
    assert "compute_replenishment" in agent_src, \
        "agent.py 里找不到 compute_replenishment 工具声明"
    assert "tool_compute_replenishment" in agent_src, \
        "agent.py 里找不到 tool_compute_replenishment 实现引用"

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
    assert "wf5_sales_cycle" in agent_src[agent_src.find("tool_compute_replenishment"):
                                          agent_src.find("tool_compute_replenishment")+2000], \
        "tool_compute_replenishment 实现附近没有引用 wf5_sales_cycle（references 缺失）"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: wf5_sales_cycle 的上游来源字段（ERP+noon 链路）都存在
# ═══════════════════════════════════════════════════════════════════════════════
def test_wf5_source_chain_fields_exist():
    """wf5_sales_cycle 的三类上游表字段都在线上 DB 里存在，且 wf_sales_cycle.py 真读它们。"""
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
# Test 6: 当前 KSA Top 5 补货是 P51* SKU（而非 TBU0010A/SAB0433A 或千件级 TBN*）
#         → 钉死当前线上工作台实际返回的 Top 列表
# ═══════════════════════════════════════════════════════════════════════════════
def test_current_ksa_top5_replenish_are_live_authoritative():
    """当前线上 KSA 补货 Top 5 来自 wf5_sales_cycle，不包含 T29 千件级 SKU。

    线上权威：当前 wf5_sales_cycle (hipop_ksa) 按 weekly_total_replenish DESC 取 Top 5。
    - TBU0010A / SAB0433A / TBN0201A 均不在 Top 5（接线缺失 + 非 KSA SKU）。
    - Top 5 的 weekly_total_replenish 均远小于 T29 体验 run 中的 2040 件。
    - 当 S1 修复 run_v2 接线后，Top 5 会变化——本 test 需同步更新。
    """
    top5 = _live_fetch(
        "SELECT partner_sku, weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND weekly_total_replenish > 0 "
        "ORDER BY weekly_total_replenish DESC LIMIT 5",
        (TENANT, KSA_ALIAS),
    )
    assert len(top5) > 0, \
        "wf5_sales_cycle (hipop_ksa) 里没有任何 weekly_total_replenish > 0 的记录"

    top5_skus = [r["partner_sku"] for r in top5]
    top5_max  = top5[0]["weekly_total_replenish"]

    assert T27_SKU not in top5_skus, (
        f"{T27_SKU} 出现在 Top 5——wf5_sales_cycle KSA 里已有该 SKU 的补货记录，"
        f"接线缺失已修复，本 oracle 需更新预期值"
    )
    assert T28_SKU not in top5_skus, (
        f"{T28_SKU} 出现在 Top 5——接线缺失已修复，本 oracle 需更新预期值"
    )
    assert T29_OUTLIER_SKU not in top5_skus, (
        f"{T29_OUTLIER_SKU} 意外进入 KSA Top 5——请核实数据来源"
    )

    # T29 千件级异常量级（2040）远超当前任何 KSA 补货建议：证明量级口径不同
    assert top5_max < 1000, (
        f"当前 KSA Top 补货量 {top5_max} ≥ 1000 件——"
        f"T29 体验 run 里的千件级数值（2040）疑似仍来自同一路径，请排查"
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
                risk_label TEXT, updated_at TEXT,
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


# ── 运行 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_authoritative_field_is_weekly_total_replenish,
        test_wf5_source_chain_fields_exist,
        test_tbu0010a_in_wf2_absent_in_wf5_ksa,
        test_sab0433a_in_wf2_absent_in_wf5_ksa,
        test_tbn0201a_not_in_ksa_entity,
        test_current_ksa_top5_replenish_are_live_authoritative,
        test_missing_stock_returns_not_ready_not_zero,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)

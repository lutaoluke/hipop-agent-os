"""Smoke：库存 ↔ 补货 合流验收（KSA, WS-62）—— 端到端真实数据 + 库存未就绪降级钉死。

这条「真实库存 → 补货建议 → 运营入口」的连接在代码层已存在
（wf_sales_cycle.run_v2 从 wf1_stock 读 noon_saleable/pending/overseas/yiwu/dongguan
算进 current_pipeline）。本 smoke 不搭新连接，而是把它做端到端真实数据验收 + 钉死
库存未就绪/不完整时的降级边界，三死法逐条堵死。

钉死的承重墙（DoD + 防三死法）：

验收 1 · 端到端真实数据（happy / 防「占位假数据」「接线错位」）
  同一 SKU：wf1_stock 真实库存高 → 补货建议为 0（管道充足）；
  把同一 SKU 库存改成显著更低 → 补货建议数量 > 0 且更大。
  并且这一变化必须传到**运营入口** get_replenishment_view（运营真看到的那份），
  不是只在 wf5 表里变。证明「数值真随真实库存变」，不是假绿。

验收 2 · 库存未就绪/不完整 降级（防「死代码短路」）
  - 空库存（0 行）→ 运营入口明确「库存未就绪」(status=empty, ready=False)；
  - 不完整库存（非空但覆盖率不足）→ 入口明确「库存不完整」(status=incomplete, ready=False)；
  - 完整库存 → status=ready, ready=True；
  绝不静默给 0 / 假确定建议：未就绪时 ready=False 必须出现在入口响应本身
  （API / chat 同源），而不是只落在 run_v2 的 log。

fail-then-pass（改动前为何 FAIL）：
  base commit 无 data.stock_readiness / data.get_replenishment_view，运营入口
  对「空库存 / 不完整库存」与「真 0 需求」无从区分 → 验收 2 断言 import/属性即
  FAIL；接上 readiness + 入口视图后 PASS。

跑法：
  python3 tests/smoke_replenish_stock_integration.py     或   make test
  （纯 SQLite 临时库，固定 HIPOP_DB；不碰 PG / 不碰 live hipop.db / 不需要 server。）
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import server.data（及 run_v2 内部 `from server import data`）之前固定 SQLite
# 路径 + 清掉 PG。两个 import 副本都从 env 读 DB_PATH → 指向同一个临时库文件。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix="_ws62.db", delete=False).name
os.environ.pop("DB_URL", None)

TENANT = 1
# 三个互不相干的销售主体，分别验「就绪 / 空 / 不完整」，避免互相污染。
ENT_READY = {"alias": "hipop_ksa", "country": "SA"}
ENT_EMPTY = {"alias": "hipop_empty", "country": "SA"}
ENT_INCOMPLETE = {"alias": "hipop_part", "country": "SA"}

SENS_SKU = "TBSENS001"   # 验收 1 的目标 SKU：库存变 → 建议变


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


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


def _seed_entity(ent):
    from hipop.server import data
    conn = data.conn()
    conn.execute(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, "
        "store_id, currency, active) VALUES (?,?,?,?,?,?,?,1)",
        (TENANT, ent["alias"], ent["country"], "noon", f"store_{ent['alias']}",
         "s1", "SAR"),
    )
    conn.commit()
    conn.close()


def _add_sku(alias, sku, *, listed=1, sales=False, stock=None):
    """插一个 wf2_sku；可选灌销量与 wf1_stock 行。stock=None 表示**不建库存行**（缺覆盖）。"""
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
        (TENANT, alias, sku, f"商品 {sku}", listed, 99.0, profit, s10, s30, s60, s180),
    )
    if stock is not None:
        conn.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, noon_saleable_qty, "
            "pending_inbound_qty, overseas_total_qty, yiwu_qty, dongguan_qty, total_stock) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, alias, sku, stock, 0, 0, 0, 0, stock),
        )
    conn.commit()
    conn.close()


def _add_hub(alias_country_sku, avg_days=30):
    """给某 SKU 建一条 wf3 物流 hub 行（KSA），提供 completed_avg_total_days → avg_transit。"""
    from hipop.server import data
    sku = alias_country_sku
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


def _set_stock(alias, sku, qty):
    from hipop.server import data
    conn = data.conn()
    conn.execute(
        "UPDATE wf1_stock SET noon_saleable_qty=?, total_stock=? "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (qty, qty, TENANT, alias, sku),
    )
    conn.commit()
    conn.close()


def _run_wf5(alias):
    from hipop.workflows import wf_sales_cycle
    return wf_sales_cycle.run_v2(TENANT, entity_aliases=[alias], verbose=False)


def _wf5_weekly_total(alias, sku):
    from hipop.server import data
    conn = data.conn()
    r = conn.execute(
        "SELECT weekly_total_replenish FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, alias, sku),
    ).fetchone()
    conn.close()
    return (r["weekly_total_replenish"] if r else None)


# ── 验收 1 · 端到端真实数据：库存变 → 建议变（运营入口同步变）───────────
def test_stock_change_changes_replenishment():
    print("== test_stock_change_changes_replenishment (验收 1: 数值真随真实库存变) ==")
    from hipop.server import data
    check = _Checker()

    # 就绪主体：>=10 个上架 SKU 且各有库存行（满足 run_v2 上游非空 + readiness ready）。
    # 其中目标 SKU 带销量 + 物流 hub，使补货量真由 current_pipeline 决定。
    for i in range(12):
        _add_sku(ENT_READY["alias"], f"FILL{i:03d}", sales=False, stock=500)
    _add_sku(ENT_READY["alias"], SENS_SKU, sales=True, stock=400)
    _add_hub(SENS_SKU, avg_days=30)

    # 高库存（400 ≥ 目标管道 (30+7)*10=370）→ 管道充足 → 建议 0
    _set_stock(ENT_READY["alias"], SENS_SKU, 400)
    _run_wf5(ENT_READY["alias"])
    qty_high = _wf5_weekly_total(ENT_READY["alias"], SENS_SKU)
    rows_high = data.get_replenishment_view("ksa", limit=200)["rows"]
    in_high = any(r["partner_sku"] == SENS_SKU for r in rows_high)

    # 低库存（50 ≪ 目标）→ 管道缺口大 → 建议 > 0
    _set_stock(ENT_READY["alias"], SENS_SKU, 50)
    _run_wf5(ENT_READY["alias"])
    qty_low = _wf5_weekly_total(ENT_READY["alias"], SENS_SKU)
    rows_low = data.get_replenishment_view("ksa", limit=200)["rows"]
    low_row = next((r for r in rows_low if r["partner_sku"] == SENS_SKU), None)

    check("高库存时 wf5 建议为 0（管道充足）", qty_high == 0, f"qty_high={qty_high}")
    check("低库存时 wf5 建议 > 0（管道缺口）", (qty_low or 0) > 0, f"qty_low={qty_low}")
    check("库存变化真改变了建议数量（非写死/非假绿）",
          qty_low != qty_high, f"qty_high={qty_high} qty_low={qty_low}")
    check("高库存时运营入口不把它列为必补", in_high is False)
    check("低库存时运营入口列出它且数量一致",
          low_row is not None and low_row["qty"] == qty_low,
          f"low_row={low_row} qty_low={qty_low}")
    return check.failures


# ── 验收 2 · 空库存：运营入口明确「库存未就绪」───────────────────────
def test_empty_stock_surfaces_not_ready():
    print("== test_empty_stock_surfaces_not_ready (验收 2: 空库存降级) ==")
    from hipop.server import data
    check = _Checker()

    _seed_entity(ENT_EMPTY)
    for i in range(15):  # 有上架 SKU，但**完全没有** wf1_stock 行
        _add_sku(ENT_EMPTY["alias"], f"EMP{i:03d}", sales=True, stock=None)

    # 用 country=SA 的第二主体没法靠 store 名解析（store→entity 走 country 唯一）。
    # 这里直接按 alias 验 readiness（入口解析逻辑由 ready 主体的 e2e 覆盖）。
    st = data.stock_readiness(TENANT, ENT_EMPTY["alias"])
    check("空库存 readiness 不就绪", st["ready"] is False, st)
    check("空库存 status=empty", st["status"] == "empty", st)
    check("message 明确『未就绪』而非静默 0", "未就绪" in st["message"], st)
    check("readiness 自带覆盖统计（listed/stock_rows 真读）",
          st["listed_skus"] == 15 and st["stock_rows"] == 0, st)
    return check.failures


# ── 验收 2 · 不完整库存：运营入口明确「库存不完整」─────────────────────
def test_incomplete_stock_surfaces_not_ready():
    print("== test_incomplete_stock_surfaces_not_ready (验收 2: 不完整库存降级) ==")
    from hipop.server import data
    check = _Checker()

    _seed_entity(ENT_INCOMPLETE)
    # 20 个上架 SKU，只有 14 个有库存行 → 覆盖率 0.70 < 0.8，但行数 14 ≥ 10（非全空）
    for i in range(20):
        has_stock = i < 14
        _add_sku(ENT_INCOMPLETE["alias"], f"PRT{i:03d}", sales=True,
                 stock=300 if has_stock else None)

    st = data.stock_readiness(TENANT, ENT_INCOMPLETE["alias"])
    check("不完整库存 readiness 不就绪", st["ready"] is False, st)
    check("不完整库存 status=incomplete", st["status"] == "incomplete", st)
    check("非全空（stock_rows≥10）也被识别为不完整",
          st["stock_rows"] >= 10 and st["coverage"] < 0.8, st)
    check("message 明确『不完整』", "不完整" in st["message"], st)
    return check.failures


# ── 就绪态对照：完整库存 → ready=True（防把正常态误报降级）──────────────
def test_ready_stock_is_ready():
    print("== test_ready_stock_is_ready (就绪态对照) ==")
    from hipop.server import data
    check = _Checker()
    st = data.stock_readiness(TENANT, ENT_READY["alias"])
    check("完整库存 status=ready", st["status"] == "ready", st)
    check("完整库存 ready=True", st["ready"] is True, st)
    check("覆盖率达标", st["coverage"] >= 0.8, st)
    return check.failures


# ── 入口同源：API 视图 + chat 工具都带 readiness（防降级只覆盖 runner）────
def test_entry_points_carry_readiness():
    print("== test_entry_points_carry_readiness (入口同源 readiness) ==")
    from hipop.server import data
    check = _Checker()

    view = data.get_replenishment_view("ksa", limit=10)
    check("API 视图含 stock_status", "stock_status" in view, list(view.keys()))
    check("API 视图含 rows", "rows" in view and isinstance(view["rows"], list))
    check("ksa（就绪主体）入口 ready=True", view["stock_status"]["ready"] is True, view["stock_status"])

    # chat 工具同源（anthropic 不可用时跳过该断言，保证 make test 健壮）
    try:
        from hipop.server import agent
    except Exception as e:  # pragma: no cover
        print(f"  (skip chat tool 断言: agent 导入失败 {e})")
        return check.failures
    res = agent.tool_compute_replenishment("ksa", limit=10)
    check("chat 工具响应含 stock_status（与 API 同源）", "stock_status" in res, list(res.keys()))
    check("chat 工具 stock_status.ready 与入口一致",
          res.get("stock_status", {}).get("ready") is True, res.get("stock_status"))
    return check.failures


if __name__ == "__main__":
    import traceback

    _init_db()
    # ENT_READY = hipop_ksa 由 schema_v2.sql 预置（含 hipop_uae），无需再 seed。

    tests = [
        test_stock_change_changes_replenishment,
        test_empty_stock_surfaces_not_ready,
        test_incomplete_stock_surfaces_not_ready,
        test_ready_stock_is_ready,
        test_entry_points_carry_readiness,
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

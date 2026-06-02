"""Smoke test: ERP 商品总表 + 订单成本利润接入（WS-17）— fail-then-pass 承重墙。

钉死的是 v2 ERP 自动拉取管道（ingest_erp_products_v2 + ingest_erp_sales_v2）的两个口径：

  ① wf2_sku 商品静态字段 + 利润率
     一个 product_id 下两个 partner_sku：一个绑了 noon platform_sku（is_listed=1）、
     一个没绑（is_listed=0）。两条都要入 wf2_sku，product_id/title/image/cost_price
     正确，绑定那条的 latest_profit_rate 由销量窗口写入。

  ② wf2_orders 订单级成本/利润
     背景：老物理切表 wf2_<a>_orders 早有 cost_local/cost_pack/cost_intl/profit/
     profit_rate 五列，但**从来没有 ingest 写它们**（典型"占位假数据"）；v2 schema
     之前干脆把列删了。WS-17 把列补回 schema_v2.sql，并接上真正的生产写入：
     ingest_erp_sales_v2 从 ERP SKU 成本利润详情按 item_nr upsert 进 wf2_orders。

fail-then-pass 证明：
  - 默认跑 → 订单成本利润 ingest 生效 → wf2_orders 有成本/利润行 → 全过。
  - SMOKE_SKIP_ORDER_COST=1 跑 → 跳过订单成本 ingest（模拟改动前）→ wf2_orders 空 →
    cost/profit 断言 FAIL。

全程用 monkeypatch 假 ERP 响应（无需真 ERP token / 网络），SQLite 自洽。

跑法：
  python3 tests/smoke_erp_orders_contract.py
  SMOKE_SKIP_ORDER_COST=1 python3 tests/smoke_erp_orders_contract.py   # 看"改动前"会 fail
  或 make test-erp-orders
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import data 之前设好 SQLite 路径 + 清掉 DB_URL，否则按 PG 跑。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

# 与 schema_v2.sql 里 seed 的 tenant 1 隔离，避免 list_entities 把 hipop_ksa/uae 也带出来。
TID = 7777
ALIAS = "smoke_ksa"
STORE = "SMOKE-NOON-KSA"

_TMP_DBS = []


# ── 假 ERP 响应 ──────────────────────────────────────────────────────
# 一个 product_id（PRD777）下两个 SKU：
#   SKU777A 绑了 noon（NOON-AAA）→ is_listed=1
#   SKU777B 没绑 → is_listed=0，image 回退到 product 主图
_FAKE_PRODUCT = {
    "product_id": "PRD777",
    "name": "测试便携榨汁杯",
    "brand": {"name": "TestBrand"},
    "product_category_detail": "厨房/榨汁",
    "product_choose_admin": {"username": "ops1"},
    "created_at": "2026-01-01",
    "images": ["https://img/prd777_main.jpg"],
    "skus": [
        {
            "sku_id": "SKU777A",
            "sku_image": "https://img/sku777a.jpg",
            "cost_price": "40.0",
            "platform_sku_ids": [
                {"platform": {"id": 2}, "store": {"name": STORE},
                 "platform_sku_id": "NOON-AAA"},
            ],
        },
        {
            "sku_id": "SKU777B",
            "sku_image": None,
            "cost_price": "25.0",
            "platform_sku_ids": [],   # 未绑 noon → 未上架
        },
    ],
}

# product-order-statistics：只有上架的 SKU777A 有动销绑定（窗口里出现）。
_FAKE_STAT_ITEM = {
    "sku_id": "SKU777A",
    "sku": {
        "product_id": "PRD777",
        "sku_image": "https://img/sku777a.jpg",
        "platform_sku_ids": [
            {"platform": {"id": 2}, "store": {"name": STORE},
             "platform_sku_id": "NOON-AAA"},
        ],
    },
    "sales_count": ["SA: 12"],
    "avg_price": ["SA: 100 SAR"],
    "newest_sale_price": ["SA: 110 SAR"],
    "newest_profit_rate": ["SA: 30%"],
    "newest_sale_time": "2026-05-30 10:00:00",
}

# SKU 详情：订单级成本/利润拆解（两单，含币种字符串 / 裸数字混合，验证容错解析）。
_FAKE_SKU_DETAIL = {
    "SKU777A": {
        "code": 200,
        "data": {
            "sku_id": "SKU777A",
            "orders": [
                {"item_nr": "PSA001", "order_date": "2026-05-30",
                 "cost_local": 30.0, "cost_pack": 2.0, "cost_intl": 8.0,
                 "profit": 30.0, "profit_rate": "30%"},
                {"item_nr": "PSA002", "ordered_time": "2026-05-28 09:00:00",
                 "cost_local": 31, "cost_pack": 2, "cost_intl": 9,
                 "profit": 28, "profit_rate": "25%"},
            ],
        },
    },
}


def _fake_fetch_products(token, max_pages=None, store_id=None, **kw):
    yield _FAKE_PRODUCT


def _fake_fetch_window(token, nation_id, days, page_size=50, max_items=None):
    return [_FAKE_STAT_ITEM]


def _fake_fetch_sku_cost_detail(token, erp_sku_id, nation_id=None):
    return _FAKE_SKU_DETAIL.get(erp_sku_id, {"code": 200, "data": {"orders": []}})


# ── DB 装置 ─────────────────────────────────────────────────────────
def _build_db():
    """新建临时 SQLite DB，建 v2 业务表，把所有 data 模块实例指向它，seed 1 个 entity。"""
    import importlib
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _TMP_DBS.append(path)

    hdata = importlib.import_module("hipop.server.data")
    hdata.DB_PATH = path
    hdata.set_current_tenant(TID)

    conn = hdata.conn()
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    cut = sql.find("DO $$")   # DO $$ 之后是 PG RLS policy，SQLite 跳过
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    # seed 本测试用的销售主体（独立 tenant，避免 schema seed 的 tenant 1 干扰）
    conn.execute(
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (TID, ALIAS, "SA", "Noon", STORE, 85, "SAR"),
    )
    conn.commit()
    conn.close()
    return path


def _patch_and_run(path):
    """monkeypatch 假 ERP + 把脚本的 data 实例指向同一个 DB，跑 products_v2 + sales_v2。"""
    from hipop.scripts import ingest_erp_products_v2 as p_mod
    from hipop.scripts import ingest_erp_sales_v2 as s_mod

    # 脚本内部 `from server import data` 是另一个 module 实例（非 hipop.server.data），
    # SQLite 下只要 DB_PATH 指向同一文件即可共享。
    p_mod._data.DB_PATH = path
    s_mod._data.DB_PATH = path

    # token：假登录
    p_mod.get_erp_token_for_tenant = lambda tid: "fake-token"
    s_mod.get_erp_token_for_tenant = lambda tid: "fake-token"
    # ERP 抓取：全假
    p_mod.fetch_products = _fake_fetch_products
    s_mod.fetch_window = _fake_fetch_window
    s_mod.fetch_sku_cost_detail = _fake_fetch_sku_cost_detail

    p_mod.run_v2(tenant_id=TID)
    s_mod.run_v2(tenant_id=TID)


# ── 断言工具 ────────────────────────────────────────────────────────
class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def _approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def _sku_row(conn, partner_sku):
    r = conn.execute(
        "SELECT * FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TID, ALIAS, partner_sku),
    ).fetchone()
    return dict(r) if r else None


def _order_rows(conn, partner_sku):
    rows = conn.execute(
        "SELECT * FROM wf2_orders WHERE tenant_id=? AND entity_alias=? AND partner_sku=? "
        "ORDER BY item_nr",
        (TID, ALIAS, partner_sku),
    ).fetchall()
    return [dict(r) for r in rows]


def test_contract():
    print("== test_contract (ERP 商品总表 + 订单成本利润) ==")
    import importlib
    hdata = importlib.import_module("hipop.server.data")

    path = _build_db()
    _patch_and_run(path)

    check = _Checker()
    conn = hdata.conn()

    # ── wf2_sku：两条 SKU 都入库 ──
    a = _sku_row(conn, "SKU777A")
    b = _sku_row(conn, "SKU777B")
    print("[wf2_sku]")
    check("SKU777A 入 wf2_sku", a is not None)
    check("SKU777B 入 wf2_sku", b is not None)
    if a:
        check("A product_id==PRD777", a["product_id"] == "PRD777", f"got {a['product_id']!r}")
        check("A title==测试便携榨汁杯", a["title"] == "测试便携榨汁杯", f"got {a['title']!r}")
        check("A image==sku777a", a["image_url"] == "https://img/sku777a.jpg",
              f"got {a['image_url']!r}")
        check("A cost_price==40.0", _approx(a["cost_price"], 40.0), f"got {a['cost_price']!r}")
        check("A is_listed==1（绑了 noon）", a["is_listed"] == 1, f"got {a['is_listed']!r}")
        check("A noon_sku==NOON-AAA", a["noon_sku"] == "NOON-AAA", f"got {a['noon_sku']!r}")
        check("A latest_profit_rate==0.30（销量窗口写入）",
              _approx(a["latest_profit_rate"], 0.30), f"got {a['latest_profit_rate']!r}")
    if b:
        check("B product_id==PRD777", b["product_id"] == "PRD777", f"got {b['product_id']!r}")
        check("B is_listed==0（未绑 noon）", b["is_listed"] == 0, f"got {b['is_listed']!r}")
        check("B cost_price==25.0", _approx(b["cost_price"], 25.0), f"got {b['cost_price']!r}")
        check("B image 回退到 product 主图",
              b["image_url"] == "https://img/prd777_main.jpg", f"got {b['image_url']!r}")

    # ── wf2_orders：SKU777A 两单成本/利润按 item_nr 落库 ──
    print("[wf2_orders]")
    orders = _order_rows(conn, "SKU777A")
    check("SKU777A 有 2 条订单成本行", len(orders) == 2, f"got {len(orders)} rows")
    by_nr = {o["item_nr"]: o for o in orders}
    if "PSA001" in by_nr:
        o1 = by_nr["PSA001"]
        check("PSA001 cost_local==30", _approx(o1["cost_local"], 30.0), f"got {o1['cost_local']!r}")
        check("PSA001 cost_pack==2", _approx(o1["cost_pack"], 2.0), f"got {o1['cost_pack']!r}")
        check("PSA001 cost_intl==8", _approx(o1["cost_intl"], 8.0), f"got {o1['cost_intl']!r}")
        check("PSA001 profit==30", _approx(o1["profit"], 30.0), f"got {o1['profit']!r}")
        check("PSA001 profit_rate==0.30", _approx(o1["profit_rate"], 0.30),
              f"got {o1['profit_rate']!r}")
        check("PSA001 关联 partner_sku==SKU777A", o1["partner_sku"] == "SKU777A")
        check("PSA001 noon_sku==NOON-AAA", o1["noon_sku"] == "NOON-AAA", f"got {o1['noon_sku']!r}")
        check("PSA001 source==erp", o1["source"] == "erp", f"got {o1['source']!r}")
        check("PSA001 order_date==2026-05-30", o1["order_date"] == "2026-05-30",
              f"got {o1['order_date']!r}")
    else:
        check("PSA001 存在", False, f"orders={list(by_nr)}")
    if "PSA002" in by_nr:
        o2 = by_nr["PSA002"]
        check("PSA002 cost_intl==9", _approx(o2["cost_intl"], 9.0), f"got {o2['cost_intl']!r}")
        check("PSA002 profit_rate==0.25", _approx(o2["profit_rate"], 0.25),
              f"got {o2['profit_rate']!r}")
        check("PSA002 order_date==2026-05-28（ordered_time 裁日期）",
              o2["order_date"] == "2026-05-28", f"got {o2['order_date']!r}")
    else:
        check("PSA002 存在", False, f"orders={list(by_nr)}")

    # 未上架 SKU 不该有订单成本
    check("SKU777B 无订单成本行（未上架）", len(_order_rows(conn, "SKU777B")) == 0)

    conn.close()
    if check.failures and os.environ.get("SMOKE_SKIP_ORDER_COST") == "1":
        print("  （SMOKE_SKIP_ORDER_COST=1：这是预期的'改动前 fail'。去掉变量再跑应全过。）")
    return check.failures


def _seed_noon_order(conn, item_nr, **noon):
    """预置一条 noon 订单行（模拟 noon ASN/订单 ingest 先写过），供 ERP 成本 upsert 撞键。"""
    cols = ["tenant_id", "entity_alias", "partner_sku", "noon_sku", "item_nr",
            "order_date", "status", "seller_price", "customer_paid", "currency",
            "fulfillment", "source"]
    vals = [TID, ALIAS, "SKU777A", "NOON-AAA", item_nr,
            noon.get("order_date"), noon.get("status"), noon.get("seller_price"),
            noon.get("customer_paid"), noon.get("currency"),
            noon.get("fulfillment"), "noon"]
    ph = ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO wf2_orders ({','.join(cols)}) VALUES ({ph})", vals)
    conn.commit()


def test_noon_not_overwritten():
    """确定性规则：ERP 只补成本/利润，noon 订单字段（含 order_date）不被 ERP 覆盖。

    红队发现的口径洞（验门人打回）：冲突更新若 order_date 走 ERP 优先，已有 noon 订单
    日期会被 ERP SKU detail 日期盖掉，污染最新订单日期/销量窗口口径。

    fail-then-pass：seed 一条 noon PSA001（order_date=2026-06-15、seller_price/
    customer_paid/status/fulfillment 都是 noon 值、成本利润为空），再跑 sales_v2 让
    ERP 成本明细按 item_nr 撞键 upsert。改动前（order_date 走 excluded 优先）→
    order_date 被覆成 ERP 的 2026-05-30 → FAIL；改动后保 noon → PASS。同时成本/利润
    必须被 ERP 写入。
    """
    print("== test_noon_not_overwritten (ERP 不覆盖 noon 订单字段) ==")
    import importlib
    hdata = importlib.import_module("hipop.server.data")

    path = _build_db()

    # 先写 noon 订单行（ERP ingest 之前 noon 已落库的状态）
    conn = hdata.conn()
    _seed_noon_order(conn, "PSA001",
                     order_date="2026-06-15", status="delivered",
                     seller_price=199.0, customer_paid=210.0,
                     currency="SAR", fulfillment="FBN")
    conn.close()

    # 再跑 ERP 管道：products_v2 + sales_v2（ERP detail 的 PSA001 order_date=2026-05-30）
    _patch_and_run(path)

    check = _Checker()
    conn = hdata.conn()
    orders = _order_rows(conn, "SKU777A")
    by_nr = {o["item_nr"]: o for o in orders}
    if "PSA001" in by_nr:
        o = by_nr["PSA001"]
        # ── noon 字段必须保住（ERP 不覆盖）──
        check("PSA001 order_date 保 noon==2026-06-15（不被 ERP 覆盖）",
              o["order_date"] == "2026-06-15", f"got {o['order_date']!r}")
        check("PSA001 seller_price 保 noon==199",
              _approx(o["seller_price"], 199.0), f"got {o['seller_price']!r}")
        check("PSA001 customer_paid 保 noon==210",
              _approx(o["customer_paid"], 210.0), f"got {o['customer_paid']!r}")
        check("PSA001 status 保 noon==delivered",
              o["status"] == "delivered", f"got {o['status']!r}")
        check("PSA001 fulfillment 保 noon==FBN",
              o["fulfillment"] == "FBN", f"got {o['fulfillment']!r}")
        # ── 成本/利润必须被 ERP 写入（这一半 upsert 是真的）──
        check("PSA001 cost_local 被 ERP 写入==30",
              _approx(o["cost_local"], 30.0), f"got {o['cost_local']!r}")
        check("PSA001 profit 被 ERP 写入==30",
              _approx(o["profit"], 30.0), f"got {o['profit']!r}")
        check("PSA001 profit_rate 被 ERP 写入==0.30",
              _approx(o["profit_rate"], 0.30), f"got {o['profit_rate']!r}")
    else:
        check("PSA001 存在", False, f"orders={list(by_nr)}")

    conn.close()
    if check.failures and os.environ.get("SMOKE_SKIP_ORDER_COST") == "1":
        print("  （SMOKE_SKIP_ORDER_COST=1：成本未 ingest，这是预期的'改动前 fail'。）")
    return check.failures


def run():
    failures = test_contract()
    print()
    failures += test_noon_not_overwritten()
    print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ ERP 商品总表 + 订单成本利润接入 smoke 全过")
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

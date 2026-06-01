"""Smoke test: 销量录入数据契约（WS-15）— fail-then-pass 承重墙。

钉死的是 v2 销量管道（wf2_sku / wf2_orders）的输出口径。背景见
hipop/workflows/wf_sales_static_v2.py：schema 建了 latest_customer_paid /
order_item_nrs_json / anomalies_json 等列，但在改动前 v2 路径里**没有任何入口**
去算它们（典型"占位假数据 / 死代码"）。

本 smoke：
  1. 临时 SQLite tenant + SA sales_entity
  2. 加载 ERP 商品/销量 fixture（tests/fixtures/erp_seed_SA.json）→ 直接写 wf2_sku
     （模拟 ingest_erp_products_v2 + ingest_erp_sales_v2 跑完的状态，不需真 ERP 登录）
  3. 跑现有入口：ingest_noon_csv_v2.process_csv_v2 → aggregate_sales_v2
  4. 跑 merge：wf_sales_static_v2.merge_entity_v2（本需求新增的合并入口）
  5. 逐字段断言契约值 == fixture 预期

fail-then-pass 证明：
  设环境变量 SMOKE_SKIP_MERGE=1 跑本 smoke，会跳过第 4 步 merge——
  模拟"改动前"状态，latest_customer_paid / 订单号集合 / anomalies_json 全为空，
  断言必然 FAIL。去掉该变量（默认）跑，merge 生效 → 全部 PASS。

跑法：
  python3 tests/smoke_sales_contract.py
  SMOKE_SKIP_MERGE=1 python3 tests/smoke_sales_contract.py   # 看"改动前"会 fail
  或 make test-sales-contract
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 关键：必须在 import hipop.server.data 之前设好 SQLite 路径 + 清掉 DB_URL，
# 否则 data 模块会按 PG 跑。data.DB_PATH 在 import 时从 env 读。
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["HIPOP_DB"] = _TMP_DB.name
os.environ.pop("DB_URL", None)

FIXTURES = os.path.join(HERE, "fixtures")
CSV_PATH = os.path.join(FIXTURES, "noon_SA_20260531.csv")
SEED_PATH = os.path.join(FIXTURES, "erp_seed_SA.json")

SKIP_MERGE = os.environ.get("SMOKE_SKIP_MERGE") == "1"


def _load_schema(conn):
    """建 wf2_sku / wf2_orders / sales_entities（schema_v2.sql 里 CREATE TABLE 部分）。
    跳过 PG 专有的 DO $$ ... $$ RLS 块（SQLite 不识别）。"""
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    # 第一个 DO $$ 之后全是 PG RLS policy，SQLite 跳过
    cut = sql.find("DO $$")
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if not s:
            continue
        conn.execute(s)
    conn.commit()


def _seed(conn, seed):
    ent = seed["entity"]
    conn.execute(
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (ent["tenant_id"], ent["alias"], ent["country"], ent["platform"],
         ent["store_name"], ent["store_id"], ent["currency"]),
    )
    for s in seed["skus"]:
        conn.execute(
            "INSERT INTO wf2_sku "
            "(tenant_id, entity_alias, partner_sku, erp_sku_id, noon_sku, product_id, "
            " title, fulfillment, brand, cost_price, currency, is_listed, "
            " latest_price, avg_price, latest_profit_rate, sales_180d) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ent["tenant_id"], ent["alias"], s["partner_sku"], s["erp_sku_id"],
             s["noon_sku"], s["product_id"], s["title"], s["fulfillment"], s["brand"],
             s["cost_price"], s["currency"], s["is_listed"],
             s["latest_price"], s["avg_price"], s["latest_profit_rate"], s["sales_180d"]),
    )
    conn.commit()


def _row(conn, tenant_id, alias, partner_sku):
    cur = conn.execute(
        "SELECT * FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, alias, partner_sku),
    )
    r = cur.fetchone()
    return dict(r) if r else None


def _approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def run():
    from hipop.server import data
    from hipop.scripts import ingest_noon_csv_v2

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid = ent["tenant_id"]
    alias = ent["alias"]
    data.set_current_tenant(tid)

    conn = data.conn()
    _load_schema(conn)
    _seed(conn, seed)

    # ── 跑现有入口：noon CSV 入库 → 聚合窗口 ──
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    ingest_noon_csv_v2.aggregate_sales_v2(tid, alias, conn)

    # ── merge：本需求新增的合并步骤（SMOKE_SKIP_MERGE=1 时跳过 → 模拟改动前）──
    if not SKIP_MERGE:
        from hipop.workflows import wf_sales_static_v2
        wf_sales_static_v2.merge_entity_v2(tid, alias, conn)

    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            failures.append(name)
            print(f"  ✗ {name} {detail}")

    # ── SKU 1: TBB0116A（happy path，ERP 价 == noon 价，无异常）──
    a = _row(conn, tid, alias, "TBB0116A")
    print("[TBB0116A]")
    check("latest_customer_paid==110.0 (最近一单 noon 实付)",
          _approx(a["latest_customer_paid"], 110.0), f"got {a['latest_customer_paid']!r}")
    check("total_orders==4", a["total_orders"] == 4, f"got {a['total_orders']!r}")
    check("valid_orders==3", a["valid_orders"] == 3, f"got {a['valid_orders']!r}")
    check("cancel_count==1", a["cancel_count"] == 1, f"got {a['cancel_count']!r}")
    check("return_count==1", a["return_count"] == 1, f"got {a['return_count']!r}")
    check("cancel_rate==0.25", _approx(a["cancel_rate"], 0.25), f"got {a['cancel_rate']!r}")
    check("return_rate==1/3", _approx(a["return_rate"], 1.0 / 3),
          f"got {a['return_rate']!r}")
    check("avg_price==100.0", _approx(a["avg_price"], 100.0), f"got {a['avg_price']!r}")
    check("latest_price(noon)==100.0", _approx(a["latest_price"], 100.0),
          f"got {a['latest_price']!r}")
    check("total_revenue==323.0", _approx(a["total_revenue"], 323.0),
          f"got {a['total_revenue']!r}")
    check("latest_order_date==2026-05-30", a["latest_order_date"] == "2026-05-30",
          f"got {a['latest_order_date']!r}")
    nrs = json.loads(a["order_item_nrs_json"]) if a["order_item_nrs_json"] else None
    check("order_item_nrs==[PSA001,PSA002,PSA003,PSA004]",
          nrs == ["PSA001", "PSA002", "PSA003", "PSA004"], f"got {nrs!r}")
    check("anomalies_json is None (价相符且有 noon 订单)",
          a["anomalies_json"] is None, f"got {a['anomalies_json']!r}")
    # 窗口字段由 aggregate_sales_v2 写：不耦合"今天"，只断言已填 + 单调
    check("sales_180d 已填且 >= sales_10d",
          a["sales_180d"] is not None and a["sales_10d"] is not None
          and a["sales_180d"] >= a["sales_10d"],
          f"180d={a['sales_180d']!r} 10d={a['sales_10d']!r}")

    # ── SKU 2: TBB0200X（ERP 价 200 vs noon 价 150 → price_mismatch）──
    b = _row(conn, tid, alias, "TBB0200X")
    print("[TBB0200X]")
    check("latest_customer_paid==160.0", _approx(b["latest_customer_paid"], 160.0),
          f"got {b['latest_customer_paid']!r}")
    check("latest_price 被 noon 覆盖为 150.0", _approx(b["latest_price"], 150.0),
          f"got {b['latest_price']!r}")
    anb = json.loads(b["anomalies_json"]) if b["anomalies_json"] else []
    pm = [x for x in anb if x.get("type") == "price_mismatch"]
    check("anomalies 含 price_mismatch(noon=150,erp=200)",
          len(pm) == 1 and _approx(pm[0].get("noon"), 150.0)
          and _approx(pm[0].get("erp"), 200.0), f"got {anb!r}")
    nrsb = json.loads(b["order_item_nrs_json"]) if b["order_item_nrs_json"] else None
    check("order_item_nrs==[PSB001]", nrsb == ["PSB001"], f"got {nrsb!r}")

    # ── SKU 3: TBB0300Z（ERP 有动销但无 noon 订单 → no_noon_orders）──
    c = _row(conn, tid, alias, "TBB0300Z")
    print("[TBB0300Z]")
    anc = json.loads(c["anomalies_json"]) if c["anomalies_json"] else []
    check("anomalies 含 no_noon_orders",
          any(x.get("type") == "no_noon_orders" for x in anc), f"got {anc!r}")
    check("latest_customer_paid 为空（无 noon 订单）",
          c["latest_customer_paid"] is None, f"got {c['latest_customer_paid']!r}")
    check("order_item_nrs_json 为空（无 noon 订单）",
          c["order_item_nrs_json"] is None, f"got {c['order_item_nrs_json']!r}")

    conn.close()
    print()
    if failures:
        print(f"✗ {len(failures)} 项契约断言失败: {failures}")
        if SKIP_MERGE:
            print("  （SMOKE_SKIP_MERGE=1：这是预期的'改动前 fail'。去掉变量再跑应全过。）")
        return 1
    print("✓ 销量录入数据契约 smoke 全过")
    return 0


if __name__ == "__main__":
    try:
        rc = run()
    finally:
        try:
            os.unlink(_TMP_DB.name)
        except OSError:
            pass
    sys.exit(rc)

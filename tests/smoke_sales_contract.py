"""Smoke test: 销量录入数据契约（WS-15）— fail-then-pass 承重墙。

钉死的是 v2 销量管道（wf2_sku / wf2_orders）的输出口径。背景见
hipop/workflows/wf_sales_static_v2.py：schema 建了 latest_customer_paid /
order_item_nrs_json / anomalies_json 等列，但在改动前 v2 路径里**没有任何入口**
去算它们（典型"占位假数据 / 死代码"）。

两块断言：
  test_contract  —— 逐字段断言契约值 == fixture 预期（含静态 ERP 字段 +
                     动态 merge 字段 + 全部 sales 窗口的明确预期）。
  test_failguard —— 钉死"merge 失败不许假绿"：monkeypatch merge_entity_v2 抛错，
                     跑真 upload pipeline（api._run_pipeline_v2），断言最终
                     step 99 必须 error / ok:false（而不是 done/ok:true）。

fail-then-pass 证明：
  - test_contract：SMOKE_SKIP_MERGE=1 跑 → 跳过 merge → 动态契约字段全空 → FAIL；
    默认跑 → merge 生效 → 全过。
  - test_failguard：改动前 api.py 的 aggregate/merge except 没置 failed=True →
    最终 ok:true 假绿 → 断言 FAIL；改动后置 failed=True → ok:false → 过。

跑法：
  python3 tests/smoke_sales_contract.py
  SMOKE_SKIP_MERGE=1 python3 tests/smoke_sales_contract.py   # 看 contract"改动前"会 fail
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
# 否则 data 模块会按 PG 跑。data.DB_PATH 在 import 时从 env 读，跑中可改 data.DB_PATH。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

FIXTURES = os.path.join(HERE, "fixtures", "sales")
CSV_PATH = os.path.join(FIXTURES, "noon_SA_20260531.csv")
SEED_PATH = os.path.join(FIXTURES, "erp_seed_SA.json")

# 时间窗基准日 —— 让 sales_*d 可确定性断言，不耦合"跑测试那天"。
# fixture CSV 里的订单日期就是按这个基准日设计的（见下方 expected 注释）。
AS_OF = "2026-06-01"

SKIP_MERGE = os.environ.get("SMOKE_SKIP_MERGE") == "1"

_TMP_DBS = []


# sqlite 友好的 agent_events（schema.sql 里是 PG BIGSERIAL，sqlite 要 AUTOINCREMENT）
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
    """新建临时 SQLite DB，建好 v2 业务表 + sales_entities + agent_events。"""
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _TMP_DBS.append(path)
    data.DB_PATH = path  # conn() 每次读模块全局 DB_PATH
    conn = data.conn()
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    cut = sql.find("DO $$")  # 第一个 DO $$ 之后全是 PG RLS policy，SQLite 跳过
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.execute(_AGENT_EVENTS_DDL)
    conn.commit()
    return conn


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


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


# ── 预期窗口值（AS_OF=2026-06-01，见 fixture CSV 订单日期）────────────
# TBB0116A 非取消单：PSA001(05-30) PSA002(05-28) PSA004(05-15)；PSA003(05-20)取消
#   10d(>=05-22):2  30d(>=05-02):3  60d:3  90d:3  120d:3  180d:3
# TBB0200X 非取消单：PSB001(05-29) → 各窗口都=1
# TBB0300Z 无 noon 订单 → aggregate 不动它 → 保留 ERP seed sales_180d=8，其它窗口 NULL
_EXPECT_WIN = {
    "TBB0116A": {"sales_10d": 2, "sales_30d": 3, "sales_60d": 3,
                 "sales_90d": 3, "sales_120d": 3, "sales_180d": 3},
    "TBB0200X": {"sales_10d": 1, "sales_30d": 1, "sales_60d": 1,
                 "sales_90d": 1, "sales_120d": 1, "sales_180d": 1},
}


def test_contract():
    print("== test_contract ==")
    from hipop.server import data
    from hipop.scripts import ingest_noon_csv_v2

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]
    data.set_current_tenant(tid)

    conn = _fresh_db(data)
    _seed(conn, seed)

    # ── 跑现有入口：noon CSV 入库 → 聚合窗口（固定 as_of）──
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    ingest_noon_csv_v2.aggregate_sales_v2(tid, alias, conn, as_of=AS_OF)

    # ── merge：本需求新增的合并步骤（SMOKE_SKIP_MERGE=1 时跳过 → 模拟改动前）──
    if not SKIP_MERGE:
        from hipop.workflows import wf_sales_static_v2
        wf_sales_static_v2.merge_entity_v2(tid, alias, conn)

    check = _Checker()

    # ── 国别 / 店铺名（来自 sales_entities，SKU 通过 entity_alias 关联）──
    erow = conn.execute(
        "SELECT country, store_name, store_id FROM sales_entities "
        "WHERE tenant_id=? AND alias=?", (tid, alias)).fetchone()
    erow = dict(erow)
    print("[entity]")
    check("country==SA", erow["country"] == "SA", f"got {erow['country']!r}")
    check("store_name==SMOKE-NOON-KSA", erow["store_name"] == "SMOKE-NOON-KSA",
          f"got {erow['store_name']!r}")

    # ── SKU 1: TBB0116A（happy path，ERP 价 == noon 价，无异常）──
    a = _row(conn, tid, alias, "TBB0116A")
    print("[TBB0116A]")
    # 静态 ERP 契约字段
    check("product_id==PRD116", a["product_id"] == "PRD116", f"got {a['product_id']!r}")
    check("title==便携榨汁杯 A 款", a["title"] == "便携榨汁杯 A 款", f"got {a['title']!r}")
    check("fulfillment==FBN", a["fulfillment"] == "FBN", f"got {a['fulfillment']!r}")
    check("latest_profit_rate==0.30", _approx(a["latest_profit_rate"], 0.30),
          f"got {a['latest_profit_rate']!r}")
    # 动态 merge 契约字段
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
    # sales 窗口逐项等于预期（as_of 固定）
    for k, exp in _EXPECT_WIN["TBB0116A"].items():
        check(f"{k}=={exp}", a[k] == exp, f"got {a[k]!r}")

    # ── SKU 2: TBB0200X（ERP 价 200 vs noon 价 150 → price_mismatch）──
    b = _row(conn, tid, alias, "TBB0200X")
    print("[TBB0200X]")
    check("product_id==PRD200", b["product_id"] == "PRD200", f"got {b['product_id']!r}")
    check("title==无线小风扇 X 款", b["title"] == "无线小风扇 X 款", f"got {b['title']!r}")
    check("fulfillment==FBP", b["fulfillment"] == "FBP", f"got {b['fulfillment']!r}")
    check("latest_profit_rate==0.40", _approx(b["latest_profit_rate"], 0.40),
          f"got {b['latest_profit_rate']!r}")
    check("latest_customer_paid==160.0", _approx(b["latest_customer_paid"], 160.0),
          f"got {b['latest_customer_paid']!r}")
    check("latest_price 被 noon 覆盖为 150.0", _approx(b["latest_price"], 150.0),
          f"got {b['latest_price']!r}")
    anb = json.loads(b["anomalies_json"]) if b["anomalies_json"] else []
    pm = [x for x in anb if x.get("type") == "price_mismatch"]
    check("anomalies 含 price_mismatch(noon=150,erp=200)",
          len(pm) == 1 and _approx(pm[0].get("noon"), 150.0)
          and _approx(pm[0].get("erp"), 200.0), f"got {anb!r}")
    # 结构化字段必须明确写入（field / diff / source_window），不止 noon/erp
    check("price_mismatch.field==latest_price",
          pm and pm[0].get("field") == "latest_price",
          f"got {pm[0].get('field') if pm else None!r}")
    check("price_mismatch.diff==50.0", pm and _approx(pm[0].get("diff"), 50.0),
          f"got {pm[0].get('diff') if pm else None!r}")
    check("price_mismatch.source_window==latest_order",
          pm and pm[0].get("source_window") == "latest_order",
          f"got {pm[0].get('source_window') if pm else None!r}")
    nrsb = json.loads(b["order_item_nrs_json"]) if b["order_item_nrs_json"] else None
    check("order_item_nrs==[PSB001]", nrsb == ["PSB001"], f"got {nrsb!r}")
    for k, exp in _EXPECT_WIN["TBB0200X"].items():
        check(f"{k}=={exp}", b[k] == exp, f"got {b[k]!r}")

    # ── SKU 3: TBB0300Z（ERP 有动销但无 noon 订单 → no_noon_orders）──
    c = _row(conn, tid, alias, "TBB0300Z")
    print("[TBB0300Z]")
    check("product_id==PRD300", c["product_id"] == "PRD300", f"got {c['product_id']!r}")
    check("title==桌面收纳盒 Z 款", c["title"] == "桌面收纳盒 Z 款", f"got {c['title']!r}")
    anc = json.loads(c["anomalies_json"]) if c["anomalies_json"] else []
    nn = [x for x in anc if x.get("type") == "no_noon_orders"]
    check("anomalies 含 no_noon_orders", len(nn) == 1, f"got {anc!r}")
    # 结构化字段：field / noon / erp / source_window 明确写入（不止自由文案 note）
    check("no_noon_orders.field==sales_180d",
          nn and nn[0].get("field") == "sales_180d",
          f"got {nn[0].get('field') if nn else None!r}")
    check("no_noon_orders.noon==0", nn and nn[0].get("noon") == 0,
          f"got {nn[0].get('noon') if nn else None!r}")
    check("no_noon_orders.erp==8 (ERP seed sales_180d)",
          nn and nn[0].get("erp") == 8, f"got {nn[0].get('erp') if nn else None!r}")
    check("no_noon_orders.source_window==sales_180d",
          nn and nn[0].get("source_window") == "sales_180d",
          f"got {nn[0].get('source_window') if nn else None!r}")
    check("latest_customer_paid 为空（无 noon 订单）",
          c["latest_customer_paid"] is None, f"got {c['latest_customer_paid']!r}")
    check("order_item_nrs_json 为空（无 noon 订单）",
          c["order_item_nrs_json"] is None, f"got {c['order_item_nrs_json']!r}")
    check("sales_180d 保留 ERP seed=8（无 noon 订单不被覆盖）",
          c["sales_180d"] == 8, f"got {c['sales_180d']!r}")

    # ── SKU 4: TBB0400N（noon CSV 有订单但 ERP 商品库无此 SKU → noon_only）──
    # 不在 erp_seed_SA.json 里；process_csv_v2 应自动插入该 SKU 行，
    # merge 后 anomalies_json 应含 noon_only。
    d = _row(conn, tid, alias, "TBB0400N")
    print("[TBB0400N]")
    check("noon-only SKU 被 process_csv_v2 插入 wf2_sku", d is not None,
          "row missing")
    if d:
        check("erp_sku_id 为空（确认是 noon-only，无 ERP 建档）",
              d["erp_sku_id"] is None, f"got {d['erp_sku_id']!r}")
        and4 = json.loads(d["anomalies_json"]) if d["anomalies_json"] else []
        no = [x for x in and4 if x.get("type") == "noon_only"]
        check("anomalies 含 noon_only", len(no) == 1, f"got {and4!r}")
        check("noon_only.field==erp_sku_id",
              no and no[0].get("field") == "erp_sku_id",
              f"got {no[0].get('field') if no else None!r}")
        check("noon_only.noon==1 (noon 订单数)",
              no and no[0].get("noon") == 1,
              f"got {no[0].get('noon') if no else None!r}")
        check("noon_only.erp is None",
              no and no[0].get("erp") is None,
              f"got {no[0].get('erp') if no else '<missing>'!r}")
        check("latest_price 被 noon 覆盖为 80.0", _approx(d["latest_price"], 80.0),
              f"got {d['latest_price']!r}")

    conn.close()
    if check.failures and SKIP_MERGE:
        print("  （SMOKE_SKIP_MERGE=1：这是预期的'改动前 fail'。去掉变量再跑应全过。）")
    return check.failures


def test_failguard():
    """merge 失败不许假绿：跑真 upload pipeline，断言 step 99 = error/ok:false。"""
    print("== test_failguard (merge 抛错 → pipeline 必须 error/ok:false) ==")
    from hipop.server import data, api
    from hipop.workflows import wf_sales_static_v2

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]
    data.set_current_tenant(tid)

    conn = _fresh_db(data)
    _seed(conn, seed)
    conn.close()

    # monkeypatch：让 merge 抛错（_run_pipeline_v2 内部是 from ... import merge_entity_v2，
    # 调用时才解析模块属性 → 改模块属性即可拦截）
    orig = wf_sales_static_v2.merge_entity_v2

    def _boom(*a, **k):
        raise RuntimeError("injected merge failure (red team)")

    wf_sales_static_v2.merge_entity_v2 = _boom
    try:
        api._run_pipeline_v2("smoke-failguard-task", [CSV_PATH], tid)
    finally:
        wf_sales_static_v2.merge_entity_v2 = orig

    events = data.get_events_after("smoke-failguard-task", 0)
    check = _Checker()
    step99 = [e for e in events if e["step_no"] == 99]
    check("有 step 99 终态事件", len(step99) >= 1, f"events={[(e['step_no'], e['status']) for e in events]}")
    if step99:
        final = step99[-1]
        check("step 99 status==error（不是 done 假绿）", final["status"] == "error",
              f"got {final['status']!r}")
        try:
            payload = json.loads(final["message"])
            ok = payload.get("ok")
        except (ValueError, TypeError):
            ok = "<unparsable>"
        check("step 99 ok==False", ok is False, f"got {ok!r}")
    # 同时确认 merge 步确实记了 error
    step5_err = [e for e in events if e["step_no"] == 5 and e["status"] == "error"]
    check("step 5（合并契约字段）记了 error", len(step5_err) >= 1,
          f"step5 events={[ (e['step_no'], e['status']) for e in events if e['step_no']==5]}")
    return check.failures


def run():
    failures = []
    failures += test_contract()
    print()
    failures += test_failguard()
    print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ 销量录入数据契约 smoke 全过（contract + failguard）")
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

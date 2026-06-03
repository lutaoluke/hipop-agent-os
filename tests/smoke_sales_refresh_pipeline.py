"""Smoke: WS-21 — 接通「每周 + 按需」销量刷新流水线（fail-then-pass 承重墙）。

背景 / 为什么存在
-----------------
评级/预测算法（sales_grading）+ noon 视角合并（wf_sales_static_v2.merge_entity_v2）
早已就位，**上传 noon CSV** 时由 api._run_pipeline_v2 调到。但「不再上传新 CSV、
只想用现有订单重刷一遍销量/评级」与「每周/每日 scheduler 自动重刷 noon 评级」
这两条入口此前**没有任何注册**去调它（refresh_all_v2 只拉 ERP 销量，从不重算 noon
聚合/评级）—— 典型「接线缺失」死法：函数在、却没人在按需/每周路径上调。

WS-21 补三处接线，本 smoke 钉死：
  1. runner `wf2_sales_refresh_v2`（workflow_runners）+ verifier（verifiers）
     —— 按需 /run-workflow 能触发，worker 跑完调对应 verifier。
  2. refresh_all_v2 的 steps 含 wf2_sales_refresh_v2 —— 每周/每日 scheduler 也重算评级。
  3. governance_actions.yaml run_workflow.allowed_workflows + RBAC trigger_workflow
     —— 有副作用 workflow 已登记，无权限/未登录用户不能触发。

fail-then-pass（改动前为何 FAIL）：
  · base commit 无 runner / verifier / governance 条目 / refresh_all step →
    下面 4 个「注册」断言全 FAIL；本 PR 接上后全 PASS。
  · 功能面：现有订单 → 运行 runner → 有订单的 SKU 必落 sales_grade；
    人为抹掉一个 grade → verifier 必判 FAIL（不假绿）。

跑法：
  python3 tests/smoke_sales_refresh_pipeline.py     或   make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db / 不需要 server。）
"""
import os
import sys
import json
import datetime
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import hipop.server.data 之前固定 SQLite 路径 + 清掉 PG。
os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix="_ws21.db", delete=False).name
os.environ.pop("DB_URL", None)

FIXTURES = os.path.join(HERE, "fixtures", "sales")
CSV_PATH = os.path.join(FIXTURES, "noon_SA_20260531.csv")
SEED_PATH = os.path.join(FIXTURES, "erp_seed_SA.json")

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


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def _fresh_db(data):
    path = tempfile.NamedTemporaryFile(suffix="_ws21.db", delete=False).name
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
            conn.execute(s)
    conn.execute(_AGENT_EVENTS_DDL)
    conn.commit()
    return conn


def _seed(conn, seed):
    ent = seed["entity"]
    conn.execute(
        "INSERT INTO sales_entities "
        "(tenant_id, alias, country, platform, store_name, store_id, currency, active) "
        "VALUES (?,?,?,?,?,?,?,1)",
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


def _skus_with_orders(conn, tid, alias):
    rows = conn.execute(
        "SELECT k.partner_sku, k.sales_grade, k.total_orders FROM wf2_sku k "
        "WHERE k.tenant_id=? AND k.entity_alias=? AND EXISTS ("
        "  SELECT 1 FROM wf2_orders o WHERE o.tenant_id=k.tenant_id "
        "  AND o.entity_alias=k.entity_alias AND o.partner_sku=k.partner_sku)",
        (tid, alias),
    ).fetchall()
    return {r["partner_sku"]: dict(r) for r in rows}


# ── 1. 注册接线（fail-then-pass 锚点）─────────────────────────────
def test_wiring_registered():
    print("== test_wiring_registered (base commit 应 FAIL：无 runner/verifier/governance) ==")
    from hipop.runtime import workflow_runners, verifiers
    check = _Checker()

    check("runner wf2_sales_refresh_v2 已注册（按需入口）",
          "wf2_sales_refresh_v2" in workflow_runners.list_runners(),
          f"runners={workflow_runners.list_runners()}")
    check("verifier wf2_sales_refresh_v2 已注册（worker 跑完会调）",
          "wf2_sales_refresh_v2" in verifiers._VERIFIERS)

    # refresh_all_v2（每周/每日 scheduler 入口）的 steps 必须含销量刷新
    import inspect
    src = inspect.getsource(workflow_runners._run_refresh_all)
    check("refresh_all_v2 steps 含 wf2_sales_refresh_v2（接通每周/每日重算评级）",
          "wf2_sales_refresh_v2" in src,
          "refresh_all_v2 未把 noon 评级接进每周链路")

    # governance + WORKFLOW_REGISTRY 登记（有副作用动作）
    import yaml
    with open(os.path.join(REPO, "hipop", "server", "governance_actions.yaml")) as f:
        gov = yaml.safe_load(f)
    allowed = (gov.get("run_workflow") or {}).get("allowed_workflows") or []
    check("governance allowed_workflows 含 wf2_sales_refresh_v2",
          "wf2_sales_refresh_v2" in allowed, f"allowed={allowed}")

    from hipop.server import api
    check("WORKFLOW_REGISTRY 含 wf2_sales_refresh_v2（/run-workflow 可路由）",
          "wf2_sales_refresh_v2" in api.WORKFLOW_REGISTRY)
    return check.failures


# ── 2. RBAC：无权限 / 未登录不能触发有副作用 workflow ───────────────
def test_rbac_gate():
    print("== test_rbac_gate ==")
    from hipop.server import rbac
    check = _Checker()
    check("ops 可触发（trigger_workflow）", rbac.can({"role": "ops"}, "trigger_workflow") is True)
    check("owner 可触发", rbac.can({"role": "owner"}, "trigger_workflow") is True)
    check("无权限角色(guest) 不可触发",
          rbac.can({"role": "guest"}, "trigger_workflow") is False)
    check("run_workflow chat tool 映射到 trigger_workflow 权限",
          rbac.TOOL_PERMISSION.get("run_workflow") == "trigger_workflow")
    # 未登录：/run-workflow 依赖 get_current_user，未登录抛 401（这里验依赖存在性）
    from hipop.server import api
    import inspect
    sig = inspect.signature(api.api_run_workflow)
    check("/run-workflow 强制登录（user=Depends(get_current_user)）",
          "user" in sig.parameters)
    return check.failures


# ── 3. 按需 runner 功能 + verifier 真查（接线缺失/占位假数据死法）─────
def test_on_demand_refresh():
    print("== test_on_demand_refresh ==")
    from hipop.server import data
    from hipop.scripts import ingest_noon_csv_v2
    from hipop.runtime import workflow_runners, verifiers
    check = _Checker()

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]
    data.set_current_tenant(tid)

    conn = _fresh_db(data)
    _seed(conn, seed)
    # 灌现有 noon 订单（不再"上传新 CSV"，模拟订单已在库）
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    # 抹掉所有评级 → 模拟"订单在、评级没跑过/过期"
    conn.execute("UPDATE wf2_sku SET sales_grade=NULL, forecast_10d=NULL, "
                 "forecast_30d=NULL, total_orders=NULL WHERE tenant_id=?", (tid,))
    conn.commit()
    conn.close()

    before = None
    cc = data.conn()
    before = _skus_with_orders(cc, tid, alias)
    cc.close()
    check("刷新前：有订单的 SKU 评级为空（待刷新）",
          all(v["sales_grade"] is None for v in before.values()) and len(before) >= 2,
          f"before={before}")

    # 跑按需 runner（生产路径：worker → get_runner → runner）
    runner = workflow_runners.get_runner("wf2_sales_refresh_v2")
    out = runner("task-ws21", tid, {"role": "ops"}, {}, {}, lambda: None, lambda p: None)
    check("runner 返回 summary", "wf2_sales_refresh" in (out or {}).get("summary", ""),
          f"out={out}")

    cc = data.conn()
    after = _skus_with_orders(cc, tid, alias)
    cc.close()
    check("刷新后：每个有订单的 SKU 都落了 sales_grade（评级真跑过）",
          all(v["sales_grade"] in ("A", "B", "C", "D") for v in after.values())
          and len(after) >= 2, f"after={after}")
    check("刷新后：有订单的 SKU total_orders > 0（聚合真跑，非占位）",
          all((v["total_orders"] or 0) > 0 for v in after.values()), f"after={after}")

    # verifier（worker 跑完会调）— happy path 全绿
    import time
    res = verifiers.run_verifier("wf2_sales_refresh_v2", "task-ws21", tid, time.time())
    check("run_verifier 非 None（verifier 接进 _VERIFIERS）", res is not None, f"res={res}")
    if res:
        check("verifier ok=True（链路全绿）", res["ok"] is True, f"res={res}")
        check("verifier evidence.graded_missing==0", res["evidence"]["graded_missing"] == 0,
              f"evidence={res['evidence']}")
        check("verifier evidence.skus_with_orders>=2",
              res["evidence"]["skus_with_orders"] >= 2, f"evidence={res['evidence']}")

    # 负向：人为抹掉一个有订单 SKU 的评级 → verifier 必须判 FAIL（不假绿）
    cc = data.conn()
    one = sorted(after.keys())[0]
    cc.execute("UPDATE wf2_sku SET sales_grade=NULL WHERE tenant_id=? AND partner_sku=?",
               (tid, one))
    cc.commit(); cc.close()
    bad = verifiers.run_verifier("wf2_sales_refresh_v2", "task-ws21", tid, time.time())
    check("抹掉一个评级后 verifier ok=False（拦死列/漏评级）", bad["ok"] is False, f"bad={bad}")
    check("verifier 命中 graded_missing>=1", bad["evidence"]["graded_missing"] >= 1,
          f"evidence={bad['evidence']}")
    return check.failures


# ── 4. 上传 noon CSV 路径 + data_health 新鲜度 ─────────────────────
def test_upload_path_and_health():
    print("== test_upload_path_and_health ==")
    from hipop.server import data, api
    check = _Checker()

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]
    data.set_current_tenant(tid)

    conn = _fresh_db(data)
    _seed(conn, seed)
    conn.close()

    # 真跑上传管道（CSV → ingest → 聚合 → 合并评级 → agent_events）
    api._run_pipeline_v2("task-ws21-upload", [CSV_PATH], tid)

    events = data.get_events_after("task-ws21-upload", 0)
    step99 = [e for e in events if e["step_no"] == 99]
    check("上传管道有 step 99 终态", len(step99) >= 1,
          f"events={[(e['step_no'], e['status']) for e in events]}")
    if step99:
        check("step 99 status==done（上传链路绿）", step99[-1]["status"] == "done",
              f"got {step99[-1]['status']!r}")

    # 评级字段更新时间推进：有订单的 SKU 落了 grade
    cc = data.conn()
    after = _skus_with_orders(cc, tid, alias)
    cc.close()
    check("上传后有订单的 SKU 落了 sales_grade",
          len(after) >= 2 and all(v["sales_grade"] in ("A", "B", "C", "D")
                                  for v in after.values()), f"after={after}")

    # data_health 新鲜度：noon_orders 反映真实最新订单日；erp_sales 今日刷新
    data.set_current_tenant(tid)
    h = data.get_data_health("KSA")
    src = h["sources"]
    check("data_health.noon_orders.latest==2026-05-31（最新订单日，真新鲜度）",
          src["noon_orders"]["latest"] == "2026-05-31",
          f"got {src['noon_orders']['latest']!r}")
    check("data_health.noon_orders.stale_days 非空（有真实新鲜度）",
          src["noon_orders"]["stale_days"] is not None,
          f"got {src['noon_orders']['stale_days']!r}")
    today = datetime.date.today().isoformat()
    check("data_health.erp_sales.latest==今天（wf2_sku 本次刷新）",
          src["erp_sales"]["latest"] == today, f"got {src['erp_sales']['latest']!r}")
    check("data_health.erp_sales.stale_days==0",
          src["erp_sales"]["stale_days"] == 0, f"got {src['erp_sales']['stale_days']!r}")
    return check.failures


def run():
    failures = []
    for t in (test_wiring_registered, test_rbac_gate,
              test_on_demand_refresh, test_upload_path_and_health):
        failures += t()
        print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ WS-21 每周+按需销量刷新流水线 smoke 全过")
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

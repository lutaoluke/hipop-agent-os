"""Smoke test: noon CSV ingest 硬化（WS-16）— fail-then-pass 承重墙。

钉死 `ingest_noon_csv_v2.process_csv_v2` 的两条残留口径，它们在 WS-15/17 的
`tests/smoke_sales_contract.py` 里没有被钉死：

  test_currency_merge —— noon 路径必须把 CSV 的 currency 合并回 wf2_sku，
      且方向正确（ERP 优先、noon 只兜底补空）：
        · noon-only SKU 或 ERP 尚未先写 currency 的场景：改动前 process_csv_v2
          只写 wf2_orders.currency，wf2_sku.currency 一直是 NULL（典型"占位假数据 /
          接线缺失"）。改动后 noon 兜底补 currency。
        · ERP 已写过 currency 的场景：noon 不覆盖。为真正钉住方向，本 smoke 让
          ERP=USD、noon CSV=SAR（二者不同），断言合并后仍为 USD。若 fixture 用
          ERP==CSV（同值），即使代码 COALESCE 反向让 noon 覆盖也照样绿 —— 那是盲点。

  test_dedup —— (tenant_id, entity_alias, partner_sku, item_nr) 去重：同一 item_nr
      在二次导入或同一文件重复出现时必须 UPDATE 而非重复计数。wf2_orders 行数、
      total_orders / valid_orders / sales_*d 不得翻倍。schema PK + ON CONFLICT
      已实现该行为，本 smoke 把它钉死，防回退。

fail-then-pass 证明：
  - test_currency_merge（两个方向都钉死）：
      · noon 兜底：改动前 process_csv_v2 不写 wf2_sku.currency → noon-only SKU 为
        NULL → FAIL；改动后 → currency==CSV → PASS。
      · ERP 优先：若把 ingest 的 `COALESCE(wf2_sku.currency, excluded.currency)`
        反向成 `COALESCE(excluded.currency, wf2_sku.currency)`（noon 覆盖 ERP），
        TBB0116A(ERP=USD) 会被 noon CSV(SAR) 覆盖成 SAR → FAIL；保持原向 → USD → PASS。
  - test_dedup：若把 wf2_orders 的 ON CONFLICT 退回成裸 INSERT（去重失效）→ 计数
    翻倍 → FAIL；保留去重 → PASS。

跑法：
  python3 tests/smoke_noon_ingest_hardening.py
  或 make test（自动聚合 tests/smoke_*.py）
"""
import os
import sys
import json
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

FIXTURES = os.path.join(HERE, "fixtures", "sales")
CSV_PATH = os.path.join(FIXTURES, "noon_SA_20260531.csv")
DUP_CSV = os.path.join(FIXTURES, "noon_SA_dup.csv")
SEED_PATH = os.path.join(FIXTURES, "erp_seed_SA.json")

# 时间窗基准日 —— 让 sales_*d 可确定性断言，不耦合"跑测试那天"。
AS_OF = "2026-06-01"

_TMP_DBS = []


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def _fresh_db():
    """临时 SQLite DB：建 v2 业务表（schema_v2.sql 的 SQLite 部分）。"""
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _TMP_DBS.append(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(REPO, "db", "schema_v2.sql"), encoding="utf-8") as f:
        sql = f.read()
    cut = sql.find("DO $$")  # 第一个 DO $$ 之后全是 PG RLS policy，SQLite 跳过
    if cut != -1:
        sql = sql[:cut]
    for stmt in sql.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    return conn


def _seed(conn, seed):
    """种 sales_entities + ERP 视角的 wf2_sku（模拟 ERP ingest 已跑）。"""
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
    r = conn.execute(
        "SELECT * FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (tenant_id, alias, partner_sku),
    ).fetchone()
    return dict(r) if r else None


def _order_count(conn, tenant_id, alias, partner_sku=None):
    if partner_sku:
        r = conn.execute(
            "SELECT COUNT(*) FROM wf2_orders "
            "WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
            (tenant_id, alias, partner_sku)).fetchone()
    else:
        r = conn.execute(
            "SELECT COUNT(*) FROM wf2_orders WHERE tenant_id=? AND entity_alias=?",
            (tenant_id, alias)).fetchone()
    return r[0]


def _run_pipeline(conn, tid, alias):
    """跑生产入口的核心三步（不经 fastapi，逻辑等价 api._run_pipeline_v2）。"""
    from hipop.scripts import ingest_noon_csv_v2
    from hipop.workflows import wf_sales_static_v2
    ingest_noon_csv_v2.aggregate_sales_v2(tid, alias, conn, as_of=AS_OF)
    wf_sales_static_v2.merge_entity_v2(tid, alias, conn)


def test_currency_merge():
    print("== test_currency_merge ==")
    from hipop.scripts import ingest_noon_csv_v2

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]

    # 关键：让 ERP currency ≠ noon CSV currency，才能真正钉死“ERP 优先”方向。
    # ERP seed 写 USD，CSV(noon_SA_20260531.csv) 里 TBB0116A 的 currency_code=SAR。
    # 合并后必须仍为 USD —— 若代码错误地让 noon 覆盖 ERP（COALESCE 反向），
    # 结果会变 SAR，本断言即 FAIL。原 fixture ERP==CSV==SAR 时此分支永远绿，是盲点。
    for s in seed["skus"]:
        if s["partner_sku"] == "TBB0116A":
            s["currency"] = "USD"

    conn = _fresh_db()
    _seed(conn, seed)
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    _run_pipeline(conn, tid, alias)

    check = _Checker()

    # noon-only SKU（不在 erp_seed → 无 ERP currency）：currency 必须来自 CSV。
    d = _row(conn, tid, alias, "TBB0400N")
    check("noon-only TBB0400N 入库", d is not None, "row missing")
    if d:
        check("noon-only.erp_sku_id 为空（确认无 ERP 建档）",
              d["erp_sku_id"] is None, f"got {d['erp_sku_id']!r}")
        check("noon-only.currency==SAR（noon 兜底写入，改动前为 NULL）",
              d["currency"] == "SAR", f"got {d['currency']!r}")

    # ERP 已写过 currency(USD) 的 SKU：noon CSV(SAR) 不覆盖（ERP 优先）。
    # ERP≠CSV，所以这条真正钉住方向 —— noon 若覆盖即变 SAR → FAIL。
    a = _row(conn, tid, alias, "TBB0116A")
    check("ERP-priced TBB0116A.currency 保持 USD（noon CSV 是 SAR，未被覆盖）",
          a and a["currency"] == "USD", f"got {a['currency'] if a else None!r}")

    conn.close()
    return check.failures


def test_dedup():
    print("== test_dedup ==")
    from hipop.scripts import ingest_noon_csv_v2

    seed = json.load(open(SEED_PATH, encoding="utf-8"))
    ent = seed["entity"]
    tid, alias = ent["tenant_id"], ent["alias"]
    check = _Checker()

    # ── 场景 A：二次导入同一文件 → 不得翻倍 ──
    conn = _fresh_db()
    _seed(conn, seed)
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    _run_pipeline(conn, tid, alias)
    base_total = _order_count(conn, tid, alias)
    a1 = _row(conn, tid, alias, "TBB0116A")

    # 再导入同一文件
    ingest_noon_csv_v2.process_csv_v2(tid, CSV_PATH, conn, entity_alias=alias)
    _run_pipeline(conn, tid, alias)
    again_total = _order_count(conn, tid, alias)
    a2 = _row(conn, tid, alias, "TBB0116A")

    print("[二次导入同一文件]")
    check("wf2_orders 总行数不变（base==again）",
          base_total == again_total, f"base={base_total} again={again_total}")
    check("TBB0116A total_orders 不翻倍（==4）",
          a2["total_orders"] == 4 == a1["total_orders"],
          f"a1={a1['total_orders']} a2={a2['total_orders']}")
    check("TBB0116A valid_orders 不翻倍（==3）",
          a2["valid_orders"] == 3 == a1["valid_orders"],
          f"a1={a1['valid_orders']} a2={a2['valid_orders']}")
    check("TBB0116A sales_30d 不翻倍（==3）",
          a2["sales_30d"] == 3 == a1["sales_30d"],
          f"a1={a1['sales_30d']} a2={a2['sales_30d']}")
    conn.close()

    # ── 场景 B：同一文件内 item_nr 重复 → UPDATE（末行胜）而非重复计数 ──
    conn = _fresh_db()
    _seed(conn, seed)
    # noon_SA_dup.csv: TBBDUP01 有 PDUP001(两行,后一行价更新) + PDUP002 → 应只 2 行
    ingest_noon_csv_v2.process_csv_v2(tid, DUP_CSV, conn, entity_alias=alias)
    _run_pipeline(conn, tid, alias)

    print("[同一文件内 item_nr 重复]")
    cnt = _order_count(conn, tid, alias, "TBBDUP01")
    check("TBBDUP01 wf2_orders 仅 2 行（PDUP001 去重，PDUP002 各 1）",
          cnt == 2, f"got {cnt}")
    dup = _row(conn, tid, alias, "TBBDUP01")
    check("TBBDUP01 total_orders==2（非 3）",
          dup["total_orders"] == 2, f"got {dup['total_orders']!r}")
    check("TBBDUP01 valid_orders==2", dup["valid_orders"] == 2,
          f"got {dup['valid_orders']!r}")
    check("TBBDUP01 sales_30d==2", dup["sales_30d"] == 2, f"got {dup['sales_30d']!r}")
    # 末行胜：PDUP001 的 seller_price 应被更新成 120（非 100，也非相加）
    p1 = conn.execute(
        "SELECT seller_price FROM wf2_orders WHERE tenant_id=? AND entity_alias=? "
        "AND partner_sku=? AND item_nr=?",
        (tid, alias, "TBBDUP01", "PDUP001")).fetchone()
    check("PDUP001 seller_price 被末行 UPDATE 为 120.0（非重复行相加）",
          p1 and abs(float(p1[0]) - 120.0) < 1e-6, f"got {p1[0] if p1 else None!r}")
    conn.close()

    return check.failures


def run():
    failures = []
    failures += test_currency_merge()
    print()
    failures += test_dedup()
    print()
    if failures:
        print(f"✗ {len(failures)} 项断言失败: {failures}")
        return 1
    print("✓ noon CSV ingest 硬化 smoke 全过（currency merge + dedup）")
    return 0


if __name__ == "__main__":
    try:
        rc = run()
    finally:
        for p in _TMP_DBS:
            try:
                os.unlink(p)
            except OSError:
                pass
    sys.exit(rc)

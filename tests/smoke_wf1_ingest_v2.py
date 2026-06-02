"""Smoke: WS-10 — Noon inventory + ASN/送仓 → v2 wf1_stock / staging。

fail-then-pass 承重墙（钉死三种死法）：
  · 接线缺失：断言 v2 producer 真把 noon_* 写进 wf1_stock、ASN 行落进
    wf1_asn_lines_staging（不是算了没人写）。
  · 死代码短路：断言**没有**创建/写入 v1 `wf1_<alias>_stock` per-alias 表
    （active runtime = v2，见 WS-9 核实）。
  · 占位假数据：noon producer 部分 upsert 不得覆盖 ERP 列，也不得伪造
    pending_inbound_qty（该列留给 WS-11，断言保持 NULL）；partner_sku 必须
    经平台 SKU 映射真正落到 partner_sku，不是把 Z 开头平台 SKU 当主键塞进去。

改动前（base commit）：ingest_noon_stock_csv_v2 / ingest_inbound_staging_v2
不存在 → import 失败 → smoke fail。改动后 → pass。

跑法：
  python3 tests/smoke_wf1_ingest_v2.py
  或 make test-wf1-ingest
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import re
import json
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "wf1_ingest_v2")

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1


def _extract_create(table: str) -> str:
    """从 db/schema_v2.sql 抠出指定表的 CREATE TABLE 语句（保持与真 schema 一致）。"""
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);",
        sql, re.DOTALL,
    )
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _setup_db():
    c = sqlite3.connect(_TMP_DB)
    for t in ("sales_entities", "wf2_sku", "wf1_stock", "tenant_erp_credentials"):
        c.executescript(_extract_create(t))
    # 销售主体：SA / AE 两个 entity
    c.executemany(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
        "VALUES (?,?,?,?,?,?,1)",
        [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
         (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
    )
    # Partner SKU ↔ Noon 平台 SKU 映射（producer 要用它把平台 SKU 回到 partner_sku）
    c.executemany(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
        [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
         (TENANT, "hipop_ksa", "SKU-B", "ZSA002"),
         (TENANT, "hipop_uae", "SKU-C", "ZAE001")],
    )
    # 预置一条带 ERP 列 + pending_inbound 的 wf1_stock 行，验 noon 部分 upsert 不覆盖
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7),
    )
    c.commit()
    c.close()


def _q(sql, params=()):
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def main():
    _setup_db()
    import ingest_noon_stock_csv_v2 as noon
    import ingest_inbound_staging_v2 as inbound

    # ── 1. Noon my inventory → wf1_stock.noon_* ────────────────────
    res = noon.run_v2(TENANT, file=os.path.join(FIXTURES, "noon_inventory.csv"))
    assert res["rows"] == 5, f"读入行数 {res['rows']} != 5"
    assert res["unmapped"] == 0, f"有 {res['unmapped']} 行平台 SKU 没映射到 partner_sku"
    assert res["skus"] == 3, f"写入 SKU 数 {res['skus']} != 3"
    assert res["by_alias"] == {"hipop_ksa": 2, "hipop_uae": 1}, res["by_alias"]

    rows = {r["partner_sku"]: r for r in _q("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    # 映射后主键是 partner_sku，绝不是 Z 开头平台 SKU
    assert set(rows) == {"SKU-A", "SKU-B", "SKU-C"}, set(rows)
    assert not any(k.startswith("Z") for k in rows), "平台 SKU 被当成主键写进去了"

    a = rows["SKU-A"]
    assert (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) == (15, 10, 5), a
    b = rows["SKU-B"]
    assert (b["noon_total_qty"], b["noon_saleable_qty"], b["noon_unsaleable_qty"]) == (20, 20, 0), b
    cc = rows["SKU-C"]
    assert (cc["noon_total_qty"], cc["noon_saleable_qty"], cc["noon_unsaleable_qty"]) == (10, 7, 3), cc
    assert len(json.loads(a["noon_warehouses_json"])) == 2, "warehouses_json 仓库明细缺失"

    # 部分 upsert：SKU-A 的 ERP 列与 pending_inbound 必须原样保留
    assert (a["yiwu_qty"], a["dongguan_qty"], a["overseas_total_qty"], a["total_stock"]) == (99, 88, 77, 264), \
        f"noon producer 覆盖了 ERP 列: {a}"
    assert a["pending_inbound_qty"] == 7, "noon producer 动了 pending_inbound_qty（应留给 WS-11）"
    # 新建行不得伪造 pending_inbound_qty
    assert b["pending_inbound_qty"] is None and cc["pending_inbound_qty"] is None, "凭空写了 pending_inbound_qty"

    # ── 1b. ERP 库存清单 fixture → wf1_stock ERP 列（DoD: ERP 行数 + ≥2 仓）──
    import ingest_erp_stock_v2 as erp

    def _erp_item(sku, qty):
        return {"sku_id": sku, "stock_total_available_count": qty,
                "platform_sku_ids": [{"platform": {"id": 2},
                                      "store": {"name": "HIPOP-NOON-KSA"},
                                      "platform_sku_id": "Z" + sku}]}
    # warehouse_id → items（义乌6 / 东莞15 / SA 海外 8,14；7,16=AE 海外返空）
    ERP_FIX = {
        6:  [_erp_item("SKU-A", 100), _erp_item("SKU-B", 50)],
        15: [_erp_item("SKU-D", 40)],
        8:  [_erp_item("SKU-A", 30)],
        14: [_erp_item("SKU-D", 10)],
    }
    touched = set()

    def fake_fetch(token, wid, **kw):
        assert token == "FAKE-TEST-TOKEN", "ERP fetch 没拿到注入的 token"
        touched.add(wid)
        return ERP_FIX.get(wid, [])

    eres = erp.run_v2(TENANT, token="FAKE-TEST-TOKEN", fetch_fn=fake_fetch)
    assert eres.get("hipop_ksa") == 3, f"ERP 写入 SKU 数 {eres} != 3"
    assert len([w for w in touched if ERP_FIX.get(w)]) >= 2, f"覆盖仓库 < 2: {touched}"

    erows = {r["partner_sku"]: r for r in _q("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    ea = erows["SKU-A"]
    assert (ea["yiwu_qty"], ea["dongguan_qty"], ea["overseas_total_qty"], ea["total_stock"]) == (100, 0, 30, 130), ea
    ed = erows["SKU-D"]
    assert (ed["dongguan_qty"], ed["overseas_total_qty"], ed["total_stock"]) == (40, 10, 50), ed
    # ERP producer 部分 upsert：不碰 noon 列（SKU-A 仍是步骤 1 的 noon 值）
    assert (ea["noon_total_qty"], ea["noon_saleable_qty"]) == (15, 10), f"ERP 覆盖了 noon 列: {ea}"

    # ── 1c. 缺 ERP token → 红灯，不回落 backfill/假数据 ─────────────
    before = _q("SELECT count(*) c FROM wf1_stock")[0]["c"]
    raised = False
    try:
        erp.run_v2(TENANT, fetch_fn=fake_fetch)   # token=None → 查无凭据 → None
    except RuntimeError:
        raised = True
    after = _q("SELECT count(*) c FROM wf1_stock")[0]["c"]
    assert raised, "缺 ERP token 时应 raise 红灯，而不是静默回落"
    assert before == after, "缺 token 仍写了库（回落到假数据/backfill）"

    # ── 2. ASN/送仓 → wf1_asn_lines_staging（供 WS-11）─────────────
    sres = inbound.run_v2(
        TENANT,
        noon_asn_file=os.path.join(FIXTURES, "noon_asn.csv"),
        erp_inbound_file=os.path.join(FIXTURES, "erp_inbound.csv"),
    )
    assert sres["noon_asn"]["lines"] == 3, sres["noon_asn"]
    assert sres["erp_inbound"]["lines"] == 2, sres["erp_inbound"]
    assert sres["asn_lines"] == 5, sres

    stg = _q("SELECT * FROM wf1_asn_lines_staging ORDER BY source, asn_number, partner_sku")
    assert len(stg) == 5, f"staging 行数 {len(stg)} != 5"
    noon_lines = [r for r in stg if r["source"] == "noon_asn"]
    assert len({r["asn_number"] for r in noon_lines}) == 2, "Noon ASN 应有 2 个 ASN Number"
    # ASN 明细按平台 SKU → partner_sku 映射回来
    asn1_a = [r for r in noon_lines if r["asn_number"] == "ASN001" and r["partner_sku"] == "SKU-A"]
    assert asn1_a and asn1_a[0]["noon_sku"] == "ZSA001" and asn1_a[0]["qty"] == 50, asn1_a
    assert not any(r["partner_sku"].startswith("Z") for r in stg), "staging 主键混进了平台 SKU"
    erp_lines = [r for r in stg if r["source"] == "erp_inbound"]
    assert {r["partner_sku"] for r in erp_lines} == {"SKU-A", "SKU-B"}, erp_lines

    # ── 3. 真实入口接线：WORKFLOW_REGISTRY + callable 可解析 ───────
    # （验门人 finding #1：只注册 runner、没进 registry，/run-workflow 会 400）
    from hipop.server import api
    for wf in ("wf1_noon_stock_v2", "wf1_inbound_staging_v2"):
        assert wf in api.WORKFLOW_REGISTRY, f"{wf} 不在 WORKFLOW_REGISTRY → /run-workflow 会 400"
        _, steps, _ = api.WORKFLOW_REGISTRY[wf]
        fn = api._resolve_callable(steps[0][2])
        assert callable(fn) and fn.__name__ == "run_v2", f"{wf} callable 解析失败: {steps}"
    from hipop.runtime import workflow_runners as wr
    assert {"wf1_noon_stock_v2", "wf1_inbound_staging_v2"} <= set(wr.list_runners()), \
        "runner 注册表缺新 workflow（后台 worker 路径）"

    # ── 4. 死代码短路：绝不创建/写入 v1 per-alias 表 ───────────────
    tables = {r["name"] for r in _q("SELECT name FROM sqlite_master WHERE type='table'")}
    v1 = {t for t in tables if re.fullmatch(r"wf1_hipop_(ksa|uae)_stock", t)}
    assert not v1, f"写到了 v1 per-alias 表（死法#2）: {v1}"

    print("✓ noon producer 写入 wf1_stock.noon_*（3 SKU，平台 SKU 已映射）")
    print("✓ ERP 库存清单 fixture 写入 wf1_stock ERP 列（3 SKU / ≥2 仓）")
    print("✓ noon↔ERP 互不覆盖；缺 ERP token 红灯且不回落 backfill/假数据")
    print("✓ ASN/送仓 5 行落 wf1_asn_lines_staging（2 ASN，partner_sku 已映射）")
    print("✓ wf1_noon_stock_v2 / wf1_inbound_staging_v2 进 WORKFLOW_REGISTRY + runner（真实入口可触发）")
    print("✓ 没有创建/写入 v1 wf1_<alias>_stock 路径")
    print("\n6/6 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

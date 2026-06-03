"""Smoke: WS-32.1 / WS-34 — noon live row producer 契约 + 3 类行 fixture 守门。

A 批契约门的「头」。本 smoke 钉死：noon 三类数据的 live row producer 有**唯一**
接口契约 + 确切行字段 + canonical 行 fixture（noon_live_contract.py），且这些 fixture
**真能进各自 ingest 的 _aggregate/_upsert 落库**（非常量 stub），后续 orders/ASN
socket(WS-35/37) 与抓取器(WS-N2/WS-57) 据此产出、不重定义字段 → 从结构上防漂移。

钉死三种死法：
  · 接线缺失：守门 `check_producers_registered` 在三类 live producer 全未注册时红灯，
    **指出缺哪类**；且 noon_my_inventory 经 registry 注册后，stock 既有生产注入点
    （noon_live_ingest runner 真消费它）立即读到 —— registry 接到真实生产路径，非死框架。
  · 死代码短路：三类 fixture 各自喂**真实 ingest**（run_live / process_csv_v2 /
    ingest_inbound_staging_v2.run_v2）真落库，逐字段断言聚合结果，证明契约字段 ==
    ingest 真正消费的字段，没另立一套。
  · 占位假数据：`validate_rows` 在 producer 边界比 ingest 更严 —— 缺必填 / 缺 SKU·路由
    键 / 含禁用字段一律红灯 raise，**不许靠 ingest 内部 safe_int/`or ""` 兜默认编数**。

fail-then-pass：
  改动前无 `hipop/scripts/noon_live_contract.py` → import 即 ImportError，smoke fail。
  实现后：守门红灯/绿灯、字段校验红灯、三类 fixture 真落库全部 pass。
  另：本 smoke 进 make test 自动聚合后，**当前**（WS-N2 抓取器未 land、orders/ASN 未
  socket）三类 producer 都未注册 → 守门红灯断言成立，正是「现在该红」的真实状态。

跑法：python3 tests/smoke_noon_live_contract.py   或   make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db。）
"""
import os
import re
import sys
import csv
import json
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_DB = tempfile.NamedTemporaryFile(suffix="_noon_contract.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _reset_db():
    """干净起点：建表 + entity/sku 映射 + ASN staging。每个落库子场景前调一次。"""
    from hipop.scripts import ingest_inbound_staging_v2 as inbound
    c = sqlite3.connect(_DB)
    try:
        for t in ("wf1_stock", "sales_entities", "wf2_sku", "wf2_orders"):
            c.executescript(f"DROP TABLE IF EXISTS {t};")
            c.executescript(_extract_create(t))
        c.executescript(f"DROP TABLE IF EXISTS {inbound.STAGING_TABLE};")
        inbound.ensure_staging_tables(c)
        c.executemany(
            "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
            "VALUES (?,?,?,?,?,?,1)",
            [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
             (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
        )
        # noon ingest 靠 noon_sku → partner_sku 映射（库存/ASN 用）。
        c.executemany(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
            [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
             (TENANT, "hipop_ksa", "SKU-B", "ZSA002"),
             (TENANT, "hipop_uae", "SKU-C", "ZAE001")],
        )
        c.commit()
    finally:
        c.close()


def _dump_stock():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


def main():
    import noon_live_contract as contract
    from hipop.scripts import ingest_noon_stock_csv_v2 as noon
    from hipop.scripts import ingest_noon_csv_v2 as orders
    from hipop.scripts import ingest_inbound_staging_v2 as inbound

    # ── 0. 契约结构自洽：DATA_TYPES / ROW_CONTRACTS / FIXTURES 一一对应 ──────
    assert set(contract.DATA_TYPES) == set(contract.ROW_CONTRACTS) == set(contract.FIXTURES), \
        "DATA_TYPES / ROW_CONTRACTS / FIXTURES 三者键不一致（契约不自洽）"
    assert set(contract.DATA_TYPES) == {"noon_orders", "noon_my_inventory", "noon_asn"}, \
        f"三类 noon 数据类型集合变了: {contract.DATA_TYPES}"
    print("✓ 契约自洽：三类 noon 数据 DATA_TYPES/ROW_CONTRACTS/FIXTURES 一一对应")

    # ── 1. 守门：三类 producer 全未注册 → 红灯，且指出缺哪类（接线缺失死法）──
    for t in contract.DATA_TYPES:           # 保证干净起点（别被环境/前序注册污染）
        contract.set_live_row_producer(t, None)
    gate = contract.check_producers_registered()
    assert gate["ok"] is False, f"三类全未注册时守门应红灯: {gate}"
    assert set(gate["missing"]) == set(contract.DATA_TYPES), \
        f"守门未指出全部缺失类型: {gate}"
    print(f"✓ 三类 live producer 全未注册 → 守门红灯，指出缺: {gate['missing']}")

    # ── 2. registry 接到真实生产路径：注册 my_inventory → stock 既有注入点立即读到 ──
    inv_producer = lambda tenant_id: [dict(r) for r in contract.FIXTURES["noon_my_inventory"]]
    contract.set_live_row_producer("noon_my_inventory", inv_producer)
    assert noon.get_live_row_producer() is inv_producer, \
        "经 registry 注册 my_inventory 后，stock 生产注入点没读到（registry 没接到生产路径）"
    # 注册 orders/asn → 守门转绿，缺失清零
    contract.set_live_row_producer("noon_orders", lambda t: list(contract.FIXTURES["noon_orders"]))
    contract.set_live_row_producer("noon_asn", lambda t: list(contract.FIXTURES["noon_asn"]))
    gate2 = contract.check_producers_registered()
    assert gate2["ok"] is True and not gate2["missing"], f"三类齐了守门应转绿: {gate2}"
    print("✓ my_inventory 经 registry 注册即被 stock 生产注入点读到；三类齐 → 守门转绿")

    # ── 3. validate_rows：3 类 canonical fixture 全过；缺字段/禁用字段红灯（占位假数据死法）─
    for t in contract.DATA_TYPES:
        n = contract.validate_rows(t, contract.FIXTURES[t])
        assert n == len(contract.FIXTURES[t]) > 0, f"{t} fixture 行数异常: {n}"
    # 缺必填字段 → 红灯
    bad_orders = [dict(contract.FIXTURES["noon_orders"][0])]; bad_orders[0].pop("item_nr")
    _assert_raises(contract, "noon_orders", bad_orders, "缺必填 item_nr")
    # 缺 SKU 键组（my_inventory：去掉唯一 SKU 键 sku）→ 红灯
    bad_inv = [dict(contract.FIXTURES["noon_my_inventory"][0])]; bad_inv[0].pop("sku")
    _assert_raises(contract, "noon_my_inventory", bad_inv, "缺 SKU 键组")
    # 缺数值字段（不许靠 ingest safe_int 兜 0 编数）→ 红灯
    bad_inv2 = [dict(contract.FIXTURES["noon_my_inventory"][0])]; bad_inv2[0]["qty"] = ""
    _assert_raises(contract, "noon_my_inventory", bad_inv2, "qty 空（不许默认编数）")
    # noon_asn 含禁用 inbound_date（会被误判成 erp_inbound）→ 红灯
    bad_asn = [dict(contract.FIXTURES["noon_asn"][0])]; bad_asn[0]["inbound_date"] = "2026-05-30"
    _assert_raises(contract, "noon_asn", bad_asn, "noon_asn 含禁用 inbound_date")
    # noon_asn 缺 entity 路由键组（去掉唯一的 country_code）→ 红灯
    bad_asn2 = [dict(contract.FIXTURES["noon_asn"][0])]; bad_asn2[0].pop("country_code")
    _assert_raises(contract, "noon_asn", bad_asn2, "noon_asn 缺路由键组")
    print("✓ validate_rows：3 类 fixture 全过；缺必填/SKU·路由键/含禁用字段一律红灯，不默认编数")

    # ── 4. 磁盘既有 fixture（reuse，不另造）逐行满足契约 —— 把复用 CSV 锁回契约防漂移 ─
    for t in contract.DATA_TYPES:
        path = contract.FIXTURE_CSV[t]
        with open(path, encoding="utf-8-sig", newline="") as f:
            disk_rows = list(csv.DictReader(f))
        assert disk_rows, f"{t} 磁盘 fixture 为空: {path}"
        contract.validate_rows(t, disk_rows)   # 不满足契约即 raise
    print("✓ 既有磁盘 fixture（noon_inventory/noon_asn/noon_SA_*.csv）逐行满足契约（复用不漂移）")

    # ── 5. my_inventory fixture 真进 stock _aggregate/_upsert 落库（非 stub）──────
    _reset_db()
    res = noon.run_live(TENANT, live_producer=inv_producer)
    assert res["source"] == "live" and res["rows"] == 5 and res["skus"] == 3, f"库存 live 计数异常: {res}"
    inv = _dump_stock()
    a = inv["SKU-A"]
    assert (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) == (15, 10, 5), a
    assert inv["SKU-B"]["noon_total_qty"] == 20 and inv["SKU-C"]["noon_saleable_qty"] == 7, inv
    assert len(json.loads(a["noon_warehouses_json"])) == 2, "SKU-A 应有 2 条仓库明细"
    print("✓ my_inventory fixture 真经 stock run_live→_aggregate/_upsert 落库（聚合逐字段对）")

    # ── 6. noon_orders fixture 真进 orders ingest 落库（非 stub）──────────────
    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        opath = os.path.join(d, "noon_orders.csv")
        _write_csv(opath, contract.FIXTURES["noon_orders"])
        oc = sqlite3.connect(_DB)
        try:
            n = orders.process_csv_v2(TENANT, opath, oc, entity_alias="hipop_ksa")
        finally:
            oc.close()
    assert n == 3, f"orders 写入行数应为 3: {n}"
    oc = sqlite3.connect(_DB); oc.row_factory = sqlite3.Row
    try:
        rows = list(oc.execute("SELECT partner_sku, item_nr, is_cancelled FROM wf2_orders "
                               "WHERE tenant_id=? ORDER BY item_nr", (TENANT,)))
    finally:
        oc.close()
    assert {r["item_nr"] for r in rows} == {"PSA001", "PSA003", "PSN001"}, rows
    cancelled = {r["item_nr"] for r in rows if r["is_cancelled"]}
    assert cancelled == {"PSA003"}, f"取消单识别错: {cancelled}"
    print("✓ noon_orders fixture 真经 process_csv_v2 落 wf2_orders（含取消单口径）")

    # ── 7. noon_asn fixture 真进 inbound staging 落库（非 stub）────────────────
    _reset_db()
    with tempfile.TemporaryDirectory() as d:
        apath = os.path.join(d, "noon_asn.csv")
        _write_csv(apath, contract.FIXTURES["noon_asn"])
        ares = inbound.run_v2(TENANT, noon_asn_file=apath)
    assert ares["asn_lines"] == 3, f"ASN staging 行数应为 3: {ares}"
    ac = sqlite3.connect(_DB); ac.row_factory = sqlite3.Row
    try:
        srows = {(r["asn_number"], r["partner_sku"]): r["qty"] for r in ac.execute(
            f"SELECT asn_number, partner_sku, qty FROM {inbound.STAGING_TABLE} "
            "WHERE tenant_id=? AND source='noon_asn'", (TENANT,))}
    finally:
        ac.close()
    assert srows == {("ASN001", "SKU-A"): 50, ("ASN001", "SKU-B"): 30, ("ASN002", "SKU-C"): 25}, srows
    print("✓ noon_asn fixture 真经 ingest_inbound_staging_v2 落 staging（SKU 映射+数量对）")

    # 清理本测注册，别污染同进程后续 smoke（make test 自动聚合多文件同进程外，
    # 但保险起见恢复未注册态，与「现在该红」的真实状态一致）。
    for t in contract.DATA_TYPES:
        contract.set_live_row_producer(t, None)

    print("\n7/7 passed")


def _assert_raises(contract, data_type, rows, label):
    try:
        contract.validate_rows(data_type, rows)
    except contract.RowContractViolation:
        return
    raise AssertionError(f"[{label}] 应 raise RowContractViolation 却通过了（字段缺失未红灯）")


def _write_csv(path, rows):
    cols = list({k for r in rows for k in r})
    # 稳定列序：以首行键序为准，补齐其余
    cols = list(rows[0].keys()) + [k for k in cols if k not in rows[0]]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass

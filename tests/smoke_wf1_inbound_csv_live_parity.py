"""Smoke: WS-32.4 — inbound ASN staging socket，行字段以 WS-34 契约为唯一来源。

承重墙(数据 ingest 契约 + 确定性等价/红灯 smoke)。本 socket 把 noon ASN /
ERP 送仓两路行落 `wf1_asn_lines_staging`，CSV 入口与 live 行入口共用同一
`_aggregate`/`_upsert`(不分叉)；行字段契约 / 校验 / producer 注册表全部以
WS-34 的 `noon_live_contract.py`(ROW_CONTRACT[ASN] / validate_row /
set_live_row_producer(ASN,...))为**唯一来源**，不在本脚本另定字段。

钉死的验收 / 三种死法：
  ① 等价：同一份 ASN/送仓输入，CSV 路径与「单一 ASN producer 喂同形行」的 live
     路径，写进 staging 的行数 + 每个业务字段(source/asn_number/partner_sku/
     noon_sku/qty/status/inbound_date)逐字段一致。
  ② 单一契约(接线缺失死法)：inbound.set_live_row_producer(fn) 注册的就是 WS-34
     contract 的 ASN 注册表(同一 fn 对象)，run_live 不带参时从该注册表读到同一
     producer——证明 socket 真接到 WS-34 单一来源，没另起一份 registry。
  ③ 字段缺失红灯(占位假数据死法，验门人打回点)：live ASN row 缺 qty / 空 qty /
     非数字 qty / 缺 asn_number / 缺 country_code / 带契约外字段 → raise
     LiveSourceUnavailable 且 staging **一行不新增**，绝不把"未知在途数量"写成 0。
  ④ 无 producer 且无 CSV 可回落 → 红灯；producer 抛错 + 有 CSV interim → 同契约
     回落真实 CSV 行。
  ⑤ 平台 SKU(Z 开头)经 noon_sku_map 回 partner_sku；未映射 → 计 unmapped、跳过
     不落(区别于"缺字段红灯"：字段在、只是映射不到，跳过即可，不红灯)。

fail-then-pass：base(main 前)的 ingest_inbound_staging_v2 无 `run_live`/
  `_aggregate(rows, tenant_id)`/`set_live_row_producer`，也未引用 noon_live_contract
  → import/调用即 AttributeError，且本 smoke 的"缺 qty 红灯"断言对旧 `safe_int`
  写 0 的实现必 fail；接 WS-34 契约 + 严格 qty 校验后 → pass。

跑法：python3 tests/smoke_wf1_inbound_csv_live_parity.py   或   make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db。）
"""
import os
import re
import sys
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

NOON_ASN_CSV = os.path.join(FIXTURES, "noon_asn.csv")
ERP_INBOUND_CSV = os.path.join(FIXTURES, "erp_inbound.csv")

# 比对字段(排除 imported_at —— datetime('now')，两路必不同，不属业务等价口径)
_CMP_COLS = ("entity_alias", "source", "noon_sku", "qty", "status", "inbound_date")


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _setup_db():
    c = sqlite3.connect(_TMP_DB)
    for t in ("sales_entities", "wf2_sku"):
        c.executescript(_extract_create(t))
    c.executemany(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
        "VALUES (?,?,?,?,?,?,1)",
        [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
         (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
    )
    c.executemany(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
        [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
         (TENANT, "hipop_ksa", "SKU-B", "ZSA002"),
         (TENANT, "hipop_uae", "SKU-C", "ZAE001")],
    )
    c.commit()
    c.close()


def _dump_staging(table):
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return {
            (r["source"], r["asn_number"], r["partner_sku"]): dict(r)
            for r in c.execute(f"SELECT * FROM {table}").fetchall()
        }
    finally:
        c.close()


def _count_staging(table):
    c = sqlite3.connect(_TMP_DB)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def _expect_red(fn, table, what):
    """断言 fn() 红灯(LiveSourceUnavailable)且 staging 一行不新增。"""
    import ingest_inbound_staging_v2 as inbound
    before = _count_staging(table)
    try:
        fn()
    except inbound.LiveSourceUnavailable:
        after = _count_staging(table)
        assert before == after, f"{what}: 红灯了却仍写了 staging（{before}→{after}）"
        return
    raise AssertionError(f"{what}: 应红灯却没 raise LiveSourceUnavailable")


def _rows(path):
    """读 fixture CSV 成 dict 行列表（live producer 产同形行用）。"""
    import ingest_inbound_staging_v2 as inbound
    return list(inbound._iter_csv_rows(path))


def main():
    _setup_db()
    import ingest_inbound_staging_v2 as inbound
    import noon_live_contract as C

    # 隔离：本 smoke 掌控 ASN 注册表状态，结束清空，不污染其它 smoke。
    inbound.set_live_row_producer(None)
    try:
        # ── 路径 1：CSV 入口 → staging，dump 出基准 ──────────────────────
        res_csv = inbound.run_v2(
            TENANT, noon_asn_file=NOON_ASN_CSV, erp_inbound_file=ERP_INBOUND_CSV)
        assert res_csv["asn_lines"] == 5, res_csv
        assert res_csv["noon_asn"]["lines"] == 3, res_csv
        assert res_csv["erp_inbound"]["lines"] == 2, res_csv
        csv_dump = _dump_staging(inbound.STAGING_TABLE)
        assert len(csv_dump) == 5, f"CSV 路径 staging 行数 {len(csv_dump)} != 5"
        # 平台 SKU（Z 开头）必须映射回 partner_sku，绝不当主键
        assert not any(psk.startswith("Z") for (_, _, psk) in csv_dump), \
            "staging 主键混进了 Z 开头平台 SKU"
        asn1_a = csv_dump[("noon_asn", "ASN001", "SKU-A")]
        assert asn1_a["noon_sku"] == "ZSA001" and asn1_a["qty"] == 50, asn1_a
        print("✓ CSV 入口落 5 行；平台 SKU 经映射回 partner_sku，Z 开头不当主键")

        # ── 验收②单一契约：inbound.set_live_row_producer == WS-34 ASN 注册表 ──
        def _asn_producer(tenant_id):
            # 单一 ASN producer 产出两路同形行（噪声：契约不区分文件来源，只看字段）
            return _rows(NOON_ASN_CSV) + _rows(ERP_INBOUND_CSV)

        inbound.set_live_row_producer(_asn_producer)
        assert C.get_live_row_producer(C.ASN) is _asn_producer, \
            "inbound 没把 producer 注册进 WS-34 contract 的 ASN 注册表（接线缺失）"
        assert inbound.get_live_row_producer() is _asn_producer
        assert C.ASN not in C.missing_live_producers(), \
            "注册后 contract 仍报 ASN 缺 producer（registry 漂成两套真相）"
        print("✓ set_live_row_producer 委托 WS-34 contract 的 ASN 注册表（单一来源）")

        # ── 路径 2：live 入口（不带参，从注册表读 producer）→ 同一 _aggregate ──
        res_live = inbound.run_live(TENANT)
        assert res_live["source"] == "live", res_live
        assert res_live["asn_lines"] == 5, res_live
        live_dump = _dump_staging(inbound.STAGING_TABLE)

        # ── 验收①等价：两路键集合 + 每个业务字段逐字段一致 ──────────────────
        assert set(csv_dump) == set(live_dump), \
            f"CSV/live staging 键不一致: csv-only={set(csv_dump)-set(live_dump)} " \
            f"live-only={set(live_dump)-set(csv_dump)}"
        for key in csv_dump:
            cv, lv = csv_dump[key], live_dump[key]
            for col in _CMP_COLS:
                assert cv[col] == lv[col], \
                    f"{key}.{col} CSV={cv[col]!r} != live={lv[col]!r}（CSV/live 落库分叉）"
        print("✓ CSV 入口与 live 行入口共用 _aggregate/_upsert，落 staging 逐字段一致（5 行）")

        # ── 验收③字段缺失红灯（验门人打回点）：一行不新增，绝不写默认 0 ────────
        base = ("ASN-RED", "Scheduled", "ZSA001", "SA")  # (asn,status,sku,country)
        red_cases = {
            # 验门人 merge 树实测的精确复现：缺 qty → 旧实现写 qty=0，本版须红灯
            "缺 qty": {"asn_number": "ASN-MISS-QTY", "status": "Scheduled",
                       "sku": "ZSA001", "country_code": "SA"},
            "空 qty": {"asn_number": base[0], "status": base[1], "sku": base[2],
                       "qty": "", "country_code": base[3]},
            "非数字 qty": {"asn_number": base[0], "status": base[1], "sku": base[2],
                           "qty": "N/A", "country_code": base[3]},
            "缺 asn_number": {"status": base[1], "sku": base[2], "qty": 10,
                              "country_code": base[3]},
            "缺 country_code": {"asn_number": base[0], "status": base[1],
                                "sku": base[2], "qty": 10},
            "契约外字段": {"asn_number": base[0], "status": base[1], "sku": base[2],
                           "qty": 10, "country_code": base[3], "eta_guess": "soon"},
        }
        for label, bad in red_cases.items():
            _expect_red(
                lambda b=bad: inbound.run_live(TENANT, live_producer=lambda t: [b],
                                               allow_csv_fallback=False),
                inbound.STAGING_TABLE, f"live ASN {label}")
        after = _dump_staging(inbound.STAGING_TABLE)
        assert not any(asn.startswith("ASN-MISS") or asn == "ASN-RED"
                       for (_, asn, _) in after), \
            "缺字段的 live ASN 行被写进 staging（占位假数据死法）"
        assert set(after) == set(csv_dump), "红灯用例后 staging 与基准不一致（写了脏行）"
        print("✓ live ASN 缺/空/非数字 qty、缺 asn_number/country_code、契约外字段 → 红灯且不写")

        # ── 验收④回落：producer 抛错但有 CSV interim → 走同一契约落真实 CSV 行 ──
        def _boom(tenant_id):
            raise RuntimeError("live fetch 挂了")

        res_fb = inbound.run_live(TENANT, live_producer=_boom,
                                  noon_asn_file=NOON_ASN_CSV,
                                  erp_inbound_file=ERP_INBOUND_CSV)
        assert res_fb["source"] == "csv_fallback", res_fb
        assert res_fb["live_error"], "回落未记录 live_error"
        assert res_fb["asn_lines"] == 5, res_fb
        assert set(_dump_staging(inbound.STAGING_TABLE)) == set(csv_dump), \
            "producer 抛错回落 CSV 后 staging 与 CSV 路径不一致"
        print("✓ producer 抛错 + 有 CSV interim → 同一契约回落真实 CSV 行")

        # ── 验收④红灯：无 producer 且无 CSV 可回落 → LiveSourceUnavailable，不写 ──
        _empty = tempfile.mkdtemp()
        inbound.set_live_row_producer(None)
        _expect_red(lambda: inbound.run_live(TENANT, inbox=_empty),
                    inbound.STAGING_TABLE, "无 producer 且无 CSV 可回落")
        print("✓ 无 producer 且无 CSV 可回落 → LiveSourceUnavailable 红灯，一行不写")

        # ── 验收⑤平台 SKU 未映射 → 计 unmapped、跳过不落（字段在，非红灯）──────
        def _unmapped(tenant_id):
            return [{"asn_number": "ASN-X", "status": "in_transit",
                     "sku": "ZUNKNOWN", "qty": 99, "country_code": "SA"}]

        res_um = inbound.run_live(TENANT, live_producer=_unmapped)
        assert res_um["lines"] == 0, f"未映射平台 SKU 被落库了: {res_um}"
        assert res_um["unmapped"] == 1, res_um
        assert not any(asn == "ASN-X" for (_, asn, _) in _dump_staging(inbound.STAGING_TABLE)), \
            "未映射的 Z 开头平台 SKU 行被写进 staging（占位假数据死法）"
        print("✓ live row 平台 SKU 未映射 → 计 unmapped、跳过不落、不红灯（字段在）")

        print("\n8/8 passed")
    finally:
        # 清空 ASN 注册表（进程级单例，别污染其它 smoke / 串跑）
        inbound.set_live_row_producer(None)
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()

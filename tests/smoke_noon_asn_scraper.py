"""Smoke: WS-N2.3 / WS-60 — noon KSA 送仓/ASN 实时抓取器（page→rows）承重墙。

与 noon 订单抓取器（WS-58）同套打法：抓取器把已登录 page 抓出的送仓行映射成 WS-34 ASN
行契约，经 `register_live_producer()` 注册进 ASN ingest（= contract ASN 注册表，WS-37
socket），run_live 不带参即真走 live → 同一 `_aggregate`/`_upsert` 落 wf1_asn_lines_staging。

只替身**浏览器边界**（page / raw_asn_fn 注入，同 smoke_platform_session）；映射/校验/注册/
路由/SKU 映射/blocked 全用真函数。

钉死三死法 + 验收：
  ① 接线缺失：注册前 contract 报 ASN 缺 producer、run_live 无 CSV → 红；register 后
     contract 读到同一 fn、run_live 不带参 → source=="live" 落 staging。
  ② 死代码短路（跳过映射）：平台 SKU（Z 开头）经 socket `_aggregate` 的 noon_sku_map 回
     partner_sku（KSA store→entity 不在抓取器反解，靠 country_code=SA 路由——修门2 的
     `44158-HIPOP-NOON-AE/SA` 合并店歧义）；staging 主键全是 partner_sku，绝无 Z。
  ③ 占位假数据：缺 qty/asn_number → validate_row 红、staging 一行不新增；登录失效
     （PlatformBrowserError blocked）→ LiveSourceUnavailable 红（不回落 CSV 掩盖）；页面结构
     变（缺关键列/无表）→ 红，不返回空行冒充无送仓；缺 asn_url 配置 → blocked（不瞎猜 URL）。
  ④ 平台 SKU 未映射 → 计 unmapped、跳过不落（字段在，非红灯）。
  ⑤ 表在、列齐、0 数据行 = 真没有在途送仓（合法），source=="live" 且不报错、不编数。
  ⑥ 生产接线门：asn_url 未配置时 live_producers 不注册 asn（保留 CSV 回落、不红灯）。

fail-then-pass：改动前 `hipop/scripts/noon_asn_scraper.py` 不存在 → import 即 fail。
死法②/③断言对「Z 当主键 / 缺值编 0」的朴素实现必 fail。

跑法：python3 tests/smoke_noon_asn_scraper.py   或   make test
（纯 SQLite 临时库；浏览器全替身，绝不碰真实紫鸟 / 真实 noon / live hipop.db。）
"""
import os
import re
import sys
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

_TMP_DB = tempfile.NamedTemporaryFile(suffix="_asn_scraper.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1


# ── DB 准备（_aggregate 路由/映射需 sales_entities + wf2_sku）──────────────
def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 变了？）"
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
         (TENANT, "hipop_ksa", "SKU-B", "ZSA002")],
    )
    c.commit()
    c.close()


def _dump_staging(table):
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return {(r["source"], r["asn_number"], r["partner_sku"]): dict(r)
                for r in c.execute(f"SELECT * FROM {table}").fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        c.close()


def _count_staging(table):
    c = sqlite3.connect(_TMP_DB)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        c.close()


# ── FakePage：表头驱动解析所需的最小 DOM API（query_selector_all/inner_text）──
class _El:
    def __init__(self, text="", children=None):
        self._text = text
        self._children = children or {}

    def inner_text(self):
        return self._text

    def query_selector_all(self, css):
        return self._children.get(css, [])


def _make_table(headers, rows):
    th = [_El(text=h) for h in headers]
    trs = [_El(children={"td": [_El(text=("" if c is None else str(c))) for c in r]}) for r in rows]
    return _El(children={"thead th": th, "tbody tr": trs})


class _FakePage:
    def __init__(self, table=None, has_table=True):
        self._table = table
        self._has_table = has_table
        self.url = "https://fbn.noon.partners/en-sa/asn?mp=noon"

    def query_selector_all(self, css):
        if css == "table" and self._has_table and self._table is not None:
            return [self._table]
        return []

    def goto(self, url, **kw):
        self.url = url
        return url


# 送仓明细表（per-line）：含平台 SKU 列；故意打乱列序 + 含未知/ERP判别列验忽略。
_HEADERS = ["Status", "ASN #", "SKU", "Quantity", "To Warehouse", "ETA", "Notes"]
_GOOD_ROWS = [
    ["in_transit", "ASN001", "ZSA001", "50", "FC-RUH", "2026-06-10", "ok"],
    ["in_transit", "ASN001", "ZSA002", "30", "FC-RUH", "2026-06-10", "ok"],
    ["scheduled",  "ASN002", "ZUNKNOWN", "25", "FC-JED", "", "unmapped"],  # 未映射 → 跳过
]


def _raw_from(headers, rows):
    """用真 parse_asn_table 把 FakePage 表抓成 raw 行（= 抓取器外部边界的产物）。"""
    import noon_asn_scraper as scraper
    return scraper.parse_asn_table(_FakePage(table=_make_table(headers, rows)),
                                   scraper._DEFAULT_SELECTORS)


def _expect_red(fn, table, what):
    import ingest_inbound_staging_v2 as inbound
    before = _count_staging(table)
    try:
        fn()
    except inbound.LiveSourceUnavailable:
        after = _count_staging(table)
        assert before == after, f"{what}: 红灯了却仍写了 staging（{before}→{after}）"
        return
    raise AssertionError(f"{what}: 应红灯却没 raise LiveSourceUnavailable")


def main():
    _setup_db()
    import noon_asn_scraper as scraper
    import ingest_inbound_staging_v2 as inbound
    from hipop.server import _platform_browser as pb
    import noon_live_contract as C

    scraper.unregister_live_producer()  # 干净起点
    try:
        # ── 0. 表头驱动解析 + 映射单测 ──────────────────────────────────────
        raw = _raw_from(_HEADERS, _GOOD_ROWS)
        assert len(raw) == 3 and raw[0]["asn_number"] == "ASN001" and raw[0]["qty"] == "50", raw
        assert "Notes" not in raw[0] and "inbound_date" not in raw[0], raw[0]
        assert raw[0].get("warehouse_code") == "FC-RUH", raw[0]
        row_a = scraper.to_contract_row(raw[0], "SA")
        assert row_a["country_code"] == "SA" and row_a["sku"] == "ZSA001" and row_a["qty"] == "50", row_a
        assert "inbound_date" not in row_a, f"noon ASN 行不得带 inbound_date: {row_a}"
        assert set(row_a).issubset(set(C.ROW_CONTRACT[C.ASN]["known"])), row_a
        row_miss = scraper.to_contract_row({"asn_number": "ASN-X", "sku": "ZSA001"}, "SA")
        assert row_miss["qty"] == "", f"缺 qty 被编了值: {row_miss}"
        print("✓ 表头驱动解析 + country 注入 + 缺值留空 + 键⊆契约 known + 不带 inbound_date")

        # ── 1. 缺 asn_url 配置 → blocked（不瞎猜 URL）+ 生产接线门 ─────────────
        assert scraper.asn_url_configured() is False, "测试预期 config asn.asn_url 为空"
        _expect_red(lambda: scraper.fetch_asn_rows(TENANT, page=_FakePage(table=_make_table(_HEADERS, _GOOD_ROWS))),
                    inbound.STAGING_TABLE, "缺 asn_url 配置（无 raw_asn_fn 走真 _fetch_raw_asn）")
        from hipop.runtime import live_producers
        os.environ["HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE"] = "1"  # 不影响本进程已 import
        assert "asn" not in live_producers.register_all(), \
            "asn_url 未配置时不应自动接线（否则缺配置红灯会破坏 CSV 回落）"
        scraper.unregister_live_producer()
        print("✓ 缺 asn_url → 抓取器 blocked；live_producers 不自动接线（保留 CSV 回落）")

        # ── 2. 接线缺失死法：注册前红灯 → register 后 contract 单一来源 ──────────
        assert C.ASN in C.missing_live_producers(), "注册前不应有 ASN producer"
        _empty = tempfile.mkdtemp()
        _expect_red(lambda: inbound.run_live(TENANT, inbox=_empty),
                    inbound.STAGING_TABLE, "注册前无 producer 无 CSV")
        raw_fn = lambda page: _raw_from(_HEADERS, _GOOD_ROWS)
        fn = scraper.register_live_producer(page_factory=lambda t: object(), raw_asn_fn=raw_fn)
        assert C.get_live_row_producer(C.ASN) is fn, "register 没注册进 contract ASN 注册表"
        assert inbound.get_live_row_producer() is fn, "ASN ingest 没读到同一 producer（漂两套真相）"
        assert C.ASN not in C.missing_live_producers()
        print("✓ 接线：注册前红灯；register_live_producer 经 WS-34 单一注册表登记（ingest 同读）")

        # ── 3. 注册后 run_live 不带参 → 真走 live；平台 SKU→partner_sku，Z 不当主键 ──
        res = inbound.run_live(TENANT)
        assert res["source"] == "live", f"应走 live: {res}"
        assert res["asn_lines"] == 2 and res["unmapped"] == 1, f"计数异常: {res}"
        dump = _dump_staging(inbound.STAGING_TABLE)
        assert set(dump) == {("noon_asn", "ASN001", "SKU-A"), ("noon_asn", "ASN001", "SKU-B")}, set(dump)
        assert not any(psk.startswith("Z") for (_, _, psk) in dump), "staging 主键混进 Z 开头平台 SKU"
        a = dump[("noon_asn", "ASN001", "SKU-A")]
        assert a["noon_sku"] == "ZSA001" and a["qty"] == 50 and a["status"] == "in_transit", a
        assert a["source"] == "noon_asn" and a["inbound_date"] is None, a
        print("✓ 注册后 run_live 真走 live：source=noon_asn，平台 SKU→partner_sku（_aggregate），Z 不当主键")

        # ── 4. 占位假数据：缺 qty / 非数字 qty / 缺 asn_number → 红，一行不新增 ──────
        base = set(_dump_staging(inbound.STAGING_TABLE))
        red = {
            "缺 qty": [["in_transit", "ASN-RED", "ZSA001", "", "FC-RUH", "", ""]],
            "非数字 qty": [["in_transit", "ASN-RED", "ZSA001", "N/A", "FC-RUH", "", ""]],
            "缺 asn_number": [["in_transit", "", "ZSA001", "10", "FC-RUH", "", ""]],
        }
        for label, rows in red.items():
            scraper.register_live_producer(page_factory=lambda t: object(),
                                           raw_asn_fn=lambda page, r=rows: _raw_from(_HEADERS, r))
            _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                        inbound.STAGING_TABLE, f"抓取行 {label}")
        assert set(_dump_staging(inbound.STAGING_TABLE)) == base, "缺字段用例后写了脏行/编数"
        print("✓ 缺 qty / 非数字 qty / 缺 asn_number → 红灯，staging 一行不新增（不编 0/编号）")

        # ── 5. 登录失效 → blocked（不回落 CSV 掩盖）────────────────────────────
        _orig = pb.get_platform_session
        pb.get_platform_session = lambda *a, **k: (_ for _ in ()).throw(
            pb.PlatformBrowserError("未登录（替身）", blocked=True))
        try:
            scraper.register_live_producer(raw_asn_fn=raw_fn)  # page_factory=None → 真走 _get_session
            _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                        inbound.STAGING_TABLE, "登录失效（会话 blocked）")
        finally:
            pb.get_platform_session = _orig
        print("✓ 登录失效（PlatformBrowserError blocked）→ LiveSourceUnavailable 红灯，不 stub")

        # ── 6. 页面结构变 → blocked（不返回空行冒充无送仓）─────────────────────
        scraper.register_live_producer(page_factory=lambda t: _FakePage(has_table=False),
                                       raw_asn_fn=None)
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "页面无表格")
        scraper.register_live_producer(
            page_factory=lambda t: _FakePage(table=_make_table(["Foo", "Bar", "Baz"], [["1", "2", "3"]])),
            raw_asn_fn=None)
        # asn_url 为空，_fetch_raw_asn 会先因缺配置 blocked —— 用 raw_asn_fn 直接喂坏表头解析
        scraper.register_live_producer(page_factory=lambda t: object(),
                                       raw_asn_fn=lambda page: scraper.parse_asn_table(
                                           _FakePage(table=_make_table(["Foo", "Bar"], [["1", "2"]])),
                                           scraper._DEFAULT_SELECTORS))
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "表头缺关键列")
        print("✓ 页面结构变（无表 / 表头缺关键列）→ 红灯，不返回空行冒充无送仓")

        # ── 7. 表在、列齐、0 数据行 = 真没有在途送仓（合法 live 空快照）──────────
        scraper.register_live_producer(page_factory=lambda t: object(),
                                       raw_asn_fn=lambda page: _raw_from(_HEADERS, []))
        res0 = inbound.run_live(TENANT)
        assert res0["source"] == "live" and res0["asn_lines"] == 0, f"0 送仓应是合法 live: {res0}"
        print("✓ 列齐但 0 数据行 → 合法空快照 source==live，不报错、不编数")

        print("\n8/8 passed")
    finally:
        os.environ.pop("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE", None)
        scraper.unregister_live_producer()
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()

"""Smoke: WS-N2.3 / WS-60 — noon KSA ASN/送仓 实时抓取器（page→rows）承重墙。

本条交付的是 ASN ingest socket（WS-32.4，PR #25）一直缺的**真 producer**：
`noon_asn_scraper.fetch_asn_rows(tenant_id)` 从已登录 noon page 抓送仓行 → 平台 SKU 映射
回 partner_sku → 对齐 WS-34 ROW_CONTRACT[ASN] → 经 `register()` 注册进 contract 单一
注册表，`ingest_inbound_staging_v2.run_live` 不带参即真走 live。

只替身**浏览器边界**（与 smoke_platform_session 同范式）：会话 `get_platform_session` 用
FakePage 替身、store→entity 映射用 fake、asn_url 注入；其余编排/导航/登录检测/表头驱动
解析/平台 SKU 映射/契约对齐/红灯全用**真函数**——三种死法就活在这些真函数里。

钉死三种死法 + 验收：
  ① 接线缺失：注册前 contract 报 ASN 缺 producer、run_live 无 CSV → 红灯；`register()`
     后 contract 读到同一 fn，run_live 不带参 → source=="live" 落 staging。
  ② 死代码短路（跳过映射）：抓出的平台 SKU（Z 开头）必须经 noon_sku_map 回 partner_sku，
     staging 主键全是 partner_sku，绝无 Z 开头平台 SKU；noon_sku 列留平台 SKU 供溯源。
  ③ 占位假数据：缺 qty / 非数字 qty / 缺 asn_number / 登录失效 / 页面结构变 / 缺配置
     → raise LiveSourceUnavailable（红灯），staging **一行不新增**，绝不编 0/编号/编 ETA。
  ④ 平台 SKU 未映射 → 只带 sku、计 unmapped、跳过不落（字段在，非红灯）。
  ⑤ 表存在且关键列齐但 0 数据行 = 真没有在途送仓（合法），source=="live" 且不报错、不编数。

fail-then-pass：改动前 `hipop/scripts/noon_asn_scraper.py` 不存在 → import 即 fail。
实现后 → 全 pass。死法②/③的断言对「把 Z 当主键 / 缺值编 0」的朴素实现必 fail。

跑法：python3 tests/smoke_noon_asn_scraper.py   或   make test
（纯 SQLite 临时库；浏览器全替身，绝不碰真实紫鸟 / 真实 noon / live hipop.db。）
"""
import os
import re
import sys
import sqlite3
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_TMP_DB = tempfile.NamedTemporaryFile(suffix="_asn_scraper.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 注入用：noon 送仓页 URL（不命中登录 marker）+ 登录页 URL（命中 marker）。
ASN_URL = "https://noon-supply.noon.partners/en/inbound/asn?store=ksa"
LOGIN_URL = "https://login.noon.partners/signin?return=asn"
OK_COOKIES = [{"name": "_npsid", "value": "x"}]   # 含会话 cookie → 登录态 ok


# ── DB 准备（noon_sku_map 需要 wf2_sku；sales_entities 一并建以防其它路径）──
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
        [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85)],
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
        return {}  # 表还没建（红灯路径在建表前就 raise 也算「未写」）
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


# ── FakePage：实现抓取器真正用到的 page DOM API（query_selector_all/inner_text/
#    goto/context.cookies/url），不引入真 playwright。 ────────────────────────
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
    trs = [_El(children={"td": [_El(text=("" if c is None else str(c))) for c in r]})
           for r in rows]
    return _El(children={"thead th": th, "tbody tr": trs})


class _FakePage:
    """has_table=False → page.query_selector_all('table') 返回 []（页面结构变）。"""
    def __init__(self, table=None, goto_url=ASN_URL, cookies=None, has_table=True):
        self._table = table
        self._goto_url = goto_url
        self._cookies = OK_COOKIES if cookies is None else cookies
        self._has_table = has_table
        self.url = "about:blank"
        self.context = SimpleNamespace(cookies=lambda: list(self._cookies))

    def query_selector_all(self, css):
        if css == "table" and self._has_table and self._table is not None:
            return [self._table]
        return []

    def goto(self, url, **kw):
        self.url = self._goto_url
        return self.url


# 标准送仓表表头（含平台 SKU 列；故意打乱列序 + 含「ETA/Notes」等列验表头驱动 + 忽略：
# - "Notes" 不在契约 → 忽略；
# - "Inbound Date/ETA" 是 ERP 送仓路的判别列，noon ASN 抓取器**不得**产出它（否则 socket
#   会把 noon 行误归 erp_inbound）→ 必须被忽略，行仍落 source=noon_asn。)
_HEADERS = ["Status", "ASN Number", "SKU", "Quantity", "Warehouse", "ETA", "Notes"]
#            status   asn_number    sku   qty       warehouse_code (忽略) (忽略)
_GOOD_ROWS = [
    ["in_transit", "ASN001", "ZSA001", "50", "FC-RUH", "2026-06-10", "ok"],
    ["in_transit", "ASN001", "ZSA002", "30", "FC-RUH", "2026-06-10", "ok"],
    ["scheduled",  "ASN002", "ZUNKNOWN", "25", "FC-JED", "", "unmapped sku"],  # 未映射 → 跳过
]


def _ok_page():
    return _FakePage(table=_make_table(_HEADERS, _GOOD_ROWS))


def _patch_session(scraper, pb, page=None, raise_blocked=False):
    """替身浏览器边界：get_platform_session + store→entity 映射 + asn_url。"""
    def _gps(tenant_id, store_key, **kw):
        if raise_blocked:
            raise pb.PlatformBrowserError("未登录（替身）", blocked=True)
        return page if page is not None else _ok_page()
    pb.get_platform_session = _gps
    scraper._resolve_store_entity = lambda tid, sk: SimpleNamespace(
        tenant_id=TENANT, entity_alias="hipop_ksa", country="SA")
    scraper._asn_url = lambda: ASN_URL


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

    _orig_gps = pb.get_platform_session
    _orig_rse = scraper._resolve_store_entity
    _orig_asn_url = scraper._asn_url
    scraper.unregister()  # 干净起点

    try:
        # ── 0. 缺配置 → blocked（不瞎猜 URL；真实 config 当前无 asn_url 键）────────
        try:
            _orig_asn_url()
            raise AssertionError("config 无 asn_url 时 _asn_url() 应 blocked")
        except scraper.LiveSourceUnavailable as e:
            assert "asn_url" in str(e), e
        print("✓ 缺 noon ASN 页 URL 配置 → blocked，绝不瞎猜抓错页")

        # ── 1. 纯解析 + 映射单测（表头驱动 / 平台 SKU→partner_sku / 缺值留空）──────
        page = _ok_page()
        raw = scraper.parse_asn_table(page)
        assert len(raw) == 3 and raw[0]["asn_number"] == "ASN001" and raw[0]["qty"] == "50", raw
        # 未知列(Notes)与 ERP 判别列(ETA/inbound_date)都不该被抓进来
        assert "Notes" not in raw[0] and "inbound_date" not in raw[0], raw[0]
        assert raw[0].get("warehouse_code") == "FC-RUH", raw[0]
        sku_map = {"ZSA001": "SKU-A", "ZSA002": "SKU-B"}
        row_a, mapped_a = scraper._raw_to_contract_row(raw[0], "SA", sku_map)
        assert mapped_a and row_a["partner_sku"] == "SKU-A" and row_a["sku"] == "ZSA001", row_a
        assert row_a["country_code"] == "SA" and row_a["qty"] == "50", row_a
        assert "inbound_date" not in row_a, f"noon ASN 行不得带 inbound_date（会被误归 erp）: {row_a}"
        row_u, mapped_u = scraper._raw_to_contract_row(raw[2], "SA", sku_map)
        assert not mapped_u and "partner_sku" not in row_u and row_u["sku"] == "ZUNKNOWN", row_u
        # 缺值不编数：构造一个缺 qty 的原始行
        row_m, _ = scraper._raw_to_contract_row(
            {"asn_number": "ASN-X", "sku": "ZSA001"}, "SA", sku_map)
        assert row_m["qty"] == "", f"缺 qty 被编了值: {row_m}"
        assert set(row_m).issubset(set(C.ROW_CONTRACT[C.ASN]["known"])), \
            f"产出键超出契约 known: {set(row_m) - set(C.ROW_CONTRACT[C.ASN]['known'])}"
        print("✓ 表头驱动解析 + 平台 SKU 映射回 partner_sku + 缺值留空 + 键⊆契约 known")

        # ── 2. 接线缺失死法：注册前 contract 报缺 producer、run_live 无 CSV → 红灯 ──
        assert C.ASN in C.missing_live_producers(), "注册前 contract 不应有 ASN producer"
        _empty = tempfile.mkdtemp()
        _expect_red(lambda: inbound.run_live(TENANT, inbox=_empty),
                    inbound.STAGING_TABLE, "注册前无 producer 无 CSV")
        # register() → 委托 WS-34 单一注册表（同一 fn 对象）
        fn = scraper.register()
        assert C.get_live_row_producer(C.ASN) is fn is scraper.fetch_asn_rows, \
            "register 没把 fetch_asn_rows 注册进 contract ASN 注册表（接线缺失/漂两套真相）"
        assert C.ASN not in C.missing_live_producers()
        print("✓ 接线：注册前红灯；register() 经 WS-34 单一注册表登记 fetch_asn_rows")

        # ── 3. 注册后 run_live 不带参 → 真走 live，落 staging（接线缺失 + 映射死法）──
        _patch_session(scraper, pb, page=_ok_page())
        res = inbound.run_live(TENANT)   # 不带 live_producer → 从注册表读 fetch_asn_rows
        assert res["source"] == "live", f"应走 live 源: {res}"
        assert res["asn_lines"] == 2, f"应落 2 行（ZUNKNOWN 未映射跳过）: {res}"
        assert res["unmapped"] == 1, f"未映射计数异常: {res}"
        dump = _dump_staging(inbound.STAGING_TABLE)
        assert set(dump) == {("noon_asn", "ASN001", "SKU-A"), ("noon_asn", "ASN001", "SKU-B")}, \
            f"staging 键异常: {set(dump)}"
        assert not any(psk.startswith("Z") for (_, _, psk) in dump), \
            "staging 主键混进了 Z 开头平台 SKU（死代码短路：跳过映射）"
        a = dump[("noon_asn", "ASN001", "SKU-A")]
        assert a["noon_sku"] == "ZSA001" and a["qty"] == 50 and a["status"] == "in_transit", a
        # noon ASN 行 source 必须是 noon_asn（未误归 erp_inbound）且 inbound_date 为 NULL
        assert a["source"] == "noon_asn" and a["inbound_date"] is None, a
        print("✓ 注册后 run_live 真走 live：平台 SKU→partner_sku，Z 不当主键，source=noon_asn，字段对齐落 staging")

        # ── 4. 占位假数据死法：缺 qty / 非数字 qty / 缺 asn_number → 红灯，一行不新增 ──
        base_dump = set(_dump_staging(inbound.STAGING_TABLE))

        def _page_with(rows):
            return _FakePage(table=_make_table(_HEADERS, rows))

        red_cases = {
            "缺 qty": [["in_transit", "ASN-RED", "ZSA001", "", "FC-RUH", "2026-06-10", ""]],
            "非数字 qty": [["in_transit", "ASN-RED", "ZSA001", "N/A", "FC-RUH", "2026-06-10", ""]],
            "缺 asn_number": [["in_transit", "", "ZSA001", "10", "FC-RUH", "2026-06-10", ""]],
        }
        for label, rows in red_cases.items():
            _patch_session(scraper, pb, page=_page_with(rows))
            _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                        inbound.STAGING_TABLE, f"抓取行 {label}")
        assert set(_dump_staging(inbound.STAGING_TABLE)) == base_dump, \
            "缺字段用例后 staging 变了（写了脏行 / 编数）"
        print("✓ 缺 qty / 非数字 qty / 缺 asn_number → 红灯，staging 一行不新增（不编 0/编号）")

        # ── 5. 登录失效 → blocked（不静默回落 CSV 冒充 live）──────────────────────
        # 5a. get_platform_session 自身 blocked
        _patch_session(scraper, pb, raise_blocked=True)
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "会话 blocked")
        # 5b. 导航到送仓页后落登录页（url 命中 login marker）
        _patch_session(scraper, pb, page=_FakePage(table=_make_table(_HEADERS, _GOOD_ROWS),
                                                   goto_url=LOGIN_URL))
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "落登录页")
        # 5c. 缺会话 cookie（_npsid）
        _patch_session(scraper, pb, page=_FakePage(table=_make_table(_HEADERS, _GOOD_ROWS),
                                                   cookies=[]))
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "缺会话 cookie")
        assert set(_dump_staging(inbound.STAGING_TABLE)) == base_dump, "登录失效路径写了脏行"
        print("✓ 登录失效（会话 blocked / 落登录页 / 缺会话 cookie）→ 红灯，不 stub 登录态")

        # ── 6. 页面结构变 → blocked（不返回空行冒充「没有送仓」）──────────────────
        _patch_session(scraper, pb, page=_FakePage(has_table=False))   # 无表
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "页面无表格")
        bad_headers = ["Foo", "Bar", "Baz"]   # 缺 asn_number/qty 关键列
        _patch_session(scraper, pb, page=_FakePage(table=_make_table(bad_headers, [["1", "2", "3"]])))
        _expect_red(lambda: inbound.run_live(TENANT, allow_csv_fallback=False),
                    inbound.STAGING_TABLE, "表头缺关键列")
        print("✓ 页面结构变（无表 / 表头缺关键列）→ 红灯，不返回空行冒充无送仓")

        # ── 7. 表在、关键列齐、0 数据行 = 真没有在途送仓（合法，非红灯，不编数）────────
        _patch_session(scraper, pb, page=_FakePage(table=_make_table(_HEADERS, [])))
        res0 = inbound.run_live(TENANT)
        assert res0["source"] == "live" and res0["asn_lines"] == 0, f"0 送仓应是合法 live: {res0}"
        print("✓ 关键列齐但 0 数据行 → 合法空快照 source==live，不报错、不编数")

        print("\n8/8 passed")
    finally:
        pb.get_platform_session = _orig_gps
        scraper._resolve_store_entity = _orig_rse
        scraper._asn_url = _orig_asn_url
        scraper.unregister()   # 进程级单例，别污染其它 smoke
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()

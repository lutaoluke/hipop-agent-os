"""Smoke: WS-N2.1 / WS-58 — noon 订单实时抓取器（page → rows，KSA）。

承重墙（抓取器 + 注册 + 真 page→真 rows 的确定性回归）：
  `noon_order_fetcher` 把 `get_platform_session(tenant_id,"noon")` 的**已登录 page** 抓出
  noon 订单行，映射成 WS-34 订单行契约，经订单 ingest `set_live_row_producer` 注册，喂同一
  `_aggregate`/`_upsert`（WS-35）落 wf2_orders/wf2_sku，与 CSV 路径逐字段一致。

  「真 page→真 rows」的唯一外部边界是 `_fetch_raw_orders(page)`（page 侧 fetch noon 接口）；
  本 smoke 注入替身 page / raw_orders_fn 做**确定性**回归（同 smoke_platform_session 替身 page，
  无需真紫鸟/playwright），把映射、字段缺失红灯、接口改版红灯、注册接线、登录失效 blocked
  全钉死在真函数里。真紫鸟下的端到端 live 跑法见模块 `__main__` 与 PR。

钉死三种死法：
  · 接线缺失：`register_live_producer` 写进的就是订单 ingest（= contract ORDERS）注册表，
    且 run_live 注册后真走 live 行 → 同一 `_aggregate`/`_upsert` 落库（spy 计数）。
  · 死代码短路 / 假绿：live 落库 == 跑同份 fixture CSV 落库（逐字段），证明抓取器映射没
    绕过契约；登录失效/坏行不返回空/旧行冒充成功。
  · 占位假数据：字段缺失/接口改版/登录失效 → 红灯 raise，断言库里没有凭空写出的订单行。

fail-then-pass：
  改动前 `noon_order_fetcher` 不存在 → import 即 fail（红）。实现后 → 全 pass（绿）。
  另：把 `to_contract_row` 退回「缺字段补默认值」即占位假数据死法，#3 的红灯断言会 FAIL。

跑法：
  python3 tests/smoke_noon_order_fetcher.py    或    make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db；不连真紫鸟。）
"""
import os
import re
import sys
import csv
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_DB = tempfile.NamedTemporaryFile(suffix="_order_fetcher.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402  （行字段 + fixture 唯一来源）
import noon_order_fetcher as F  # noqa: E402  （被测抓取器）
import ingest_noon_csv_v2 as noon  # noqa: E402  （订单 ingest socket）

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"

# 单一事实源：fixture = noon_live_contract 的 ORDERS fixture（WS-34）。
ORDER_ROWS = C.load_fixture_rows(C.ORDERS)
_CSV_COLS = list(ORDER_ROWS[0].keys())
EXPECT_ITEMS = {r["item_nr"] for r in ORDER_ROWS}

# 契约键 → noon 接口风格字段名（camelCase 别名，全在 _NOON_ORDER_FIELD_MAP 候选里）。
# 用它把 fixture 行「伪装成」noon 原始接口记录，喂抓取器映射回契约键，证明映射正确。
_RAW_KEYMAP = {
    "partner_sku": "partnerSku", "sku": "noonSku", "item_nr": "itemNr",
    "order_timestamp": "orderTimestamp", "status": "orderStatus",
    "fulfillment_model": "fulfillmentModel", "offer_price": "offerPrice",
    "gmv_lcy": "gmvLcy", "currency_code": "currencyCode", "dest_country": "destCountry",
}
RAW_RECORDS = [{_RAW_KEYMAP[k]: v for k, v in r.items()} for r in ORDER_ROWS]

_ORDER_COMPARE_COLS = ("partner_sku", "noon_sku", "item_nr", "order_date", "status",
                       "is_cancelled", "is_return", "seller_price", "customer_paid",
                       "currency", "fulfillment", "destination", "source")


# ── 替身 page（同 smoke_platform_session 形态；只实现 evaluate）──────────────
class FakePage:
    """可控 page：复刻 Sales Dashboard 的 POST 取数契约（同 smoke_platform_session 替身 page）。

    evaluate(js, {url, body}) 返回 {status, json}；pages 给定则按 body.page 分页发 hits，
    否则单页发 payload（list 或 dict）。记录被 goto/evaluate 调到的 url/arg。
    """
    def __init__(self, payload=None, *, pages=None, total=None, status=200):
        self.payload = payload
        self.pages = pages
        self.total = total
        self.status = status
        self.evaluated = []
        self.goto_calls = []

    def goto(self, url, **kw):
        self.goto_calls.append(url)

    def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        if self.pages is not None:
            page_no = (arg or {}).get("body", {}).get("page", 1)
            hits = self.pages.get(page_no, [])
            return {"status": self.status, "json": {"hits": hits, "total": self.total}}
        return {"status": self.status, "json": self.payload}


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _reset_db():
    c = sqlite3.connect(_DB)
    try:
        for t in ("wf2_orders", "wf2_sku"):
            c.executescript(f"DROP TABLE IF EXISTS {t};")
            c.executescript(_extract_create(t))
        c.commit()
    finally:
        c.close()


def _write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in ORDER_ROWS:
            w.writerow(r)


def _dump(table, key):
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r[key]: dict(r)
                for r in c.execute(f"SELECT * FROM {table} ORDER BY {key}")}
    finally:
        c.close()


def _expect_red(fn, must_contain, label):
    raised = False
    try:
        fn()
    except F.LiveSourceUnavailable as e:
        raised = True
        assert must_contain in str(e), f"[{label}] 红灯异常应含 {must_contain!r}: {e}"
    assert raised, f"[{label}] 必须红灯 raise LiveSourceUnavailable（不得冒充成功）"


def main():
    failures = []

    # 全程自己掌控 orders producer 注册表状态，结束清空，不污染其它 smoke。
    C.set_live_row_producer(C.ORDERS, None)
    try:
        # ── 1. socket：抓取器对外接口齐备 ────────────────────────────────
        for name in ("fetch_order_rows", "to_contract_row", "make_live_row_producer",
                     "register_live_producer", "unregister_live_producer",
                     "_fetch_raw_orders", "LiveSourceUnavailable"):
            assert hasattr(F, name), f"抓取器缺 {name}（接线缺失）"
        assert F.LiveSourceUnavailable is C.LiveSourceUnavailable, \
            "抓取器 LiveSourceUnavailable 必须就是 contract 的同一类（不另起一份）"
        # 字段映射键 ⊆ 契约 known（防漂出契约外字段）。
        assert set(F._NOON_ORDER_FIELD_MAP) <= set(C.ROW_CONTRACT[C.ORDERS]["known"]), \
            "字段映射键超出 WS-34 契约 known 集"
        print("✓ 抓取器 socket 齐备 + LiveSourceUnavailable/字段映射对齐 WS-34 契约")

        # ── 2. to_contract_row：noon 原始记录 → 契约行，逐字段映射正确且过校验 ──
        raw0 = RAW_RECORDS[0]
        row0 = F.to_contract_row(raw0)
        C.validate_row(C.ORDERS, row0)  # 不抛 = 合契约
        for ck in ("partner_sku", "sku", "item_nr", "order_timestamp", "status",
                   "fulfillment_model", "offer_price", "gmv_lcy", "currency_code",
                   "dest_country"):
            assert row0[ck] == ORDER_ROWS[0][ck], \
                f"映射 {ck} 错: {row0.get(ck)!r} != {ORDER_ROWS[0][ck]!r}"
        # 产出键 ⊆ 契约 known（不带契约外字段）。
        assert set(row0) <= set(C.ROW_CONTRACT[C.ORDERS]["known"]), \
            f"to_contract_row 产出契约外字段: {set(row0) - set(C.ROW_CONTRACT[C.ORDERS]['known'])}"
        print("✓ to_contract_row：noon 原始记录逐字段映射回 WS-34 契约键，过 validate_row")

        # ── 3. 字段缺失红灯：缺必填 item_nr 的原始记录 → 红灯，绝不补默认值编数 ──
        bad_raw = {k: v for k, v in raw0.items() if k != "itemNr"}  # 抽掉 item_nr 来源
        bad_row = F.to_contract_row(bad_raw)
        assert "item_nr" not in bad_row, "缺源字段时不得补默认 item_nr（占位假数据死法）"
        _expect_red(lambda: C.validate_row(C.ORDERS, bad_row), "item_nr", "缺必填红灯")
        # 经 fetch_order_rows（注入 raw_orders_fn）端到端也红灯
        _expect_red(
            lambda: F.fetch_order_rows(TENANT, page=object(),
                                       raw_orders_fn=lambda p: [bad_raw]),
            "item_nr", "fetch 缺必填红灯")
        print("✓ 关键字段缺失（item_nr）→ 红灯 LiveSourceUnavailable，不编造订单字段")

        # ── 4. _fetch_raw_orders 走真函数：缺配置/HTTP 错/结构变 blocked，正常分页汇总 ──
        _orig_cfg = F._orders_cfg
        _cfg = lambda d: (lambda store_key=F.DEFAULT_STORE_KEY: dict(d))
        # 4a. 缺 api_url 配置 → blocked（绝不猜 URL）。
        F._orders_cfg = _cfg({})
        try:
            _expect_red(lambda: F._fetch_raw_orders(FakePage([])),
                        "api_url", "缺接口配置 blocked")
            # 4a'. 有 api_url 但缺国别 → blocked（绝不默认国别）。
            F._orders_cfg = _cfg({"api_url": "https://x/api"})
            _expect_red(lambda: F._fetch_raw_orders(FakePage([])),
                        "country_code", "缺国别 blocked")
            # 4b. 返回结构非预期（dict 无 list 容器）→ blocked。
            F._orders_cfg = _cfg({"api_url": "https://x/api", "country_code": "SA"})
            _expect_red(lambda: F._fetch_raw_orders(FakePage({"unexpected": 1})),
                        "list", "接口结构变 blocked")
            # 4c. HTTP 非 200 → blocked（登录态失效/接口改版，不回落旧值）。
            _expect_red(lambda: F._fetch_raw_orders(FakePage([], status=403)),
                        "HTTP 403", "HTTP 错 blocked")
            # 4d. 正常分页：per_page=2 跨 3 页汇总 5 行，POST body 带 country/page。
            F._orders_cfg = _cfg({"api_url": "https://x/api", "country_code": "SA",
                                  "per_page": 2, "report_page_url": "https://x/sales"})
            fp = FakePage(pages={1: RAW_RECORDS[0:2], 2: RAW_RECORDS[2:4],
                                 3: RAW_RECORDS[4:5]}, total=len(RAW_RECORDS))
            recs = F._fetch_raw_orders(fp)
            assert recs == RAW_RECORDS, f"分页汇总应等于全量 records: {len(recs)}"
            assert fp.goto_calls == ["https://x/sales"], \
                f"取数前应先导到同域报表页（避 CORS）: {fp.goto_calls}"
            assert len(fp.evaluated) == 3, f"per_page=2/total=5 应翻 3 页: {len(fp.evaluated)}"
            assert fp.evaluated[0]["url"] == "https://x/api" and \
                fp.evaluated[0]["body"]["country_code"] == "SA" and \
                fp.evaluated[0]["body"]["page"] == 1, \
                f"POST body 契约不符: {fp.evaluated[0]}"
        finally:
            F._orders_cfg = _orig_cfg
        print("✓ _fetch_raw_orders：缺配置/缺国别/HTTP 错/结构变 → blocked；正常分页 POST 汇总全量")

        # ── 5. 注册接线 + live==CSV：register → run_live 真走 live 行 → 同一 _aggregate/_upsert ──
        # 5a. 改前（未注册）：run_live 无 producer + 无 CSV → 红灯回落失败（接线缺失死法）。
        F.unregister_live_producer()
        assert noon.get_live_row_producer() is None, "未注册时 ingest 视图应为 None"
        _reset_db()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(
                lambda: noon.run_live(TENANT, inbox=empty_dir, entity_alias=ALIAS),
                "producer", "未注册无 CSV 红灯")
        assert _dump("wf2_orders", "item_nr") == {}, "未注册红灯路径不得凭空写订单行"

        # 5b. 注册（注入替身 page + raw_orders_fn 产出伪装 noon 记录）→ 单一来源注册表。
        page_factory = lambda tenant_id: FakePage(RAW_RECORDS)
        producer = F.register_live_producer(
            page_factory=page_factory, raw_orders_fn=lambda p: list(p.payload))
        assert C.get_live_row_producer(C.ORDERS) is producer, \
            "register_live_producer 必须写进订单 ingest（=contract ORDERS）注册表（单一来源）"
        assert noon.get_live_row_producer() is producer, "ingest 视图应读到同一 producer"

        # 5c. run_live 真走 live 行，spy 证明经模块级 _aggregate/_upsert（没另起炉灶）。
        calls = {"agg": 0, "ups": 0}
        _oa, _ou = noon._aggregate, noon._upsert
        noon._aggregate = lambda *a, **k: (calls.__setitem__("agg", calls["agg"] + 1) or _oa(*a, **k))
        noon._upsert = lambda *a, **k: (calls.__setitem__("ups", calls["ups"] + 1) or _ou(*a, **k))
        try:
            _reset_db()
            res_live = noon.run_live(TENANT, entity_alias=ALIAS)
        finally:
            noon._aggregate, noon._upsert = _oa, _ou
        assert res_live["source"] == "live", f"注册后应走 live 源: {res_live}"
        assert res_live["orders"] == len(ORDER_ROWS), f"live 行数异常: {res_live}"
        assert calls["agg"] >= 1 and calls["ups"] >= 1, \
            f"run_live 未经模块级 _aggregate/_upsert（疑似旁路）: {calls}"
        live_orders = _dump("wf2_orders", "item_nr")

        # 同份 fixture 走 CSV 路径，逐字段对比（证明抓取器映射没绕过契约/没编数）。
        _reset_db()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_orders.csv")
            _write_csv(csv_path)
            noon.run_v2(TENANT, file=csv_path, entity_alias=ALIAS)
        csv_orders = _dump("wf2_orders", "item_nr")
        assert set(live_orders) == set(csv_orders) == EXPECT_ITEMS, \
            f"live/CSV item_nr 集合不一致: {set(live_orders)} vs {set(csv_orders)}"
        for it in EXPECT_ITEMS:
            for col in _ORDER_COMPARE_COLS:
                if col == "source":
                    continue  # live vs noon(csv) source 标记本就不同
                assert live_orders[it][col] == csv_orders[it][col], \
                    f"live 抓取器落库 {it}.{col} 与 CSV 分叉: {live_orders[it][col]!r} != {csv_orders[it][col]!r}"
            assert live_orders[it]["source"] == "noon", \
                f"{it} source 应为 ingest 标准 'noon'（落库一致）"
        print(f"✓ register → run_live 真走 live 行 → 同一 _aggregate/_upsert，"
              f"与同份 fixture CSV 落库逐字段一致（{len(ORDER_ROWS)} 行）")

        # ── 6. 登录态失效 blocked：page_factory 抛 blocked → run_live 无 CSV 红灯，库里无凭空行 ──
        from hipop.server import _platform_browser as pb

        def _login_blocked(tenant_id):
            raise pb.PlatformBrowserError(
                "平台 noon 未登录：缺会话 cookie _npsid。请参照 refresh-dbuyerp-token "
                "流程在本机紫鸟重登该店一次", blocked=True)

        F.register_live_producer(page_factory=_login_blocked)
        _reset_db()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(
                lambda: noon.run_live(TENANT, inbox=empty_dir, entity_alias=ALIAS),
                "refresh-dbuyerp-token", "登录失效无 CSV 红灯")
        assert _dump("wf2_orders", "item_nr") == {}, \
            "登录失效红灯路径凭空写了订单行（占位假数据死法）"
        assert _dump("wf2_sku", "partner_sku") == {}, \
            "登录失效红灯路径凭空写了 SKU 行（占位假数据死法）"
        # 有 CSV interim 时回落同契约（不丢运营手工数据），带失败信号。
        _reset_db()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_orders.csv")
            _write_csv(csv_path)
            res_fb = noon.run_live(TENANT, file=csv_path, entity_alias=ALIAS)
        assert res_fb["source"] == "csv_fallback", f"登录失效有 CSV 应回落: {res_fb}"
        assert res_fb.get("live_error") and "refresh-dbuyerp-token" in res_fb["live_error"], \
            f"回落必须带登录失效失败信号（含人工登录提示）: {res_fb}"
        print("✓ 登录态失效 → blocked 上抛：无 CSV 红灯且库里无凭空行；有 CSV 回落同契约带失败信号")

        print("\n6/6 passed")
    finally:
        F.unregister_live_producer()
        C.set_live_row_producer(C.ORDERS, None)

    if failures:
        print(f"✗ {len(failures)} 失败: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass
    sys.exit(rc)

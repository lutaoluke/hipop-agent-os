"""Smoke: WS-N2.2 / WS-59 — noon 可售库存实时抓取器（page → rows，KSA）。

承重墙（抓取器 + 注册 + 真 page→真 rows 的确定性回归）：
  `noon_stock_fetcher` 把 `get_platform_session(tenant_id,"noon")` 的**已登录 page** 抓出
  noon my inventory 行，映射成 WS-34 库存行契约，经库存 ingest `set_live_row_producer`
  注册，喂同一 `_aggregate`/`_upsert`（WS-N3.1/N3.2）部分 upsert 落 wf1_stock.noon_*，
  与 CSV 路径逐字段一致，且绝不碰 ERP 列 / pending_inbound_qty。

  「真 page→真 rows」的唯一外部边界是 `_fetch_raw_inventory(page)`（page 侧 fetch noon
  接口）；本 smoke 注入替身 page / raw_inventory_fn 做**确定性**回归（同
  smoke_platform_session 替身 page，无需真紫鸟/playwright），把映射、字段缺失红灯、
  接口改版红灯、注册接线、登录失效 blocked 全钉死在真函数里。真紫鸟下的端到端 live 跑法
  见模块 `__main__` 与 PR。

钉死三种死法：
  · 接线缺失：`register_live_producer` 写进的就是库存 ingest（= contract MY_INVENTORY）
    注册表，且 run_live 注册后真走 live 行 → 同一 `_aggregate`/`_upsert` 落库（spy 计数）。
  · 死代码短路 / 假绿：live 落库 == 跑同份 fixture CSV 落库（逐字段），证明抓取器映射没
    绕过契约；登录失效/坏行不返回空/旧行冒充成功。
  · 占位假数据：字段缺失/接口改版/登录失效 → 红灯 raise，断言库里没有凭空写出的库存行
    / 0 假库存 / 编造仓库 JSON。

fail-then-pass：
  改动前 `noon_stock_fetcher` 不存在 → import 即 fail（红）。实现后 → 全 pass（绿）。
  另：把 `to_contract_row` 退回「缺字段补默认 0」即占位假数据死法，#3 的红灯断言会 FAIL。

跑法：
  python3 tests/smoke_noon_stock_fetcher.py    或    make test
（纯 SQLite 临时库，不碰 PG / 不碰 live hipop.db；不连真紫鸟。）
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
_DB = tempfile.NamedTemporaryFile(suffix="_stock_fetcher.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402  （行字段 + fixture 唯一来源）
import noon_stock_fetcher as F  # noqa: E402  （被测抓取器）
import ingest_noon_stock_csv_v2 as noon  # noqa: E402  （库存 ingest socket）

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 单一事实源：fixture = noon_live_contract 的 MY_INVENTORY fixture（WS-34）。
# 列：country_code, sku, warehouse_code, qty, inventory_type, title。
INVENTORY_ROWS = C.load_fixture_rows(C.MY_INVENTORY)
_CSV_COLS = list(INVENTORY_ROWS[0].keys())
EXPECT_SKUS = {"SKU-A", "SKU-B", "SKU-C"}  # 经 wf2_sku 映射后的 partner_sku

# 契约键 → noon 接口风格字段名（camelCase 别名，全在 _NOON_STOCK_FIELD_MAP 候选里）。
# 用它把 fixture 行「伪装成」noon 原始接口记录，喂抓取器映射回契约键，证明映射正确。
_RAW_KEYMAP = {
    "country_code": "countryCode", "sku": "skuCode", "warehouse_code": "warehouseCode",
    "qty": "quantity", "inventory_type": "inventoryType", "title": "title",
}
RAW_RECORDS = [{_RAW_KEYMAP[k]: v for k, v in r.items()} for r in INVENTORY_ROWS]

_NOON_COMPARE_COLS = ("noon_total_qty", "noon_saleable_qty",
                      "noon_unsaleable_qty", "noon_warehouses_json")
_ERP_SEED = (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7)  # 验部分 upsert 不覆盖
_ERP_COLS = ("yiwu_qty", "dongguan_qty", "overseas_total_qty", "total_stock")


# ── 替身 page（同 smoke_platform_session 形态；只实现 evaluate）──────────────
class FakePage:
    """可控 page：evaluate(js, url) 返回预置 JSON；记录被调到的 url。"""
    def __init__(self, payload):
        self.payload = payload
        self.evaluated = []

    def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return self.payload


def _extract_create(table: str) -> str:
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _seed_entities():
    """建 sales_entities + wf2_sku（country→entity、平台 SKU→partner_sku 映射，建一次）。"""
    c = sqlite3.connect(_DB)
    try:
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
    finally:
        c.close()


def _reset_stock():
    """重建 wf1_stock + 预置 ERP 行（每个落库子场景前调一次，保证两路从同一干净起点可比）。"""
    c = sqlite3.connect(_DB)
    try:
        c.executescript("DROP TABLE IF EXISTS wf1_stock;")
        c.executescript(_extract_create("wf1_stock"))
        c.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _ERP_SEED,
        )
        c.commit()
    finally:
        c.close()


def _write_csv(path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in INVENTORY_ROWS:
            w.writerow(r)


def _dump():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


# 抓取器（纯函数路径）raise contract 的 LiveSourceUnavailable；库存 ingest run_live
# 自身（无 producer / 回落失败）raise 它**自有**的 LiveSourceUnavailable（WS-N3.x 早于
# WS-34 收口，stock socket 保留了一份独立异常类——既有事实，不在本条范围内动）。两条红灯
# 路径都要钉死，故 _expect_red 接受这两类。
_RED = (F.LiveSourceUnavailable, noon.LiveSourceUnavailable)


def _expect_red(fn, must_contain, label):
    raised = False
    try:
        fn()
    except _RED as e:
        raised = True
        assert must_contain in str(e), f"[{label}] 红灯异常应含 {must_contain!r}: {e}"
    assert raised, f"[{label}] 必须红灯 raise LiveSourceUnavailable（不得冒充成功）"


def main():
    _seed_entities()

    # 全程自己掌控 my_inventory producer 注册表状态，结束清空，不污染其它 smoke。
    C.set_live_row_producer(C.MY_INVENTORY, None)
    try:
        # ── 1. socket：抓取器对外接口齐备 ────────────────────────────────
        for name in ("fetch_inventory_rows", "to_contract_row", "make_live_row_producer",
                     "register_live_producer", "unregister_live_producer",
                     "_fetch_raw_inventory", "LiveSourceUnavailable"):
            assert hasattr(F, name), f"抓取器缺 {name}（接线缺失）"
        assert F.LiveSourceUnavailable is C.LiveSourceUnavailable, \
            "抓取器 LiveSourceUnavailable 必须就是 contract 的同一类（不另起一份）"
        assert set(F._NOON_STOCK_FIELD_MAP) <= set(C.ROW_CONTRACT[C.MY_INVENTORY]["known"]), \
            "字段映射键超出 WS-34 契约 known 集"
        print("✓ 抓取器 socket 齐备 + LiveSourceUnavailable/字段映射对齐 WS-34 契约")

        # ── 2. to_contract_row：noon 原始记录 → 契约行，逐字段映射正确且过校验 ──
        raw0 = RAW_RECORDS[0]
        row0 = F.to_contract_row(raw0)
        C.validate_row(C.MY_INVENTORY, row0)  # 不抛 = 合契约
        for ck in ("country_code", "sku", "warehouse_code", "qty", "inventory_type", "title"):
            assert row0[ck] == INVENTORY_ROWS[0][ck], \
                f"映射 {ck} 错: {row0.get(ck)!r} != {INVENTORY_ROWS[0][ck]!r}"
        assert set(row0) <= set(C.ROW_CONTRACT[C.MY_INVENTORY]["known"]), \
            f"to_contract_row 产出契约外字段: {set(row0) - set(C.ROW_CONTRACT[C.MY_INVENTORY]['known'])}"
        print("✓ to_contract_row：noon 原始记录逐字段映射回 WS-34 契约键，过 validate_row")

        # ── 3. 字段缺失红灯：缺必填 qty 的原始记录 → 红灯，绝不补默认 0 假库存 ──
        bad_raw = {k: v for k, v in raw0.items() if k != "quantity"}  # 抽掉 qty 来源
        bad_row = F.to_contract_row(bad_raw)
        assert "qty" not in bad_row, "缺源字段时不得补默认 qty=0（占位假数据死法）"
        _expect_red(lambda: C.validate_row(C.MY_INVENTORY, bad_row), "qty", "缺必填红灯")
        # 经 fetch_inventory_rows（注入 raw_inventory_fn）端到端也红灯
        _expect_red(
            lambda: F.fetch_inventory_rows(TENANT, page=object(),
                                           raw_inventory_fn=lambda p: [bad_raw]),
            "qty", "fetch 缺必填红灯")
        # 缺 SKU 主键来源（country/qty/type 在，但无 partner_sku/sku/noon_sku）→ 红灯
        no_sku = {k: v for k, v in raw0.items() if k != "skuCode"}
        _expect_red(
            lambda: F.fetch_inventory_rows(TENANT, page=object(),
                                           raw_inventory_fn=lambda p: [no_sku]),
            "SKU", "fetch 缺 SKU 主键红灯")
        print("✓ 关键字段缺失（qty / SKU 主键）→ 红灯 LiveSourceUnavailable，不写 0 假库存")

        # ── 4. 接口/页面改版红灯：_fetch_raw_inventory 走真函数（缺配置/结构变都 blocked）──
        _orig_cfg = F._stock_cfg
        # 4a. 缺 api_url 配置 → blocked（绝不猜 URL）。
        F._stock_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {}
        try:
            _expect_red(lambda: F._fetch_raw_inventory(FakePage([])),
                        "api_url", "缺接口配置 blocked")
        finally:
            F._stock_cfg = _orig_cfg
        # 4b. 有 api_url 但返回结构非预期（dict 无 list 容器）→ blocked。
        F._stock_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {"api_url": "https://x/api"}
        try:
            _expect_red(lambda: F._fetch_raw_inventory(FakePage({"unexpected": 1})),
                        "list", "接口结构变 blocked")
            # 4c. 正常结构（list）→ 真走 page.evaluate 拿回 records。
            fp = FakePage(RAW_RECORDS)
            recs = F._fetch_raw_inventory(fp)
            assert recs == RAW_RECORDS and fp.evaluated == ["https://x/api"], \
                f"_fetch_raw_inventory 应同源 fetch 配置 api_url 并返回 records: {fp.evaluated}"
            # 4d. records_path 给定 → 按键逐层走到 list。
            F._stock_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {
                "api_url": "https://x/api", "records_path": ["data", "inventory"]}
            nested = FakePage({"data": {"inventory": RAW_RECORDS}})
            assert F._fetch_raw_inventory(nested) == RAW_RECORDS, "records_path 逐层走 list 失败"
        finally:
            F._stock_cfg = _orig_cfg
        print("✓ 缺接口配置 / 接口结构变 → blocked 红灯；正常结构 + records_path 经 page.evaluate 取回 records")

        # ── 5. 注册接线 + live==CSV：register → run_live 真走 live 行 → 同一 _aggregate/_upsert ──
        # 5a. 改前（未注册）：run_live 无 producer + 无 CSV → 红灯回落失败（接线缺失死法）。
        F.unregister_live_producer()
        assert noon.get_live_row_producer() is None, "未注册时 ingest 视图应为 None"
        _reset_stock()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(lambda: noon.run_live(TENANT, inbox=empty_dir),
                        "producer", "未注册无 CSV 红灯")
        assert set(_dump()) == {"SKU-A"}, "未注册红灯路径不得凭空写库存行（仅剩 ERP 种子行）"

        # 5b. 注册（注入替身 page + raw_inventory_fn 产出伪装 noon 记录）→ 单一来源注册表。
        page_factory = lambda tenant_id: FakePage(RAW_RECORDS)
        producer = F.register_live_producer(
            page_factory=page_factory, raw_inventory_fn=lambda p: list(p.payload))
        assert C.get_live_row_producer(C.MY_INVENTORY) is producer, \
            "register_live_producer 必须写进库存 ingest（=contract MY_INVENTORY）注册表（单一来源）"
        assert noon.get_live_row_producer() is producer, "ingest 视图应读到同一 producer"
        assert C.MY_INVENTORY not in C.missing_live_producers(), \
            "注册后 contract.missing 不应再缺 my_inventory（WS-38 收口可见）"

        # 5c. run_live 真走 live 行，spy 证明经模块级 _aggregate/_upsert（没另起炉灶）。
        calls = {"agg": 0, "ups": 0}
        _oa, _ou = noon._aggregate, noon._upsert
        noon._aggregate = lambda *a, **k: (calls.__setitem__("agg", calls["agg"] + 1) or _oa(*a, **k))
        noon._upsert = lambda *a, **k: (calls.__setitem__("ups", calls["ups"] + 1) or _ou(*a, **k))
        try:
            _reset_stock()
            res_live = noon.run_live(TENANT)
        finally:
            noon._aggregate, noon._upsert = _oa, _ou
        assert res_live["source"] == "live", f"注册后应走 live 源: {res_live}"
        assert res_live["rows"] == len(INVENTORY_ROWS), f"live 行数异常: {res_live}"
        assert res_live["skus"] == len(EXPECT_SKUS) and res_live["unmapped"] == 0, \
            f"live SKU/unmapped 异常: {res_live}"
        assert calls["agg"] >= 1 and calls["ups"] >= 1, \
            f"run_live 未经模块级 _aggregate/_upsert（疑似旁路）: {calls}"
        live_db = _dump()

        # 同份 fixture 走 CSV 路径，逐字段对比（证明抓取器映射没绕过契约/没编数）。
        _reset_stock()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_inventory.csv")
            _write_csv(csv_path)
            res_csv = noon.run_v2(TENANT, file=csv_path)
        assert res_csv["rows"] == len(INVENTORY_ROWS) and res_csv["unmapped"] == 0, \
            f"CSV 计数异常: {res_csv}"
        csv_db = _dump()

        assert set(live_db) == set(csv_db) == (EXPECT_SKUS | {"SKU-A"}), \
            f"live/CSV partner_sku 集合不一致: {set(live_db)} vs {set(csv_db)}"
        assert not any(k.startswith("Z") for k in live_db), "平台 SKU 被当成主键写进去了（映射未回 partner_sku）"
        for sku in EXPECT_SKUS:
            for col in _NOON_COMPARE_COLS:
                assert live_db[sku][col] == csv_db[sku][col], \
                    f"live 抓取器落库 {sku}.{col} 与 CSV 分叉: {live_db[sku][col]!r} != {csv_db[sku][col]!r}"
            assert json.loads(live_db[sku]["noon_warehouses_json"]) == \
                json.loads(csv_db[sku]["noon_warehouses_json"]), f"{sku} 仓库 JSON 结构不一致"
        # 真实聚合口径（非 hardcode）：total = saleable + unsaleable。
        a = live_db["SKU-A"]
        assert a["noon_total_qty"] == a["noon_saleable_qty"] + a["noon_unsaleable_qty"], \
            f"SKU-A total != saleable+unsaleable: {a}"
        # 部分 upsert 边界：SKU-A 的 ERP 列 / pending_inbound 不被 noon 路径覆盖。
        assert tuple(a[c] for c in _ERP_COLS) == (99, 88, 77, 264), f"noon 路径覆盖了 ERP 列: {a}"
        assert a["pending_inbound_qty"] == 7, f"noon 路径动了 pending_inbound_qty: {a}"
        print(f"✓ register → run_live 真走 live 行 → 同一 _aggregate/_upsert，与同份 fixture CSV "
              f"落库 noon_* 逐字段一致（{len(INVENTORY_ROWS)} 行/{len(EXPECT_SKUS)} SKU），且不碰 ERP 列/pending")

        # ── 6. 登录态失效 blocked：page_factory 抛 blocked → run_live 无 CSV 红灯，库里无凭空行 ──
        from hipop.server import _platform_browser as pb

        def _login_blocked(tenant_id):
            raise pb.PlatformBrowserError(
                "平台 noon 未登录：缺会话 cookie _npsid。请参照 refresh-dbuyerp-token "
                "流程在本机紫鸟重登该店一次", blocked=True)

        F.register_live_producer(page_factory=_login_blocked)
        _reset_stock()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(lambda: noon.run_live(TENANT, inbox=empty_dir),
                        "refresh-dbuyerp-token", "登录失效无 CSV 红灯")
        assert set(_dump()) == {"SKU-A"}, \
            "登录失效红灯路径凭空写了库存行（占位假数据死法，应仅剩 ERP 种子行）"
        # 有 CSV interim 时回落同契约（不丢运营手工数据），带失败信号。
        _reset_stock()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_inventory.csv")
            _write_csv(csv_path)
            res_fb = noon.run_live(TENANT, file=csv_path)
        assert res_fb["source"] == "csv_fallback", f"登录失效有 CSV 应回落: {res_fb}"
        assert res_fb.get("live_error") and "refresh-dbuyerp-token" in res_fb["live_error"], \
            f"回落必须带登录失效失败信号（含人工登录提示）: {res_fb}"
        assert set(_dump()) == (EXPECT_SKUS | {"SKU-A"}), "回落应落真实 CSV 库存行"
        print("✓ 登录态失效 → blocked 上抛：无 CSV 红灯且库里无凭空行；有 CSV 回落同契约带失败信号")

        # ── 7. live 源空回（0 行）→ 不冒充 source=live（门2 返工点①）──────────────
        # 验门人打回：producer→[] 后 run_live 原样返回 {'source':'live','rows':0,...}，0 行假绿。
        # 现在：无可落 SKU → 无 CSV 红灯（blocked）/ 有 CSV 显式回落（source=csv_fallback，不报 live）。
        F.register_live_producer(page_factory=lambda t: FakePage([]),
                                 raw_inventory_fn=lambda p: [])
        _reset_stock()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(lambda: noon.run_live(TENANT, inbox=empty_dir),
                        "空库存", "live 空回无 CSV 红灯")
        assert set(_dump()) == {"SKU-A"}, \
            "live 空回红灯路径凭空写了库存行（应仅剩 ERP 种子行）"
        _reset_stock()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_inventory.csv")
            _write_csv(csv_path)
            res_e = noon.run_live(TENANT, file=csv_path)
        assert res_e["source"] == "csv_fallback", f"live 空回有 CSV 应回落、绝不报 source=live: {res_e}"
        assert res_e.get("live_error") and "空库存" in res_e["live_error"], \
            f"回落须带 live 空回失败信号: {res_e}"
        print("✓ live 空回（0 行）→ 不冒充 source=live：无 CSV 红灯 / 有 CSV 显式回落（source=csv_fallback）")

        # ── 8. live 行平台 SKU 无法映射回 partner_sku → 不冒充 source=live（门2 返工点②）──
        # 验门人打回：unmapped 行原只计数 continue，run_live 仍返回 {'source':'live','unmapped':1}。
        # 现在：任一 live 行缺 wf2_sku 映射 → 无 CSV 红灯 / 有 CSV 显式回落，绝不只 unmapped++ 成功。
        unmapped_raw = {**RAW_RECORDS[0], "skuCode": "ZUNKNOWN"}  # 平台 SKU 无 wf2_sku 映射
        F.register_live_producer(page_factory=lambda t: FakePage([unmapped_raw]),
                                 raw_inventory_fn=lambda p: [dict(unmapped_raw)])
        _reset_stock()
        with tempfile.TemporaryDirectory() as empty_dir:
            _expect_red(lambda: noon.run_live(TENANT, inbox=empty_dir),
                        "无法映射回 partner_sku", "live 缺映射无 CSV 红灯")
        assert set(_dump()) == {"SKU-A"}, \
            "live 缺映射红灯路径凭空写了库存行（应仅剩 ERP 种子行）"
        _reset_stock()
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "noon_inventory.csv")
            _write_csv(csv_path)
            res_u = noon.run_live(TENANT, file=csv_path)
        assert res_u["source"] == "csv_fallback", f"live 缺映射有 CSV 应回落、绝不报 source=live: {res_u}"
        assert res_u.get("live_error") and "无法映射回 partner_sku" in res_u["live_error"], \
            f"回落须带 live 缺映射失败信号: {res_u}"
        print("✓ live 行缺 SKU 映射 → 不冒充 source=live：无 CSV 红灯 / 有 CSV 显式回落（source=csv_fallback）")

        print("\n8/8 passed")
        return 0
    finally:
        F.unregister_live_producer()
        C.set_live_row_producer(C.MY_INVENTORY, None)


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass
    sys.exit(rc)

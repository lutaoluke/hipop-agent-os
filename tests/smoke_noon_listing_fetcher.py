"""Smoke: WS-183/S2（WS-188）— noon listing 在售目录实时抓取器（page → rows）+ 307 映射预检。

承重墙（抓取器 + 注册 + 真 page→真 rows 的确定性回归 + 映射预检）：
  `noon_listing_fetcher` 把 `get_platform_session(tenant_id,"noon")` 的**已登录 page** 抓出
  noon 后台在售/在架 listing 行，映射成 WS-183/S1 listing 行契约
  （`noon_live_contract.ROW_CONTRACT[LISTINGS]`），经 `register_live_producer` 注册进
  **contract 的 LISTINGS 注册表**（listings 不在 KINDS/refresh_all_v2，故直接注册 contract，
  不经 ingest 模块），供 listing ingest/对账下游按需取用。

  「真 page→真 rows」的唯一外部边界是 `_fetch_raw_listings(page)`（page 侧取数）；本 smoke
  注入替身 page / raw_listings_fn 做**确定性**回归（同 smoke_platform_session 替身 page，
  无需真紫鸟/playwright），把映射、字段缺失红灯、接口改版红灯、注册接线、登录失效 blocked、
  以及 307 映射预检全钉死在真函数里。真紫鸟下的端到端 live 跑法见模块 `__main__` 与 PR。

钉死三种死法：
  · 接线缺失：`register_live_producer` 写进的就是 contract 的 LISTINGS 注册表，
    `get_live_row_producer(LISTINGS)` 取得到同一 producer；未注册 → `assert_listing_producer_ready`
    明确 raise LiveSourceUnavailable（不静默当“没有 listing”）。
  · 死代码短路 / 假绿：登录失效 / 接口改版 / 缺字段不返回空目录冒充成功；映射预检不把
    noon-only 无法映射的 listing 静默塞进“可映射”。
  · 占位假数据：字段缺失/接口改版/登录失效 → 红灯 raise；缺 partner/seller SKU 又无 wf2_sku
    绑定的行 → 计入 mapping gap（绝不补默认 partner_sku 凑数）。

307 映射预检（验收 #4）：listing 行只带 noon 平台 SKU 时，必须先经 wf2_sku 绑定
（noon_sku → partner_sku）尝试回映；回不到的计入 gap，逐条可点名，绝不静默吞。
本 smoke 用 fixture 直接造出“direct partner_sku / noon-only 经绑定可映 / noon-only 无绑定 gap”
三类，断言预检计数与覆盖率正确，并证明 gap 行被点名而非静默丢弃。

fail-then-pass：
  改动前 `noon_listing_fetcher` 不存在 → import 即 fail（红）。实现后 → 全 pass（绿）。
  另：把 `to_contract_row` 退回「缺字段补默认 listing_status」或把 mapping_precheck 退回
  「noon-only 无绑定也算可映射」即对应死法，相关红灯断言会 FAIL。

跑法：
  python3 tests/smoke_noon_listing_fetcher.py    或    make test
（纯内存/文件读，不碰 DB / 不碰 live；不连真紫鸟。）
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402  （行字段 + fixture 唯一来源 WS-183/S1）
import noon_listing_fetcher as F  # noqa: E402  （被测抓取器 WS-188）

TENANT = 1

# 单一事实源：fixture = noon_live_contract 的 LISTINGS fixture（WS-183/S1）。
# 列：country_code, store_name, noon_sku, partner_sku, sku, listing_status, is_listed, title。
LISTING_ROWS = C.load_fixture_rows(C.LISTINGS)

# 契约键 → noon catalog 接口风格字段名（camelCase 别名，全在 _NOON_LISTING_FIELD_MAP 候选里）。
# 用它把 fixture 行「伪装成」noon 原始接口记录，喂抓取器映射回契约键，证明映射正确。
_RAW_KEYMAP = {
    "country_code": "countryCode", "store_name": "storeName",
    "noon_sku": "noonSku", "partner_sku": "partnerSku", "sku": "skuCode",
    "listing_status": "listingStatus", "is_listed": "isListed", "title": "title",
}
RAW_RECORDS = [{_RAW_KEYMAP[k]: v for k, v in r.items()} for r in LISTING_ROWS]


class FakePage:
    """可控 page：evaluate(js, arg) 返回预置结果；记录被调到的参数（同 smoke_platform_session 形态）。"""
    def __init__(self, payload):
        self.payload = payload
        self.evaluated = []
        self.goto_urls = []

    def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return self.payload

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)


def _expect_red(fn, must_contain, label):
    raised = False
    try:
        fn()
    except F.LiveSourceUnavailable as e:
        raised = True
        assert must_contain in str(e), f"[{label}] 红灯异常应含 {must_contain!r}: {e}"
    assert raised, f"[{label}] 必须红灯 raise LiveSourceUnavailable（不得冒充成功）"


def main():
    # 全程自己掌控 listings producer 注册表状态，结束清空，不污染其它 smoke。
    C.set_live_row_producer(C.LISTINGS, None)
    try:
        # ── 1. socket：抓取器对外接口齐备 + 类型/字段映射对齐契约 ──────────────
        for name in ("fetch_listing_rows", "to_contract_row", "make_live_row_producer",
                     "register_live_producer", "unregister_live_producer",
                     "get_registered_producer", "assert_listing_producer_ready",
                     "mapping_precheck", "_fetch_raw_listings", "LiveSourceUnavailable"):
            assert hasattr(F, name), f"抓取器缺 {name}（接线缺失）"
        assert F.LiveSourceUnavailable is C.LiveSourceUnavailable, \
            "抓取器 LiveSourceUnavailable 必须就是 contract 的同一类（不另起一份）"
        assert set(F._NOON_LISTING_FIELD_MAP) <= set(C.ROW_CONTRACT[C.LISTINGS]["known"]), \
            "字段映射键超出 WS-183/S1 listing 契约 known 集"
        print("✓ 抓取器 socket 齐备 + LiveSourceUnavailable/字段映射对齐 listing 契约")

        # ── 2. to_contract_row：noon 原始记录 → 契约行，逐字段映射正确且过校验 ──
        raw0 = RAW_RECORDS[0]
        row0 = F.to_contract_row(raw0)
        C.validate_row(C.LISTINGS, row0)  # 不抛 = 合契约
        for ck in ("country_code", "store_name", "noon_sku", "partner_sku",
                   "sku", "listing_status", "is_listed", "title"):
            assert row0[ck] == LISTING_ROWS[0][ck], \
                f"映射 {ck} 错: {row0.get(ck)!r} != {LISTING_ROWS[0][ck]!r}"
        assert set(row0) <= set(C.ROW_CONTRACT[C.LISTINGS]["known"]), \
            f"to_contract_row 产出契约外字段: {set(row0) - set(C.ROW_CONTRACT[C.LISTINGS]['known'])}"
        # 端到端 fetch_listing_rows（注入 raw_listings_fn）→ 全部行合契约
        rows = F.fetch_listing_rows(TENANT, page=object(), raw_listings_fn=lambda p: RAW_RECORDS)
        assert len(rows) == len(LISTING_ROWS), f"fetch 行数异常: {len(rows)}"
        print("✓ to_contract_row：noon 原始记录逐字段映射回 listing 契约键，过 validate_row")

        # ── 3. 字段缺失红灯：缺 listing_status / 缺 SKU 主键 / 自造字段 → 红灯，不补默认 ──
        # 3a. 缺 listing_status 来源
        no_status = {k: v for k, v in raw0.items() if k != "listingStatus"}
        bad_row = F.to_contract_row(no_status)
        assert "listing_status" not in bad_row, "缺源字段时不得补默认 listing_status（占位假数据死法）"
        _expect_red(lambda: C.validate_row(C.LISTINGS, bad_row), "listing_status", "缺在售状态红灯")
        _expect_red(
            lambda: F.fetch_listing_rows(TENANT, page=object(),
                                         raw_listings_fn=lambda p: [no_status]),
            "listing_status", "fetch 缺在售状态红灯")
        # 3b. 缺 SKU 主键来源（noon_sku/partner_sku/sku 全无）
        no_sku = {"countryCode": "SA", "listingStatus": "active"}
        _expect_red(
            lambda: F.fetch_listing_rows(TENANT, page=object(),
                                         raw_listings_fn=lambda p: [no_sku]),
            "SKU", "fetch 缺 SKU 主键红灯")
        # 3c. 自造字段（接口漂移）→ 红灯（契约外字段）
        bad_field = {**raw0, "extra_unknown": "x"}
        # extra_unknown 不在 _NOON_LISTING_FIELD_MAP，故 to_contract_row 不会带它出来；
        # 直接对带契约外键的 row 校验，证明 validate_row 守门。
        _expect_red(lambda: C.validate_row(
            C.LISTINGS, {"country_code": "SA", "noon_sku": "N1",
                         "listing_status": "active", "invented": "x"}),
            "未知字段", "契约外字段红灯")
        print("✓ 缺在售状态 / 缺 SKU 主键 / 契约外字段 → 红灯 LiveSourceUnavailable，不补默认")

        # ── 4. 接口/页面改版红灯：_fetch_raw_listings 走真函数（缺配置/占位符/结构变都 blocked）──
        _orig_cfg = F._listings_cfg
        # 4a. 缺 api_url 配置 → blocked（绝不猜 URL）。
        F._listings_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {}
        try:
            _expect_red(lambda: F._fetch_raw_listings(FakePage([])),
                        "api_url", "缺接口配置 blocked")
        finally:
            F._listings_cfg = _orig_cfg
        # 4b. api_url 仍是未注入的 env 占位符 → blocked（绝不拿 ${...} 当真实 URL）。
        F._listings_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {"api_url": "${NOON_LISTINGS_API_URL}"}
        try:
            _expect_red(lambda: F._fetch_raw_listings(FakePage([])),
                        "占位符", "占位符未注入 blocked")
        finally:
            F._listings_cfg = _orig_cfg
        # 4c. 有 api_url 但返回结构非预期（dict 无 list 容器）→ blocked。
        F._listings_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {"api_url": "https://x/api"}
        try:
            _expect_red(lambda: F._fetch_raw_listings(FakePage({"unexpected": 1})),
                        "list", "接口结构变 blocked")
            # 4d. 正常结构（list）→ 真走 page.evaluate 拿回 records。
            fp = FakePage(RAW_RECORDS)
            recs = F._fetch_raw_listings(fp)
            assert recs == RAW_RECORDS, f"_fetch_raw_listings 应返回 records: {recs}"
            assert fp.evaluated, "应经 page.evaluate 取数（碰真实接口边界）"
            # 4e. records_path 给定 → 按键逐层走到 list。
            F._listings_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {
                "api_url": "https://x/api", "records_path": ["data", "listings"]}
            nested = FakePage({"data": {"listings": RAW_RECORDS}})
            assert F._fetch_raw_listings(nested) == RAW_RECORDS, "records_path 逐层走 list 失败"
        finally:
            F._listings_cfg = _orig_cfg
        print("✓ 缺接口配置 / 占位符未注入 / 接口结构变 → blocked 红灯；正常结构 + records_path 取回 records")

        # ── 5. 注册接线：register → contract LISTINGS 注册表单一来源；未注册明确红灯 ──
        # 5a. 未注册：assert_listing_producer_ready 明确红灯（不静默当“没有 listing”）。
        F.unregister_live_producer()
        assert C.get_live_row_producer(C.LISTINGS) is None, "未注册时 contract 视图应为 None"
        assert F.get_registered_producer() is None, "未注册时 get_registered_producer 应为 None"
        _expect_red(F.assert_listing_producer_ready, "listing", "未注册红灯")
        # listings 不在 KINDS：注册与否都不影响 refresh_all_v2 的三类收口判定。
        assert C.LISTINGS not in C.KINDS, "listings 不应混进 KINDS(refresh 集)"
        assert C.LISTINGS not in C.missing_live_producers(), \
            "listings 不应出现在 missing_live_producers（不走 refresh 注册表收口）"

        # 5b. 注册（注入替身 page + raw_listings_fn）→ contract LISTINGS 注册表读到同一 producer。
        page_factory = lambda tenant_id: FakePage(RAW_RECORDS)
        producer = F.register_live_producer(
            page_factory=page_factory, raw_listings_fn=lambda p: list(p.payload))
        assert C.get_live_row_producer(C.LISTINGS) is producer, \
            "register_live_producer 必须写进 contract 的 LISTINGS 注册表（单一来源）"
        assert F.get_registered_producer() is producer, "get_registered_producer 应读到同一 producer"
        F.assert_listing_producer_ready()  # 注册后不再红灯
        # producer 真跑 → 返回校验过的 listing 行（经真实 page→rows 路径）
        out_rows = list(producer(TENANT))
        assert len(out_rows) == len(LISTING_ROWS), f"注册 producer 产出行数异常: {len(out_rows)}"
        for r in out_rows:
            assert C.row_problems(C.LISTINGS, r) == [], f"producer 产出行不合契约: {r}"
        print("✓ register → contract LISTINGS 注册表单一来源；未注册明确红灯；不混进 KINDS")

        # ── 6. 登录态失效 blocked：page_factory 抛 blocked → 上抛红灯，不返回空目录冒充成功 ──
        from hipop.server import _platform_browser as pb

        def _login_blocked(tenant_id):
            raise pb.PlatformBrowserError(
                "平台 noon 未登录：缺会话 cookie _npsid。请参照 refresh-dbuyerp-token "
                "流程在本机紫鸟重登该店一次", blocked=True)

        blocked_producer = F.make_live_row_producer(page_factory=_login_blocked)
        # 登录失效原样上抛 PlatformBrowserError(blocked=True)（同订单/库存抓取器，不吞成空目录）。
        raised = False
        try:
            list(blocked_producer(TENANT))
        except pb.PlatformBrowserError as e:
            raised = True
            assert getattr(e, "blocked", False), f"登录失效应是 blocked 信号: {e}"
            assert "refresh-dbuyerp-token" in str(e), f"应带人工登录提示: {e}"
        assert raised, "登录失效必须 blocked 上抛（不得返回空目录冒充成功）"
        print("✓ 登录态失效 → blocked 上抛（带人工登录提示），不返回空目录冒充成功")

        # ── 7. 307 映射预检：direct / 经绑定可映 / noon-only 无绑定 gap 三类计数 + 覆盖率 ──
        #   构造 KSA listing：
        #     L1 带 partner_sku=PSA001            → direct
        #     L2 仅 noon_sku=ZSA888（wf2_sku 有绑定）→ 经绑定可映
        #     L3 仅 noon_sku=ZSA999（wf2_sku 无绑定）→ gap（无法映射回 partner_sku）
        #     L4 国别=AE                            → 不计入 KSA 预检
        ksa1 = {"country_code": "SA", "noon_sku": "ZSA001", "partner_sku": "PSA001",
                "listing_status": "active"}
        ksa2 = {"country_code": "SA", "noon_sku": "ZSA888", "listing_status": "active"}
        ksa3 = {"country_code": "SA", "noon_sku": "ZSA999", "listing_status": "active"}
        uae = {"country_code": "AE", "noon_sku": "ZAE001", "partner_sku": "PAE001",
               "listing_status": "active"}
        all_rows = [ksa1, ksa2, ksa3, uae]
        # wf2_sku 绑定索引（noon_sku → partner_sku）：仅 ZSA888 有绑定，ZSA999 缺。
        sku_index = {"ZSA888": "PKSA888"}

        rep = F.mapping_precheck(all_rows, sku_index=sku_index, country_code="SA")
        assert rep["country_code"] == "SA", rep
        assert rep["total"] == 3, f"KSA listing 应 3 行（AE 不计）: {rep}"
        assert rep["direct_partner_sku"] == 1, f"direct 应 1: {rep}"
        assert rep["mapped_via_binding"] == 1, f"经绑定可映应 1: {rep}"
        assert rep["unmapped"] == 1, f"无绑定 gap 应 1: {rep}"
        assert rep["mappable"] == 2, f"可映射(direct+binding)应 2: {rep}"
        # gap 行必须被点名（platform sku），不静默吞
        assert "ZSA999" in rep["gap_platform_skus"], f"gap 行未点名: {rep}"
        assert "ZSA888" not in rep["gap_platform_skus"], f"可映行误判为 gap: {rep}"
        # 覆盖率 = 2/3
        assert abs(rep["coverage_pct"] - (2 / 3 * 100)) < 1e-6, f"覆盖率算错: {rep}"
        # 死法守门：把无绑定 noon-only 算成可映射就会让 unmapped=0 —— 这里钉死 gap≠0。
        assert rep["unmapped"] >= 1, "noon-only 无绑定必须计 gap，绝不静默塞进可映射"
        print(f"✓ 307 映射预检：total=3 direct=1 binding=1 gap=1（ZSA999 点名），覆盖率={rep['coverage_pct']:.1f}%")

        # ── 8. store_key 接线（首审打回返工）：默认 fetch path 必须把 store_key 透传 ──
        #   bug：fetch_listing_rows(store_key='custom-noon') 默认路径调 `_fetch_raw_listings(page)`
        #   丢了 store_key，下游 _listings_cfg 退化成默认 'noon'，多 noon 平台配置时读错 listing
        #   配置。这里 spy `_fetch_raw_listings` 钉死：默认路径（不注入 raw_listings_fn）必须把
        #   同一个 store_key 落到 _fetch_raw_listings；注入 raw_listings_fn 的测试路径仍兼容（§2/§5）。
        captured: list = []
        _orig_fetch = F._fetch_raw_listings

        def _spy_fetch(page, *, store_key=F.DEFAULT_STORE_KEY):
            captured.append(store_key)
            return list(RAW_RECORDS)

        F._fetch_raw_listings = _spy_fetch
        try:
            # 8a. fetch_listing_rows 默认路径 → store_key 原样透传
            captured.clear()
            F.fetch_listing_rows(TENANT, store_key="custom-noon", page=FakePage(RAW_RECORDS))
            assert captured == ["custom-noon"], \
                f"fetch_listing_rows 默认路径丢了 store_key: {captured}（多平台会读错 listing 配置）"
            # 8b. make_live_row_producer → store_key 落到 _fetch_raw_listings
            captured.clear()
            prod = F.make_live_row_producer(
                store_key="custom-noon", page_factory=lambda t: FakePage(RAW_RECORDS))
            list(prod(TENANT))
            assert captured == ["custom-noon"], \
                f"make_live_row_producer 丢了 store_key: {captured}"
            # 8c. register_live_producer 注册的 producer → store_key 同样落到 _fetch_raw_listings
            captured.clear()
            reg = F.register_live_producer(
                store_key="custom-noon", page_factory=lambda t: FakePage(RAW_RECORDS))
            list(reg(TENANT))
            assert captured == ["custom-noon"], \
                f"register_live_producer 丢了 store_key: {captured}"
        finally:
            F._fetch_raw_listings = _orig_fetch
            F.unregister_live_producer()
        # 注入 raw_listings_fn 的替身路径仍兼容（spy 还原后，default-page 注入 fn 不碰 _fetch_raw_listings）
        rows_inj = F.fetch_listing_rows(TENANT, store_key="custom-noon", page=object(),
                                        raw_listings_fn=lambda p: RAW_RECORDS)
        assert len(rows_inj) == len(LISTING_ROWS), "注入 raw_listings_fn 路径应仍兼容（不受 store_key 接线影响）"
        print("✓ store_key 接线：默认 fetch path 把 store_key 透传到 _fetch_raw_listings（多平台不读错配置）；注入路径兼容")

        print("\n8/8 passed")
        return 0
    finally:
        F.unregister_live_producer()
        C.set_live_row_producer(C.LISTINGS, None)


if __name__ == "__main__":
    sys.exit(main())

"""noon_listing_fetcher — noon 在售/在架 listing 目录实时抓取器（page → rows，WS-183/S2 / WS-188）。

平台 fetcher 第三类（订单 noon_order_fetcher / 库存 noon_stock_fetcher 之后，复用同一样板）。
职责单一：把 WS-41 `get_platform_session(tenant_id,"noon")` 拿到的**真实已登录 page** 抓出
noon 后台当前在售/在架 listing 目录，映射成 WS-183/S1 listing 行契约
（`noon_live_contract.ROW_CONTRACT[LISTINGS]`），经 `register_live_producer` 注册进
**contract 的 LISTINGS 注册表**，供 listing ingest / is_listed 对账下游按需取用。

为什么直接注册 contract（而非像订单/库存那样经 ingest 模块）：listings **不在 KINDS**
（refresh_all_v2 收口的三类 orders/my_inventory/asn），WS-183/S1 明确「listings 不接
refresh_all_v2」。本任务只交付「抓取器 + 注册 + 映射预检」，**不写** is_listed 落库口径、
refresh_all 编排、schema。故注册落在 contract 的通用 LISTINGS 注册表
（`set/get_live_row_producer(LISTINGS, fn)`），下游（WS-183 后续步骤）从同一处取 producer。

边界（本条只交付抓取器 + 注册 + 映射预检 + smoke）：
  - 读：`get_platform_session`（WS-41）、noon catalog/listing 后台页或接口、WS-183/S1 行契约
    与 fixture、wf2_sku 绑定（noon_sku → partner_sku，经 sales_entity_v2.noon_sku_map，仅
    映射预检只读用）。
  - 写：本模块 + 其 smoke；调用 contract `set_live_row_producer(LISTINGS, fn)` 注册 producer。
  - 不写：`is_listed` 落库口径、`refresh_all_v2` 编排、目标表 schema、分析工作流、
    prompt/skill、真实凭据。

确定性 blocked / 字段缺失规则（全在代码 + smoke，绝不进 prompt）：
  · 登录态失效 / 缺会话  → `get_platform_session` raise PlatformBrowserError(blocked)，
    本模块原样上抛（reason 含 refresh-dbuyerp-token 式人工登录提示），绝不 stub 旧/空目录。
  · 关键字段缺失（缺 country_code/listing_status，或缺 SKU 主键来源）→ `validate_row` 红灯
    LiveSourceUnavailable，绝不补默认在售状态 / 编 SKU。
  · 页面/接口结构变（取数返回非预期结构、records 非 list）→ 红灯 LiveSourceUnavailable，
    不静默吞行、不回落旧值冒充成功，更不返回空目录冒充“全部下架”。
  · 缺 noon listing 接口配置 / api_url 仍是未注入的 env 占位符（${...}）→ blocked
    （绝不猜 URL / 拿占位符当真实地址；首次 live 由运营核对真实接口后经
    NOON_LISTINGS_API_URL 注入）。

307 映射预检（WS-188 验收 #4，`mapping_precheck`）：noon listing 行常只带平台 SKU
（noon_sku），不带 partner/seller SKU。直接用 partner_sku 当主键会丢掉那约 307 条
「在 noon 在售、但 ERP 未回填 noon_sku 绑定」的商品。预检对 KSA listing 逐行判定：
  · 行自带 partner_sku            → direct（可直接回业务 SKU）
  · 仅平台 SKU 但 wf2_sku 有绑定   → mapped_via_binding（经 noon_sku→partner_sku 回映）
  · 仅平台 SKU 且 wf2_sku 无绑定   → unmapped（mapping gap，逐条点名平台 SKU）
能映射回业务 SKU 才允许说可打通补货/库存；不能映射的计入 gap，**绝不静默塞进下游**。

「真 page→真 rows」的唯一外部边界是 `_fetch_raw_listings(page)`（page 侧取数）；其余
映射/校验/注册/预检/blocked 规则都是纯函数，可被 smoke 注入替身确定性回归（与
`smoke_platform_session` 替身 page 同形）。
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

# 行契约 + producer 注册表的唯一来源（WS-183/S1）。listing 对账下游也引用同一模块，故
# `set_live_row_producer(LISTINGS, fn)` 写读同一处（单一来源，详见 noon_live_contract docstring）。
import noon_live_contract as _contract  # noqa: E402

# 对外沿用 contract 的失败信号类，整链 except 同一类型（不另起一份）。
LiveSourceUnavailable = _contract.LiveSourceUnavailable

DEFAULT_STORE_KEY = "noon"


# ── 配置 ────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    """读 hipop.json（dual import path：scripts 同级 / 包路径都可）。"""
    try:
        from _config import load_config  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover - 包路径回落
        from hipop.scripts._config import load_config  # type: ignore
    return load_config()


def _listings_cfg(store_key: str = DEFAULT_STORE_KEY) -> dict:
    """noon listing 取数配置：platform_browser.platforms.<平台>.listings。

    平台无关：按 store_key 平台名子串命中（noon / NOON-SA 都落 noon），与
    `_platform_browser._platform_cfg_for` 同口径。缺 listings 配置 → 空 dict，由
    `_fetch_raw_listings` 据此 blocked（不在这里猜默认接口）。
    """
    platforms = (_load_config().get("platform_browser") or {}).get("platforms") or {}
    k = str(store_key).lower()
    for name, pc in platforms.items():
        if name.lower() in k or k in name.lower():
            return pc.get("listings") or {}
    if len(platforms) == 1:
        return (next(iter(platforms.values())).get("listings") or {})
    return {}


# ── 字段映射（唯一需要的人工映射事实，钉在代码里、可被 smoke 回归）────────────
# noon catalog/listing 后台/接口字段名 → WS-183/S1 listing 行契约键。契约键全集（=本 map
# 键集）⊆ noon_live_contract.ROW_CONTRACT[LISTINGS]['known']，故映射出的 row 不会带契约外
# 字段。真 noon 接口字段命名（snake / camel / 别名）首次 live 由运营核对后在此收口——映射
# 命中不到的必填字段会被 validate_row 红灯，绝不默认补在售状态 / 编 SKU。
_NOON_LISTING_FIELD_MAP: dict[str, tuple] = {
    "country_code":   ("country_code", "countryCode", "country",
                       "dest_country", "destCountry"),
    "store_name":     ("store_name", "storeName", "store", "shop_name", "shopName"),
    "noon_sku":       ("noon_sku", "noonSku", "noon_sku_code", "noonSkuCode",
                       "psku", "pSku"),
    "partner_sku":    ("partner_sku", "partnerSku", "seller_sku", "sellerSku"),
    "sku":            ("sku", "sku_code", "skuCode"),
    "listing_status": ("listing_status", "listingStatus", "status", "offer_status",
                       "offerStatus", "state", "catalog_status", "catalogStatus"),
    "is_listed":      ("is_listed", "isListed", "listed", "is_active", "isActive"),
    "title":          ("title", "name", "product_title", "productTitle",
                       "item_title", "itemTitle"),
}

# 自检：映射键 ⊆ 契约 known（防漂出契约外字段，红队点）。
assert set(_NOON_LISTING_FIELD_MAP) <= set(
    _contract.ROW_CONTRACT[_contract.LISTINGS]["known"]), \
    "noon listing 字段映射键超出 WS-183/S1 契约 known 集（会被 validate_row 当契约外字段红灯）"


def _pick(raw: dict, candidates: tuple):
    """取 raw 里首个非空候选字段值（None / 空串视为缺；数值 0 是真实值，保留）。"""
    for c in candidates:
        v = raw.get(c)
        if v is not None and not (isinstance(v, str) and v.strip() == ""):
            return v
    return None


def to_contract_row(raw: dict) -> dict:
    """单条 noon listing 原始记录 → WS-183/S1 listing 行契约 dict。

    只产出**命中到值**的契约键：缺失的必填（country_code/listing_status）字段不补默认值
    （尤其绝不把缺失在售状态补成 active），交由 `validate_row` 红灯。产出键 ⊆ 契约 known，
    故不会带契约外字段。
    """
    if not isinstance(raw, dict):
        raise LiveSourceUnavailable(
            f"noon listing 原始记录非 dict（得到 {type(raw).__name__}）—— 疑似接口改版，blocked")
    row: dict = {}
    for key, candidates in _NOON_LISTING_FIELD_MAP.items():
        v = _pick(raw, candidates)
        if v is not None:
            row[key] = v
    return row


# ── 真 page → 真 rows 的唯一外部边界（smoke 注入替身）────────────────────────
# page 侧取数：在已登录 page 上同源 fetch noon catalog/listing 接口，拿回 JSON。fetch 带
# credentials='include' 复用紫鸟接管会话的 cookie；非 2xx / 解析失败都当登录态失效或接口
# 改版 → blocked。catalog 落地页在 noon.partners 域，若 api_url 同域则可直接 fetch；本函数
# 是本模块唯一碰真实 noon 页面/接口的地方（smoke 用 raw_listings_fn 注入替身绕过）。
_LISTING_FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}
"""


def _walk_records(raw, records_path: Optional[list]):
    """从接口 JSON 走到 listing 记录 list。

    records_path 给定 → 按键逐层走；否则 raw 本身是 list 直接用，或在常见容器键里找第一个
    list。走不到 list 交由调用方红灯（不强行造 list）。
    """
    if records_path:
        cur = raw
        for k in records_path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("hits", "data", "listings", "catalog", "products", "results",
                  "rows", "items", "records"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("listings", "catalog", "products", "rows", "items",
                           "records", "list", "hits"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _fetch_raw_listings(page, *, store_key: str = DEFAULT_STORE_KEY) -> list:
    """在真实已登录 page 上抓 noon listing 原始记录 list（唯一外部边界）。

    缺接口配置 / api_url 仍是 env 占位符 → blocked（绝不猜 URL、绝不拿 ${...} 当真实地址）。
    fetch 失败 / 返回结构非预期 → blocked（疑似登录态失效或接口改版，不静默吞、不回落旧值、
    不返回空目录冒充“全部下架”）。
    """
    cfg = _listings_cfg(store_key)
    api_url = (cfg.get("api_url") or "").strip()
    if not api_url:
        raise LiveSourceUnavailable(
            "缺 noon listing 接口配置 platform_browser.platforms.noon.listings.api_url"
            "（首次 live 由运营核对真实 catalog/listing 接口后配置）—— blocked，绝不猜 URL/编数")
    if api_url.startswith("${"):
        raise LiveSourceUnavailable(
            f"noon listing api_url 仍是未注入的 env 占位符 {api_url!r} —— blocked，"
            "请运营经 NOON_LISTINGS_API_URL 注入真实接口后再 live（绝不拿占位符当真实地址）")
    try:
        raw = page.evaluate(_LISTING_FETCH_JS, api_url)
    except Exception as e:  # noqa: BLE001 — page 侧各类取数错都归 blocked
        raise LiveSourceUnavailable(
            f"noon listing 页取数失败（page.evaluate fetch {api_url}）: "
            f"{type(e).__name__}: {e} —— 疑似登录态失效/接口改版，blocked，"
            "请参照 refresh-dbuyerp-token 流程在本机紫鸟重登该店一次") from e
    records = _walk_records(raw, cfg.get("records_path"))
    if not isinstance(records, list):
        raise LiveSourceUnavailable(
            f"noon listing 接口返回结构非预期（期望 records 为 list，得 "
            f"{type(records).__name__}）—— 疑似页面/接口改版，blocked，不静默吞行/不返回空目录")
    return records


# ── session 获取（lazy import _platform_browser，避免无 playwright 环境 import 失败）──
def _get_session(tenant_id, store_key: str, account: Optional[str]):
    from hipop.server import _platform_browser as pb
    return pb.get_platform_session(tenant_id, store_key, account=account)


# ── 抓取器主入口 + producer 工厂 + 注册 ──────────────────────────────────
def fetch_listing_rows(tenant_id, *, store_key: str = DEFAULT_STORE_KEY,
                       account: Optional[str] = None, page=None,
                       raw_listings_fn: Optional[Callable] = None) -> list:
    """抓取器主入口：真实已登录 page → 校验过的 WS-183/S1 listing 行 list。

    page=None（生产）→ `get_platform_session(tenant_id, store_key)` 拿真实 page（登录态
    失效则上抛 blocked）；smoke 注入 page / raw_listings_fn 做确定性替身（同
    smoke_platform_session）。每行经 WS-183/S1 `validate_row` 守门：缺必填（country_code/
    listing_status）/ 缺 SKU 主键 / 契约外字段 → 红灯 LiveSourceUnavailable，绝不补默认在售
    状态 / 编 SKU。输出绑定真实 page（不接受预造 rows）。
    """
    if page is None:
        page = _get_session(tenant_id, store_key, account)
    # 默认路径必须把 store_key 透传到 _fetch_raw_listings → _listings_cfg，否则多 noon 平台
    # 配置时 store_key 退化成默认 'noon'、读错 listing 配置（首审打回的接线缺失）。注入
    # raw_listings_fn 的 smoke 替身路径只收 page，保持兼容（不强加 store_key 关键字）。
    if raw_listings_fn is not None:
        raw_records = list(raw_listings_fn(page))
    else:
        raw_records = list(_fetch_raw_listings(page, store_key=store_key))
    rows = []
    for raw in raw_records:
        row = to_contract_row(raw)
        _contract.validate_row(_contract.LISTINGS, row)  # 缺字段/契约外字段红灯，不编数
        rows.append(row)
    return rows


def make_live_row_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_listings_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """造 `fn(tenant_id) -> Iterable[dict]` live row producer（喂 listing 对账下游）。

    page_factory(tenant_id) -> page：缺省经 `get_platform_session` 取真实 page；smoke 注入
    替身 page（无需真紫鸟）。登录失效 / 字段缺 / 接口改版均上抛红灯，下游据此报 blocked，
    绝不返回空目录冒充成功。
    """
    def producer(tenant_id):
        page = page_factory(tenant_id) if page_factory is not None \
            else _get_session(tenant_id, store_key, account)
        return fetch_listing_rows(tenant_id, store_key=store_key, account=account,
                                  page=page, raw_listings_fn=raw_listings_fn)
    return producer


def register_live_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_listings_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """把 noon listing live producer 注册进 contract 的 LISTINGS 注册表。

    listings 不在 KINDS（不接 refresh_all_v2），故直接注册 contract 通用注册表（非经 ingest
    模块）。注册后 WS-183 后续步骤可经 `noon_live_contract.get_live_row_producer(LISTINGS)`
    取到本 producer 走真实 live 目录。返回注册的 producer（便于测试断言注册表单一来源）。
    """
    producer = make_live_row_producer(
        store_key=store_key, account=account,
        page_factory=page_factory, raw_listings_fn=raw_listings_fn)
    _contract.set_live_row_producer(_contract.LISTINGS, producer)
    return producer


def unregister_live_producer() -> None:
    """清除 listings live producer（回到未注册状态）。"""
    _contract.set_live_row_producer(_contract.LISTINGS, None)


def get_registered_producer() -> Optional[Callable[[int], Iterable[dict]]]:
    """读 contract LISTINGS 注册表当前 producer（未注册 → None）。"""
    return _contract.get_live_row_producer(_contract.LISTINGS)


def assert_listing_producer_ready() -> None:
    """listing live producer 必须已注册，否则明确红灯 LiveSourceUnavailable。

    下游 listing ingest / 对账用它判定「是否真有 listing 实时来源」：未注册 →
    raise，报 blocked，**绝不静默当“没有 listing / 全部下架”**。
    """
    if get_registered_producer() is None:
        raise LiveSourceUnavailable(
            "noon listing live producer 未注册 —— 无实时 listing 来源，报 blocked，"
            "绝不静默当‘没有 listing / 全部下架’、不编数")


# ── 307 映射预检（WS-188 验收 #4；只读 wf2_sku 绑定，不落库、不改 is_listed 口径）────
def _resolve_partner_sku(row: dict, sku_index: dict):
    """单条 listing 行 → (partner_sku, 来源)。与 ingest `_resolve_partner_sku` 同口径：

    优先行自带 partner_sku（direct）；否则平台 SKU（noon_sku/sku）经 wf2_sku 绑定回映
    （binding）；都回不到 → (None, 'gap')。绝不补默认 partner_sku 凑数。
    """
    psk = (str(row.get("partner_sku") or "")).strip()
    if psk:
        return psk, "direct"
    plat = (str(row.get("noon_sku") or row.get("sku") or "")).strip()
    if plat:
        mapped = sku_index.get(plat)
        if mapped:
            return mapped, "binding"
        return None, "gap"
    return None, "gap"


def mapping_precheck(rows, *, sku_index: Optional[dict] = None,
                     country_code: str = "SA") -> dict:
    """307 映射预检：对指定国别 listing 行逐条判定能否回映业务 SKU（partner_sku）。

    rows       : listing 行（contract 形态 dict）。
    sku_index  : wf2_sku 绑定 {平台 SKU(noon_sku): partner_sku}（live 经
                 sales_entity_v2.noon_sku_map 取；smoke 注入）。None → 视为无绑定（保守）。
    country_code: 只统计该国别（KSA=SA）；其它国别行不计入。

    返回逐类计数 + 覆盖率 + gap 行点名（平台 SKU），供 Luke/下游判断「是否覆盖那约 307 条」。
    direct + mapped_via_binding = mappable（可回业务 SKU）；unmapped = mapping gap，
    逐条点名、绝不静默塞进下游。
    """
    sku_index = sku_index or {}
    cc = (country_code or "").strip().upper()
    direct = 0
    binding = 0
    gap_platform_skus: list = []
    for row in rows:
        rcc = (str(row.get("country_code") or "")).strip().upper()
        if cc and rcc != cc:
            continue
        _, src = _resolve_partner_sku(row, sku_index)
        if src == "direct":
            direct += 1
        elif src == "binding":
            binding += 1
        else:
            plat = (str(row.get("noon_sku") or row.get("sku")
                        or row.get("partner_sku") or "")).strip()
            gap_platform_skus.append(plat or "<无任何 SKU 键>")
    total = direct + binding + len(gap_platform_skus)
    mappable = direct + binding
    return {
        "country_code": cc,
        "total": total,
        "direct_partner_sku": direct,
        "mapped_via_binding": binding,
        "unmapped": len(gap_platform_skus),
        "mappable": mappable,
        "coverage_pct": (mappable / total * 100.0) if total else 0.0,
        "gap_platform_skus": gap_platform_skus,
    }


def _live_sku_index(tenant_id) -> dict:
    """live 路径：从 wf2_sku 取 {noon_sku: partner_sku} 绑定索引（只读，用于映射预检）。"""
    try:
        from sales_entity_v2 import noon_sku_map  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover - 包路径回落
        from hipop.scripts.sales_entity_v2 import noon_sku_map  # type: ignore
    return noon_sku_map(tenant_id)


if __name__ == "__main__":  # pragma: no cover - 真 live 手动入口
    import argparse
    import json
    from server import data as _data

    ap = argparse.ArgumentParser(
        description="noon listing 在售目录实时抓取器（真紫鸟 live 手动跑）+ 307 映射预检")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    ap.add_argument("--account", default=None)
    ap.add_argument("--country", default="SA", help="映射预检国别（KSA=SA）")
    ap.add_argument("--precheck", action="store_true",
                    help="抓完后跑 307 映射预检（对 wf2_sku 绑定核覆盖率）")
    args = ap.parse_args()

    _data.set_current_tenant(args.tenant)
    rows = fetch_listing_rows(args.tenant, store_key=args.store_key, account=args.account)
    print(f"[noon_listing_fetcher] tenant={args.tenant} 抓到 {len(rows)} 行 listing（已过契约校验）")
    for r in rows[:10]:
        print("  ", r)
    if args.precheck:
        idx = _live_sku_index(args.tenant)
        rep = mapping_precheck(rows, sku_index=idx, country_code=args.country)
        print(f"\n[307 映射预检] country={rep['country_code']}")
        print(json.dumps({k: v for k, v in rep.items() if k != "gap_platform_skus"},
                         ensure_ascii=False, indent=2))
        print(f"mapping gap（无法回映 partner_sku 的平台 SKU，共 {rep['unmapped']} 条）:")
        for s in rep["gap_platform_skus"][:50]:
            print("   ", s)

"""noon_asn_scraper — noon KSA **ASN/送仓**实时抓取器（page → rows，WS-N2.3 / WS-60）。

与 noon 订单抓取器（WS-58 / noon_order_fetcher）同套打法：把 WS-41
`get_platform_session(tenant_id,"noon")` 拿到的**真实已登录 page** 抓出 noon 送仓行，
映射成 WS-34 ASN 行契约（`noon_live_contract.ROW_CONTRACT[ASN]`），经 ASN ingest 的
`ingest_inbound_staging_v2.set_live_row_producer` 注册（WS-37 socket），喂同一
`_aggregate`/`_upsert` 落 `wf1_asn_lines_staging`。只保证实时行进 staging，**不算**
`pending_inbound_qty`（WS-11）、不碰目标表 schema / 分析工作流 / prompt / 凭据。

KSA / store→entity 绑定（修 WS-60 门2 打回点）
----------------------------------------------
门2 复验在 store→entity 处 blocked：紫鸟唯一 store `44158-HIPOP-NOON-AE/SA` 名字同时
命中 AE+SA，旧版在抓取器里用 `resolve_store_entity` 反解国别 → 歧义 blocked。**本版与订单
抓取器一致：抓取器不反解 store→entity**，KSA 隔离靠 **noon 送仓页 URL 的 locale**
（`en-sa`=KSA / `en-ae`=UAE，实测 `_svc/sc-fbn/api/v1/partner/countries` 返回
`["SA","AE"]`）+ 配置 `country_code=SA`。每行带 `country_code`，路由/平台 SKU→partner_sku
映射都交给 socket 的 `_aggregate`（`_route_entity` 按 country_code → `get_entity_by_country`，
`_resolve_partner_sku` 经 `noon_sku_map`）——单一来源、不在抓取器另造一套映射（死法②由
socket 的实时路径钉死，smoke 断言 staging 主键是 partner_sku、无 Z 开头平台 SKU）。

唯一外部边界 = `_fetch_raw_asn(page)`
----------------------------------------------
真 page→真 rows 只发生在 `_fetch_raw_asn(page)`：导航到配置的 noon 送仓页（locale 含国别）
→ 抓**送仓明细行**（表头驱动，按表头文案映射列）。其余映射/校验/注册/blocked 规则都是纯
函数，可被 smoke 注入替身确定性回归（与 smoke_platform_session 替身 page 同形）。

确定性 blocked / 缺字段规则（全在代码 + live smoke，不进 prompt）：
  · 登录态失效 / 缺会话 → `get_platform_session` raise PlatformBrowserError(blocked)，本模块
    转成 `LiveSourceUnavailable` 让 run_live **红灯**（不静默回落 CSV 冒充实时成功）。
  · 缺关键字段（asn_number/qty/country_code 等）→ `validate_row` 红灯，绝不编造 ASN
    number / qty / ETA。
  · 页面/接口结构变（缺关键列、抓不到送仓表）→ 红灯 LiveSourceUnavailable，不返回空行
    冒充「没有送仓」（区别于：表在、列齐、0 数据行 = 真没有在途送仓，合法空快照）。
  · 缺 noon 送仓页配置（asn_url）→ blocked（绝不瞎猜 URL/编数；真实 per-line 送仓源由运营
    /参谋长确认后填 config）。
  · `inbound_date` 是 ERP 送仓/拣货那一路的判别列（socket `_row_source` 据它分流），**本
    noon 抓取器不产出 inbound_date**，保证行落 `source=noon_asn`。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

# 行契约 + producer 注册表的唯一来源（WS-34）。ASN ingest 也引用同一模块，故
# set_live_row_producer 写读同一处（单一来源）。
import noon_live_contract as _contract  # noqa: E402

LiveSourceUnavailable = _contract.LiveSourceUnavailable  # 整链 except 同一类型
ASN = _contract.ASN

DEFAULT_STORE_KEY = "noon"

# 表格选择器（标准 HTML 表；可经 config asn.selectors 覆写）。表头驱动 → 容列序变化。
_DEFAULT_SELECTORS = {
    "table": "table",
    "header_cell": "thead th",
    "body_row": "tbody tr",
    "cell": "td",
}

# 送仓明细行的列表头文案（归一化）→ WS-34 ASN 契约键。真实 per-line 送仓源的确切列名
# 由首次非空 live 由运营核对后在此收口；映射不中的必填列会被 validate_row 红灯，绝不编数。
# 刻意不含 inbound_date（ERP 路判别列，见模块 docstring）。
_HEADER_ALIASES = {
    "asn_number": {
        "asn", "asn number", "asn no", "asn no.", "asn #", "asn编号", "asn 编号",
        "shipment", "shipment #", "shipment number", "shipment no", "送仓单号", "送仓单",
    },
    "partner_sku": {"partner sku", "partner-sku", "psku", "partner_sku", "卖家sku", "商家sku"},
    "sku": {"sku", "noon sku", "platform sku", "item sku", "sku code", "noon_sku", "平台sku"},
    "qty": {
        "qty", "quantity", "units", "unit", "scheduled qty", "asn scheduled qty",
        "expected qty", "expected quantity", "数量", "件数", "送仓数量", "预报数量",
    },
    "status": {"status", "asn status", "shipment status", "状态", "送仓状态"},
    "warehouse_code": {
        "warehouse", "warehouse code", "to warehouse", "fc", "fulfillment center",
        "wh", "仓库", "仓库编码", "目的仓",
    },
    "country_code": {"country", "country code", "国家", "国别"},
}
_REQUIRED_HEADER_FIELDS = {"asn_number", "qty"}  # 缺它们 = 抓错页/页面结构变 → blocked

# 自检：映射键 ⊆ 契约 known（防漂出契约外字段）。
assert set(_HEADER_ALIASES) <= set(_contract.ROW_CONTRACT[ASN]["known"]), \
    "noon ASN 表头映射键超出 WS-34 契约 known 集"


# ── 配置 ────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    try:
        from _config import load_config  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover
        from hipop.scripts._config import load_config  # type: ignore
    return load_config()


def _asn_cfg(store_key: str = DEFAULT_STORE_KEY) -> dict:
    """noon 送仓取数配置：platform_browser.platforms.<平台>.asn（平台名子串命中，同
    _platform_cfg_for 口径）。缺 → 空 dict，由 _fetch_raw_asn 据此 blocked。"""
    platforms = (_load_config().get("platform_browser") or {}).get("platforms") or {}
    k = str(store_key).lower()
    for name, pc in platforms.items():
        if name.lower() in k or k in name.lower():
            return pc.get("asn") or {}
    if len(platforms) == 1:
        return next(iter(platforms.values())).get("asn") or {}
    return {}


def asn_url_configured(store_key: str = DEFAULT_STORE_KEY) -> bool:
    """真实 per-line 送仓源是否已配置（asn.asn_url 非空）。生产自动接线（live_producers）
    据此判定：未配置则不接线、保留 CSV interim 回落，绝不让缺配置红灯破坏现有路径。"""
    return bool((_asn_cfg(store_key).get("asn_url") or "").strip())


def _selectors(cfg: dict) -> dict:
    sel = dict(_DEFAULT_SELECTORS)
    ov = cfg.get("selectors") or {}
    if isinstance(ov, dict):
        sel.update({k: v for k, v in ov.items() if k in sel and v})
    return sel


# ── 字段映射（纯函数，smoke 可回归）────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _match_header(label: str) -> Optional[str]:
    h = _norm(label)
    if not h:
        return None
    for field, aliases in _HEADER_ALIASES.items():
        if h in aliases:
            return field
    return None


def _clean(v):
    s = "" if v is None else str(v).strip()
    return s or None


def to_contract_row(raw: dict, country_code: str) -> dict:
    """单条送仓明细原始行 → WS-34 ASN 行契约 dict。

    只产出命中到值的契约键；缺失的 asn_number/qty 留空（不编号/编 0），交由 validate_row
    红灯。country_code 来自配置/locale（KSA=SA），单店单国注入；若页面自带 country 列则以
    页面值优先。不含 inbound_date（保 source=noon_asn）。产出键 ⊆ 契约 known。
    """
    if not isinstance(raw, dict):
        raise LiveSourceUnavailable(
            f"noon 送仓原始记录非 dict（得到 {type(raw).__name__}）—— 疑似页面/接口改版，blocked")
    row = {
        "asn_number": _clean(raw.get("asn_number")) or "",
        "qty": raw["qty"] if raw.get("qty") not in (None,) else "",  # 原值透传，不编数
        "country_code": _clean(raw.get("country_code")) or country_code,
    }
    plat = _clean(raw.get("sku"))
    if plat:
        row["sku"] = plat
    partner = _clean(raw.get("partner_sku"))
    if partner:
        row["partner_sku"] = partner
    for opt in ("status", "warehouse_code"):
        v = _clean(raw.get(opt))
        if v is not None:
            row[opt] = v
    return row


# ── 真 page → 真 rows 的唯一外部边界（smoke 注入替身）────────────────────────
def parse_asn_table(page, selectors: dict) -> list:
    """从 noon 送仓页 page 抓送仓明细行（表头驱动）。返回原始 dict 行 list（键 = 契约字段）。

    选第一张能认出关键列（asn_number + qty）的表；找不到 → blocked（抓错页 / 页面结构变 /
    配置的页面非 per-line 送仓源），绝不返回空行冒充「没有送仓」。表在、列齐、0 数据行
    = 真没有在途送仓（合法空快照）→ 返回 []。
    """
    tables = list(page.query_selector_all(selectors["table"]) or [])
    if not tables:
        raise LiveSourceUnavailable(
            "noon 送仓页未找到任何表格（页面结构变 / 抓错页 / 未加载）—— blocked，不编数")
    for table in tables:
        headers = list(table.query_selector_all(selectors["header_cell"]) or [])
        field_by_idx = {}
        for i, th in enumerate(headers):
            f = _match_header(th.inner_text())
            if f and f not in field_by_idx.values():
                field_by_idx[i] = f
        if not _REQUIRED_HEADER_FIELDS.issubset(set(field_by_idx.values())):
            continue
        rows = []
        for tr in (table.query_selector_all(selectors["body_row"]) or []):
            cells = list(tr.query_selector_all(selectors["cell"]) or [])
            if not cells:
                continue
            raw = {field_by_idx[i]: (cells[i].inner_text() or "").strip()
                   for i in field_by_idx if i < len(cells)}
            rows.append(raw)
        return rows
    raise LiveSourceUnavailable(
        "noon 送仓页表头缺关键列（需至少认出 asn_number + qty）—— 配置的页面非 per-line 送仓"
        "明细源 / 页面结构变，blocked，绝不抓错列编数。")


def _goto_asn_page(page, url: str) -> None:
    """导航到 noon 送仓页（locale 含国别）。冷 goto 被紫鸟扩展 abort 时重试一次；连续失败
    → blocked（不在错误页上瞎抓）。"""
    last = None
    for _ in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise LiveSourceUnavailable(
        f"导航 noon 送仓页 {url} 失败: {type(last).__name__}: {last}"
        " —— 疑似登录态失效/紫鸟扩展拦截，blocked")


def _fetch_raw_asn(page, *, store_key: str = DEFAULT_STORE_KEY) -> list:
    """在真实已登录 page 上抓 noon 送仓明细原始行 list（唯一外部边界）。

    缺 asn_url 配置 → blocked（绝不瞎猜 per-line 源 URL）。导到送仓页（locale 含国别，KSA=
    en-sa）→ 表头驱动抓送仓明细表。HTTP/页面结构异常 → parse_asn_table 红灯。
    """
    cfg = _asn_cfg(store_key)
    url = (cfg.get("asn_url") or "").strip()
    if not url:
        raise LiveSourceUnavailable(
            "缺 noon 送仓页配置 platform_browser.platforms.noon.asn.asn_url（per-line 送仓明细"
            "源，KSA locale=en-sa）—— blocked，绝不瞎猜 URL/编数；待运营/参谋长确认真实 per-line"
            "源后填 config。已探明的 FBN 送仓入口：https://fbn.noon.partners/en-sa/asn?mp=noon"
            "（该列表页为 shipment 级，无 per-SKU/qty；per-line 明细需 shipment 详情/ASN GRN 报表）。")
    _goto_asn_page(page, url)
    return parse_asn_table(page, _selectors(cfg))


# ── session 获取（lazy import _platform_browser）──────────────────────────
def _get_session(tenant_id, store_key: str, account: Optional[str]):
    from hipop.server import _platform_browser as pb
    try:
        return pb.get_platform_session(tenant_id, store_key, account=account)
    except pb.PlatformBrowserError as e:
        # 登录失效/缺会话/缺紫鸟 → 转 LiveSourceUnavailable 让 run_live 红灯（不回落 CSV 掩盖）。
        raise LiveSourceUnavailable(f"平台会话不可用（blocked）：{e}") from e


# ── producer 工厂 + 注册（与 noon_order_fetcher 同形）──────────────────────
def fetch_asn_rows(tenant_id, *, store_key: str = DEFAULT_STORE_KEY,
                   account: Optional[str] = None, page=None,
                   raw_asn_fn: Optional[Callable] = None) -> list:
    """抓取器主入口：真实已登录 page → 校验过的 WS-34 ASN 行 list。

    page=None（生产）→ get_platform_session 拿真实 page；smoke 注入 page / raw_asn_fn 做
    确定性替身。country_code 来自配置（KSA=SA），路由/SKU 映射交给 socket 的 _aggregate。
    每行经 validate_row 守门：缺必填/契约外字段 → 红灯，绝不编数。
    """
    cfg = _asn_cfg(store_key)
    country = (cfg.get("country_code") or "").strip()
    if not country:
        raise LiveSourceUnavailable(
            "缺 noon 送仓国别配置 platform_browser.platforms.noon.asn.country_code"
            "（KSA=SA）—— blocked，绝不默认国别")
    if page is None:
        page = _get_session(tenant_id, store_key, account)
    fetch = raw_asn_fn or _fetch_raw_asn
    raw_records = list(fetch(page))
    rows = []
    for raw in raw_records:
        row = to_contract_row(raw, country)
        _contract.validate_row(ASN, row)  # 缺字段/契约外字段红灯，不编数
        rows.append(row)
    return rows


def make_live_row_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_asn_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """造 fn(tenant_id) -> Iterable[dict] live row producer（喂 ASN ingest run_live）。"""
    def producer(tenant_id):
        page = page_factory(tenant_id) if page_factory is not None \
            else _get_session(tenant_id, store_key, account)
        return fetch_asn_rows(tenant_id, store_key=store_key, account=account,
                              page=page, raw_asn_fn=raw_asn_fn)
    return producer


def _ingest_module():
    """ASN ingest 模块（dual import path）。set_live_row_producer 写 contract 的 ASN
    注册表 —— 与本模块校验用的 _contract 同一处（单一来源）。"""
    try:
        import ingest_inbound_staging_v2 as ingest  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover
        from hipop.scripts import ingest_inbound_staging_v2 as ingest  # type: ignore
    return ingest


def register_live_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_asn_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """把 noon 送仓 live producer 注册进 ASN ingest（= contract 的 ASN 注册表）。

    注册后 `ingest_inbound_staging_v2.run_live(tenant_id)` 不再因「无 producer」回落 CSV，
    而是走真实 live 行 → 同一 _aggregate/_upsert（WS-37）落 wf1_asn_lines_staging。返回注册
    的 producer（便于测试断言单一来源）。
    """
    producer = make_live_row_producer(
        store_key=store_key, account=account,
        page_factory=page_factory, raw_asn_fn=raw_asn_fn)
    _ingest_module().set_live_row_producer(producer)
    return producer


def unregister_live_producer() -> None:
    """清除 ASN live producer（回到 run_live 回落 CSV 的状态）。"""
    _ingest_module().set_live_row_producer(None)


if __name__ == "__main__":  # pragma: no cover - 真 live 手动入口
    import argparse
    import json
    from server import data as _data

    ap = argparse.ArgumentParser(description="noon 送仓/ASN 实时抓取器（真紫鸟 live 手动跑）")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    ap.add_argument("--account", default=None)
    ap.add_argument("--ingest", action="store_true",
                    help="注册 producer 并真跑 ingest_inbound_staging_v2.run_live（落 staging）")
    args = ap.parse_args()
    _data.set_current_tenant(args.tenant)
    if args.ingest:
        ingest = _ingest_module()
        register_live_producer(store_key=args.store_key, account=args.account)
        try:
            res = ingest.run_live(args.tenant, allow_csv_fallback=False)
            print(json.dumps(res, ensure_ascii=False, default=str))
        finally:
            unregister_live_producer()
    else:
        rows = fetch_asn_rows(args.tenant, store_key=args.store_key, account=args.account)
        print(f"[noon_asn_scraper] tenant={args.tenant} 抓到 {len(rows)} 行送仓（已过 WS-34 校验）")
        for r in rows[:10]:
            print("  ", r)

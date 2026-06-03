"""noon_order_fetcher — noon KSA 订单实时抓取器（page → rows，WS-N2.1 / WS-58）。

平台 fetcher 第一类样板。职责单一：把 WS-41 `get_platform_session(tenant_id,"noon")`
拿到的**真实已登录 page** 抓出 noon 后台订单行，映射成 WS-34 订单行契约
（`noon_live_contract.ROW_CONTRACT[ORDERS]`），经订单 ingest 的
`ingest_noon_csv_v2.set_live_row_producer` 注册，喂同一 `_aggregate`/`_upsert`
（WS-35 socket）落 wf2_orders / wf2_sku，与 CSV 路径逐字段一致、不分叉。

边界（本条只交付抓取器 + 注册 + live smoke）：
  - 读：`get_platform_session`（WS-41）、noon 订单后台页/接口、WS-34 行契约与 fixture、
    WS-35 落的订单 ingest 注入点（`set_live_row_producer`）。
  - 写：本模块 + 其 smoke；调用 `set_live_row_producer` 注册 producer。
  - 不写：`_aggregate`/`_upsert` 落表逻辑、`refresh_all_v2`、分析工作流、prompt/skill、凭据。

确定性 blocked / 字段缺失规则（全在代码 + live smoke，绝不进 prompt）：
  · 登录态失效 / 缺会话  → `get_platform_session` raise PlatformBrowserError(blocked)，
    本模块原样上抛（reason 含 refresh-dbuyerp-token 式人工登录提示），绝不 stub 旧/空行。
  · 关键字段缺失（缺 partner_sku/item_nr 等）→ `validate_row` 红灯 LiveSourceUnavailable，
    绝不编造订单号/销量/金额/取消退货字段。
  · 页面/接口结构变（取数返回非预期结构、records 非 list）→ 红灯 LiveSourceUnavailable，
    不静默吞行、不回落旧值冒充成功。
  · 缺 noon 订单接口配置 → blocked（绝不猜 URL/编数；首次 live 由运营确认真实接口）。

「真 page→真 rows」的唯一外部边界是 `_fetch_raw_orders(page)`（page 侧取数）；其余
映射/校验/注册/blocked 规则都是纯函数，可被 smoke 注入替身确定性回归（与
`smoke_platform_session` 替身 page 同形）。
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

# 行契约 + producer 注册表的唯一来源（WS-34）。订单 ingest 也引用同一模块，故
# `set_live_row_producer` 写读同一处（单一来源，详见 noon_live_contract docstring）。
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


def _orders_cfg(store_key: str = DEFAULT_STORE_KEY) -> dict:
    """noon 订单取数配置：platform_browser.platforms.<平台>.orders。

    平台无关：按 store_key 平台名子串命中（noon / NOON-SA 都落 noon），与
    `_platform_browser._platform_cfg_for` 同口径。缺 orders 配置 → 空 dict，由
    `_fetch_raw_orders` 据此 blocked（不在这里猜默认接口）。
    """
    platforms = (_load_config().get("platform_browser") or {}).get("platforms") or {}
    k = str(store_key).lower()
    for name, pc in platforms.items():
        if name.lower() in k or k in name.lower():
            return pc.get("orders") or {}
    if len(platforms) == 1:
        return (next(iter(platforms.values())).get("orders") or {})
    return {}


# ── 字段映射（唯一需要的人工映射事实，钉在代码里、可被 smoke 回归）────────────
# noon 订单后台/接口字段名 → WS-34 订单行契约键。契约键全集（=值集合）严格等于
# noon_live_contract.ROW_CONTRACT[ORDERS]['known']，故映射出的 row 不会带契约外字段。
# 真 noon 接口字段命名（snake / camel / 别名）首次 live 由运营核对后在此收口——映射
# 命中不到的必填字段会被 validate_row 红灯，绝不默认编数。
_NOON_ORDER_FIELD_MAP: dict[str, tuple] = {
    "partner_sku":       ("partner_sku", "partnerSku", "seller_sku", "sellerSku"),
    "sku":               ("sku", "noon_sku", "noonSku"),
    "item_nr":           ("item_nr", "itemNr", "item_number", "itemNumber",
                          "id_item", "idItem"),
    "order_timestamp":   ("order_timestamp", "orderTimestamp", "ordered_at",
                          "orderedAt", "order_date", "orderDate", "createdAt"),
    "status":            ("status", "order_status", "orderStatus"),
    "fulfillment_model": ("fulfillment_model", "fulfillmentModel",
                          "fulfilment_model", "fulfilmentModel", "fulfillment"),
    "offer_price":       ("offer_price", "offerPrice", "seller_price", "sellerPrice"),
    "gmv_lcy":           ("gmv_lcy", "gmvLcy", "gmv", "customer_paid", "customerPaid"),
    "currency_code":     ("currency_code", "currencyCode", "currency"),
    "dest_country":      ("dest_country", "destCountry", "destination",
                          "destination_country", "destinationCountry", "country"),
    "family":            ("family", "family_code", "familyCode"),
    "brand_code":        ("brand_code", "brandCode", "brand"),
}

# 自检：映射键 ⊆ 契约 known（防漂出契约外字段，红队点）。
assert set(_NOON_ORDER_FIELD_MAP) <= set(
    _contract.ROW_CONTRACT[_contract.ORDERS]["known"]), \
    "noon 订单字段映射键超出 WS-34 契约 known 集（会被 validate_row 当契约外字段红灯）"


def _pick(raw: dict, candidates: tuple):
    """取 raw 里首个非空候选字段值（None / 空串视为缺）。"""
    for c in candidates:
        v = raw.get(c)
        if v is not None and not (isinstance(v, str) and v.strip() == ""):
            return v
    return None


def to_contract_row(raw: dict) -> dict:
    """单条 noon 订单原始记录 → WS-34 订单行契约 dict。

    只产出**命中到值**的契约键：缺失的必填（partner_sku/item_nr）字段不补默认值，
    交由 `validate_row` 红灯（绝不编造订单号/销量/金额）。产出键 ⊆ 契约 known，
    故不会带契约外字段。
    """
    if not isinstance(raw, dict):
        raise LiveSourceUnavailable(
            f"noon 订单原始记录非 dict（得到 {type(raw).__name__}）—— 疑似接口改版，blocked")
    row: dict = {}
    for key, candidates in _NOON_ORDER_FIELD_MAP.items():
        v = _pick(raw, candidates)
        if v is not None:
            row[key] = v
    return row


# ── 真 page → 真 rows 的唯一外部边界（smoke 注入替身）────────────────────────
# page 侧取数：在已登录 page 上**同源** POST noon Sales Dashboard 接口（订单/销售明细），
# 分页拿回 JSON {hits:[...], total}。fetch 带 credentials='include' 复用紫鸟接管会话的
# cookie；非 2xx / 结构非预期都当登录态失效或接口改版 → blocked。
# 同源约束：Sales 接口在 reports.noon.partners 域，catalog 落地页跨域 fetch 会被 CORS 拦，
# 故取数前必须先把 page 导到 report_page_url（同域）。本函数 + _goto_report_page 是本模块
# 唯一碰真实 noon 页面/接口的地方（smoke 用 raw_orders_fn 注入替身绕过）。
_ORDER_FETCH_JS = """
async ({url, body}) => {
  const r = await fetch(url, {method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
    body: JSON.stringify(body || {})});
  let json = null;
  try { json = await r.json(); } catch (e) { json = null; }
  return {status: r.status, json: json};
}
"""


def _walk_records(raw, records_path: Optional[list]):
    """从接口 JSON 走到订单记录 list。

    records_path 给定 → 按键逐层走；否则 raw 本身是 list 直接用，或在常见容器键
    （含 noon Sales 的 `hits`）里找第一个 list。走不到 list 交由调用方红灯（不强行造 list）。
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
        for k in ("hits", "data", "orders", "results", "rows", "items", "records"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("orders", "rows", "items", "records", "list", "hits"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _date_window(lookback_days: int) -> tuple:
    """订单取数日期窗 (from_date, to_date)，ISO date。"""
    import datetime as _dt
    today = _dt.date.today()
    return (today - _dt.timedelta(days=max(1, lookback_days))).isoformat(), today.isoformat()


def _goto_report_page(page, report_page_url: str) -> None:
    """取数前把 page 导到订单/销售报表页（同域），否则跨域 fetch 被 CORS 拦。

    冷 goto 被紫鸟扩展 abort 时重试一次；连续失败 → blocked（不在错误域上瞎 fetch）。
    """
    last = None
    for _ in range(2):
        try:
            page.goto(report_page_url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise LiveSourceUnavailable(
        f"导航 noon 订单报表页 {report_page_url} 失败: {type(last).__name__}: {last}"
        " —— 疑似登录态失效/紫鸟扩展拦截，blocked")


def _fetch_raw_orders(page, *, store_key: str = DEFAULT_STORE_KEY) -> list:
    """在真实已登录 page 上抓 noon 订单原始记录 list（唯一外部边界）。

    流程：导到销售报表页（同域）→ 分页 POST Sales Dashboard 接口 → 汇总 `hits`。
    缺接口/国别配置 → blocked（绝不猜 URL/国别）。HTTP 非 200 / 结构非预期 → blocked
    （疑似登录态失效或接口改版，不静默吞、不回落旧值、不编数）。
    """
    cfg = _orders_cfg(store_key)
    api_url = (cfg.get("api_url") or "").strip()
    if not api_url:
        raise LiveSourceUnavailable(
            "缺 noon 订单接口配置 platform_browser.platforms.noon.orders.api_url"
            " —— blocked，绝不猜 URL/编数")
    country = (cfg.get("country_code") or "").strip()
    if not country:
        raise LiveSourceUnavailable(
            "缺 noon 订单国别配置 platform_browser.platforms.noon.orders.country_code"
            "（KSA=SA）—— blocked，绝不默认国别")
    per_page = int(cfg.get("per_page") or 200)
    max_pages = int(cfg.get("max_pages") or 500)
    from_date, to_date = _date_window(int(cfg.get("lookback_days") or 90))
    records_path = cfg.get("records_path")

    report_page = (cfg.get("report_page_url") or "").strip()
    if report_page:
        _goto_report_page(page, report_page)

    records: list = []
    page_no = 1
    while page_no <= max_pages:
        body = {"country_code": country, "page": page_no, "per_page": per_page,
                "filters": {}, "from_date": from_date, "to_date": to_date}
        try:
            resp = page.evaluate(_ORDER_FETCH_JS, {"url": api_url, "body": body})
        except Exception as e:  # noqa: BLE001 — page 侧各类取数错都归 blocked
            raise LiveSourceUnavailable(
                f"noon 订单页取数失败（page.evaluate POST {api_url} page={page_no}）: "
                f"{type(e).__name__}: {e} —— 疑似登录态失效/接口改版，blocked，"
                "请参照 refresh-dbuyerp-token 流程在本机紫鸟重登该店一次") from e
        status = resp.get("status") if isinstance(resp, dict) else None
        if status is not None and int(status) != 200:
            raise LiveSourceUnavailable(
                f"noon 订单接口 HTTP {status}（{api_url} page={page_no}）—— 疑似登录态"
                "失效/接口改版，blocked，绝不回落旧值/编数")
        payload = resp.get("json") if isinstance(resp, dict) else resp
        hits = _walk_records(payload, records_path)
        if not isinstance(hits, list):
            raise LiveSourceUnavailable(
                f"noon 订单接口返回结构非预期（期望 records 为 list，得 "
                f"{type(hits).__name__}；page={page_no}）—— 疑似页面/接口改版，blocked")
        records.extend(hits)
        total = payload.get("total") if isinstance(payload, dict) else None
        if len(hits) < per_page:
            break
        if total is not None and len(records) >= int(total):
            break
        page_no += 1
    return records


# ── session 获取（lazy import _platform_browser，避免无 playwright 环境 import 失败）──
def _get_session(tenant_id, store_key: str, account: Optional[str]):
    from hipop.server import _platform_browser as pb
    return pb.get_platform_session(tenant_id, store_key, account=account)


# ── producer 工厂 + 注册 ────────────────────────────────────────────────
def fetch_order_rows(tenant_id, *, store_key: str = DEFAULT_STORE_KEY,
                     account: Optional[str] = None, page=None,
                     raw_orders_fn: Optional[Callable] = None) -> list:
    """抓取器主入口：真实已登录 page → 校验过的 WS-34 订单行 list。

    page=None（生产）→ `get_platform_session(tenant_id, store_key)` 拿真实 page（登录态
    失效则上抛 blocked）；smoke 注入 page / raw_orders_fn 做确定性替身（同 smoke_platform_session）。
    每行经 WS-34 `validate_row` 守门：缺必填 / 契约外字段 → 红灯 LiveSourceUnavailable，
    绝不编数。输出绑定真实 page（不接受预造 rows）。
    """
    if page is None:
        page = _get_session(tenant_id, store_key, account)
    fetch = raw_orders_fn or _fetch_raw_orders
    raw_records = list(fetch(page))
    rows = []
    for raw in raw_records:
        row = to_contract_row(raw)
        _contract.validate_row(_contract.ORDERS, row)  # 缺字段/契约外字段红灯，不编数
        rows.append(row)
    return rows


def make_live_row_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_orders_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """造 `fn(tenant_id) -> Iterable[dict]` live row producer（喂订单 ingest run_live）。

    page_factory(tenant_id) -> page：缺省经 `get_platform_session` 取真实 page；smoke 注入
    替身 page（无需真紫鸟）。登录失效 / 字段缺 / 接口改版均上抛红灯，run_live 据此回落
    CSV interim 或无 CSV 红灯（绝不写默认值冒充成功）。
    """
    def producer(tenant_id):
        page = page_factory(tenant_id) if page_factory is not None \
            else _get_session(tenant_id, store_key, account)
        return fetch_order_rows(tenant_id, store_key=store_key, account=account,
                                page=page, raw_orders_fn=raw_orders_fn)
    return producer


def _ingest_module():
    """订单 ingest 模块（dual import path）。set_live_row_producer 写 contract 的 ORDERS
    注册表 —— 与本模块校验用的 _contract 同一处（单一来源，不再各持一份）。"""
    try:
        import ingest_noon_csv_v2 as ingest  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover - 包路径回落
        from hipop.scripts import ingest_noon_csv_v2 as ingest  # type: ignore
    return ingest


def register_live_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_orders_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """把 noon 订单 live producer 注册进订单 ingest（= contract 的 ORDERS 注册表）。

    注册后 `ingest_noon_csv_v2.run_live(tenant_id)` 不再因「无 producer」回落 CSV，而是
    走真实 live 行 → 同一 `_aggregate`/`_upsert`（WS-35）落 wf2_orders/wf2_sku。返回注册的
    producer（便于测试断言注册表单一来源）。
    """
    producer = make_live_row_producer(
        store_key=store_key, account=account,
        page_factory=page_factory, raw_orders_fn=raw_orders_fn)
    _ingest_module().set_live_row_producer(producer)
    return producer


def unregister_live_producer() -> None:
    """清除 orders live producer（回到 run_live 回落 CSV 的状态）。"""
    _ingest_module().set_live_row_producer(None)


if __name__ == "__main__":  # pragma: no cover - 真 live 手动入口
    import argparse
    from server import data as _data

    ap = argparse.ArgumentParser(description="noon 订单实时抓取器（真紫鸟 live 手动跑）")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    ap.add_argument("--account", default=None)
    args = ap.parse_args()

    _data.set_current_tenant(args.tenant)
    rows = fetch_order_rows(args.tenant, store_key=args.store_key, account=args.account)
    print(f"[noon_order_fetcher] tenant={args.tenant} 抓到 {len(rows)} 行订单（已过 WS-34 校验）")
    for r in rows[:10]:
        print("  ", r)

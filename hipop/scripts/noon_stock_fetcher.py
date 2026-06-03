"""noon_stock_fetcher — noon KSA 可售库存实时抓取器（page → rows，WS-N2.2 / WS-59）。

平台 fetcher 第二类（订单抓取器 noon_order_fetcher 之后，复用同一样板）。职责单一：
把 WS-41 `get_platform_session(tenant_id,"noon")` 拿到的**真实已登录 page** 抓出
noon my inventory（可售库存）行，映射成 WS-34 库存行契约
（`noon_live_contract.ROW_CONTRACT[MY_INVENTORY]`），经库存 ingest 的
`ingest_noon_stock_csv_v2.set_live_row_producer` 注册，喂同一 `_aggregate`/`_upsert`
（WS-N3.1/WS-N3.2 socket）部分 upsert 落 wf1_stock.noon_*（total/saleable/unsaleable_qty
+ warehouses_json），与 CSV 路径逐字段一致、不分叉，且绝不碰 ERP 列 /
pending_inbound_qty。

边界（本条只交付抓取器 + 注册 + live smoke）：
  - 读：`get_platform_session`（WS-41）、noon my inventory 页/接口、WS-34 行契约与
    fixture、WS-N3.1/WS-N3.2 落的库存 ingest 注入点（`set_live_row_producer`）。
  - 写：本模块 + 其 smoke；调用 `set_live_row_producer` 注册 producer。
  - 不写：`_aggregate`/`_upsert` 落表逻辑、ERP 库存列、`pending_inbound_qty`、
    `refresh_all_v2`、分析工作流、prompt/skill、凭据。

确定性 blocked / 字段缺失规则（全在代码 + live smoke，绝不进 prompt）：
  · 登录态失效 / 缺会话  → `get_platform_session` raise PlatformBrowserError(blocked)，
    本模块原样上抛（reason 含 refresh-dbuyerp-token 式人工登录提示），绝不 stub 旧/空行。
  · 关键字段缺失（缺 country_code/qty/inventory_type，或缺 SKU 主键来源）→
    `validate_row` 红灯 LiveSourceUnavailable，绝不写 0 假库存、不编仓库 JSON。
  · 页面/接口结构变（取数返回非预期结构、records 非 list）→ 红灯 LiveSourceUnavailable，
    不静默吞行、不回落旧值冒充成功。
  · 缺 noon 库存接口配置 → blocked（绝不猜 URL/编数；首次 live 由运营确认真实接口）。

「真 page→真 rows」的唯一外部边界是 `_fetch_raw_inventory(page)`（page 侧取数）；其余
映射/校验/注册/blocked 规则都是纯函数，可被 smoke 注入替身确定性回归（与
`smoke_platform_session` 替身 page 同形）。

KSA-only：抓取器本身平台无关地抓全部返回行（含 country_code），entity 路由
（SA→ksa）由下游 ingest `_aggregate` 的 `get_entity_by_country` 按 tenant 白名单完成；
本模块不在这里裁 country，避免与 ingest 路由口径分叉。
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

# 行契约 + producer 注册表的唯一来源（WS-34）。库存 ingest 也引用同一模块，故
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


def _stock_cfg(store_key: str = DEFAULT_STORE_KEY) -> dict:
    """noon 可售库存取数配置：platform_browser.platforms.<平台>.my_inventory。

    平台无关：按 store_key 平台名子串命中（noon / NOON-SA 都落 noon），与
    `_platform_browser._platform_cfg_for` 同口径。缺 my_inventory 配置 → 空 dict，由
    `_fetch_raw_inventory` 据此 blocked（不在这里猜默认接口）。
    """
    platforms = (_load_config().get("platform_browser") or {}).get("platforms") or {}
    k = str(store_key).lower()
    for name, pc in platforms.items():
        if name.lower() in k or k in name.lower():
            return pc.get("my_inventory") or {}
    if len(platforms) == 1:
        return (next(iter(platforms.values())).get("my_inventory") or {})
    return {}


# ── 字段映射（唯一需要的人工映射事实，钉在代码里、可被 smoke 回归）────────────
# noon my inventory 后台/接口字段名 → WS-34 库存行契约键。契约键全集（=本 map 键集）
# ⊆ noon_live_contract.ROW_CONTRACT[MY_INVENTORY]['known']，故映射出的 row 不会带契约
# 外字段。真 noon 接口字段命名（snake / camel / 别名）首次 live 由运营核对后在此收口——
# 映射命中不到的必填字段会被 validate_row 红灯，绝不默认写 0 / 编仓库明细。
_NOON_STOCK_FIELD_MAP: dict[str, tuple] = {
    "country_code":   ("country_code", "countryCode", "country",
                       "dest_country", "destCountry"),
    "partner_sku":    ("partner_sku", "partnerSku", "seller_sku", "sellerSku"),
    "sku":            ("sku", "sku_code", "skuCode"),
    "noon_sku":       ("noon_sku", "noonSku", "noon_sku_code", "noonSkuCode"),
    "warehouse_code": ("warehouse_code", "warehouseCode", "fc_code", "fcCode",
                       "fc", "warehouse", "wh_code", "whCode"),
    "qty":            ("qty", "quantity", "stock", "available", "available_qty",
                       "availableQty", "on_hand", "onHand", "units"),
    "inventory_type": ("inventory_type", "inventoryType", "stock_type",
                       "stockType", "type", "bucket"),
    "title":          ("title", "name", "product_title", "productTitle",
                       "item_title", "itemTitle"),
}

# 自检：映射键 ⊆ 契约 known（防漂出契约外字段，红队点）。
assert set(_NOON_STOCK_FIELD_MAP) <= set(
    _contract.ROW_CONTRACT[_contract.MY_INVENTORY]["known"]), \
    "noon 库存字段映射键超出 WS-34 契约 known 集（会被 validate_row 当契约外字段红灯）"


def _pick(raw: dict, candidates: tuple):
    """取 raw 里首个非空候选字段值（None / 空串视为缺；数值 0 是真实值，保留）。"""
    for c in candidates:
        v = raw.get(c)
        if v is not None and not (isinstance(v, str) and v.strip() == ""):
            return v
    return None


def to_contract_row(raw: dict) -> dict:
    """单条 noon 可售库存原始记录 → WS-34 库存行契约 dict。

    只产出**命中到值**的契约键：缺失的必填（country_code/qty/inventory_type）字段不补
    默认值（尤其绝不把缺失数量写成 0），交由 `validate_row` 红灯。产出键 ⊆ 契约 known，
    故不会带契约外字段。
    """
    if not isinstance(raw, dict):
        raise LiveSourceUnavailable(
            f"noon 库存原始记录非 dict（得到 {type(raw).__name__}）—— 疑似接口改版，blocked")
    row: dict = {}
    for key, candidates in _NOON_STOCK_FIELD_MAP.items():
        v = _pick(raw, candidates)
        if v is not None:
            row[key] = v
    return row


# ── 真 page → 真 rows 的唯一外部边界（smoke 注入替身）────────────────────────
# page 侧取数：在已登录 page 上同源 fetch noon my inventory 接口，拿回 JSON。fetch 带
# credentials='include' 复用紫鸟接管会话的 cookie；非 2xx / 解析失败都当登录态失效或
# 接口改版 → blocked。本函数是本模块唯一碰真实 noon 页面/接口的地方。
_STOCK_FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}
"""


def _walk_records(raw, records_path: Optional[list]):
    """从接口 JSON 走到库存记录 list。

    records_path 给定 → 按键逐层走；否则 raw 本身是 list 直接用，或在常见容器键里
    找第一个 list。走不到 list 交由调用方红灯（不强行造 list）。
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
        for k in ("data", "inventory", "stock", "results", "rows", "items", "records"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("inventory", "stock", "rows", "items", "records", "list"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _fetch_raw_inventory(page, *, store_key: str = DEFAULT_STORE_KEY) -> list:
    """在真实已登录 page 上抓 noon my inventory 原始记录 list（唯一外部边界）。

    缺接口配置 → blocked（绝不猜 URL）。fetch 失败 / 返回结构非预期 → blocked
    （疑似登录态失效或接口改版，不静默吞、不回落旧值）。
    """
    cfg = _stock_cfg(store_key)
    api_url = (cfg.get("api_url") or "").strip()
    if not api_url:
        raise LiveSourceUnavailable(
            "缺 noon 可售库存接口配置 platform_browser.platforms.noon.my_inventory.api_url"
            "（首次 live 由运营核对真实 my inventory 接口后配置）—— blocked，绝不猜 URL/编数")
    try:
        raw = page.evaluate(_STOCK_FETCH_JS, api_url)
    except Exception as e:  # noqa: BLE001 — page 侧各类取数错都归 blocked
        raise LiveSourceUnavailable(
            f"noon 可售库存页取数失败（page.evaluate fetch {api_url}）: "
            f"{type(e).__name__}: {e} —— 疑似登录态失效/接口改版，blocked，"
            "请参照 refresh-dbuyerp-token 流程在本机紫鸟重登该店一次") from e
    records = _walk_records(raw, cfg.get("records_path"))
    if not isinstance(records, list):
        raise LiveSourceUnavailable(
            f"noon 可售库存接口返回结构非预期（期望 records 为 list，得 "
            f"{type(records).__name__}）—— 疑似页面/接口改版，blocked，不静默吞行")
    return records


# ── session 获取（lazy import _platform_browser，避免无 playwright 环境 import 失败）──
def _get_session(tenant_id, store_key: str, account: Optional[str]):
    from hipop.server import _platform_browser as pb
    return pb.get_platform_session(tenant_id, store_key, account=account)


# ── live-only 严格数量校验（堵 safe_int 把坏值静默转 0 的假绿）────────────────
def _assert_live_qty(raw_qty) -> None:
    """live 行 qty 必须是可解析的非负数量，否则红灯 LiveSourceUnavailable（blocked）。

    门2 返工③：ingest `safe_int()` 对不可解析数量静默转 0，会把「qty='not-a-number'」
    当 0 库存写进 wf1_stock 并报 source=live 成功——等于把真实库存误清零却报实时成功。
    缺失/空白的 qty 已由 `validate_row`（contract required）拦在前面；这里专打**存在但
    非数字/坏值/负数**这一类，绝不让它走到 `_aggregate`→`safe_int`→写 0。

    只校 live 路径（本函数仅在 fetcher 里调）；CSV `run_v2` 的旧宽松口径（safe_int 容错）
    保持不动。
    """
    s = raw_qty.strip() if isinstance(raw_qty, str) else raw_qty
    try:
        f = float(s)
    except (TypeError, ValueError):
        raise LiveSourceUnavailable(
            f"live 行 qty 非数字/坏值: {raw_qty!r} —— 不可解析数量绝不静默当 0 库存写入"
            "（会把真实库存误清零却报 live 成功），blocked")
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        raise LiveSourceUnavailable(
            f"live 行 qty 非有限数值: {raw_qty!r} —— blocked，不写 0 假库存")
    if f < 0:
        raise LiveSourceUnavailable(
            f"live 行 qty 为负: {raw_qty!r} —— 负库存视为坏值，blocked，不写 0 假库存")


# ── producer 工厂 + 注册 ────────────────────────────────────────────────
def fetch_inventory_rows(tenant_id, *, store_key: str = DEFAULT_STORE_KEY,
                         account: Optional[str] = None, page=None,
                         raw_inventory_fn: Optional[Callable] = None) -> list:
    """抓取器主入口：真实已登录 page → 校验过的 WS-34 库存行 list。

    page=None（生产）→ `get_platform_session(tenant_id, store_key)` 拿真实 page（登录态
    失效则上抛 blocked）；smoke 注入 page / raw_inventory_fn 做确定性替身（同
    smoke_platform_session）。每行经 WS-34 `validate_row` 守门：缺必填 / 缺 SKU 主键 /
    契约外字段 → 红灯 LiveSourceUnavailable；再经 live-only `_assert_live_qty`：qty 非数字/
    坏值/负 → 红灯，绝不让坏数量被 safe_int 静默转 0 写进库存。绝不写 0 假库存 / 编仓库 JSON。
    输出绑定真实 page（不接受预造 rows）。
    """
    if page is None:
        page = _get_session(tenant_id, store_key, account)
    fetch = raw_inventory_fn or _fetch_raw_inventory
    raw_records = list(fetch(page))
    rows = []
    for raw in raw_records:
        row = to_contract_row(raw)
        _contract.validate_row(_contract.MY_INVENTORY, row)  # 缺字段/契约外字段红灯
        _assert_live_qty(row.get("qty"))  # live-only：坏 qty 红灯，堵 safe_int 静默转 0
        rows.append(row)
    return rows


def make_live_row_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_inventory_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """造 `fn(tenant_id) -> Iterable[dict]` live row producer（喂库存 ingest run_live）。

    page_factory(tenant_id) -> page：缺省经 `get_platform_session` 取真实 page；smoke 注入
    替身 page（无需真紫鸟）。登录失效 / 字段缺 / 接口改版均上抛红灯，run_live 据此回落
    CSV interim 或无 CSV 红灯（绝不写 0 假库存冒充成功）。
    """
    def producer(tenant_id):
        page = page_factory(tenant_id) if page_factory is not None \
            else _get_session(tenant_id, store_key, account)
        return fetch_inventory_rows(tenant_id, store_key=store_key, account=account,
                                    page=page, raw_inventory_fn=raw_inventory_fn)
    return producer


def _ingest_module():
    """库存 ingest 模块（dual import path）。set_live_row_producer 写 contract 的
    MY_INVENTORY 注册表 —— 与本模块校验用的 _contract 同一处（单一来源，不各持一份）。"""
    try:
        import ingest_noon_stock_csv_v2 as ingest  # scripts 目录在 sys.path
    except ModuleNotFoundError:  # pragma: no cover - 包路径回落
        from hipop.scripts import ingest_noon_stock_csv_v2 as ingest  # type: ignore
    return ingest


def register_live_producer(*, store_key: str = DEFAULT_STORE_KEY,
                           account: Optional[str] = None,
                           page_factory: Optional[Callable] = None,
                           raw_inventory_fn: Optional[Callable] = None
                           ) -> Callable[[int], Iterable[dict]]:
    """把 noon 可售库存 live producer 注册进库存 ingest（= contract 的 MY_INVENTORY
    注册表）。

    注册后 `ingest_noon_stock_csv_v2.run_live(tenant_id)` 不再因「无 producer」回落 CSV，
    而是走真实 live 行 → 同一 `_aggregate`/`_upsert`（WS-N3.1/N3.2）部分 upsert 落
    wf1_stock.noon_*。返回注册的 producer（便于测试断言注册表单一来源）。
    """
    producer = make_live_row_producer(
        store_key=store_key, account=account,
        page_factory=page_factory, raw_inventory_fn=raw_inventory_fn)
    _ingest_module().set_live_row_producer(producer)
    return producer


def unregister_live_producer() -> None:
    """清除 my_inventory live producer（回到 run_live 回落 CSV 的状态）。"""
    _ingest_module().set_live_row_producer(None)


if __name__ == "__main__":  # pragma: no cover - 真 live 手动入口
    import argparse
    from server import data as _data

    ap = argparse.ArgumentParser(description="noon 可售库存实时抓取器（真紫鸟 live 手动跑）")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    ap.add_argument("--account", default=None)
    args = ap.parse_args()

    _data.set_current_tenant(args.tenant)
    rows = fetch_inventory_rows(args.tenant, store_key=args.store_key, account=args.account)
    print(f"[noon_stock_fetcher] tenant={args.tenant} 抓到 {len(rows)} 行可售库存（已过 WS-34 校验）")
    for r in rows[:10]:
        print("  ", r)

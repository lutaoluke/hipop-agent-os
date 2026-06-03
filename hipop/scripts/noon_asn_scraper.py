"""noon_asn_scraper — noon KSA **ASN/送仓**实时抓取器（page→rows，WS-N2.3 / WS-60）。

定位
----
WS-32.4（PR #25）已把 ASN ingest 做成 **socket**：`ingest_inbound_staging_v2.run_live`
读一个 `fn(tenant_id) -> Iterable[dict]` 的 live row producer，逐行过 WS-34
`noon_live_contract.validate_row(ASN, row)` 守门后，走与 CSV 入口**同一** `_aggregate`/
`_upsert` 落 `wf1_asn_lines_staging`（不分叉）。但在本条之前，那个 producer 还没有真实
实现 —— 实时路径无人注册 producer，`run_live` 只能回落 CSV interim。

本模块就是那个**真 producer**：
  `fetch_asn_rows(tenant_id, store_key="noon") -> list[dict]`
    1. 经 WS-41 `get_platform_session(tenant_id, "noon")` 拿**已登录** noon page；
    2. 导航到 noon 送仓/ASN 页（URL 取自 config，未配 → blocked，不瞎猜）；
    3. 从页面表格抓送仓行（表头驱动，容列序变化）；
    4. 平台 SKU（Z 开头）经 `sales_entity_v2.noon_sku_map` 回 **partner_sku**
       —— 未映射不把 Z 开头平台 SKU 当主键（只带 `sku`，下游 `_aggregate` 计 unmapped 跳过）；
    5. 行字段对齐 WS-34 `ROW_CONTRACT[ASN]`（键 ⊆ known，缺字段**不编数**：留空，
       由 `run_live` 的 `validate_row`/`_require_int` 红灯）；
  `register(store_key="noon")` —— 把本 producer 经 `set_live_row_producer(ASN, fn)`
    注册进 WS-34 单一注册表（钉「接线缺失」死法：注册后 `run_live` 真走 live）。

本模块只保证**实时行进 staging**：
  - **不**算 `wf1_stock.pending_inbound_qty`（仍归 WS-11）；
  - **不**碰目标表 schema、不改 `_aggregate`/`_upsert`、不改分析工作流；
  - **不**另定行字段（唯一来源 = WS-34 contract）、不另起 producer 注册表（单一来源 = contract）。

三种死法（确定性规则写在代码里，不进 prompt）：
  ① 接线缺失 —— 抓取器写好但没注册 → 实时路径回落 CSV。`register()` 经 contract
     单一注册表登记；smoke 断言注册后 `run_live` 不带参 → `source=="live"`。
  ② 死代码短路 —— 实时路径跳过平台 SKU→partner_sku 映射。本抓取器在产出端就
     `noon_sku_map` 映射；smoke 断言 staging 主键是 partner_sku，绝无 Z 开头平台 SKU。
  ③ 占位假数据 —— 缺 ASN/送仓字段时编造 qty/ETA/ASN number。本抓取器**只抓页面真有
     的值**，缺值留空（由 contract 红灯），登录失效/页面结构变 → raise
     `LiveSourceUnavailable`（blocked），绝不生成虚假 staging 行。

登录失效 / 缺会话 / 平台会话不可用：`get_platform_session` 抛 `PlatformBrowserError
(blocked=True)`，本模块转成 `LiveSourceUnavailable` 让 `run_live` **红灯**（而非静默回落
CSV 冒充实时成功）。真正的瞬时取数错误（如导航网络超时）保持原异常类型抛出 →
`run_live` 视作 transient 回落 CSV interim（同契约）。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # .../hipop-agent-os
sys.path.insert(0, REPO)                 # hipop.* 包路径（pb 内部 from hipop.scripts._config）
sys.path.insert(0, os.path.dirname(HERE))  # hipop/  → from server import ...
sys.path.insert(0, HERE)                   # scripts/ → import noon_live_contract

from sales_entity_v2 import noon_sku_map           # noqa: E402
from server import data as _data                   # noqa: E402（与 sales_entity_v2 同一 data 实例）
from hipop.server import _platform_browser as pb   # noqa: E402（会话/登录检测/store 映射）
from hipop.scripts._config import load_config       # noqa: E402
import noon_live_contract as _contract              # noqa: E402（行契约 / 红灯 / 注册表唯一来源）

ASN = _contract.ASN
# 关键：复用 contract 的 LiveSourceUnavailable（与 ingest_inbound_staging_v2 同一类对象，
# 经 scripts 路径 import），这样本模块 raise 的 blocked 才会被 run_live 的
# `except LiveSourceUnavailable: raise` 接住红灯，而不是掉进 `except Exception` 回落 CSV。
LiveSourceUnavailable = _contract.LiveSourceUnavailable

DEFAULT_STORE_KEY = "noon"

# ── 页面表格选择器（标准 HTML 表；可经 config 覆写，便于活页结构确认后微调）──
# 表头驱动解析：按表头文案把列映射到契约字段，故对列序变化稳健。
_DEFAULT_SELECTORS = {
    "table": "table",
    "header_cell": "thead th",
    "body_row": "tbody tr",
    "cell": "td",
}

# 表头文案（归一化后）→ 契约字段。noon 送仓页常见中英文表头都收一些；不认识的列直接忽略
# （不会带进 row，故不会触发契约「未知字段」红灯）。partner sku 与平台 sku 分开映射，
# 避免把平台 SKU 当 partner_sku。
_HEADER_ALIASES = {
    "asn_number": {
        "asn", "asn number", "asn no", "asn no.", "asn id", "asn编号", "asn 编号",
        "shipment id", "shipment number", "shipment no", "送仓单号", "送仓单", "入库单号",
    },
    "partner_sku": {
        "partner sku", "partner-sku", "psku", "partner_sku", "卖家sku", "商家sku",
    },
    "sku": {
        "sku", "noon sku", "platform sku", "item sku", "sku code", "noon_sku",
        "平台sku", "平台 sku",
    },
    "qty": {
        "qty", "quantity", "units", "unit", "expected qty", "expected quantity",
        "expected units", "数量", "件数", "送仓数量", "预报数量",
    },
    "status": {
        "status", "asn status", "shipment status", "状态", "送仓状态",
    },
    # 刻意**不**映射 inbound_date/ETA：socket `_row_source` 以「是否带 inbound_date」
    # 区分两路 source —— 带 inbound_date → erp_inbound，否则 → noon_asn。noon ASN 行属
    # noon_asn 一路，故绝不携带 inbound_date（与 fixtures/noon_asn.csv 的列集一致：
    # asn_number/status/sku/qty/country_code）。inbound_date 是 ERP 送仓/拣货那一路的
    # 判别列，由 ERP 导出携带，不由本 noon 抓取器产出。页面若有 ETA 列 → 直接忽略，
    # 绝不据此把 noon 行误归 erp_inbound、也绝不编造 ETA（死法③）。
    "warehouse_code": {
        "warehouse", "warehouse code", "fc", "fulfillment center", "fulfilment center",
        "wh", "仓库", "仓库编码", "目的仓",
    },
    "country_code": {
        "country", "country code", "国家", "国别",
    },
}

# 表头里**必须**认出来的关键列；缺它们 = 页面结构变了（或抓错表）→ blocked，
# 绝不当成「成功但 0 行」静默放过。
_REQUIRED_HEADER_FIELDS = {"asn_number", "qty"}


# ── 注册（WS-34 单一注册表）─────────────────────────────────────────────
def register(store_key: str = DEFAULT_STORE_KEY):
    """把本抓取器注册成 ASN live row producer（委托 contract 单一注册表）。返回注册的 fn。

    `run_live` 不带 `live_producer` 时即从该注册表读到本 fn → 实时路径真走 live。
    store_key 非默认时包一层把它传进 `fetch_asn_rows`（producer 签名固定 fn(tenant_id)）。
    """
    if store_key == DEFAULT_STORE_KEY:
        fn = fetch_asn_rows
    else:
        def fn(tenant_id, _sk=store_key):
            return fetch_asn_rows(tenant_id, store_key=_sk)
    _contract.set_live_row_producer(ASN, fn)
    return fn


def unregister():
    """清除 ASN live row producer（测试隔离 / 回退用）。"""
    _contract.set_live_row_producer(ASN, None)


# ── config ──────────────────────────────────────────────────────────────
def _noon_platform_cfg() -> dict:
    return ((load_config().get("platform_browser") or {}).get("platforms") or {}).get("noon") or {}


def _asn_url() -> str:
    """noon 送仓/ASN 页 URL（config platform_browser.platforms.noon.asn_url）。

    未配置 → blocked，**绝不瞎猜 URL** —— 抓错页等于占位假数据。真实 URL 须在活页确认后
    填进 config（与 root_url/check_url 同处），这是唯一需要人工确认的 live 事实。
    """
    url = (_noon_platform_cfg().get("asn_url") or "").strip()
    if not url:
        raise LiveSourceUnavailable(
            "noon 送仓/ASN 页 URL 未配置（config platform_browser.platforms.noon.asn_url）"
            " —— 绝不瞎猜抓错页；请在活页确认真实送仓页 URL 后填入 config 再跑实时。")
    return url


def _selectors() -> dict:
    sel = dict(_DEFAULT_SELECTORS)
    override = _noon_platform_cfg().get("asn_selectors") or {}
    if isinstance(override, dict):
        sel.update({k: v for k, v in override.items() if k in sel and v})
    return sel


# ── 会话 / 导航 / 登录态（复用 WS-41 / WS-33.3 的确定性规则，不 stub）─────────
def _open_session(tenant_id, store_key):
    """拿已登录 noon page。平台会话 blocked（登录失效/缺会话/缺紫鸟）→ 转成
    LiveSourceUnavailable 让 run_live 红灯，绝不让它掉进 CSV 回落冒充实时成功。"""
    try:
        return pb.get_platform_session(tenant_id, store_key)
    except pb.PlatformBrowserError as e:
        raise LiveSourceUnavailable(
            f"平台会话不可用（blocked）：{e}") from e


def _resolve_store_entity(tenant_id, store_key):
    """把 store_key 对应的 noon store 解析到唯一 (tenant, entity_alias, country)（WS-46）。

    给抓出来的行补 country_code（页面单店单国，country 来自 store 映射而非每行）+ 选
    partner_sku 映射表。映射 blocked（缺映射/不唯一）→ LiveSourceUnavailable，绝不默认
    塞 tenant/entity。"""
    try:
        stores = pb.list_stores(store_key=store_key)
        if not stores:
            raise LiveSourceUnavailable(
                f"account 下没有任何 store（store_key={store_key!r}）—— 缺会话")
        store = pb.select_store(stores, store_key)
        se = pb.resolve_store_entity(store)
    except pb.PlatformBrowserError as e:
        raise LiveSourceUnavailable(f"store→entity 映射 blocked：{e}") from e
    if int(se.tenant_id) != int(tenant_id):
        raise LiveSourceUnavailable(
            f"store {store_key!r} 映射到 tenant={se.tenant_id} 与调用 tenant={tenant_id} 不符"
            f" —— 越权红灯，绝不串租户")
    return se


def _navigate_to_asn(page, store_key):
    """导航到 ASN 页并校验仍是登录态。落登录页/缺会话 cookie → blocked（不 stub 登录态）。"""
    url = _asn_url()
    pcfg = pb._platform_cfg_for(store_key)
    # 导航失败（网络/abort）保持原异常类型抛出 → run_live 视 transient 回落 CSV。
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    login = pb._detect_login(page, pcfg)
    if login.kind != "ok":
        raise LiveSourceUnavailable(
            f"导航 noon 送仓页后判定未登录：{login.detail} —— blocked，"
            f"请用紫鸟手动登录该店一次后重试，绝不 stub 登录态。")


# ── 表格解析（表头驱动；纯函数边界，可被 fixture page 替身覆盖）──────────────
def _norm_header(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ").strip().lower())


def _match_header(label: str):
    """归一化表头 → 契约字段名；不认识返回 None（该列被忽略，不进 row）。"""
    h = _norm_header(label)
    if not h:
        return None
    for field, aliases in _HEADER_ALIASES.items():
        if h in aliases:
            return field
    return None


def parse_asn_table(page) -> list[dict]:
    """从 noon 送仓页 page 抓送仓行（表头驱动）。返回原始 dict 行列表（键 = 契约字段）。

    选第一张能认出关键列（asn_number + qty）的表；找不到这样的表 → blocked（页面结构
    变 / 抓错页），绝不返回空行冒充「没有送仓」。表存在且关键列齐、但 0 数据行 = 真的没有
    在途送仓（合法快照），返回 []。
    """
    sel = _selectors()
    tables = list(page.query_selector_all(sel["table"]) or [])
    if not tables:
        raise LiveSourceUnavailable(
            "noon 送仓页未找到任何表格（页面结构变 / 抓错页 / 未加载）—— blocked，不编数")

    for table in tables:
        header_cells = list(table.query_selector_all(sel["header_cell"]) or [])
        field_by_idx = {}
        for i, th in enumerate(header_cells):
            f = _match_header(th.inner_text())
            if f and f not in field_by_idx.values():  # 同字段多列时取第一列
                field_by_idx[i] = f
        if not _REQUIRED_HEADER_FIELDS.issubset(set(field_by_idx.values())):
            continue  # 不是送仓表 / 关键列缺失，看下一张表

        rows = []
        for tr in (table.query_selector_all(sel["body_row"]) or []):
            cells = list(tr.query_selector_all(sel["cell"]) or [])
            if not cells:
                continue  # 跳过分组/空行
            raw = {}
            for i, field in field_by_idx.items():
                if i < len(cells):
                    raw[field] = (cells[i].inner_text() or "").strip()
            rows.append(raw)
        return rows

    raise LiveSourceUnavailable(
        "noon 送仓页表头缺关键列（需至少认出 asn_number + qty）—— 页面结构变？"
        " blocked，绝不抓错列编数。")


# ── 行 → 契约行（平台 SKU 映射 + 缺值留空不编数）────────────────────────────
def _clean(v):
    s = "" if v is None else str(v).strip()
    return s or None


def _raw_to_contract_row(raw: dict, country_code: str, sku_map: dict) -> tuple[dict, bool]:
    """原始抓取行 → 对齐 WS-34 ROW_CONTRACT[ASN] 的行。返回 (row, mapped)。

    - 平台 SKU（raw['sku']，Z 开头）经 sku_map 回 partner_sku；未映射 → 只带 sku，
      **绝不**把 Z 开头平台 SKU 写成 partner_sku（mapped=False，下游计 unmapped 跳过）。
    - qty/asn_number 等缺值**留空**（不编 0 / 不编号），由 run_live 的 validate_row /
      _require_int 红灯 —— 占位假数据死法在此被钉死。
    - 只产出 ⊆ ROW_CONTRACT[ASN]['known'] 的键；country_code 来自 store 映射（单店单国）。
    """
    plat = _clean(raw.get("sku"))
    partner_raw = _clean(raw.get("partner_sku"))
    partner_sku = partner_raw or (sku_map.get(plat) if plat else None)

    row = {
        "asn_number": _clean(raw.get("asn_number")) or "",  # 缺 → 空串，contract 红灯
        "qty": raw.get("qty") if raw.get("qty") not in (None,) else "",  # 原值透传，不编数
        "country_code": _clean(raw.get("country_code")) or country_code,
    }
    if plat:
        row["sku"] = plat
    if partner_sku:
        row["partner_sku"] = partner_sku
    # 注意：不含 inbound_date —— 见 _HEADER_ALIASES 处说明（保 source=noon_asn 路由）。
    for opt in ("status", "warehouse_code"):
        v = _clean(raw.get(opt))
        if v is not None:
            row[opt] = v
    return row, bool(partner_sku)


# ── producer 入口 ────────────────────────────────────────────────────────
def fetch_asn_rows(tenant_id, store_key: str = DEFAULT_STORE_KEY) -> list:
    """noon KSA 送仓页 → ASN live rows（producer，签名 fn(tenant_id) 兼容）。

    读：WS-41 `get_platform_session`、noon 送仓页、WS-46 store→entity 映射、wf2_sku 的
        noon_sku→partner_sku 映射、config 的 asn_url/asn_selectors。
    产出：list[dict]，每行键 ⊆ ROW_CONTRACT[ASN]['known']；交给 `run_live` 逐行
        validate_row 守门 + 同一 `_aggregate`/`_upsert` 落 wf1_asn_lines_staging。
    blocked（登录失效/缺映射/缺配置/页面结构变）→ raise LiveSourceUnavailable（红灯）。
    """
    page = _open_session(tenant_id, store_key)
    se = _resolve_store_entity(tenant_id, store_key)
    _data.set_current_tenant(tenant_id)  # sales_entity_v2 用的同一 data 实例
    sku_map = noon_sku_map(tenant_id, se.entity_alias)

    _navigate_to_asn(page, store_key)
    raw_rows = parse_asn_table(page)

    out = []
    n_unmapped = 0
    for raw in raw_rows:
        row, mapped = _raw_to_contract_row(raw, se.country, sku_map)
        if not mapped and row.get("sku"):
            n_unmapped += 1
        out.append(row)

    print(f"[noon_asn_scraper] tenant={tenant_id} store={store_key} entity={se.entity_alias}"
          f" country={se.country}: {len(out)} rows, {n_unmapped} 平台SKU未映射（下游跳过）",
          file=sys.stderr)
    return out


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="noon KSA ASN/送仓 实时抓取器（live smoke 入口）")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--store-key", default=DEFAULT_STORE_KEY)
    ap.add_argument("--ingest", action="store_true",
                    help="注册 producer 并真跑 ingest_inbound_staging_v2.run_live（落 staging）")
    args = ap.parse_args()
    if args.ingest:
        import ingest_inbound_staging_v2 as _inbound
        register(args.store_key)
        try:
            res = _inbound.run_live(args.tenant, allow_csv_fallback=False)
            print(json.dumps(res, ensure_ascii=False, default=str))
        finally:
            unregister()
    else:
        rows = fetch_asn_rows(args.tenant, store_key=args.store_key)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))

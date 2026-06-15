"""noon_live_contract — noon 实时取数「行契约」的唯一来源（WS-32.1 / WS-34）。

背景：noon 三类数据(订单 / my inventory / 送仓 ASN)要从「手工 CSV」迁到
「实时抓取」。stock 链已落地形状(WS-28/WS-N3.1/WS-N3.2)：fetcher 产出
**同形 dict row** → 同一个 `_aggregate(rows)` → 同一个 `_upsert`，live 与 CSV
逐字段一致、不分叉。本模块把这套形状从 stock 一条**抽成三类共用的单一契约**：

  1. producer 接口签名     —— `fn(tenant_id:int) -> Iterable[dict]`
  2. 三类数据各自的行字段  —— REQUIRED / SKU 主键 / known(唯一来源，禁各自定义)
  3. live producer 注册表  —— set/get/missing/assert(WS-N2 抓取器在此注册)
  4. 行校验 + fixture 装载 —— 字段缺失红灯，绝不默认编数

谁来引用本模块（防契约漂移）：
  · WS-35 订单 socket / WS-37 ASN socket：`_aggregate(rows)` 读的 row 键、
    等价 smoke 的 fixture，都以本模块为准，不在脚本里另定字段。
  · WS-N2 / WS-57 三个 noon 抓取器：产出的 live row 必须 `validate_row` 通过，
    并 `set_live_row_producer(kind, fn)` 注册进来。
  · stock 链(已 land)沿用其脚本内既有 set/get_live_row_producer；本模块的
    注册表是「迁移收口」(WS-38 refresh_all_v2)判定三类是否就绪的统一入口。

本模块**不**实现真抓取、不改目标表 schema、不改分析工作流。
"""
from __future__ import annotations

import os
import sys
import csv
import types
from typing import Callable, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # .../hipop-agent-os
FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "wf1_ingest_v2")


class LiveSourceUnavailable(Exception):
    """live 取数失败 / 无 producer / 字段缺失 —— 明确的失败信号(红灯)。

    专门区别于「成功但 0 行」：取数挂了就 raise，绝不让上游把 0 / 空值 / 默认值
    当成功落库(占位假数据死法)。整链应据此报 blocked，不静默回落、不编数。
    """


# ── 四类 noon 数据 ────────────────────────────────────────────────────
ORDERS = "orders"               # noon 订单    → wf2_orders / wf2_sku
MY_INVENTORY = "my_inventory"   # noon 可售库存 → wf1_stock.noon_*
ASN = "asn"                     # noon 送仓/ASN → wf1_asn_lines_staging
LISTINGS = "listings"           # noon listing 在售状态 → 对账用（WS-183）

# KINDS = refresh_all_v2 需要 live producer 的三类(维持不变，不接 listings)。
# listing 行契约通过 ROW_CONTRACT 校验，不走 live producer 注册表。
KINDS = (ORDERS, MY_INVENTORY, ASN)


# ── 行字段契约（唯一来源；键 = fetcher / CSV 行的 dict 键，对齐 noon 列名）──
# required        : 必填，缺失或空 → 红灯，不默认编数。
# sku_key_one_of  : SKU 主键候选，至少命中一个非空(平台 SKU 经映射回 partner_sku
#                   归各 ingest 脚本，本契约只保证「有主键来源」)。
# known           : 该类已知字段全集(含 required)；live 与 CSV 都从这里取，禁自造。
ROW_CONTRACT = {
    ORDERS: {
        "required": ("partner_sku", "item_nr"),
        "sku_key_one_of": ("partner_sku",),
        "known": (
            "partner_sku", "sku", "item_nr", "order_timestamp", "status",
            "fulfillment_model", "offer_price", "gmv_lcy", "currency_code",
            "dest_country", "family", "brand_code",
        ),
    },
    MY_INVENTORY: {
        "required": ("country_code", "qty", "inventory_type"),
        "sku_key_one_of": ("partner_sku", "sku", "noon_sku"),
        "known": (
            "country_code", "sku", "noon_sku", "partner_sku",
            "warehouse_code", "qty", "inventory_type", "title",
        ),
    },
    ASN: {
        "required": ("asn_number", "qty", "country_code"),
        "sku_key_one_of": ("partner_sku", "sku"),
        "known": (
            "asn_number", "status", "sku", "partner_sku", "qty",
            "country_code", "inbound_date", "warehouse_code",
        ),
    },
    LISTINGS: {
        # listing_status: noon 平台 listing 生命周期状态(active/inactive/rejected/pending 等)
        # 是否在售 = listing_status 语义；is_listed 为可选辅助字段(0/1)。
        # 缺 country_code 或 listing_status → 不知道「哪国/是否在售」→ 红灯。
        "required": ("country_code", "listing_status"),
        "sku_key_one_of": ("noon_sku", "partner_sku", "sku"),
        "known": (
            "country_code", "store_name",
            "noon_sku", "partner_sku", "sku",
            "listing_status", "is_listed",
            "title",
        ),
    },
}

# 四类 fixture(行字段唯一来源；my_inventory / asn 复用既有 CSV)。
FIXTURES = {
    ORDERS: os.path.join(FIXTURE_DIR, "noon_orders.csv"),
    MY_INVENTORY: os.path.join(FIXTURE_DIR, "noon_inventory.csv"),
    ASN: os.path.join(FIXTURE_DIR, "noon_asn.csv"),
    LISTINGS: os.path.join(FIXTURE_DIR, "noon_listings.csv"),
}


def _check_kind(kind: str) -> None:
    if kind not in ROW_CONTRACT:
        raise ValueError(f"未知 noon 数据类型: {kind!r}（已知: {tuple(ROW_CONTRACT)}）")


# ── live producer 注册表（WS-N2 抓取器接入点 / WS-38 收口判定入口）──────
# producer 签名：fn(tenant_id: int) -> Iterable[dict]，每个 dict 是一条
# 同形行(键见 ROW_CONTRACT[kind]['known'])。未注册 = 该类尚无实时来源。
#
# 单一来源加固（WS-34 红队点）：本模块会被以两种 import 路径加载——
#   · `noon_live_contract`             （scripts 目录在 sys.path 顶层；脚本/smoke 用）
#   · `hipop.scripts.noon_live_contract`（包路径；runtime workflow_runners 用）
# Python 按 import 名缓存 module，两条路径 = 两个 module 对象 = 两份 _PRODUCERS
# = 两套真相（在一条路径注册的 producer，另一条 get/missing 看不到）。这里把注册表
# 锚到一个「import 路径无关」的进程级 holder（sys.modules 固定键），任一路径 set、
# 另一路径都 get 得到，杜绝「统一 producer 接口」漂成两套真相。
# 关键：下面所有读写只**原地 mutate** 这个 dict（pop/赋值键），从不重新绑定
# `_PRODUCERS` 名 —— 故无论哪个 module 实例，名都指向同一 dict 对象。
_REGISTRY_HOLDER = "_noon_live_contract_registry__singleton"
if _REGISTRY_HOLDER not in sys.modules:
    _holder = types.ModuleType(_REGISTRY_HOLDER)
    _holder.PRODUCERS = {}
    sys.modules[_REGISTRY_HOLDER] = _holder
_PRODUCERS: dict[str, Callable[[int], Iterable[dict]]] = sys.modules[_REGISTRY_HOLDER].PRODUCERS


def set_live_row_producer(kind: str, fn: Callable[[int], Iterable[dict]] | None) -> None:
    """注册/清除某类 noon live row producer。fn=None 清除。"""
    _check_kind(kind)
    if fn is None:
        _PRODUCERS.pop(kind, None)
    else:
        _PRODUCERS[kind] = fn


def get_live_row_producer(kind: str) -> Callable[[int], Iterable[dict]] | None:
    _check_kind(kind)
    return _PRODUCERS.get(kind)


def missing_live_producers() -> list[str]:
    """返回尚未注册 live producer 的数据类型(按 KINDS 顺序)。"""
    return [k for k in KINDS if _PRODUCERS.get(k) is None]


def assert_live_producers_ready() -> None:
    """三类 live producer 必须全注册，否则红灯并指出缺哪类。

    迁移收口(WS-38 refresh_all_v2 / WS-36)用它判定「是否真走了实时」：
    缺任何一类 → raise LiveSourceUnavailable，整链报 blocked，
    **绝不静默回落 CSV 冒充 source=live 成功**。
    """
    missing = missing_live_producers()
    if missing:
        raise LiveSourceUnavailable(
            "noon live producer 未注册: " + ", ".join(missing)
            + " —— 该类无实时来源，报 blocked，不回落 CSV 冒充 live、不编数"
        )


# ── 行校验（字段缺失红灯，不默认编数）─────────────────────────────────
def row_problems(kind: str, row: dict) -> list[str]:
    """返回该行违反契约的问题列表；空列表 = 合格。不修改 row、不填默认值。"""
    _check_kind(kind)
    spec = ROW_CONTRACT[kind]
    problems: list[str] = []

    def _blank(field: str) -> bool:
        v = row.get(field)
        return v is None or (isinstance(v, str) and v.strip() == "")

    for f in spec["required"]:
        if _blank(f):
            problems.append(f"缺必填字段 {f}")

    sku_keys = spec["sku_key_one_of"]
    if sku_keys and all(_blank(f) for f in sku_keys):
        problems.append("缺 SKU 主键来源（需 " + "/".join(sku_keys) + " 之一非空）")

    # 契约漂移红灯：known 是该类唯一字段集（docstring 明示禁自造）。任何不在
    # known 里的键 = live row 私自带了契约外字段 → 红灯，绝不放行冒充合规。
    known = set(spec["known"])
    for f in sorted(k for k in row.keys() if k not in known):
        problems.append(f"未知字段 {f}（不在契约 known 集，禁自造）")

    return problems


def validate_row(kind: str, row: dict) -> None:
    """行不合契约 → raise LiveSourceUnavailable(指出缺哪些字段)。"""
    problems = row_problems(kind, row)
    if problems:
        raise LiveSourceUnavailable(f"[{kind}] 行字段不合契约: " + "; ".join(problems))


def load_fixture_rows(kind: str) -> list[dict]:
    """装载该类 fixture 为 dict 行列表(键 = CSV 列名 = 契约 known 字段)。"""
    _check_kind(kind)
    path = FIXTURES[kind]
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

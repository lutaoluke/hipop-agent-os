"""
ProductRecord — 选品 agent 的跨平台数据契约。

按 §5 of 点购_选品Agent_工程交底.md (2026-05-05 版).

核心 hard rule:
- SalesSignal 的相对值字段 (percentile_in_query / tier_in_query / z_score_in_query)
  是决策唯一依据。raw_value (绝对销量) 仅供归一层输入, 决策层禁止用 'sold>=30' 这种
  绝对阈值。原因: §A 步骤 4 — "好销量不是绝对值, 是竞争视角下的相对值"。

- type='unknown' (无销量字段) = 低 tier, 不是"未知待定"。原因: §A 步骤 4 —
  "如果没有销量相关字段, 说明销售情况不好"。

- ReturnRisk 是硬要求 (§A 步骤 7): 高客单价+多配件+分体设计 → high。
- market_meta.vat_rate 是硬要求 (§A 步骤 7): UAE=0.05 / SA=0.15 (法定)。
  但 N10 利润核算用 pricing_table.yaml 里的实算 (1.5%/3.9%, 即 noon 平台代收
  落到卖家结算的份额), 不是法定 VAT。两套数, 两个用途。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal, Optional


Platform = Literal[
    "noon_ae", "noon_sa",
    "amazon_ae", "amazon_sa", "amazon_us",
    "trendyol", "shein",
    "alibaba_1688",
]

SalesSignalType = Literal["absolute_count", "badge", "estimated", "unknown"]
SalesTier = Literal["top", "high", "mid", "low", "none"]
ReturnRiskLevel = Literal["low", "mid", "high"]


@dataclass
class SalesSignal:
    """跨平台销量归一. §A 步骤 4 hard rule: 决策只看 relative_*, 不看 raw_value."""

    # 原始信号 (采集层填)
    type: SalesSignalType
    raw_value: Optional[float] = None       # 30 (sold) / BSR=1234 / etc
    raw_text: Optional[str] = None          # 'Selling out fast' / '30+ sold recently'
    freshness_days: Optional[int] = None
    source: str = ""                         # 'noon_badge', 'amazon_bsr', '1688_monthly_sold', ...
    confidence: float = 0.0                  # 0-1, 数据源可靠度先验

    # 相对位置 (归一层填, 同流量词/类目内计算)
    percentile_in_query: Optional[float] = None      # 0-100
    tier_in_query: Optional[SalesTier] = None
    z_score_in_query: Optional[float] = None

    # 起量识别 (时间维度)
    is_rising: bool = False
    rising_evidence: Optional[str] = None    # '近 30 天评论从 0→8' / '上架 <2mo'

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence 必须 0-1, got {self.confidence}")
        if self.percentile_in_query is not None and not (0 <= self.percentile_in_query <= 100):
            raise ValueError(f"percentile 必须 0-100, got {self.percentile_in_query}")


@dataclass
class SKU:
    """商品下的具体规格变体."""
    spec_axes: dict                           # {'size': '24in', 'material': 'ABS+PC', 'color': '莫兰迪绿'}
    price: float
    currency: str
    stock_signal: Optional[str] = None        # '充足' / '紧张' / None
    review_count: Optional[int] = None        # 该 SKU 评论数 (高评论 SKU 识别)
    sold_count: Optional[int] = None          # 该 SKU 销量 (高销 SKU 识别)
    is_oversize: bool = False                  # 加大版 SKU (头程运费翻倍判断, §A 步骤 11)


@dataclass
class ReturnRisk:
    """退货风险打标 (§A 步骤 7)."""
    level: ReturnRiskLevel
    reasons: list[str] = field(default_factory=list)   # ['多配件', '高客单价', '分体可拆装']
    suggestion: Optional[str] = None                    # '保留观望, 优先选一体折叠款'


@dataclass
class ProductRecord:
    """跨平台商品统一表示. L0 抓取 → L1 规范化产物 → L2/L3/L4 消费."""

    # 身份
    id: str                                    # f"{platform}:{platform_id}"
    platform: Platform
    url: str
    title: str
    brand: Optional[str]
    category_path: list[str]

    # 媒体
    images: list[str]                          # 主图 + 详情图 + SKU 图 URL
    image_embeddings: dict = field(default_factory=dict)
    # ↑ {'siglip': vec/None, 'dinov2': vec/None}, L0 时可全 None

    # 价格 / 规格
    price: dict = field(default_factory=dict)
    # ↑ {'value', 'currency', 'original', 'discount'}
    skus: list[SKU] = field(default_factory=list)

    # 销量 (核心)
    sales_signal: SalesSignal = field(
        default_factory=lambda: SalesSignal(
            type="unknown", source="placeholder",
            tier_in_query="low",   # §A 步骤 4: unknown → low
        )
    )

    # 评论
    reviews: dict = field(default_factory=dict)
    # ↑ {'avg', 'count', 'recent_distribution', 'salient_phrases',
    #    'real_photos': [URL], 'sku_mentions': {sku_id: count},
    #    'is_spam_pattern': bool}

    # LLM 抽出的卖点词 (N6 节点写)
    inferred_features: list[str] = field(default_factory=list)

    # 物流
    shipping: dict = field(default_factory=dict)
    # ↑ {'package_dims': {l, w, h}, 'weight': kg,
    #    'can_nest': bool, 'is_oversize_overall': bool}

    # 政策
    policy_flags: dict = field(default_factory=dict)
    # ↑ {'brand_risk': bool, 'brand_mindshare': str, 'compliance_per_platform': {plat: bool}}

    # 退货风险 (§A 步骤 7)
    return_risk: Optional[ReturnRisk] = None

    # 市场参数 (国别税率等, §A 步骤 7)
    market_meta: dict = field(default_factory=dict)
    # ↑ {'vat_rate': 0.05/0.15 (法定),
    #    'platform_vat_share_seller': 0.015/0.039 (实算, 见 pricing_table.yaml),
    #    'commission_rate': 0.20}

    # 元数据
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    source_path: str = ""              # 'firecrawl_v3' / '1688_playwright_2026-05'

    def __post_init__(self):
        if not self.id or ":" not in self.id:
            raise ValueError(f"id 必须形如 'platform:platform_id', got {self.id!r}")
        platform_prefix = self.id.split(":", 1)[0]
        if platform_prefix != self.platform:
            raise ValueError(
                f"id 前缀 ({platform_prefix}) 必须与 platform ({self.platform}) 一致"
            )

    # --- 序列化 ---

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.isoformat() if self.fetched_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProductRecord":
        d = dict(d)
        if isinstance(d.get("fetched_at"), str):
            d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        if isinstance(d.get("sales_signal"), dict):
            d["sales_signal"] = SalesSignal(**d["sales_signal"])
        if isinstance(d.get("skus"), list):
            d["skus"] = [SKU(**s) if isinstance(s, dict) else s for s in d["skus"]]
        if isinstance(d.get("return_risk"), dict):
            d["return_risk"] = ReturnRisk(**d["return_risk"])
        return cls(**d)


# ---- 校验 ----

REQUIRED_FIELDS = ("id", "platform", "url", "title")


def validate(record: ProductRecord) -> list[str]:
    """返回 issue 列表, 空 = 通过. L0 抓完 L1 入库前必跑."""
    issues: list[str] = []
    for f in REQUIRED_FIELDS:
        if not getattr(record, f, None):
            issues.append(f"缺字段 {f}")
    if record.sales_signal.type == "unknown" and record.platform != "alibaba_1688":
        # 1688 是供给端不需要销量, 其他平台必须有
        if record.sales_signal.tier_in_query is None:
            issues.append(
                f"{record.platform} sales_signal=unknown 时必须显式 tier_in_query='low' "
                "(§A 步骤 4: '没有销量相关字段, 说明销售情况不好')"
            )
    if not record.images:
        issues.append("images 为空, 无法做审美 rerank / 同款检测")
    return issues

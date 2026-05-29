# 点购 · 选品 Agent 工程交底 (v2 · 2026-05-06 干净版)

> 给接手实施的 Claude Code：
> - **§A 业务原文是终极仲裁**。本文档前面的章节是工程提炼,任何冲突以 §A 为准。
> - 这是 v2 干净重写,不要回头读旧版。旧版有几轮反复改的累赘。
> - 凭证 / Firecrawl key / 1688 账号 / 飞书 App 信息都在同目录 `点购_选品_凭证.md`。

---

## 1. 业务速览

| 项 | 值 |
|---|---|
| 用户 | Luke,跨境电商运营 |
| 主市场 | 中东 noon UAE/SA + Amazon UAE/SA |
| 品类 | 行李箱 / boss 椅 / 婴儿车(高景观+轻量化) 等 |
| 节奏 | 每月 3 次集中选品,每次目标 5-6 个可拿样品(候选池应 200+) |
| 三种触发 | T1 月度自动(基于店铺已上架类目) / T2 人工输入流量词 / T3 人工输入类目 |
| 团队 | 1 运营 + 2 助理 + 1 采购,一人看 2 店 |
| 主原则 | **防"无脑跟卖卷价格"陷阱**:销量越好越警惕,利润不够或竞品已极致性价比就是陪跑 |

---

## 2. 整体架构(5 层)

```
L4 交付层    选品文档(md+json+飞书) | 工作台前端(终局) | 群通知 | 询盘工作区
L3 编排层    OpenClaw selector / detailer / buyer 三 agent + 选品文档生成器
L2 知识层    审美画像 / 品类硬约束 / 国家政策 / 库存协同 / 历史选品记忆 / pricing_table
L1 规范化层  ProductRecord 统一对象 + embedding 索引(SigLIP+DINOv2,后期)
L0 数据层    softtime MCP(Amazon) / Firecrawl(noon) / 本地 Playwright(1688) / 私有数据
```

**铁律**:
1. **LLM 不进浏览器点击循环**——L0 全确定性,LLM 只在 L3 决策点工作
2. **Source of Truth 在数据库(SQLite/Postgres)**,飞书表只是 view
3. **询盘必须半自动**——AI 起草,人审,影刀点发

---

## 3. 数据源决策(实证后版本)

| 平台/数据 | 主路径 | Fallback | 实证 |
|---|---|---|---|
| **noon UAE/SA 列表+详情+徽章** | Firecrawl `/scrape` (basic, 1 credit/页) | — | ✅ 2026-05-05 verified |
| **Amazon UAE/SA 全量(列表/详情/评论/销量/BSR/上架时间)** | **Sorftime MCP**(试用,已实证)→ Sorftime API(转正) | Firecrawl `/scrape` `proxy:"stealth"`(仅徽章+bullets,**评论拿不到**) | ✅ 2026-05-06 实证:56 工具,SA/AE 支持,product_detail/product_reviews/product_trend/product_variations/similar_product_feature/ali1688_similar_product 全部跑通 |
| **1688 文字搜+详情+评论+图搜上传** | **本地 Playwright + Luke 登录态**(独立 Chrome profile,GBK keyword 编码) | — | ✅ 列表/详情验证 |
| 千牛/旺旺询盘 | 影刀 RPA + 人审后点发 | — | 合规死结无 API |
| 微信供应商沟通 | 企业微信外部联系人(日均 1/客户硬限) | 邮件 | 个微自动化有 2025 判例风险 |

### 关键 caveat

- **1688 keyword 编码必须 GBK**:`urllib.parse.quote(kw.encode('gbk'))`,UTF-8 会 mojibake 成 `?`
- **Firecrawl Amazon 评论拿不到**(详情页 + `/product-reviews/` 都被登录墙挡)——这就是切 softtime 的核心理由
- **Cookie banner 噪音**:noon markdown 前面有 cookie 段,normalizer 过滤
- **本地 Playwright 反爬节奏**:list 30-60s / 翻页 5-10s / 详情 2-5s / 连续 3 次失败暂停 10 分钟 / 触发 punish URL 立即停
- **登录态续期**:Luke 第一次扫码后 Playwright 复用 `user_data_dir`;每周日 health check,失效发飞书提醒

### 死路(不要走)

- ❌ Amazon SP-API 拿竞品销量(只能看自家店)
- ❌ Amazon PA-API 给冷启动账号(30 天 10 笔合规销售门槛)
- ❌ 阿里云 Image Search 直接接 1688(要自建图库)
- ❌ 个人微信自动化(itchat 已废,wechaty 有判例风险)
- ❌ 千牛 outbound API 给买家发首条询盘(没这接口)
- ❌ 1688 keyword 用 UTF-8

---

## 4. 双 Pipeline 架构(核心创新点)

```
┌─ T1 月度自动 ─────────────────────────────────┐
│  从【店铺销售类目】展开成 N 个流量词           │
│  数据源: 产品表.xlsx + hipop.db.wf2_*         │
└──────────────────┬─────────────────────────────┘
                   │
   ┌───── T2/T3 用户输入 ────────────────────┐
   │  流量词 / 类目                           │
   └───────────┬─────────────────────────────┘
               │
               ▼
┌══════════════════════════════════════════════════════════════════════┐
║  N1 流量词扩展 (中→英/阿,组合特点词)                                  ║
╚══════════════════════════════════════════════════════════════════════╝
               │
       ┌───────┴───────────────┐
       ▼                       ▼
┌──── Demand Pipeline ────┐  ┌──── Supply Pipeline ──────┐
│ N2-D 多平台抓取         │  │ N7a 1688 一次搜(流量词)    │
│  noon → Firecrawl       │  │       本地 Playwright + 登录│
│  Amazon → softtime MCP  │  │ N7b 早期短路(列表价 ×0.9   │
│ N3 垄断/政策/退货风险过滤│  │     超采购上限直接跳过)     │
│ N4 价格分桶+三重检查    │  │ N7c 评分<4 剔/新鲜度<4mo   │
│ N5 多维销量综合判断     │  │     /刷屏识别              │
│   (BSR+上架时间+评论增  │  │ N8 颜值+功能差异化挖掘     │
│    速+真实销量,非徽章)  │  │                            │
│ N6 卖点抽取(10 stage,   │  │ === Supply Brief (产物) ===│
│   多模态读图,见 §5)     │  │   候选 SKU 池/采购价段/    │
│ === Demand Brief (产物)═│  │   质量信号/差异化亮点/     │
│   价格带分布/Top-K/特点词│  │   评论 SKU 命中            │
│   云/起量品/垄断判断    │  │                            │
└────────────┬────────────┘  └────────────┬───────────────┘
             │                            ▲
             │ Demand Brief.特点词 反向输入│
             └────────► N7b 二次搜 ────────┘
                       "流量词 + 特点词" 组合多轮
                       (§A 步骤 6/8 的核心动作)
             │                            │
             └──────────────┬─────────────┘
                            ▼
┌══════════════ Inventory & Costing ═══════════════════════════════════┐
║ N9 库存反向约束 (反向修改 N4 价格带 / N7 尺寸过滤 / N8 功能必选)      ║
║    定位: 不是选最赚钱,是消化既有库存劣势 (§A 步骤 9)                  ║
║ N10 利润核算 + 黄牌机制 (≥20% OK / 10-20% 黄牌 / <10% kill)          ║
║     国家分账: UAE VAT 5% / SA VAT 15% / 头程系数分国 / 阶梯佣金       ║
╚══════════════════════════════════════════════════════════════════════╝
                            ▼
┌══════════════ Merge & Deliver ═══════════════════════════════════════╗
║ N11 审美 rerank (user_vector cosine + LLM judge,后期接 SigLIP)       ║
║ N12 询盘草稿 (模板话术 + 加大版头程判断 + 评论实物图前置) ← 人审      ║
║ N13 选品文档生成 (md + json + 飞书同步)                              ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 选品文档产出三件套

```
batches/<batch_id>/
├── 选品报告.md          # 人读 (Luke 周一看一眼能拍板)
│   1. 元信息 + 数据覆盖度声明
│   2. 价格带分布
│   3. 销量多维信号分布 (tier + rising/steady/declining)
│   4. 特征词云 (高销 vs 一般 + 按 tier 加权)
│   5. 颜色/材质/尺寸维度
│   6. 品牌格局 + 垄断判断
│   7. Top-K 深度档案
│   8. 给 N7b 二次搜的关键词包
│   9. 选品 hypothesis (为什么这些卖好)
│  10. 候选品 Top 5-6 (含黄牌)
│  11. 询盘草稿
├── candidates.jsonl    # 机读: ProductRecord 列表
├── brief.json          # 机读: 特征词云/分布/hypothesis
└── feishu_sync.json    # 同步到飞书表的 payload
```

参考样本: `refs/sample_demand_brief_luggage_amazonsa_2026-05-06.md`

---

## 5. N5 多维销量判断(softtime 时代的核心升级)

⚠️ **范式变更**:不再靠徽章筛"高销品"。徽章是 Amazon 的一维粗信号,有滞后和噪音。

softtime 数据下,N5 综合 5 个维度:

```python
def classify(s: SalesSignal) -> tuple[str, bool, bool, bool]:
    # 1. 销量真值排序 (BSR 是相对排名,符合"相对值不是绝对值"原则)
    if s.bsr_current <= 50:    tier = 'top'
    elif s.bsr_current <= 200: tier = 'high'
    elif s.bsr_current <= 1000: tier = 'mid'
    else: tier = 'low'

    # 2. 起量品三条件齐 (§A 步骤 4: "一点点冒头的销量值得关注")
    is_rising = (
        s.bsr_trend == 'rising'              # 排名近期上升
        and s.review_velocity_rising          # 评论增速也上升
        and s.listing_age_days < 180          # 上架不久(<6mo)
    )

    # 3. 稳定经营品 (老品但销量稳)
    is_steady = (
        s.listing_age_days > 365
        and s.bsr_trend == 'stable'
        and s.rating_trend_30d >= 0
        and tier in ('top', 'high')
    )

    # 4. 衰退品
    is_declining = (
        s.bsr_trend == 'declining'
        and s.rating_trend_30d < -0.2
    )

    return tier, is_rising, is_steady, is_declining
```

**硬规则**:
- 决策只看 `tier` + `is_rising/steady/declining`,**禁止用 raw 数字做绝对阈值**(`sold>=30` 这种代码直接打回)
- `tier='low' + is_rising=True` 仍要进观察池(起量新品)
- Firecrawl 降级路径(softtime 故障/字段缺)只有徽章 → 退化到一维 tier_guess,confidence 标 0.4-0.6

### N5 输出动作

- 按价格归堆儿(§A 步骤 4 末)→ Top-K × 价格带矩阵
- 标记每个商品的 (tier, rising, steady, declining)
- 喂下游 N6

---

## 6. N6 卖点抽取(10 阶段细分)

| Stage | 做什么 | 数据来源 |
|---|---|---|
| S1 数据收齐 | 列表 ASIN + 详情 bullets/spec/SKU 变体 + 评论文本/时间戳 + softtime 销量历史 | softtime MCP + Firecrawl + Playwright |
| S2 结构化文本抽取 | 标题级 regex(material/size/pack/color) + Bullets dictionary + Spec 表 + 变体组合 | 确定性 |
| **S3 图像理解(多模态)** | 主图风格归类(莫兰迪/工业/卡其) + 详情图卖点抽取(拓展层/前置开口/咖啡杯架 可视化功能) + 实拍 vs 渲染判定 | LLM 多模态 (§A 步骤 5: "很多详情用图片制作,要有读图能力") |
| **S4 评论挖掘** | Top 优点 cluster / Top 槽点 cluster / 时间戳分布判新品 / 刷屏识别 / 买家秀实物图 | softtime MCP (主) / Playwright (1688) |
| S5 跨 SKU 同款聚合 | 颜色变体合并 / 高销 SKU + 高评论 SKU 识别 / 评论按变体归并 | 确定性 |
| S6 销量归一(相对值) | percentile/tier 切档 + rising/steady/declining 三态 + 跨平台互校验 | 见 §5 |
| S7 跨商品聚合 | 高销组 vs 一般组频次对比 / 价格带 × 特征矩阵 / 品牌格局 | 确定性 |
| S8 假设生成(LLM) | "为什么卖好"候选解释 + 跟国家/品类常识库交叉验证 + 起量品新趋势识别 | LLM prompt |
| S9 差异化机会识别 | 高销组共性 vs 供给端空白 / 给 N7b 的特点词组合 | LLM + 常识库 |
| S10 Brief 产出 | md 人读 + JSON 机读 + 飞书 sync 落库 | 模板 + DB |

完整方法论参考样本: `refs/sample_demand_brief_luggage_amazonsa_2026-05-06.md`

---

## 7. ProductRecord & SalesSignal Schema

```python
@dataclass
class SalesSignal:
    """多维销量信号. softtime 直出 + Firecrawl 降级 fallback.
    决策层只看 tier / is_rising / is_steady / is_declining,
    禁用 raw 字段做绝对阈值过滤."""

    # ===== softtime 直出 (主路径,Amazon) =====
    monthly_units_sold: Optional[int]
    monthly_revenue: Optional[float]
    bsr_current: Optional[int]
    bsr_30d_avg: Optional[int]
    bsr_trend: Optional[Literal['rising','stable','declining']]
    listing_age_days: Optional[int]
    review_count_total: Optional[int]
    review_count_30d: Optional[int]
    review_velocity_rising: Optional[bool]
    avg_rating: Optional[float]
    rating_trend_30d: Optional[float]
    price_history_30d: Optional[list]

    # ===== Firecrawl 降级 (noon 主路径,Amazon fallback) =====
    raw_badges: list[str]                  # ['Best Seller', 'Selling out fast', '30+ sold recently'] 等
    raw_review_count: Optional[int]        # 列表页 (X,XXX) 括号里
    raw_rating: Optional[float]            # "4.3 out of 5"

    # ===== 综合分类 (决策层只看这几个) =====
    tier: Literal['top','high','mid','low','unknown']
    is_rising: bool
    is_steady: bool
    is_declining: bool

    # ===== 元数据 =====
    source: Literal['softtime_mcp','softtime_api','firecrawl_badges','noon_badges','alibaba_native']
    confidence: float          # softtime 0.9+ / firecrawl 0.4-0.6 / unknown 0.2
    fetched_at: datetime

@dataclass
class SKU:
    spec_axes: dict           # {'size': '24in', 'material': 'ABS+PC', 'color': '莫兰迪绿'}
    price: float
    currency: str
    stock_signal: Optional[str]
    review_count: Optional[int]    # 该 SKU 评论数 → "高评论 SKU" 识别
    sold_count: Optional[int]      # 该 SKU 销量 → "高销 SKU" 识别
    is_oversize: bool = False      # 加大版 SKU → N12 头程翻倍判断

@dataclass
class ReturnRisk:
    """§A 步骤 7. 高客单 + 多配件 + 分体设计 = 高风险."""
    level: Literal['low', 'mid', 'high']
    reasons: list[str]
    suggestion: Optional[str]

@dataclass
class ProductRecord:
    id: str                        # f"{platform}:{platform_id}"
    platform: Literal['noon_ae','noon_sa','amazon_ae','amazon_sa','alibaba_1688']
    url: str
    title: str
    brand: Optional[str]
    category_path: list[str]
    images: list[str]
    image_embeddings: dict          # 后期填
    price: dict                     # {value, currency, original, discount}
    skus: list[SKU]
    sales_signal: SalesSignal
    reviews: dict                   # {avg, count, recent_distribution, salient_phrases,
                                    #  real_photos, sku_mentions, is_spam_pattern}
    inferred_features: list[str]    # LLM 抽出的卖点词
    shipping: dict                  # {package_dims, weight, can_nest, is_oversize_overall}
    policy_flags: dict              # {brand_risk, brand_mindshare, compliance_per_platform}
    return_risk: Optional[ReturnRisk]
    market_meta: dict               # {vat_rate, commission_rate, listing_fee, ...}
    fetched_at: datetime
    source_path: str                # 'softtime_mcp_v1' / 'firecrawl_basic' / '1688_playwright'
```

---

## 8. 知识库 seed (YAML,在 §A know-how 基础上预填)

`l2_knowledge/constraint_db/`:

```
categories/
  luggage.yaml      # 材质硬度 PC<ABS+PC<ABS / 高销 20-24"/莫兰迪色 /
                    # 拓展层(既是卖点又可套装省物流) / 前置开口 / 充电口 / 咖啡杯架 / 弹簧承重轮
  chair.yaml        # 面料硬度 猫抓皮>PU>科技布>灯芯绒 / 拒灯芯绒(中东天气) /
                    # 全海绵+公仔棉(乳胶虚高) / 钢质脚 / 带脚踏 /
                    # 高客单不分体(退货风险) / 椅子必须先到自有仓质检
  stroller.yaml     # 必备词: 高景观/轻量化/遛娃神器/可折叠 /
                    # 铝合金>碳钢(宝妈减重) / 双向推把 / 双轮稳定 /
                    # 横躺当婴儿床 / 一键折叠 / 一体折叠优先(分体退货丢配件)
countries/
  noon.yaml         # VAT: ae 5% / sa 15% / 偏好白黑绿(沙特国旗色) /
                    # 包容度最高(仿大牌可上,玩具枪可上) /
                    # 半托管同品类前后价段差不能 >1.5×
  amazon_me.yaml    # 中等政策容忍 / Sponsored 列表必过滤 / SP-API 不可用于竞品
  _global.yaml      # 全平台禁: LV / 乐高小人偶 / 严格平台: Shein / Tiktok
pricing_table.yaml  # 国家×类目佣金/VAT/头程系数
                    # 主源: refs/noon_uae_fbn_fees_2025-09-01.md (已抓存)
                    # 补源: 项目根 定价表.xlsx (Luke 自整,可能含 KSA/半托管)
```

---

## 9. 学习闭环 (preferences.jsonl)

每个事件写入流水:

```json
{
  "event_id": "...",
  "ts": "2026-05-04T10:00:00Z",
  "stage": "candidate_review|inquiry|sample_qc|post_launch",
  "product_ref": "amazon_sa:B07P4ZVNZQ",
  "context": {"trigger": "monthly|keyword|info_flow", "batch_id": "..."},
  "action": "accept|reject|hold|evaluate",
  "reason_tags": ["材质_灯芯绒", "退货风险高", "极致性价比", "审美_配色丑"],
  "reason_text": "中东天气热,灯芯绒不透气",
  "score": 8,
  "agent_predicted": {"rank": 3, "confidence": 0.78, "reasons": ["..."]},
  "outcome": null
}
```

**学习节奏**:
- 0-30 事件: 硬约束库 + 冷启动问卷
- 30-300: 自动 user_vector + LLM rerank 注入 5 正 5 负 few-shot
- 300-1000: 拒绝理由聚类 → 提议入硬约束库(用户审过加入)
- 1000+ 且 30+ 上架回溯: LoRA SFT;2000+ 偏好对: DPO

**两个自我纠错**:
1. 预测对账: 每个候选池产出时记 agent 排名+置信度,N 周后对照实际决策画校准曲线
2. 拒绝理由抽象层级判定: 避免过度泛化("拒了一款蓝色"≠"以后蓝色都不要")

---

## 10. 反卷规则(与利润 20% 红线并列)

**主原则**: 销量越好的款越要警惕(§A 总原则)

1. **极致性价比检测**: 1688 同款最低采购 + 头程 + VAT > 当前在售款 70% 价位 → 标"已极致性价比,仅观察",不进候选池
2. **同款检测**: SigLIP cosine >0.92 且无显著功能升级 → 标"同款回避"
3. **趋势走势**: 新子类必须看月销趋势,下行品类降权
4. **黄牌机制**: 利润<20% 不硬删,标 PROFIT_LOW_BUT_VALUABLE + LLM 生成"为什么没扔"理由,人审决定

---

## 11. 工程约束(不能违背)

1. **询盘必须半自动**: AI 起草 + 人审 + 影刀点发
2. **利润<20% 是黄牌不是淘汰**: PROFIT_LOW_BUT_VALUABLE
3. **拒绝理由必填**: 枚举 + 自由文本,没这个就没差异化资产
4. **Browser Worker 不调 LLM**: 确定性数据采集,LLM 只在 L3 节点
5. **Chrome profile 物理隔离**: 1688 自动化用独立 profile,不能和 Luke 日常采购共用
6. **Source of Truth 在数据库**: 飞书表只是 view
7. **相对值不是绝对值** (§A 步骤 4): 决策只看 tier/percentile,raw 数字只是归一层输入

---

## 12. 目录结构

```
点购_选品/
  l0_data/
    softtime_mcp_client.py     # Amazon 主路径 (MCP 试用 / API 转正)
    firecrawl_client.py        # noon 主路径 + Amazon fallback
    fetchers/
      noon_fetcher.py
      amazon_fetcher.py        # 优先调 softtime, 缺数据降级 Firecrawl
    browser_worker/            # 本地 Playwright + Luke 1688 登录态
      alibaba_session.py       # 持久化 Chrome profile, 登录健康检查
      alibaba_list_fetcher.py  # GBK encoding
      alibaba_detail_fetcher.py
      alibaba_review_fetcher.py
      alibaba_image_search.py
  l1_normalize/
    product_record.py
    cross_platform_id.py
  l2_knowledge/
    aesthetic_profile.py
    constraint_db/             # YAML, 见 §8
    pricing_table.yaml
    shop_categories.py         # T1 月度任务输入: 拉店铺已上架类目
    category_history.py        # 历史已上架利润率 (N10 对账)
    inventory_sync.py          # 自家仓库存 (N9 反向约束)
    history_loader.py          # 冷启动种子: Luke 历史选品成功品
  l3_orchestration/
    nodes/                     # N1-N13 每节点一个文件
    pipeline.py
    brief_generator.py         # 10-stage N6 → 选品报告
  l4_delivery/
    feishu_writer.py
    inquiry_template.py
    workstation_api.py         # 给前端工作台的 HTTP 接口
  shared/
    embeddings.py              # SigLIP wrapper (后期)
    llm_client.py
    db.py                      # SQLite 起步
  cli/
    select.py
  preferences.jsonl
  batches/                     # 每次选品产出 (md+json+payload)
```

---

## 13. 阶段任务(接手就做,按顺序)

### Step 0:环境对账(5 分钟)

跟 Luke 确认(不要假设):
- [x] Firecrawl key (`fc-...8e3` 已在凭证)
- [ ] **softtime MCP 凭证**(Server URL + 试用 token + 工具列表/文档)
- [ ] 1688 第一次扫码登录到 `/Users/luke/.chrome-profiles/agent-1688/`
- [x] 飞书 App `cli_a96a395aaafa5cb5` (App Secret 已在凭证, `bitable:app:table:record:edit` 已有)
- [ ] 候选池写入的飞书 Base / Table ID (Luke 建表后给)

**自查既有项目(不要再问 Luke)**:
- 历史选品记录 → `产品表.xlsx` + `hipop.db.wf2_*` + `selector/` 子目录
- 店铺销售类目 → `产品表.xlsx` 类目列 + hipop.db SKU 表
- 历史利润率 → `定价表.xlsx` + `HIPOP-补货总表-2026.4.16.xlsx` + `hipop.db`
- noon UAE 佣金 → `refs/noon_uae_fbn_fees_2025-09-01.md` (已抓存) + 项目根 `定价表.xlsx` 互校

### Step 1:项目骨架 + 知识库 seed (1 天)

1. 建 `点购_选品/` 目录(§12)
2. 写 `product_record.py` (ProductRecord/SalesSignal/SKU/ReturnRisk 全套,见 §7)
3. 写 `db.py` (SQLite,schema 对应 ProductRecord)
4. 写 `firecrawl_client.py` (重试/429 退避/credits 计数)
5. **把 §A know-how 显式预填 YAML**(§8 列出的所有 categories/* 和 countries/*)
6. 写 `shop_categories.py` + `category_history.py` 占位,Step 0 自查结果接进来
7. Git init + 第一个 commit

### Step 2:1688 fetcher (1 天)

本地 Playwright + Luke 登录态。**评论 must-have**(评分<4 剔/槽点+优点/时间戳判新品/刷屏识别/买家秀实物图)。

1. `alibaba_session.py` (持久化 profile + 健康检查 + 失效飞书提醒)
2. `alibaba_list_fetcher.py` (GBK keyword + 双轮搜:流量词 / 流量词+特点词 + 早期短路 §A tip 1)
3. `alibaba_detail_fetcher.py` (SKU 阶梯价/规格/材质/颜色/图 + 加大版标记)
4. `alibaba_review_fetcher.py` (分页 + 时间戳 + 评分 + SKU 提及 + 买家秀图 + 刷屏识别)
5. CLI: `python cli/select.py --platform 1688 --keyword 行李箱`

### Step 3:noon fetcher (半天)

`https://www.noon.com/{site}/search/?q={keyword}` ({site} = `uae-en`/`saudi-en`)。Firecrawl basic 默认参数。

- 流量词用英语(部分阿语回退)
- parse badges + price + rating + review_count + category_rank
- **没销量徽章 → tier='low' + type='unknown'** (§A 步骤 4)
- market_meta.vat_rate: ae=0.05 / sa=0.15

### Step 4:Amazon ME fetcher via softtime MCP (1 天)

**主路径 softtime MCP**(等凭证到位即可开工):
- Step 0 拿到 MCP server URL + token 后,先 5 分钟联通测 + 拿 tools schema
- 写 `softtime_mcp_client.py`,包一层调用,统一返回到 SalesSignal 多维字段
- 用 wf1 已跑的 25 一档 + 50 二档 ASIN 做实证对比(对照 sample brief)

**Fallback path: Firecrawl stealth**(softtime 故障/字段缺时降级):
- `https://www.amazon.ae/s?k={keyword}` / `.sa/...`
- `proxy: "stealth"` (5 credits/页),否则 503
- 只拿徽章 + bullets + 评分 + 评论数(评论文本拿不到,见 §3 caveat)
- 过滤 `Sponsored`

### Step 5:第一次端到端跑通 (半天)

`python cli/select.py --keyword 行李箱 --markets 1688,noon-ae,noon-sa,amazon-ae,amazon-sa` → 多平台并行抓 → 归一 → N3-N5 跑通 → N6 出 Demand Brief → 写 SQLite + 飞书表。

**漏斗目标**: 召回 ~600 → N3-N5 后 ~200 → 候选池 5-6 个

### Step 6:让 Luke 看候选池

发飞书群 @Luke,他在表里挑/拒,**必填理由** → `preferences.jsonl`(冷启动种子)

### Step 7:PoC 评估点

- 数据质量(softtime 字段密度 / 噪音 / 与 Firecrawl 互校验)
- 候选池命中率(Luke 看了愿意往下询盘的比例)
- credits & MCP 调用量是否在预算内
- 决定下阶段:进 MVP(N6 LLM 完整跑/N7b 二次搜/N9 库存反向/N10 利润+黄牌/N11 审美/N12 询盘草稿) 还是先补 S3/S4 数据

### 不要做的事(节省时间)

- 不要重新调研数据源(§3 已实证)
- 不要让 LLM 进浏览器点击循环
- 不要先写前端工作台(数据底座未通时前端没意义)
- 不要先做 SigLIP 审美建模(要 30+ 选品事件之后才有数据)
- 不要规划"全自动询盘"(合规死结,必须半自动)
- 不要把 1688 自动化和 Luke 日常 Chrome 共用 profile

---

## 14. 与 OpenClaw 衔接

- 不要碰 `~/.openclaw/openclaw.json` 的 channel/binding
- selector/detailer/buyer agent workspace 在 `~/.openclaw/workspace-<id>/`
- 新写的 skill 放 agent workspace 的 `skills/` 下,**不能软链要 `cp -R`**
- skill 调本服务用 curl HTTP(参考 `openclaw-skill/feishu-pull.md` 的写法)
- skill 输出格式按 channel 分支: 飞书可 markdown,微信只能纯文本+裸 URL(OpenClaw memory 第 33 条)
- 部署细节必读: `~/.claude/projects/-Users-luke/memory/project_openclaw_setup.md`

---

## 15. 凭证与关键文件

| 文件 | 用途 |
|---|---|
| `点购_选品_凭证.md`(同目录) | 所有凭证 + 自查指南 |
| `refs/noon_uae_fbn_fees_2025-09-01.md` | noon UAE FBN 官方费率表(佣金/头程/仓储/退货/增值) |
| `refs/sample_demand_brief_luggage_amazonsa_2026-05-06.md` | Demand Brief 实样,N6 产物格式参考 |
| `产品表.xlsx`(项目根) | 历史已上架商品 = 选品成功样本(冷启动种子) |
| `定价表.xlsx`(项目根) | Luke 整理的定价表(可能含 KSA/半托管补充) |
| `hipop.db`(项目根) | SKU/销量历史数据 |
| `selector/`(项目根) | 已有 selector agent 资产,先读 README |
| `openclaw-skill/`(项目根) | 多个 skill 范例,模仿写新 skill |

---

## 用法

终端 Claude Code 第一句:

> 读 `/Users/luke/code/hipop/点购_选品Agent_工程交底.md`,先把 §A 业务原文从头到尾读一遍,再回看 §1-§13,从 Step 0 开始。凭证在同目录 `点购_选品_凭证.md`。

任何架构层疑问回 Luke 这边的对话。代码层遇到的事实冲突,**信你看到的事实**,回报给 Luke 同步更新本文档。

---

## §A 业务原文(最高保真度)

> 以下是 Luke 本人的完整描述。**所有判断以这里为准**,前面章节是提炼。冲突时以此为准并回头更新对应章节。

---

选品整体基于店铺销售类目进行定期持续发现(如一个月选三次,每次目标是选出 5-6 个可以采购拿样的品,候选池就需要更大才行),也可以通过人工输入流量词或者品类/类目发起选品流程。

### 一、需求端调研

**1. 竞品情况收集**:通过销售国家的各大电商平台了解竞品信息,通过通过插件如 softtime/卖家精灵等应用,获取目标品类/类目下的竞品的销售情况(softtime 的估算偏高,可以回调个 10%),做法是用一个流量词在电商平台上进行搜索(通常是一个品类词,比如目标过语言/英文的"婴儿推车"、"boss 椅"、"行李箱"),搜索结果页会把相关的竞品链接召回展示,一般关注前两页的产品,因为搜索排序越好的越靠前,不能只看一个平台,要多看几个当地国家的目标电商平台(如中东市场,要看 noon 和 Amazon 中东站,国内可能要看下淘宝、拼多多)。

**2. 查看垄断性和判断进入门槛**:利用插件判断流量词/类目下产品的销量分布是否具有垄断性,看销量是否过度集中于某几个商家或者是否该类目已有品牌心智(如乐高直接定义了积木这个品类),来判断进入门槛,越垄断越有品牌独占性,进入门槛越高。注意,看的是有没有"超级大平台的超级垄断",前十集中是正常的,不算垄断。

**3. 计算品类当前市场价格分布,判断自己用户消费价格是否匹配**:明确自己店铺相关品类的价格带,通过查看搜索结果页中的各个产品的价格情况,获取该类目/流量词下产品的价格分布情况,别忘了结合商品的 SKU 情况(有的是一件装,有的是多件装;有的是材质不同价格不同;有的是容量不同价格不同)综合判断是否适合自己店铺目前买家在当前品类的价格带接受度,以及可以尝试的价格带大致情况。这里对 noon 平台的半托管价格做一个特别说明:半托管选品时在同品类上架的前后品价格差异不宜过大(之前是 80 美金 的款式,现在是 300 的款式,审核上架可能不通过)。

**4. 通过竞品销量情况判断当前类目/流量词的目标竞品**:对于 noon 平台,一般看商品卡片上循环滚动的小标签,里面有销量情况的字段,可以判断销售情况的好坏(如最好的是 selling-out recently,或者明确的销售数量,这个数量可以理解为该商品近 30 天的总销量情况,或者其他可以体现销量的字段,如有一点点冒头的销量,可能是一个正在起量的新品,值得关注。但如果没有销量相关字段,说明销售情况不好。因为数据是有加载时间或者 UI 滚动,有时候需要等等数据显示)。在 Amazon 那个平台上,商品卡片一般有近期销量情况,或者通过插件获取数据。销量好的商品记录下来,可以进一步观察商品情况。同时也把这些好销量品按照价格归堆儿。**注意好销量不是绝对值,是竞争视角下的相对值**。

**5. 目标竞品的高销量原因总结选品的基本方向**:选出销量好/正在冒头的商品,可以查看商品详情,通过标题、商品详情、主图、评论、高销售 SKU、高评论 SKU 等,找到这些高销售商品的特色。比如"行李箱"这个类目经常提及材质,其中铝框和另外一种材质的箱体更结实;通过高销尺寸,其中 20 寸箱和 24 寸箱更好卖;近期起量的颜色,其中莫兰迪色更好卖。比如"boss 椅"都提及了材质,其中猫抓皮更好卖;比如"婴儿车",标题和评论都大面积出现了"高景观"、"轻量化"、"遛娃神器"等功能和场景特点,还有些个性化功能设计被反复提及,比如"可当做婴儿床"、"遮阳"、"一键折叠"。**很多商品详情是用图片制作,所以需要有读图的能力**。

### 二、供给端匹配与反向选品

可以通过需求端竞品找供给,也可以从供给端发现新品。

**6. 在 1688 或者微信等渠道上了解竞品和现货情况**:用 1688 一个重要原因是确保供给端是有现货的。通过流量词或者流量词结合竞品高销量原因总结出的特点词,在平台进行搜索找到潜在供给候选,也可以直接拿需求端整理好的竞品图片进行图片搜索:通过商品的标题、商品详情页和评论的实物图、评分和评语判断商品的基本质量,评分过低(4 分以下绝对低分)或者评论中反复出现的吐槽体现了商品的硬伤,需要告警规避(辨别商品评论的真实性,是不是刷屏,刷屏的评论是噪音);通过销量和评论情况判断商品的真实性和供应时间是否为近期新品(通过评论的时间戳、展示销量综合判断供给是否新品,要排除掉老商品,因为越新的东西,竞争对手越少);通过对应 SKU 的价格先判断个基本的采购成本价格(展示价格虚高,找到目标 SKU 看价格,一般可以在展示价格上打个 9 折粗估下采购成本价,具体价格要等询盘议价确认);通过商品情况和价格,未来结合综合成本和目标定价判断采购可行性(碳钢、铝合金,对应轻量化,肯定选铝合金,再结合价格看下性价比)。
- **tips 1**:一般 1688 平台上搜索后的商品列表页展示的商品价格是里面 SKU 最低的价格,如果通过利润空间判断成本价(也就是列表展示价)过高,就先不用点进去看了。
- **tips 2**:以图搜索一般是同品找低价。

**7. 销售国家和商品的背景知识与 agent 长期审美形成**:"给 agent 过往所有成功品 → 它会学你的知识与审美 → 模拟你",一些例子:
- 中东偏好白/黑/绿(沙特国旗色直接加分)
- 沙特天气炎热 → 灯芯绒面料直接 pass(不透气)
- **沙特增值税 15% vs 阿联酋 5%** → 同款在两个市场定价差很多
- 椅子的面料硬度排序:猫抓皮 > PU > 科技布 > 灯芯绒
- 行李箱材质硬度:PC(最软) < ABS(最硬),ABS+PC 复合偏硬
- 退货风险作为选品筛选维度:比如"分体可拆装婴儿车"——高客单价 + 多配件的产品,一旦退货容易丢配件,损失大。所以优先选一体折叠款,分体款保留观望。
- 平台政策维度:noon 包容度最高(仿大牌可上、玩具枪可上),shein/tiktok 严,LV/乐高小人偶在哪都绝对禁碰。

### 三、差异化筛选

**8. 在供给端找到一些差异化点**:通过流量词或者流量词结合竞品高销量原因总结出的特点词,在平台进行搜索找到潜在供给候选的同时,可以关注供给端提供的差异化点,**重点还是在颜值和功能上**。比如"行李箱"发现供给端的高销量品有个商品特色是具备"拓展层"(**拓展层既可以作为商品新卖点,又可以跟其他商品嵌套在一起发货,降低物流费用**),或者发现更多功能性(有放手机和小东西的空间、咖啡杯架、万向轮及弹簧),或者可以通过目标商品的 SKU 发现其他可选变体情况:通过查看商品详情的 SKU 情况,可以总结和找到灵感,比如"boss 椅"在需求侧上看到猫抓皮高销量,但是在供给端上又发现有纯皮卖的比猫抓皮更好,也可以进入供给候选池;**还可以通过评论提及 SKU 和 SKU 销量分布跟需求端交叉验证可以看下哪些特色卖的更好**。

**9. 库存反向约束**:**不是为了"选最赚钱的品",是为了消化既有库存劣势去选什么品**。它会反向约束你前面所有步骤:需求端的尺寸偏好、供给端的尺寸过滤、差异化筛选的功能必选。(比如国内仓 20 寸还有 30 个 → 本期只看 24 寸 + 拓展层(用拓展层套走 20 寸))

### 四、成本与定价核算

**10. 初步核算利润空间**:结合需求端的售价、供给端的采购价预估、结合货物的包装尺寸粗估头程运费、销售平台的佣金比例(noon 有各个类目的佣金表),通过已有的常识公式-定价表判断利润空间,算出初步利润率,是否合理看下目前该品类过往的利润率做个参考,但最终一般不能低于 20%。

> 注:Luke 后续追加确认——利润<20% 改为软告警 + 人工覆盖(黄牌机制 PROFIT_LOW_BUT_VALUABLE),不是硬淘汰。

### 五、询盘 & 精算确认最终选品池

把潜在供给候选池准备进入询盘进一步确认详细信息、拿样和改款可能性。

**11. 在 1688 上通过旺旺跟卖家询盘**了解:
- 可能的采购底价
- 商品具体的材质情况(比如 boss 椅的面料和内料都有什么;比如 boss 椅的商品详情展示了乳胶材质,那乳胶含量有多少)
- 跟商家要实物图(**也可以通过评论中的图片找到的商品在现实生活中的真实样子,这是售后隐患减少的保障**)
- 了解包装是否齐全,包装是什么尺寸(用来评估准确头程运费。**尤其是 SKU 可能有加大版,如果遇到加大版,头程费用没有增加多少,那就可以考虑,但是如果运费增加一倍,难以接受,因为售价会涨更多,买家可能无法接受**)
- 商品是否支持拿样和拿样后改进

以上都完成后,利润空间、产品质量、款式和功能性综合排序后,可以开始选择是否拿样和改款。为后续采购上架做的选品环节就基本完成了。

### 总原则(必须显式贯穿到 agent 决策)

**一定要注意避免进入"无脑跟卖"的卷价格怪圈:销量越好的款越要警惕,不要因为它销量好就跟,如果利润空间算下来不足或者竞品已经极致性价比了,那跟进选品和采购只能是陪跑。**

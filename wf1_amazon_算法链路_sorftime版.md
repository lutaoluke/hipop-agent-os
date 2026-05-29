# wf1 · Amazon 算法链路 (Sorftime 版)

> v2 vs v1 关键变化(读这一段就懂):
> - **Step 2**: Firecrawl scrape 徽章 → **Sorftime `product_search`**(直接拿真销量结构化对象,零解析负担,零 Sponsored 过滤)
> - **Step 6 N5**: 徽章映射(一维稀疏,SA 站 234 个 ASIN 只有 9 个有徽章) → **多维综合判断**(近期销量 + 上架时间 + 最早评价时间 + 销量曲线 + BSR 趋势)
> - **Step 7 N5.6**: 全 singleton 死路 → 用 **`product_variations`** 拿真子体,**高销 SKU / 高评论 SKU 终于可识别**
> - **Step 8 N6/N6.5**: 跳过 → 完整 10 stages 都能跑(Sorftime 给齐 detail/reviews/trend)
> - **Step 9 N11 v2**: 算法不变,但输入精度从"徽章猜"升到"真销量数字",**sales_pct / rising / feature_pct 三维全部从弱信号变强信号**
> - **Step 10 demand brief**: §A 步骤 5 终于能完整闭环——评论 / SKU 变体 / 销量历史 / 最早评价时间分布 都齐了

---

## 0. 输入

```
keyword="luggage"
country="ksa"   (Sorftime amzSite="SA")
category="luggage"
yaml: l2_knowledge/constraint_db/categories/luggage.yaml
yaml: l2_knowledge/constraint_db/countries/amazon_me.yaml
```

---

## Step 1. N1 keyword_expansion (unchanged)

```
luggage → ['luggage', 'luggage set', 'carry on luggage', 'suitcase',
           'trolley bag', 'cabin luggage', 'hardside luggage']
```

通用算法: 从 `categories/luggage.yaml.search_keywords` 读 7 个变体。

---

## Step 2. amazon_fetcher.search × 7 关键词 — **全面 Sorftime 化**

### v1 (废弃)

```
fetcher: Firecrawl /scrape × 7, proxy=stealth, waitFor=5s, 5 credits/页
parser:  正则抓 ASIN / 色变体跳过 / Sponsored 过滤 / 徽章字符串匹配
入库:    234 unique ASIN,但徽章稀(9/234 = 4%),N5 大多 tier='low'
```

### v2 (Sorftime)

```
对每个流量词 kw,调:
  product_search({
    "searchName": kw,
    "amzSite": "SA",
    "page": 1   # 50 条/页,可翻页
  })

Sorftime 返回的每条对象已包含:
  ASIN / parent_asin / title / brand / image
  price / rating / review_count
  monthly_sales_volume   ← 真月销,不是徽章
  monthly_revenue
  cat_rank (在所属细分类目的排名)
  listing_age_days
  FBA fee
  category_path

跨 7 个关键词去重(用 parent_asin 而非 ASIN,避免色变体重复)→ 入库 ~250 父 ASIN
```

**省掉的工程负担**(对照 v1):

| v1 要做 | v2 不用做 |
|---|---|
| markdown ASIN_RE 正则 | Sorftime 直出 ASIN |
| 色变体识别(短 title 跳过) | Sorftime 已按父 ASIN 聚合 |
| Sponsored 字符串前 1.2K 字符匹配 | Sorftime 返回的就是自然位 |
| 徽章字符串切片(Best Seller / X+ bought) | 真销量数字直出 |

**Credit 对比**: v1 = 35 Firecrawl credits + 大量解析代码 / v2 = 7 Sorftime MCP 调用。

---

## Step 3. N3 filter (规则不变,信号更准)

通用算法:

- `hard_ban` (yaml `_global.yaml` + `categories/luggage.yaml` 硬黑名单)
- `brand_mindshare` 标记(乐高定义积木那种品类心智占位品牌——不淘汰但标识)
- `monopoly_alert` (品牌集中度告警)
- `return_risk` (退货风险关键词)

**Sorftime 加持点**:

- `monopoly_alert` 现在用**真销量**算品牌集中度(Top-N 品牌占总月销量的份额),不再"前 10 名 Best Seller 占比"猜测
- 品牌字段直接从 Sorftime 返回值取,不用从 title 正则抽

---

## Step 4. N3.5 relevance_check (unchanged)

通用算法:

```
inclusion_keywords 任一命中 (luggage/suitcase/trolley bag/...) AND
exclusion_keywords 都不命中 (duffel/tote/garment bag/...)
+ brand_marker (识别标题里的显式品牌名)
```

---

## Step 5. N4 price_analysis (算法不变,self_band 补强)

```
pack_size  = 从标题/特征抽 (1/2/3/4 件)
unit_price = price / pack_size
self_band  = 自家 KSA bags_luggage 价段
  v1 数据源: 自家产品表 (n=0 时无对照)
  v2 数据源: 自家产品表 + Sorftime product_search(brand=自家品牌, amzSite='SA')
            → 拿到自家在售品的现状价段, 即便产品表 n=0 也能对照
too_high / too_low 标记 (按自家)
```

---

## Step 6. N5 sales_normalize — **核心重构(徽章 → 多维)**

### v1 算法(废弃)

```
amazon 徽章映射:
  Best Seller / #1 in Category → tier=top
  Amazon's Choice → tier=high
  X+ bought in past month → percentile_in_query
  无徽章 → tier=low

问题: SA 站徽章稀(9/234 有),大多 tier=low,无区分度
```

### v2 算法(多维综合)

#### a. 对每个入选 ASIN 拉 4 路 Sorftime 数据

```
a. product_detail(asin, amzSite='SA')
   → monthly_sales_volume, cat_rank, listing_age_days,
     review_count_total, avg_rating, FBA fee

b. product_trend(asin, productTrendType='SalesVolume', amzSite='SA')
   → 24 月销量曲线 [(yyyy-MM, units), ...]

c. product_trend(asin, productTrendType='Rank', amzSite='SA')
   → 24 月 BSR 曲线 (排名数字, 越小越好)

d. product_reviews(asin, reviewType='Both', amzSite='SA', page=1)
   → 评论列表 (最多 100), 每条带 评论日期/评星/SKU 属性
   → 算 earliest_review_date 和 review_count_30d
```

#### b. 衍生指标

```python
bsr_3m_avg  = mean(bsr_curve[-3:])
bsr_12m_avg = mean(bsr_curve[-12:])
sales_trend = trend(sales_curve)      # 'rising' / 'stable' / 'declining'
bsr_trend   = trend(bsr_curve)        # 排名数字增 = declining,减 = rising
earliest_review_age_days = (today - earliest_review_date).days
review_velocity_30d = review_count_30d / max(review_count_total, 1)
```

#### c. tier 切档(基于**类目级**排名,不是全站)

```
cat_rank ≤ 20  → top
cat_rank ≤ 100 → high
cat_rank ≤ 500 → mid
> 500          → low
cat_rank 缺失   → unknown (降级到 Firecrawl 徽章 fallback)
```

#### d. 起量识别 `is_rising` (任一为真即标 rising)

```
(a) "新品起量":
    listing_age_days < 180
    AND sales_trend == 'rising'
    AND monthly_sales_volume >= 30

(b) "评价初起":
    earliest_review_age_days < 90
    AND review_velocity_30d > 0.3
    (最早评价才出现不久, 近 30 天评价占总评论比例 >30%)

(c) "BSR 复苏":
    bsr_3m_avg < bsr_12m_avg × 0.5
    (近 3 月均排名比近 12 月一半还好)
```

#### e. 其它状态

```
is_steady     = listing_age > 365 AND sales_trend='stable' AND tier in (top, high)
is_recovering = listing_age > 365 AND bsr_3m_avg < bsr_12m_avg × 0.6   ← Sorftime 时代才识别得出
is_declining  = sales_trend='declining' AND bsr_trend='declining'
```

#### f. 输出 `SalesSignal` 多维字段

写入 `ProductRecord.sales_signal` (schema 见工程交底 §7)。

#### g. 降级

Sorftime 调用失败或字段缺 → fallback 到 raw_badges 徽章映射,`confidence=0.4`,`source='firecrawl_badges_fallback'`。

#### h. 硬规则不变 (§A 步骤 4)

> 决策只看 `tier` / `is_rising` / `is_steady` / `is_declining`。**禁止用 raw 数字做绝对阈值**(`sold>=30` 这种代码 review 直接打回)。

---

## Step 7. N5.6 sku_group — **用 product_variations 实现真聚合**

### v1 状况

```
amazon SA 293 SKU → 293 group (全 singleton)
原因: 父 ASIN ↔ 色变体 ASIN 分离, Step 2 已跳过色变体,
      只剩父 ASIN 单条 → 无邻居可聚合
```

### v2 改造

```
对每个父 ASIN, 调:
  product_variations(asin=parent_asin, amzSite='SA')

返回子体明细:
  variants = [
    {asin, attrs: {Color: 'Black', Size: '24-inch'}, sales, review_count},
    {asin, attrs: {Color: 'Navy',  Size: '24-inch'}, sales, review_count},
    ...
  ]

group:
  root              = 父 ASIN
  variants          = 上面那个列表
  high_selling_sku  = variants 按 sales DESC top-3       ← §A 步骤 5 "高销售 SKU"
  high_review_sku   = variants 按 review_count DESC top-3 ← §A 步骤 5 "高评论 SKU"
  sku_distribution  = 各变体属性分布
                      (eg Color: Black 40%, Navy 20%, ...)
```

这些数据**直接喂 §A 步骤 8 "评论提及 SKU 跟需求端交叉验证"**——Firecrawl 时代这一层信号根本拿不到。

---

## Step 8. N6 LLM 卖点提取 + N6.5 detail_features — **现在能完整跑**

v1 状况: 跳过(`amazon_detail_fetcher` 没写,没有 bullets/specifications)

v2 状况: 数据齐备,跑完整 10 stages(完整方法论见工程交底 §6)。

### S1 数据收齐(Step 6/7 已拿到,不重复调用)

| 数据 | 来源 |
|---|---|
| bullets / specifications / attributes | `product_detail`(Step 6 a) |
| 评论文本 + SKU 属性 + 时间戳 | `product_reviews(Positive)` + `product_reviews(Negative)` |
| SKU 子体 | `product_variations`(Step 7) |
| 销量/BSR 曲线 | `product_trend`(Step 6 b/c) |

### S2 标题级 deterministic regex

```
material   regex: ABS / PC / Polycarbonate / Aluminum / Nylon / Canvas / EVA
size       regex: 14 / 18 / 20 / 24 / 28 / 30 inch
pack_size  regex: 1/2/3/4-piece, set of N
color      regex: black/white/navy/blue/burgundy/olive/champagne/...
features   dict:  spinner / expandable / TSA / hardside / softside / lightweight /
                  aluminum_frame / front_pocket / laptop / usb / cup_holder / trolley / unisex
```

### S3 多模态读图(LLM)

- 主图风格归类(莫兰迪 / 工业 / 卡其 / 中东偏好色 white/black/green)
- 详情图卖点抽取(拓展层 / 前置开口 / 咖啡杯架 / 双向推把 等可见功能 — §A 步骤 5 强调的"读图")
- 实拍 vs 渲染 判定

### S4 评论挖掘(LLM cluster)

- Positive: Top 优点 cluster + 跨商品高频词
- Negative: Top 槽点 cluster(质量硬伤 — §A 步骤 6 强调)
- earliest_review_date 分布 → 新品判断
- 刷屏识别(跨商品文本 cosine,刷屏 = 噪音 — §A 步骤 6)
- 阿语/英语混合分析(中东本土口碑信号)

### S5 跨 SKU 聚合(来自 Step 7)

高销 SKU + 高评论 SKU + sku_distribution 直接进 brief。

### S6 销量归一(Step 6 已完成)

### S7 跨商品聚合(deterministic)

- 高销组(top + high + rising)vs 一般组频次对比
- 价格带 × 特征 矩阵
- 品牌格局(Sorftime brand 字段,不用从 title 猜)

### S8 假设生成(LLM)

- "为什么这些卖好"候选解释
- 跟 §A know-how 库交叉验证(中东偏白黑绿 / 灯芯绒 pass / 退货风险 等)
- 起量品的新趋势识别

### S9 差异化机会识别(LLM + 常识库)

- 高销组共性 ∩ 供给端空白 → 给 N7b 1688 二次搜的特点词组合

### S10 brief 产出

```
batches/<batch_id>/
├── 选品报告.md        # 人读,Luke 拍板用
├── candidates.jsonl  # 机读 ProductRecord 列表
├── brief.json        # 机读特征词云 / 分布 / hypothesis
└── feishu_sync.json  # 飞书表 payload
```

样板参考: `refs/sample_demand_brief_luggage_amazonsa_2026-05-06.md`(Firecrawl 版,Sorftime 加持后会补充 S3/S4/S5 三段,新增"评论维度 / 颜色 SKU 变体分布 / Top-K 深度档案 含 rising 品 / 销量真值分布"四块)。

---

## Step 9. N11 v2 价格带分桶打分(算法不变,输入精度大幅升级)

### 通用算法(unchanged)

```
1. 取所有 valid SKU.unit_price, 按分位数 (p20/p40/p60/p80) 切 5 个 bucket
   (amazon SA 经验切点: [107, 161, 219, 310] SAR)

2. 每个 bucket 内独立打分:
     overall = 0.40 × sales_pct      ← 在 bucket 内的销量分位
             + 0.20 × rising         ← 是否起量 (0/1)
             + 0.20 × rating_pct     ← bucket 内评分分位
             + 0.20 × feature_pct    ← bucket 内 N6 功能词数分位

3. bucket 内分档:
     top 10% → 一档 (这价格带的明星)
     top 10-30% → 二档
     top 30-60% → 三档
     60-100% → 四档

4. group 内 propagate (现在有真 group 了, Step 7 改造后此条生效)
```

### 输入精度对比

| 维度 | v1 | v2 |
|---|---|---|
| `sales_pct` | 徽章映射的 percentile,大多 tier='low' 无区分度 | `monthly_sales_volume` 直接百分位,区分度满 |
| `rising` | 0(无判断逻辑) | Step 6 多维 classify 的 `is_rising`,准 |
| `feature_pct` | 标题 dictionary 命中数(信号弱) | N6 完整产出的 `inferred_features` count(信号强) |
| `rating_pct` | 列表页评分 | `product_detail.avg_rating`(同) |

### 期望输出量级

按 SA luggage 历史经验: ≈ 25 一档 / 50 二档 / 70 三档 / 90 四档 / 58 drop(总计 ~293 SKU)。

Sorftime 版 v2 一档/二档**品质**应显著好于 v1——是真正基于真销量+起量+评分+功能词四维加权,不再是徽章猜测。

---

## Step 10. 一档 + 二档 → Demand Brief

### 输入

Step 9 产出的一档 + 二档 ASIN 列表(典型 25 + 50 = 75)。

### 数据齐备性(无需再调 Sorftime,Step 6/7/8 已拉完)

- `product_detail` ✅
- `product_trend(SalesVolume + Rank)` ✅
- `product_reviews(Positive + Negative)` ✅
- `product_variations` ✅

### 产出

```
refs/luggage_amazonsa_demand_brief_<batch_id>.md     # 选品报告
refs/luggage_amazonsa_brief_<batch_id>.json          # 机读
飞书表写入                                              # 协作
```

### 布局(参照 refs/sample_demand_brief 并扩充)

```
1. 元信息 + 数据覆盖度声明(注明 Sorftime 字段密度 vs Firecrawl 时代)
2. 销量真值分布(取代 v1 的"徽章覆盖率诉苦")
3. 价格带分布(N11 五桶)
4. 销量多维信号分布(tier + rising/steady/recovering/declining 五态)
5. 特征词云(高销 vs 一般,按 tier 加权)
6. 颜色/材质/尺寸维度 + **SKU 变体分布**(新)
7. 品牌格局 + 垄断判断(用真销量算)
8. Top-K 深度档案: 一档 ×5 + 二档 ×5 + **rising 起量品 ×5**(新)
9. **评论维度**: Positive/Negative cluster + 时间戳 + 阿语英语混合分析(新)
10. 给 N7b 1688 二次搜的关键词包(自动从 `product_traffic_terms` + 高销品共性特征生成)
11. 选品 hypothesis: 为什么这些卖好
12. 候选品 Top 5-6(含黄牌)
13. 询盘草稿(N12)
```

---

## Sorftime MCP 工具映射(Step → 调用)

| wf1 步骤 | Sorftime 工具 | 用途 |
|---|---|---|
| Step 2 | `product_search` × 7 keywords | 替换 Firecrawl scrape |
| Step 3 monopoly_alert | (复用 Step 2 数据) | 真销量算品牌集中度 |
| Step 5 self_band | `product_search(brand=自家)` | 拿自家在售品价段补对照 |
| Step 6 N5 a | `product_detail` | 月销量 / 类目排名 / 上架时间 |
| Step 6 N5 b | `product_trend(SalesVolume)` | 24 月销量曲线 |
| Step 6 N5 c | `product_trend(Rank)` | 24 月 BSR 曲线 |
| Step 6 N5 d | `product_reviews(Both, page=1)` | 最早评价时间 / 评论密度 |
| Step 7 N5.6 | `product_variations` | 真子体聚合 + 高销/高评论 SKU |
| Step 8 S4 | `product_reviews(Positive)` + `product_reviews(Negative)` | 优/槽点 cluster |
| Step 10 §10 | `product_traffic_terms` + `competitor_product_keywords` | N7b 反查关键词包 |
| 可选 baseline | `similar_product_feature(productName, amzSite)` | 类目级特征 baseline,省 LLM |
| N7 1688 接力(跨流程) | `ali1688_similar_product` | 直接给 1688 货源候选,减负本地 Playwright |

---

## 关键 enum 值(已踩坑,接手 CC 直接抄)

```
amzSite enum (大写英文):
  SA / AE / US / GB / DE / FR / IN / CA / JP / ES / IT / MX / AU / BR / Unknow (sic,Sorftime 拼写)

reviewType enum (英文,description 里写的中文是误导):
  Both / Positive / Negative

productTrendType enum (英文):
  SalesVolume / SalesAmount / Price / Rank
```

---

## 端到端 MCP 调用量估算(单次 luggage 选品)

| Step | 调用 | 数量 |
|---|---|---|
| Step 2 | `product_search × 7` | 7 |
| Step 3-5 | 不调用 | 0 |
| Step 6 (N5 入选 ~80 品 × 4 路) | `product_detail + product_trend×2 + product_reviews(Both)` | 320 |
| Step 7 (~80 品) | `product_variations` | 80 |
| Step 8 (一档+二档 75 × 2) | `product_reviews(Positive) + product_reviews(Negative)` | 150 |
| Step 10 词包 (75 × 1) | `product_traffic_terms` 或 `competitor_product_keywords` | 75 |
| 可选 baseline | `similar_product_feature` | 1 |
| **合计** | | **~630 calls / batch** |

- 月 3 次 × 3 品类(行李箱/boss 椅/婴儿车)≈ **5,700 calls/月**
- 试用期 7 天看能跑几个 batch,决定切 API
- 优化点: Step 6 的 reviews 在 Step 8 复用(避免重复调用);Step 7 的 variations 可缓存

---

## 与工程交底的对接关系

- 本文档是 wf1 Amazon 算法链路的**实现规范**
- ProductRecord / SalesSignal schema 见**工程交底 §7**
- 10-stage N6 的方法论见**工程交底 §6**
- pricing_table / categories yaml seed 见**工程交底 §8**
- 反卷规则 / 工程约束 见**工程交底 §10-§11**
- 当本文档与工程交底冲突时,**实证为准**(已实证就采纳实证,未实证就走交底默认)

---

## v1 → v2 迁移 checklist

- [ ] 写 `softtime_mcp_client.py`(MCP HTTP/SSE 包一层,重试 + 速率 + credit 计数)
- [ ] 重写 `amazon_fetcher.search` → 改用 `product_search`(Step 2)
- [ ] 写 `amazon_detail_fetcher`(`product_detail` + `product_trend × 2` + `product_reviews × 2` + `product_variations`)
- [ ] 重写 N5 sales_normalize 算法(§5 多维 classify)
- [ ] 升级 N5.6 sku_group 用 `product_variations`
- [ ] 接通 N6 10-stage 跑完整(Step 8)
- [ ] N11 v2 算法不变,只换输入字段映射
- [ ] Step 10 brief 模板扩充(评论维度 / SKU 变体 / rising 档案 / 销量真值分布 / N7b 词包)
- [ ] 保留 Firecrawl Amazon fetcher 作为 fallback(Sorftime 故障时降级,confidence=0.4)

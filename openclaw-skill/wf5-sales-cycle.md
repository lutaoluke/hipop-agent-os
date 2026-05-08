---
name: wf5-sales-cycle
display_name: 工作流五 — 销售周期 + 补货分析（per-entity）
version: 2.0.0
author: hipop
description: 跨境电商 noon 平台周度运营决策。每个销售主体（国别×平台×店铺）独立分析：销量趋势、库存分层、在途批次、物流均值、丢货必补合并，输出 wf5_<alias>_sales_cycle 表 + 同步对应飞书数据表（PRESERVE 操作状态/实际下单数）。每周一定期运行或运营随时召唤。2026-05-02 重构：从单表改为按销售主体物理切分。
tags: [ecommerce, inventory, replenishment, operations, noon, weekly-review, multi-entity]
---

## 功能概述

针对 hipop / noon 中东平台的运营人员，每次运行对每个 sales_entity 自动完成：

1. **销量趋势判断**：6 态趋势标签（↑↑/↑/→平稳/→波动/↓/↓↓/无销量），动态计算日均销量
2. **库存分层**：即时可售 / 腾挪可售 / 在途批次 / 国内仓
3. **物流均值**：从 `wf3_logistics_hub.groups_json` 按国别过滤，得当前 entity 的 avg_transit_days
4. **丢货必补**：合并 `wf6_<alias>_replenishment_queue` 未消费记录
5. **断货风险**：在途断货 / 到齐后断货 / 无
6. **本周必补总量**：wf5 正常补货量 + 丢货必补
7. **紧急度** + **运营建议**

---

## 设计原则

**每个销售主体一张物理表**——跟工作流二（wf2）的 sales_entities 架构对齐：

```
config/hipop.json -> sales_entities[]
  ├─ hipop_ksa  → wf5_hipop_ksa_sales_cycle  + wf6_hipop_ksa_replenishment_queue
  └─ hipop_uae  → wf5_hipop_uae_sales_cycle  + wf6_hipop_uae_replenishment_queue
```

物理切分理由：销售主体的销量、库存、物流均值、补货决策**都不可比**——KSA 和 UAE 是两个独立运营单元。下游报表/飞书展示也按 entity 分开。

---

## 安装前准备

依赖工作流二（wf2）的产出：
- `wf2_<alias>_sku` 表必须有数据（销量字段从这里来）
- `wf3_logistics_hub` 必须有数据（物流均值从 groups_json 取）
- `wf1_<alias>_stock` 表必须有数据（库存字段从这里来, 已切换, 不再走 sa_main）

```bash
pip install requests
```

---

## 数据流

```
wf2_<alias>_sku                 → 销量(10/30/60/180) + latest_profit_rate
wf3_logistics_hub               → 在途数量 + 物流均值（按国别过滤 groups_json）
wf6_<alias>_replenishment_queue → 未消费的丢货必补
wf1_<alias>_stock               → 库存 (per-entity: noon_saleable_qty + pending_inbound_qty + overseas_total_qty + yiwu_qty + dongguan_qty)
                                ↓
wf5_<alias>_sales_cycle         ← 销售周期 + 补货建议（每 entity 一张物理表）
                                ↓
飞书 wf5_<alias>_decisions       ← 经营决策表（运营在飞书填操作状态/实际下单数）
                                ↓ (反向: feishu_pull)
wf5_ops_actions                 ← 飞书侧"操作状态/实际下单数"回流到本地 (sku × week_tag)
```

**反向同步**: `feishu_pull` 定时把飞书子表 3 里运营填的"操作状态" + "实际下单数"读回来, 写入 `wf5_ops_actions`. 主键 `(sku, week_tag)`. 用途: 让 wf5 下次跑时知道哪些建议被采纳了, 计算"采纳率"等指标.

---

## 算法

### 趋势计算（七态 + 无销量，2026-05-08 v3）

基于 r10 = d10/10、r30 = d30/30、r60 = d60/60：

| 条件 | 趋势 | 日均 |
|---|---|---|
| d30 == 0 | 无销量 | 0 |
| **0 < d30 < 5（新增）** | **低销** | r30（不进 momentum 放大） |
| d30 ≥ 5 + short_accel & r30 ≥ r60×0.9 | 加速增长 | r10 × (r10/r30) |
| r30 > r60×1.15 | 增长 | max(r10, r30) |
| r30 < r60×0.7 | 急速下降 | max(r30, r60×0.7) |
| r30 < r60×0.85 | 下降 | r30 |
| short_accel | 波动 | r60 |
| 其它 | 平稳 | r30 |

`short_accel = r10 > r30 × 1.3`

**为什么加"低销"分支**：30 天卖 1-3 件的低销 SKU，r10/r30 比值容易把噪音放大成"加速增长 momentum 放大日均"，造成全量补货建议噪音。修法：d30<5 强制走 r30 平直路径。实测 KSA 立即补货从 72 → 44，砍掉 28 个噪音。

### 风险标签

```python
if daily <= 0:                                                risk = "无"
elif (immediate + transfer) / daily < avg_transit_days:       risk = "在途断货"
elif current_pipeline / daily < avg_transit + 7d:             risk = "到齐后断货"
else:                                                          risk = "无"
```

### 补货量 (v1 综合算法, 多变量决策)

不是单一公式给数字, 而是基于**库存分层 + 时间线模拟 + 历史批量**综合输出:

```python
# 1. 库存分层
current_pipeline = immediate (即时可售) + transfer (腾挪+7天) +
                   total_transit (各 batch ETA) + domestic (国内仓)

# 2. simulate_sellable_days — 时间线事件消耗模拟
events = [(0, immediate), (7, transfer), (b.eta, b.qty for b in transit_batches)]
gaps = []
for event in sorted(events): consume daily_rate × gap_days, 记录 stockout 空窗
gaps_during = [g for g in gaps if g.type == "在途期间"]  # 关键: 在途期间是否断货

# 3. 决策窗口
sellable_days = (sum batches + 兜底库存) / daily
decision_days = sellable_days - avg_transit_days  # 距离必须下单还有几天

# 4. 补货量 = 历史中位批量 × 多批追平 (不是一次到位 OUT)
target = (avg_transit + 7) × daily       # 管道目标
shortfall = max(0, target - current_pipeline)
hist_med = median(在途批次 qty)            # 该 SKU 历史常规批量
batch_suggest = hist_med if hist_med else max(1, round(7 × daily))
replenish_qty = batch_suggest             # 这周补一批 (= 历史批量大小)
batches_needed = ceil(shortfall / batch_suggest)  # 总共需要 N 批追平

# 5. 慢销 v3 (2026-05-08 阈值提高)
is_slow_d30      = sales_30d < 10                         # 30 天卖 < 10 件视为慢销（旧版 < 3 太宽）
is_growth_trend  = trend in (加速增长, 增长, 波动)          # 有 momentum 信号
is_pipeline_safe = current_pipeline / daily ≥ avg_transit + ORDER_CYCLE  # 库存够撑过物流周期

slow_mover = is_slow_d30 AND (NOT is_growth_trend) AND is_pipeline_safe
            → 关补货, 标 EOL 候选

low_margin = profit < 20% → 关补货, 建议调价
```

### 慢销定义 v2 — 重要 spec 更新 (2026-05-03)

**原裸 spec** `is_slow = sales_30d < 3` 反复出现两类生产事故:
1. **加速增长爆款被拦** — d30=2 但近 10 天 r10 超 r30 × 1.3, momentum daily 已 0.6+, 算法判 slow 不补 → 失爆款窗口
2. **急速下降库存干涸不补** — d30=1 但 pipeline=0, 真断货, 算法仍判 slow → 实际断货扩大损失

**v2 修法 (B+C 组合)**:
```
slow = (sales_30d < 3)
       AND (trend NOT IN 加速增长/增长/波动)         ← B: momentum 保护
       AND (sellable_days >= avg_transit + 7d)      ← C: 断货保护

= 只有"短期销量低 AND 没 momentum 信号 AND 库存能撑过物流周期"
  三条全满足才是真慢销
```

**KSA 实测对比** (809 SKU, 212 有销量):
| 维度 | 裸 spec (v1) | 慢销 v2 (B+C) |
|---|---|---|
| 有销量内卷入率 | 66% | 19% |
| 加速增长被拦 | 42 | 0 |
| 增长被拦 | 47 | 0 |
| 真补货 SKU | 26 | 72 |

### 双维度 Status (v1 关键差异化)

输出**两个独立判断**, 分别给运营和采购:

**status_ops (运营策略)**:
- 在途期间断货 → "立即调价控流 / 下架部分变体"
- 滞销+大库存 → "降价促销 / 参加 Deal 清库"
- 加速增长 → "保持运营 / 可适当提价测试"
- 零库存 → "立即停止广告"

**status_buy (采购决策)** (按 decision_days 分级):
- decision_days <= 0 → 🔴 本周立即采购 X 件 (已过窗口)
- decision_days < 14 → 🔴 本周必须下单 X 件 (窗口仅剩 N 天)
- decision_days < 21 → 🟡 N 天内采购 X 件
- 其它 → 🟢 N 天后采购约 X 件

### 关键设计原则

> **"保证仓库 / 运输中 / 采购中 每个环节都有一部分货分摊成本"**
>
> 不是一次性 OUT 模型补到目标, 而是**按历史批量周周下单**, 让 pipeline 在 lead_time 周内自然爬到稳态. 同时如果在途期间断货, 给出 "调价控流" 等运营策略缓冲.

### 紧急度

```
weekly_total == 0                → 无需采购
immediate + transit == 0         → 立即（紧急断货）
risk == 在途断货                  → 立即
sellable_days < avg_transit       → 立即
sellable_days < avg_transit × 1.5 → 本周
其它                              → 正常
```

---

## 表结构

每个销售主体一张：

```sql
CREATE TABLE wf5_<alias>_sales_cycle (
  partner_sku             TEXT PRIMARY KEY,
  trend                   TEXT,
  daily_rate              REAL,
  forecast_10_days        INTEGER,
  forecast_30_days        INTEGER,
  risk_label              TEXT,
  current_pipeline        INTEGER,
  target_pipeline         INTEGER,        -- (avg_transit+7) × daily
  wf5_replenish_qty       INTEGER,        -- 本周下单数 (= hist_med 历史批量)
  lost_replenish_qty      INTEGER,
  weekly_total_replenish  INTEGER,
  trigger_reasons         TEXT,           -- JSON array
  urgency                 TEXT,           -- 立即/本周/正常/无需采购
  ops_advice              TEXT,           -- 拼接的双维度文本 (给飞书)
  week_tag                TEXT,
  updated_at              DATETIME,
  -- v1 综合算法字段:
  sellable_days           REAL,           -- 时间线模拟得到的可售天数
  decision_days           INTEGER,        -- = sellable_days - avg_transit
  status_ops              TEXT,           -- 运营策略 (调价/控流/Deal 等)
  status_buy              TEXT,           -- 采购决策 (本周下单/N天后等)
  hist_med                INTEGER,        -- 历史批量中位数
  batches_needed          INTEGER,        -- 追平管道缺口需多少批
  gaps_during_json        TEXT            -- 在途期间断货 gap JSON
);
```

---

## 使用方法

```
/wf5-sales-cycle [--entities <alias>] [--skus ...] [--no-sync]
```

| 调用 | 效果 |
|---|---|
| `/wf5-sales-cycle` | 全部 sales_entity 跑 + 同步飞书 |
| `/wf5-sales-cycle --entities hipop_ksa` | 限定单一销售主体 |
| `/wf5-sales-cycle --skus TBB0116A` | 指定 SKU |
| `/wf5-sales-cycle --no-sync` | 只算不同步飞书 |

或在 Python 中：
```python
from workflows.wf_sales_cycle import run
results = run(entity_aliases=None, write_db=True)
# results: {"hipop_ksa": [{...}, ...], "hipop_uae": [...]}
```

---

## 飞书经营决策表（per-entity）

每个销售主体一张独立飞书数据表，命名 `wf5_<alias>_decisions`，17 字段：

```
SKU(主键) / 趋势 / 日均销量 / 预测10天 / 预测30天 / 断货风险 /
当前管道量 / 目标管道量 / wf5建议补货 / 丢货必补 / 本周必补总量 /
触发原因 / 紧急度 / 运营建议 / 周标签 /
操作状态(运营填) / 实际下单数(运营填)
```

**新建销售主体的飞书表**：

```bash
python3 hipop/scripts/wf5_feishu_setup.py --alias <new_alias>
```

table_id 自动写回 `config/hipop.json -> sales_entities[].feishu_decisions_table_id`。

**同步规则**：upsert by SKU；update 时跳过 `操作状态` / `实际下单数`（运营填字段）。

---

## 输出示例

```
[entity hipop_ksa] country=SA store=HIPOP-NOON-KSA
  [hipop_ksa] ✓ TBB0116A | 趋势=增长 日均=1.27 | 必补=18(本周) | 风险=到齐后断货
  [hipop_ksa] ✓ TBS0228A | 趋势=平稳 日均=1.53 | 必补=0(无需采购) | 风险=无
  ...
  → 688 skus 写入 wf5_hipop_ksa_sales_cycle

[entity hipop_uae] country=AE store=HIPOP-NOON-UAE
  ...
完成：共 1376 个 SKU 写入各 wf5_<alias>_sales_cycle
  [hipop_ksa] 立即=2 本周=15 正常=80 无需采购=591
  [hipop_uae] 立即=1 本周=8 正常=42 无需采购=637

→ 同步到飞书 (decisions 表)...
  [hipop_ksa] synced 688 records
  [hipop_uae] synced 688 records
```

---

## 下游消费

```sql
-- KSA 必补清单（按紧急度排序）
SELECT partner_sku, weekly_total_replenish, urgency, ops_advice
FROM wf5_hipop_ksa_sales_cycle
WHERE weekly_total_replenish > 0
ORDER BY CASE urgency WHEN '立即' THEN 1 WHEN '本周' THEN 2 ELSE 3 END;

-- 跨主体看同一 SKU 表现
SELECT 'KSA' AS s, * FROM wf5_hipop_ksa_sales_cycle WHERE partner_sku=?
UNION ALL
SELECT 'UAE',     * FROM wf5_hipop_uae_sales_cycle WHERE partner_sku=?;
```

---

## 注意事项

- SKU 全部"无需采购" 大概率是 `wf3_logistics_hub` 数据稀疏（`avg_transit_days` 缺失）→ 跑 wf3 全量后修复
- 库存字段从 `wf1_<alias>_stock` 读 (per-entity, 已不再走 sa_main 老主表)
- 销量来自 `wf2_<alias>_sku`（noon orders 实时滚动）, 比老版 sa_main 快照准确得多
- wf1 / wf2 / wf3 / wf5 / wf6 全链路已对齐 sales_entity 模型, 不再混用 sa_main
- 新增销售主体务必：
  1. 加 `config/hipop.json -> sales_entities`
  2. 跑 `wf5_feishu_setup.py --alias <new>` 建好飞书表
  3. 才能跑 `/wf5-sales-cycle`

---

## 执行指令

1. 加载 `config/hipop.json -> sales_entities`，无配置则报错
2. 解析 `$ARGUMENTS`（--entities / --skus / --no-sync）
3. 对每个 entity：循环 SKU → analyze_one → write_record 到 `wf5_<alias>_sales_cycle`
4. 默认末尾 `sync_decisions()` 同步各 entity 的飞书数据表
5. 终端汇总：每个主体的紧急度分布

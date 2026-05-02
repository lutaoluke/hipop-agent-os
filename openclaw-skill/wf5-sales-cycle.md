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
- `sa_main` 仍作库存 fallback（等工作流一覆盖）

```bash
pip install requests
```

---

## 数据流

```
wf2_<alias>_sku                 → 销量(10/30/60/180) + latest_profit_rate
wf3_logistics_hub               → 在途数量 + 物流均值（按国别过滤 groups_json）
wf6_<alias>_replenishment_queue → 未消费的丢货必补
sa_main                         → 库存（fallback：noon平台/送仓未上架/海外仓可用/义乌仓/东莞仓）
                                ↓
wf5_<alias>_sales_cycle         ← 销售周期 + 补货建议（每 entity 一张物理表）
                                ↓
飞书 wf5_<alias>_decisions       ← 经营决策表（运营在飞书填操作状态/实际下单数）
```

---

## 算法

### 趋势计算（六态 + 无销量）

基于 r10 = d10/10、r30 = d30/30、r60 = d60/60：

| 条件 | 趋势 | 日均 |
|---|---|---|
| d30 == 0 | 无销量 | 0 |
| short_accel & r30 ≥ r60×0.9 | 加速增长 | r10 × (r10/r30) |
| r30 > r60×1.15 | 增长 | max(r10, r30) |
| r30 < r60×0.7 | 急速下降 | max(r30, r60×0.7) |
| r30 < r60×0.85 | 下降 | r30 |
| short_accel | 波动 | r60 |
| 其它 | 平稳 | r30 |

`short_accel = r10 > r30 × 1.3`

### 风险标签

```python
if daily <= 0:                                                risk = "无"
elif (immediate + transfer) / daily < avg_transit_days:       risk = "在途断货"
elif current_pipeline / daily < avg_transit + 7d:             risk = "到齐后断货"
else:                                                          risk = "无"
```

### 补货量

```python
if is_slow or is_low_profit or daily <= 0:    wf5_qty = 0
elif avg_transit_days:
    target = (avg_transit + 7) × daily
    wf5_qty = max(0, target − current_pipeline)
else:                                          wf5_qty = 0  (无物流均值参考)
weekly_total = wf5_qty + lost_replenish_qty
```

- `is_slow = d30 < 3`
- `is_low_profit = 0 < latest_profit_rate < 0.20`

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
  target_pipeline         INTEGER,
  wf5_replenish_qty       INTEGER,
  lost_replenish_qty      INTEGER,
  weekly_total_replenish  INTEGER,
  trigger_reasons         TEXT,        -- JSON array
  urgency                 TEXT,        -- 立即/本周/正常/无需采购
  ops_advice              TEXT,
  week_tag                TEXT,        -- 例 "2026-W18"
  updated_at              DATETIME
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
- 库存字段当前从 `sa_main` fallback，等工作流一覆盖后会切到新源
- 销量来自 `wf2_<alias>_sku`（noon orders 实时滚动），比老版 sa_main 快照准确得多
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

---
name: wf-logistics-status
display_name: 工作流三 — 在途与已完成物流情况（数据中枢）
version: 0.1.0
author: hipop
description: 跨境电商物流情况数据中枢。对接 dbuyerp ERP + 主流货代追踪网站，对每个 SKU 按 (国家×平台×货代) 分组，输出在途批次的当前阶段/停留/历史耗时基准 + 已完成近 3 笔截断后的总耗时。结果写入 hipop.db 的 wf3_logistics_hub 表，作为分析层（物流周期计算、销售周期/补货）的统一数据源。
tags: [ecommerce, logistics, erp, hub, cross-border, hipop]
---

## 功能概述

工作流三是点购体系中"在途与已完成物流情况"的数据中枢。它**只采集和归一化数据，不做判断、不做合成**——所有判断（卡单告警、补货建议）由下游的分析工作流读这张表完成。

针对使用 **dbuyerp** 的跨境卖家，自动完成：

1. **多平台分组**：按 ERP 的"平台店铺"字段把 SKU 拆到 (国家 × 平台 × 货代 × 货运方式) 四维分组
2. **在途批次明细**：每张未完成货单的当前节点 → 阶段分类 → 停留天数 → 历史基准
3. **已完成历史均值**：每分组取近 3 笔已完成单，截断到"已提回海外仓"为止的总耗时
4. **货代级历史池**：跨 SKU 汇总同货代的所有阶段历史耗时，按时间倒序加权（0.5/0.3/0.2）
5. **运营标识**：阳光UAE 等暂未支持物流站的批次自动标 `needs_ops_input=1`，等待运营手动填入
6. **卡单标识**：阶段停留 > 1.5× 历史均值，标 `has_stuck_batch=1`，供下游告警

支持物流商：**义特无忧**（nextsls）/ **安时达 KSA & UAE** / **飞坦** / **维勒**
待补充：**阳光UAE**（运营手动填入）

---

## 安装前准备

复用 `erp-logistics-tracker` 的环境（同一套 Playwright + dbuyerp ERP 凭据）。如已安装则跳过。

```bash
pip install requests playwright
playwright install chromium
```

配置文件 `config/hipop.json` 的关键字段：

```json
{
  "erp": { "url": "https://www.dbuyerp.com", "username": "...", "password": "..." },
  "db":  { "path": "../hipop.db" }
}
```

---

## 阶段分类口径（核心）

物流节点统一归到 7 个阶段，跨货代/中英文混合都适用：

| 阶段 | 中文示例 | 英文示例 | 用途 |
|---|---|---|---|
| 国内仓 | `义乌仓库 已收货` / `已下单` | `Arrived at YIWU01 Hub` / `shipped out` | 计入总耗时 |
| 装柜出港 | `已装柜` / `报关` | `customs declaration` / `expected to leave/depart` / `delayed for departure` | 计入总耗时 |
| 海运中 | `已开船` / `预计X到港` / `中转` / `战争绕道` | `transported by sea` / `change ships` / `expected to arrive` / `arrived at [中间港] Port` | 计入总耗时 |
| 到港待清关 | `已到港，待清关` | `arrived at DUBAI and clearing` | 计入总耗时 |
| 清关完成 | `清关已完成，待提柜` | `finished customs clearance` | 计入总耗时 |
| **海外仓** | `已提回海外仓，待拆柜派送` | `arrive at the local distribution center` | **截断点：到此为止** |
| 已签收（排除）| `已签收` | `Delivered - Signed for by customer` | 不计（消费者签收延迟与物流无关）|

**关键消歧**：
- `delayed ... for departure / leave / depart` → 装柜出港（在港等待出发）
- `delayed to arrive at` → 海运中（海上延误到港）
- `Port of [非目标港]` 不带 `pending customs / clearing` → 海运中（中间港）
- `arrived at [目标港] and clearing` → 到港待清关

**标签过滤**（不算真实节点，时间自动并入前后节点）：
- `凭证 / Proof`
- `The shipping company's website has not updated...`

---

## 加权方案

历史耗时合成时：
- **已完成批次**：每个货代×阶段取近 3 笔（按时间倒序）
- **在途批次**：已经走过该阶段的全部纳入（在途数据更新鲜，全部用上）
- 合并后按阶段结束时间倒序，前 3 笔加权 **0.5 / 0.3 / 0.2**（最新权重最高）

---

## 数据中枢表

```sql
CREATE TABLE wf3_logistics_hub (
    sku                       TEXT PRIMARY KEY,
    in_transit_total_qty      INTEGER NOT NULL DEFAULT 0,
    in_transit_batch_count    INTEGER NOT NULL DEFAULT 0,
    needs_ops_input           INTEGER NOT NULL DEFAULT 0,   -- 含阳光UAE 等无 URL 物流
    has_stuck_batch           INTEGER NOT NULL DEFAULT 0,   -- 阶段停留 > 1.5x 历史
    groups_json               TEXT NOT NULL,                 -- 详见下方
    last_run_status           TEXT,
    updated_at                DATETIME NOT NULL
);
```

`groups_json` 结构（数组，每元素为一个分组）：

```json
[{
  "country": "UAE",
  "platform": "noon",
  "forwarder": "安时达UAE",
  "shipping_method": "海运",
  "needs_ops_input": false,
  "in_transit_count": 2,
  "in_transit_qty": 16,
  "in_transit_batches": [
    {
      "order_no": "PDZ0028823",
      "tracking_no": "8A07WKY",
      "qty": 8,
      "delivery_at": "2026-04-09",
      "current_stage": "海运中",
      "current_status_text": "The goods are transported by sea",
      "stage_started_at": "2026-04-13T11:34:00",
      "stage_stay_days": 3,
      "history_stage_days": 27.6,
      "history_pool_size": 3,
      "is_stuck": false,
      "note": "",
      "nodes": [{"time": "...", "status": "..."}]
    }
  ],
  "completed_avg_total_days": 34,
  "completed_n": 2,
  "completed_recent3": [
    {
      "order_no": "PDZ0024323",
      "tracking_no": "8A06KGL",
      "delivery_at": "2025-12-23",
      "in_storage_at": "2026-01-31",
      "total_days": 31,
      "note": "",
      "nodes": [...]
    }
  ]
}]
```

---

## 使用方法

```
/wf-logistics-status [SKU列表]
```

| 调用方式 | 效果 |
|---|---|
| `/wf-logistics-status` | 全量模式：扫 sa_main 所有 SKU，写入 wf3_logistics_hub |
| `/wf-logistics-status TBJ0057A` | 指定单 SKU |
| `/wf-logistics-status TBJ0057A TBA0210A TBC0168A` | 指定多个 SKU |

或在 Python 中：
```python
from workflows.wf_logistics_status import analyze_skus
results = analyze_skus(["TBJ0057A"], write_db=True)
```

---

## 输出示例

```
=== 阶段 1：ERP 拉单 ===
  TBJ0057A: 在途 4 | 已完成 6
  TBA0210A: 在途 7 | 已完成 3
  ...

=== 阶段 2：物流站抓节点 ===
  TBJ0057A/PDZ0028823 | 安时达UAE | 8A07WKY
  TBJ0057A/PDZ0027158 | 安时达UAE | 8A07CKW
  ...

完成。共 10 个 SKU 写入 wf3_logistics_hub。
  ⚠️ 卡单 SKU: 4 个 — ['TBJ0057A','TBA0210A','TBJ0056A','TBP0289A']
  🔔 待运营 SKU: 5 个 — ['TBJ0057A','TBC0168A','TBP0260A','TBS0357A','SDA1874A']
```

---

## 下游消费方式

分析层工作流（物流周期计算、销售周期/补货）从 `wf3_logistics_hub` 读：

```sql
-- 拿某 SKU 的最新物流情况
SELECT * FROM wf3_logistics_hub WHERE sku = ?;

-- 找所有有卡单的 SKU
SELECT sku FROM wf3_logistics_hub WHERE has_stuck_batch = 1;

-- 找待运营手动填入的 SKU
SELECT sku FROM wf3_logistics_hub WHERE needs_ops_input = 1;
```

```python
import json
row = conn.execute("SELECT groups_json FROM wf3_logistics_hub WHERE sku=?", (sku,)).fetchone()
groups = json.loads(row[0])
for g in groups:
    if g["forwarder"] == "义特无忧KSA":
        print(g["completed_avg_total_days"])  # 该 SKU 在该货代的物流均值
```

---

## 注意事项

- 全量模式扫 ~700 个 SKU，物流站逐批次抓，约 10-15 分钟
- 同一 tracking_no 跨多 SKU（混批）只查一次，结果共享
- 阳光UAE 未支持自动追踪，对应批次的物流字段会留空，运营在 `needs_ops_input=1` 的 SKU 上人工填入
- 凭证/Proof 等标签节点会被过滤掉，时间自动并入前后真实节点
- 截断点统一在"已提回海外仓 / arrive at the local distribution"，不计后续签收等待

---

## 执行指令

1. 确认配置文件存在，否则引导用户完成 erp-logistics-tracker 的安装前准备
2. 解析 `$ARGUMENTS`：有参数则指定模式，无参数则全量
3. 运行 `analyze_skus(skus, write_db=True, verbose=True)`
4. 终端打印进度 + 卡单/待运营汇总
5. 数据写入 `wf3_logistics_hub`，下游工作流可直接 SQL 查询消费

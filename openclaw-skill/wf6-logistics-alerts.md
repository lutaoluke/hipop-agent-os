---
name: wf6-logistics-alerts
display_name: 工作流六 — 物流告警 + 反馈联动
version: 2.0.0
author: hipop
description: 跨境电商物流告警与反馈中枢。从 wf3_logistics_hub 重建 (货单×货代) 视图，按阶段超时阈值生成告警，运营在飞书填状态/物流回复，"已确认丢货" 自动联动写入对应销售主体的丢货必补队列（按 forwarder 名字 KSA/UAE 路由），下次工作流五合并为补货建议。2026-05-02 起：丢货必补队列按销售主体物理切分。
tags: [ecommerce, logistics, alert, feedback, replenishment, multi-entity]
---

## 功能概述

物流告警与反馈闭环：

1. **生成告警**：从 wf3_logistics_hub.groups_json 重建货单视图，按阶段停留时长超阈值打标
2. **同步飞书**：写入子表 1 alerts（含 SKU 列表、停留天数、超额、运营填字段）+ 子表 4 warehouse_appt（清关完成-需约仓）
3. **运营反馈**：飞书 alerts.操作状态 = "已确认丢货" 时，按 forwarder 名字（KSA/UAE）路由到对应 entity 的 `wf6_<alias>_replenishment_queue`，下次 wf5 合并为补货
4. **消费回写**：飞书 wf5 决策表标"已下单"时，feishu_pull 遍历所有 entity 的丢货队列 mark consumed_at，避免重复必补

---

## 设计原则：单 vs 多

| 表 | 维度 | 是否 per-entity | 原因 |
|---|---|---|---|
| `wf6_logistics_alerts` | 货单 × 告警类型 | ❌ **不分 entity** | 一张货单可能含多 SKU 跨多 entity，按 entity 切会拆碎货单 |
| `wf6_<alias>_replenishment_queue` | partner_sku × order_no | ✅ **per-entity** | 丢货必补结果要回到具体销售主体的补货流程，跟 wf5 对齐 |

确认丢货时如何路由：**按 forwarder 名字尾缀**：
- `义特无忧KSA` / `安时达KSA` → SA → `wf6_hipop_ksa_replenishment_queue`
- `安时达UAE` / `阳光UAE` → AE → `wf6_hipop_uae_replenishment_queue`

---

## 数据流

```
wf3_logistics_hub.groups_json
  │ aggregate_orders_from_hub: 重建 (order_no, forwarder, sku_list)
  ▼
[evaluate_order] 阶段超时检测：stage_stay_days vs history_stage_days × 阈值倍数
  │ 红/橙/黄/蓝 四级
  ▼
wf6_logistics_alerts (UNIQUE on order_no+alert_reason WHERE resolved_at IS NULL)
  │
  ▼
飞书 alerts 子表  ← 自动 sync
  │ [运营在飞书改 ops_status]
  ▼
feishu_pull → update_alert_status(alert_id, ops_status)
  │
  └── if ops_status == "已确认丢货":
        ├─ 按 forwarder 推 country (KSA/UAE → SA/AE)
        ├─ 写入 wf6_<alias>_replenishment_queue
        └─ 下次 wf5 合并为 lost_replenish_qty

[运营在 wf5 飞书表标"已下单"]
  │
  ▼
feishu_pull → 遍历 sales_entities 的 wf6_<alias>_replenishment_queue
  └─ mark consumed_at
```

---

## 表结构

### `wf6_logistics_alerts`（单表）

```sql
CREATE TABLE wf6_logistics_alerts (
  alert_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no              TEXT NOT NULL,
  forwarder             TEXT NOT NULL,
  alert_reason          TEXT NOT NULL,
  alert_level           TEXT NOT NULL,    -- 红/橙/黄/蓝
  stage                 TEXT NOT NULL,
  threshold_days        INTEGER,
  actual_stay_days      REAL,
  history_stage_days    REAL,
  excess_over_threshold REAL,
  sku_list_json         TEXT NOT NULL,    -- 该货单含的 SKU+qty 列表
  action_owner          TEXT NOT NULL DEFAULT '刘鹤',
  supervisor            TEXT NOT NULL DEFAULT '运营',
  required_action       TEXT,
  ops_status            TEXT,             -- 运营回填
  ops_contact_log       TEXT,
  ops_status_updated_at DATETIME,
  resolved_at           DATETIME,
  created_at            DATETIME NOT NULL,
  updated_at            DATETIME NOT NULL
);
```

### `wf6_<alias>_replenishment_queue`（per-entity）

```sql
CREATE TABLE wf6_<alias>_replenishment_queue (
  partner_sku    TEXT NOT NULL,
  lost_qty       INTEGER NOT NULL,
  order_no       TEXT NOT NULL,
  forwarder      TEXT NOT NULL,
  confirmed_at   DATETIME NOT NULL,
  week_tag       TEXT NOT NULL,
  consumed_at    DATETIME,                -- NULL=待补货, 非NULL=已下单
  PRIMARY KEY (partner_sku, order_no)
);
```

---

## 告警级别（占位规则）

| 级别 | 触发 |
|---|---|
| 🔴 红 | stage_stay > 历史均值 × 2 |
| 🟠 橙 | stage_stay > 历史均值 × 1.5 |
| 🟡 黄 | stage_stay > 历史均值 × 1.2 |
| 🔵 蓝 | 清关完成-需约仓 / 阳光UAE 待运营手动 / 其他需关注 |

---

## 使用方法

```
/wf6-logistics-alerts [--no-sync]
```

或：

```bash
cd /Users/luke/Downloads/点购工作流
python3 hipop/workflows/wf_logistics_alerts.py
```

跑完默认自动 sync alerts + warehouse_appt 到飞书。

---

## 飞书反向同步（feishu_pull，每 30 分钟）

```python
# 1) alerts 表 → wf6_logistics_alerts
飞书 alerts.操作状态 in TERMINAL → resolved_at = now
飞书 alerts.操作状态 == "已确认丢货" → 按 forwarder 路由 → wf6_<alias>_replenishment_queue

# 2) wf5 决策表 → wf5_ops_actions + 消费 wf6 队列
飞书 wf5_<alias>_decisions.操作状态 == "已下单"
  → 写 wf5_ops_actions(sku, week_tag, actual_qty, ...)
  → 遍历所有 sales_entities 的 wf6_<alias>_replenishment_queue
  → SET consumed_at = now WHERE partner_sku=? AND week_tag=? AND consumed_at IS NULL
```

---

## 输出示例

```
inserted #42 [红] PDZ0027158 | 安时达UAE | 海运中-停留过久 (40d > 30d)
inserted #43 [蓝] PDZ0028823 | 义特无忧KSA | 清关完成-需约仓
updated  #41 [橙] PDZ0026554 | 飞坦 | 装柜出港-停留过久 (15d > 12d)

汇总：新增 2，更新 1
  级别分布：红=1 蓝=1 橙=1
```

---

## 下游消费

```sql
-- 当前 active 告警（按级别排序）
SELECT alert_id, order_no, forwarder, alert_reason, alert_level, ops_status
FROM wf6_logistics_alerts
WHERE resolved_at IS NULL
ORDER BY CASE alert_level WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 ELSE 4 END;

-- KSA 待补货（未消费的丢货）
SELECT partner_sku, lost_qty, order_no, forwarder
FROM wf6_hipop_ksa_replenishment_queue
WHERE consumed_at IS NULL;

-- 全 entity 丢货总览
SELECT 'KSA' AS s, partner_sku, SUM(lost_qty) AS qty FROM wf6_hipop_ksa_replenishment_queue WHERE consumed_at IS NULL GROUP BY partner_sku
UNION ALL
SELECT 'UAE',      partner_sku, SUM(lost_qty)        FROM wf6_hipop_uae_replenishment_queue WHERE consumed_at IS NULL GROUP BY partner_sku
ORDER BY qty DESC;
```

---

## 注意事项

- `wf6_logistics_alerts` **不要** per-entity 化——告警按货单维度，跨 entity 是物理事实
- `wf6_<alias>_replenishment_queue` **必须** per-entity——丢货必补回到具体销售主体的补货
- forwarder 名字尾缀（KSA / UAE）必须显式带，否则丢货必补会跳过该告警 + 打 stderr warning
- 飞书 alerts 表手工修复后下次 feishu_pull 才会同步回 DB
- 已下单 SKU 没 mark consumed_at → 检查飞书 wf5 表的 SKU 字段是否跟数据库 partner_sku 一致

---

## 执行指令

1. 跑 `wf_logistics_alerts.py:generate_alerts()`
2. 默认末尾 `sync_all(tables=["alerts", "warehouse_appt"])` 同步飞书
3. feishu_pull 每 30 分钟触发反向同步（独立 launchd job `com.hipop.pull`）
4. 终端打印：新增/更新告警条数 + 级别分布

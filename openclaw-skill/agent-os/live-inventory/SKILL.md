---
name: agent-os-live-inventory
display_name: Agent OS · 实时库存
version: 0.1.0
author: hipop
description: chat Agent 回答库存类问题（库存够不够、可撑多少天、各仓分布）的能力。数据源 erp_stock（6 仓）+ noon_stock；先走 fact-source-contract 查新鲜度，erp_stock 陈旧 run_workflow(wf1_stock)，noon_stock（needs_csv）引导上传。在途货物不算可售库存，缺数据不编库存数字。
tags: [hipop, agent-os, inventory, stock]
---

# 实时库存

回答「库存够不够 / 还能撑几天 / 各仓多少」。前置见
[`fact-source-contract`](../fact-source-contract/SKILL.md)。

## 依赖源 & intent

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| 库存够不够 | `stock` | erp_stock + noon_stock | `query_sku`（库存可撑天 / 在途字段） |

- **erp_stock** = `auto`：陈旧 → `run_workflow("wf1_stock", followup_prompt=用户原问)`。
  wf1 = ERP 6 仓 + noon Inventory，写 `wf1_<alias>_stock`，affected_modules: sales / replenish。
- **noon_stock** = `needs_csv`：陈旧 → 引导上传 noon Inventory CSV，**Agent 不能代跑**。

## 口径红线（事实源契约）

- **总库存合计口径单一真相**：`TOTAL_STOCK_COMPONENTS`（见 `_factslot_contract.py`）。
  any 漂移 → 红灯。
- **在途 `in_transit_total_qty` 不计入可售库存**：在途货物不是可售库存，禁止加进 total_stock。
  在途归 [`live-logistics`](../live-logistics/SKILL.md) 域。
- 缺时间戳 / 缺来源标签的行**不能当事实**，缺数据时不编库存数字。

## list_products（库存盘点配套）

双维度统计：product 维度对齐 ERP 后台 + SKU 维度含变体，区分在售 / 未上架。
返回必带 `references` → 📎 数据出处。

回归：`tests/smoke_t12_stock_source_contract.py` / `smoke_t37_stock_refresh.py` /
`smoke_wf1_stock_merge_v2.py` / `smoke_inventory_refresh_confirm.py`（刷新需用户确认）。

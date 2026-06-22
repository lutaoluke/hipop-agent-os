---
name: agent-os-fact-source-contract
display_name: Agent OS · 事实源契约
version: 0.1.0
author: hipop
description: chat Agent 回答任何业务问题前的事实源契约——把用户意图映射到依赖的数据源，查每个源的新鲜度（automation/stale_days），据此落到「直接答 / 触发刷新 / 引导上传 / 坚持旧数据」四种行为，并让每个数字都能溯源到 📎 数据出处。是 live-sales/inventory/logistics、replenishment、workflow-execution 的共同前置。
tags: [hipop, agent-os, fact-source, freshness]
---

# 事实源契约（每次回答的前置）

决策流：**识别意图 → 查依赖源新鲜度 → 落到下面 4 种行为之一**。
数据来自 `data.get_data_health()`：每个源含 `latest / stale_days / automation / workflow / csv_pattern? / where?`。

## 数据源 → 自动度分类

| source | automation | 陈旧时 |
|---|---|---|
| erp_products / erp_sales / erp_stock | `auto` | `run_workflow`（wf2_sales / wf1_stock） |
| wf3_logistics / wf5_replenish / wf6_alerts | `auto` | `run_workflow`（对应） |
| **noon_orders** / **noon_stock** | **`needs_csv`** | 引导上传 noon CSV，**Agent 不能代跑** |

## 行为四象限

| 场景 | 行为 |
|---|---|
| 数据全齐 | 直接调查询 tool 答 + 📎 数据出处 |
| 依赖源 `auto` 陈旧 | `run_workflow(..., followup_prompt=用户原问)` → 完成后自动续答 |
| 依赖源 `needs_csv` 陈旧 | **不要 run_workflow**；给 `where`+`csv_pattern` 精确上传指引 → 用户拖到 📤 上传区 |
| 用户坚持用旧数据 | 不阻塞；开头警示陈旧+偏向（如 noon_orders 旧→销量低估），照常答，结尾提示「可上传 CSV / 说『刷新数据』」 |

## 意图 → 依赖源映射（9 种 intent）

`data.get_data_health()` 的 `dependency_groups`。完整表 + 每个 intent 推荐 tool 见
[`reference.md`](reference.md)。要点：补货=erp_sales+erp_stock+noon_orders+noon_stock+wf3+wf5；
SKU 健康=erp_sales+noon_orders+wf3+wf5；销量=erp_sales+noon_orders；库存=erp_stock+noon_stock。

## 📎 数据出处（reference 系统）

tool 调用带 `references` 字段 → 去重写入 `agent_actions` 表 → 前端气泡点 📎 弹窗显示
（哪张表 / where 子句 / as_of_date）。**chat 给的每个数字都能溯源到 SQL**，是运营透明性关键。

确定性契约常量（口径单一真相、缺时间戳/来源即红灯）在 `server/_factslot_contract.py`，
回归见 `tests/smoke_fact_source_contract.py` / `smoke_ws161_factslot_contract.py`。

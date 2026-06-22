---
name: agent-os-replenishment-query
display_name: Agent OS · 补货查询
version: 0.1.0
author: hipop
description: chat Agent 回答补货类问题（哪些要补货、补多少、海空运怎么选）的能力。补货建议来自 wf5 销售周期算法，依赖最全（erp_sales+erp_stock+noon_orders+noon_stock+wf3+wf5）；齐就调 compute_replenishment，任一陈旧按 fact-source-contract 触发刷新或引导上传。补货数字必须有证据，不编。
tags: [hipop, agent-os, replenishment, wf5]
---

# 补货查询

回答「哪些要补货 / 补多少 / 海空运怎么选」。补货是**依赖最全**的问题，前置见
[`fact-source-contract`](../fact-source-contract/SKILL.md)：任一依赖源陈旧都要先处理。

## 依赖源 & intent

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| 哪些要补货 / 补多少 | `replenishment` | erp_sales + erp_stock + noon_orders + noon_stock + wf3_logistics + wf5_replenish | `compute_replenishment` |
| 海空运怎么选 | `air_freight_roi` | erp_sales + noon_orders + wf5_replenish | `compute_air_freight_roi` |

- `auto` 源陈旧 → `run_workflow`（最常 `wf5_sales_cycle`，会重算销售周期+补货）。
- `needs_csv` 源（noon_orders / noon_stock）陈旧 → 引导上传，**不能代跑**。

## tool

| tool | 用途 | 写库 |
|---|---|---|
| `compute_replenishment` | 列当前店铺补货建议（来自 `wf5_<alias>_sales_cycle`） | 否 |
| `compute_air_freight_roi` | 单 SKU 海空运 ROI 估算 | 否 |

均返回 `references`（wf5 表 / where / as_of_date）→ 📎 数据出处。

## 红线（补货证据门）

- 补货建议**必须来自 wf5 写库结果**，缺数据/源陈旧时按四象限先刷新或上传，**不凭空给补货量**。
- 补货量、可撑天数等数字均须可溯源。

回归：`tests/smoke_replenish_workflow.py` / `smoke_replenish_algorithm.py` /
`smoke_t27_replenishment_evidence.py` / `smoke_t45_evidence_gate.py`（证据门：无证据不出补货数）。

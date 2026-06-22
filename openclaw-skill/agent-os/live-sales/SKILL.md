---
name: agent-os-live-sales
display_name: Agent OS · 实时销量
version: 0.1.0
author: hipop
description: chat Agent 回答销量类问题（某 SKU 趋势、销量数字、急速下降）的能力。数据源 erp_sales + noon_orders；先走 fact-source-contract 查新鲜度，齐就调 query_sku，erp_sales 陈旧就 run_workflow(wf2_sales)，noon_orders 陈旧（needs_csv）就引导上传。绝不在缺数据时编销量数字。
tags: [hipop, agent-os, sales, query_sku]
---

# 实时销量

回答「`<SKU>` 趋势 / 销量多少 / 哪些在涨在跌」。前置见
[`fact-source-contract`](../fact-source-contract/SKILL.md)：先查依赖源新鲜度再落行为。

## 依赖源 & intent

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| `<SKU>` 趋势 / 库存够不够 | `sku_health` | erp_sales + noon_orders + wf3_logistics + wf5_replenish | `query_sku` |
| 销量数字 | `sales_only` | erp_sales + noon_orders | `query_sku` |

- **erp_sales** = `auto`：陈旧 → `run_workflow("wf2_sales", followup_prompt=用户原问)`，
  完成后自动续答（见 [`workflow-execution`](../workflow-execution/SKILL.md)）。
- **noon_orders** = `needs_csv`：陈旧 → **不要 run_workflow**，给上传指引（`csv_pattern` + `where`），
  用户拖到 📤 上传区，`/api/upload` ingest 后续答。

## query_sku tool

查 SKU 健康：趋势 / 利润 / 库存可撑天 / 在途 / 告警，最多 3 个 SKU，不写库。
销量字段含 10/30/60/90/120/180d 多时间窗（来自 `wf2_<alias>_sku` 销量列）。
返回必带 `references`（哪张表 / where / as_of_date）→ 前端 📎 数据出处。

## 红线（反 hallucinate）

- 销量数字必须来自 tool 返回，**严禁凭记忆或推测编造**。
- noon_orders 旧 → 销量**低估**、新爆款看不到：用户坚持用旧数据时开头明确警示这一偏向。
- 时间戳只到**日期**粒度。

回归：`tests/smoke_t03_live_sales_path.py` / `smoke_t03_sku_sales_freshness.py` /
`smoke_sales_contract.py`（live 失败 → `live_sales_failed=True`，不回落编数）。

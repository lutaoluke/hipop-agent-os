---
name: hipop-agent-os
display_name: 点购 Agent OS — 索引
version: 0.2.0
author: hipop
description: 点购工作台 chat 协作 Agent 的能力索引。Agent OS 是消费层（FastAPI + Jinja2 + Alpine 工作台，右侧 chat 走 tool-use 调 11 个 tool），数据全部来自 hipop 工作流写入的表。本文件只做路由——9 个聚焦 skill 各自承载一域能力，复杂明细走渐进披露引用文件。
tags: [hipop, agent-os, index]
---

# 点购 Agent OS — 能力索引

工作目录：`/Users/luke/code/hipop`。Agent OS 是**消费层**：自己不写工作流逻辑，
只通过 `run_workflow` tool 触发已有 workflow，所有业务数字都能溯源到写库的工作流表。

> 旧版本是一份 423 行的单体 skill。已按能力域拆成下面 9 个 ≤80 行的聚焦 skill；
> 加载方应进各 skill 入口，不再依赖单体长文档。运行/部署等底座说明见 reference/。

## 9 个能力 skill（`openclaw-skill/agent-os/<slug>/SKILL.md`）

| slug | 域 | 一句话 |
|---|---|---|
| `fact-source-contract` | 事实源契约 | 数据源→自动度分类、意图→依赖源映射、新鲜度门、📎 数据出处 |
| `live-sales` | 实时销量 | 销量趋势/数字（erp_sales + noon_orders），陈旧时怎么取实时 |
| `live-inventory` | 实时库存 | 6 仓库存 + noon_stock，可撑天数，库存够不够 |
| `live-logistics` | 实时物流 | 在途/货到哪了/卡单（wf3_logistics），query_order |
| `replenishment-query` | 补货查询 | compute_replenishment（来自 wf5）+ 海空运 ROI |
| `workflow-execution` | 工作流执行 | run_workflow → SSE 进度 → 模块自动刷新 + 自动 follow-up |
| `rulebook` | 规则书 | chat 行为四象限 + SYSTEM_PROMPT 调用规则 + 反 hallucinate 三层 |
| `experience-eval` | 体验评估 | smoke 覆盖 + chat 持久化/切页面继承（chat continuity） |
| `governance-gate` | 治理门 | Auth + RBAC + 多租户 RLS + 门控 stub tool + actor 留痕 |

## 渐进披露引用（底座，非能力 skill）

- `agent-os/reference/runtime-ops.md` — 三种启动模式 / 健康检查 / Zeabur 部署 / DB 分派（SQLite↔PG）/ 关键依赖 / 已知 gotcha。
- `agent-os/reference/architecture.md` — server 目录结构 / 真多租户 v2 ingest 链路 / 扩展点（加店铺/tool/workflow/意图/数据源/模块页）。

## 与现有 hipop 工作流的关系

销量/SKU→`wf2_<alias>_sku`；库存→`wf1_<alias>_stock`；物流→`wf3_logistics_hub`；
补货→`wf5_<alias>_sales_cycle`；告警→`wf6_logistics_alerts`。Agent OS 只触发、不重写。

---
name: agent-os-live-logistics
display_name: Agent OS · 实时物流
version: 0.1.0
author: hipop
description: chat Agent 回答物流类问题（货到哪了、在途多少、卡单/告警、某货单 PDxxx 状态）的能力。数据源 wf3_logistics（+ wf6_alerts）；齐就调 query_order，陈旧 run_workflow(wf3_logistics)。反馈货单状态走 update_alert_status（写库前需确认意图）。在途≠可售库存。
tags: [hipop, agent-os, logistics, query_order]
---

# 实时物流

回答「货到哪了 / 在途多少 / 卡单 / `<PDxxx>` 怎么样 / 有哪些告警」。前置见
[`fact-source-contract`](../fact-source-contract/SKILL.md)。

## 依赖源 & intent

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| 货到哪了 / 在途 | `logistics_track` | wf3_logistics | `query_order` |
| 告警 / 卡单 / `<PDxxx>` | `alerts` | wf3_logistics + wf6_alerts | `query_order` |

- wf3_logistics / wf6_alerts = `auto`：陈旧 → `run_workflow("wf3_logistics" / "wf6_alerts", followup_prompt=...)`。
  affected_modules: logistics / replenish。
- 在途数据源 `wf3_logistics_hub`：每 SKU 按 (国家×平台×货代) 分组，含当前阶段 / 停留 / 历史基准。

## query_order tool

查货单告警 + 涉及 SKU + 处理状态，不写库。返回必带 `references` → 📎 数据出处。

## update_alert_status tool（写库）

反馈货单告警状态（写 `wf6_logistics_alerts`）。**涉及写入，需先确认用户意图再调**，
不像 run_workflow 那样可直接触发。终态联动补货队列。

## 红线

- 在途 `in_transit_total_qty` **不算可售库存**（归属本域，不进 [`live-inventory`](../live-inventory/SKILL.md) 的 total_stock）。
- 货单不存在时如实说「未找到」，不编造物流节点。

回归：`tests/smoke_wf3_logistics_t21.py`（T21 入口路由 + 证据降级）/ `smoke_t26_logistics_ext.py`。

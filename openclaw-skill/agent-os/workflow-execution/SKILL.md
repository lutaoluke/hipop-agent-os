---
name: agent-os-workflow-execution
display_name: Agent OS · 工作流执行
version: 0.1.0
author: hipop
description: chat Agent 异步触发后台工作流的能力。run_workflow tool 立即返回 task_id；前端订阅 SSE 显示进度（step_no 0→1..N→99），完成后按 affected_modules 自动刷新模块页，并用 followup_prompt 自动续答。run_workflow 不需二次确认；新增 workflow 只在 WORKFLOW_REGISTRY 加一项。详表见 reference.md。
tags: [hipop, agent-os, workflow, run_workflow, sse]
---

# 工作流执行

链路：`chat → run_workflow → SSE → 模块刷新 + 自动续答`。

## run_workflow tool（唯一触发口）

- **异步**触发后台工作流，**立即返回 `task_id`**（+ label / total_steps / affected_modules）。
- **不需要二次确认**（页面有进度条）。调完只告诉用户 task_id + 完成后会刷新哪些页面，
  **不要再 query 数据**（还没跑完）。
- 写 `agent_events`，前端订阅 `/api/events/stream/<task_id>` 渲染进度。

## WORKFLOW_REGISTRY（已注册）

| name | affected_modules |
|---|---|
| `wf1_stock` / `wf2_sales` | sales（+ replenish） |
| `wf3_logistics` / `wf6_alerts` | logistics / replenish |
| `wf5_sales_cycle` | sales / replenish |
| `daily` / `weekly` | 见 reference |

新增 workflow **只需在 `api.WORKFLOW_REGISTRY` 加一项**，无需改 agent.py / chat_panel.html。

## SSE 协议（要点）

step_no：`0`=初始化（带 affected_modules）/ `1..N`=步骤 / `99`=完成。
收到 99 → dispatch `workflow-done(affected_modules)` → 对应模块 `init()` 监听并自动 refetch。
完整事件 JSON 结构 / 连接保持策略 / 三种触发通道（chat·UI·cron，均留痕 `actor_*`）/
自动 follow-up 与 `/api/upload` 同协议，见 [`reference.md`](reference.md)。

## 自动 follow-up（关键体验）

`run_workflow(workflow, followup_prompt="用户原问")` → 跑完 step_no=99 → 前端把
followup_prompt 当新一轮 user 消息发回 → Agent 第二轮用**新鲜数据**给最终结论。

回归：`tests/smoke_t21_sub2_workflow_receipt.py` / `smoke_ws134_operational_numeric_tools.py` /
`smoke_workflow_failure_explicit.py`（失败显式，不静默成功）。

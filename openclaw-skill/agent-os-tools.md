---
name: agent-os-tools
display_name: 点购 Agent OS — chat 工具 + 意图路由规则
version: 1.0.0
author: hipop
description: Agent 调用规则：11 个 chat tool 的用途与调用时机、四象限行为决策、数据源自动度分类、意图→依赖源映射、自动 follow-up 链路。
tags: [agent-os, chat, tools, intent, hipop]
---

## chat 协作 Agent — 11 个 tool

定义在 `server/agent.py:TOOLS`。

**查询/计算类**（数据回答）:

| tool | 用途 | 写库 |
|---|---|---|
| `query_sku` | 查 SKU 健康（趋势/利润/库存可撑天/在途/告警），最多 3 个 SKU | 否 |
| `query_order` | 查货单告警 + 涉及 SKU + 处理状态 | 否 |
| `scope_overview` | 店铺概览（在售 SKU 数 / 急速下降 / 在途 / 红色告警）| 否 |
| `compute_replenishment` | 列当前店铺补货建议（来自 wf5）| 否 |
| `compute_air_freight_roi` | 单 SKU 海空运 ROI 估算 | 否 |
| `data_health_check` | 各表最新写入时间 + 自动度 + 依赖图 | 否 |
| `list_products` | **双维度统计**（product 维度对齐 ERP 后台 + SKU 维度含变体）| 否 |

**触发/写入类**:

| tool | 用途 | 写库 |
|---|---|---|
| `update_alert_status` | 反馈货单告警状态 | ✅ wf6_logistics_alerts |
| `run_workflow` | **异步触发后台工作流**，立即返回 task_id；前端订阅 SSE 显示进度 + 完成后自动刷新模块页 | ✅ agent_events |

**反 hallucinate 门控类**（拦 Qwen 类幻觉）:

| tool | 用途 | 必调场景 |
|---|---|---|
| `export_table` | 用户问"导出/Excel/给我表格" | 当前 stub 引导浏览器另存。**严禁绕过自己宣称"已生成 Excel"** |
| `navigate_user_to` | 用户问"打开 X 页面" | 返回真实 `localhost:8765/module/<name>` 路径。**严禁编造虚构域名** |
| `notify_via_feishu` | 用户问"发飞书/通知同事" | stub 返回"只读集成"。**严禁宣称"已发到飞书"** |

**调用规则**（写在 SYSTEM_PROMPT 里）：
- 优先调工具拿真数据再回答
- `update_alert_status` 涉及写入需确认意图后再调
- `run_workflow` 不需要二次确认（页面有进度条），调完直接告诉用户 task_id + 完成后会刷新哪些页面，**不要再 query 数据**（还没跑完）

## chat 行为四象限（v2，所有问题都遵守）

每次用户提问，Agent 内部决策流：**识别意图 → 查依赖源新鲜度 → 落到下面 4 种行为之一**。

| 用户场景 | Agent 行为 |
|---|---|
| **数据全齐** | 直接调对应查询 tool 答 + 📎 数据出处 |
| **依赖源 automation=auto 陈旧** | 调 `run_workflow` 带 `followup_prompt`（用户原始问题）→ 立即返回 task_id；前端 step_no=99 完成后自动重发 followup_prompt → Agent 第二轮用最新数据答 |
| **依赖源 automation=needs_csv 陈旧**（noon_orders / noon_stock） | **不要 run_workflow**！给精确上传指引（`sources[<src>].where` + `csv_pattern`）→ 用户拖到工作台 📤 上传区 → `/api/upload` 自动 ingest 后 step_no=99 触发 followup_prompt 续答 |
| **用户坚持用旧数据**（"就用现在的" / "不用更新" / "凑合给个" 等信号） | 不阻塞。开头明确警示陈旧细节 + 偏向（noon_orders 旧 → 销量低估、新爆款看不到等），照常调查询 tool 给数据，结尾"如需更准上传 CSV 或说『刷新数据』" |

## 数据源 → 自动度分类

| source | automation | 陈旧时怎么办 |
|---|---|---|
| erp_products / erp_sales / erp_stock | auto | run_workflow(wf2_sales 或 wf1_stock) |
| wf3_logistics / wf5_replenish / wf6_alerts | auto | run_workflow(对应) |
| **noon_orders** / **noon_stock** | **needs_csv** | 引导上传 noon CSV，**Agent 不能代跑** |

完整字段在 `data.get_data_health()` 返回的 `sources`：每个源含 `latest / stale_days / automation / workflow / csv_pattern? / where?`。

## 意图 → 依赖源映射（9 种 intent）

`data.get_data_health()` 返回的 `dependency_groups` 字段：

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| 哪些要补货 / 补多少 | `replenishment` | erp_sales + erp_stock + noon_orders + noon_stock + wf3_logistics + wf5_replenish | compute_replenishment |
| `<SKU>` 趋势 / 库存够不够 | `sku_health` | erp_sales + noon_orders + wf3_logistics + wf5_replenish | query_sku |
| 货到哪了 / 在途 | `logistics_track` | wf3_logistics | query_order |
| 告警 / 卡单 / `<PDxxx>` | `alerts` | wf3_logistics + wf6_alerts | query_order |
| 海空运怎么选 | `air_freight_roi` | erp_sales + noon_orders + wf5_replenish | compute_air_freight_roi |
| 商品总数 / SKU 数 / 未上架 | `products_count` | erp_products | list_products |
| 整体怎么样 / 概览 | `overview` | erp_sales + wf3_logistics + wf5_replenish + wf6_alerts | scope_overview |
| 销量数字 | `sales_only` | erp_sales + noon_orders | query_sku |
| 库存够不够 | `stock` | erp_stock + noon_stock | query_sku |

## 自动 follow-up 链路（关键体验）

`run_workflow(workflow, followup_prompt="...")` → 后台跑 → step_no=99 完成 →
- chat_panel.html 监听 SSE，dispatch `workflow-done` + 模块自动 refresh
- 如果 `followup_prompt` 不空 → setTimeout 800ms 后 `send({autoFollowup:true})` → 前端把 followup_prompt 当新一轮 user 消息发回 chat
- Agent 第二轮看到的是新鲜数据，调对应查询 tool 给最终结论

`/api/upload` 也走同样协议（接受 followup_prompt + 写 step_no=99）。上传场景：
- chat 里 Agent 给上传指引时引导用户**记住自己的原始问题**
- 用户上传时，前端可在 FormData 中携带 followup_prompt
- 跑完后通过 `chat-attach-task` 事件让 chat_panel 在气泡里 inline 显示进度 + 自动续问

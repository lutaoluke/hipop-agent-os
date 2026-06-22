# 工作流执行 · 完整明细（渐进披露）

供 [`SKILL.md`](SKILL.md) 引用。

## 触发链路（chat → run_workflow → SSE → 模块刷新）

```
[1] 用户在 chat 发：跑一下 KSA 销售周期
[2] Agent → run_workflow(workflow="wf5_sales_cycle")
[3] api.WORKFLOW_REGISTRY 解析 → 后台线程调 scripts.weekly_run:step_wf5
[4] 每 step 写 agent_events（step_no=0 init / 1..N steps / 99=终结，含 affected_modules）
[5] 立即返回 {task_id, label, total_steps, affected_modules}
[6] chat_panel.html attachTask() 订阅 /api/events/stream/<task_id>
    - inline 在 Agent 气泡里渲染进度（▶ → ✓ / ✗）
    - 同时 dispatch task-started 给顶部 progress_card
[7] 收到 step_no=99 → dispatch workflow-done(affected_modules)
[8] sales / logistics / replenish 模块 init() 监听该事件 → 自动 refetch
```

## WORKFLOW_REGISTRY（完整）

| name | label | affected_modules |
|---|---|---|
| `wf1_stock` | wf1 商品库存（ERP 6 仓 + noon Inventory） | sales / replenish |
| `wf2_sales` | wf2 商品总表 + 销量 | sales |
| `wf3_logistics` | wf3 物流采集 | logistics / replenish |
| `wf5_sales_cycle` | wf5 销售周期 + 补货 | sales / replenish |
| `wf6_alerts` | wf6 物流告警 | logistics / replenish |
| `daily` | 每日例行（wf3 + wf6 + 日报） | logistics / replenish |
| `weekly` | 每周例行全链路 | sales / logistics / replenish |

新增 workflow 只需在 `api.WORKFLOW_REGISTRY` 加一项，无需改 agent.py / chat_panel.html。

## 三种触发通道（都留痕 actor_*）

| 通道 | 入口 | actor_source | 备注 |
|---|---|---|---|
| chat | Agent 调 `run_workflow` tool | `chat` | user 来自 JWT，Agent 直接选 workflow |
| UI 按钮 | `sidebar.html` "数据刷新" 按钮 | `ui` | `POST /api/run-workflow {workflow, source:'ui'}`，Alpine `refreshPanel()` 跟进度 |
| 定时 cron | `server/scheduler.py` APScheduler 02:00 | `cron` | 每天给每 tenant 跑 `refresh_all_v2`；`DAILY_REFRESH_HOUR/MINUTE` 可调；`DISABLE_DAILY_REFRESH=1` 关 |

留痕字段：`agent_events.actor_user_id / actor_email / actor_role / actor_source`。审计：
```sql
SELECT task_id, status, actor_email, actor_role, actor_source, created_at
FROM agent_events WHERE step_no = 0 AND tenant_id = <tid>
ORDER BY created_at DESC LIMIT 50;
```

## SSE 协议（/api/events/stream/<task_id>）

```
{ "id":..., "task_id":..., "step_no": 0|1..N|99, "step_name":"...",
  "status":"started"|"done"|"error"|"skipped",
  "message":"...",   // step_no=0/99 时含 JSON: {workflow, label, affected_modules, total_steps}
  "created_at":"..." }
```
特殊 step_no：`0`=初始化（携带 affected_modules）；`99`=管道完成。
连接保持（已修 30s 误关 bug）：收到 step_no=99 后再 idle 5 tick 才关；否则 30 分钟硬超时。

## 自动 follow-up 链路

`run_workflow(workflow, followup_prompt="...")` → 后台跑 → step_no=99 完成 →
- chat_panel.html 监听 SSE，dispatch `workflow-done` + 模块自动 refresh；
- `followup_prompt` 不空 → setTimeout 800ms 后 `send({autoFollowup:true})` → 前端把
  followup_prompt 当新一轮 user 消息发回 chat；
- Agent 第二轮看到新鲜数据，调对应查询 tool 给最终结论。

`/api/upload` 走同样协议（接受 followup_prompt + 写 step_no=99）：Agent 给上传指引时引导用户
记住原始问题；用户上传时前端在 FormData 携带 followup_prompt；跑完通过 `chat-attach-task`
事件让 chat_panel 在气泡里 inline 显示进度 + 自动续问。

---
name: agent-os
display_name: 点购 Agent OS — 工作台 + 协作 Agent + 工作流触发
version: 0.1.0
author: hipop
description: FastAPI + Jinja2 + Alpine 的本地工作台，右侧 chat 通过 Anthropic tool-use 调用 8 个 tool；用户在 chat 触发后台工作流后，进度实时推到气泡 + 完成后受影响模块页自动刷新。OAuth 走 macOS keychain 不需要 ANTHROPIC_API_KEY。chat 历史落 sqlite。
tags: [agent-os, fastapi, claude-tool-use, sse, chat, hipop]
---

点购 Agent OS — 工作台 + 协作 Agent + 工作流触发（hipop/server）：FastAPI + Jinja2 + Alpine 的本地工作台，右侧 chat 通过 Anthropic tool-use 调用 7 个 tool；触发工作流后实时进度推到气泡 + 模块页自动刷新。

工作目录：/Users/luke/Downloads/点购工作流

## 启动

```bash
cd /Users/luke/Downloads/点购工作流
python3 -m uvicorn hipop.server.main:app --host 0.0.0.0 --port 8765
# 或 ./start_server.sh（带 cloudflared 隧道公网暴露）
```

打开 http://localhost:8765 看工作台。

**不需要 ANTHROPIC_API_KEY**：`server/_auth.py` 优先读 keychain 里 Claude Code 的 OAuth token（macOS `security find-generic-password -s "Claude Code-credentials"`），加 `anthropic-beta: oauth-2025-04-20` header 走 claude.ai 订阅配额。`/login` 后 token 轮换会自动重读（agent.py 捕获 `AuthenticationError` 后调 `_auth.reset()` 重试一次）。

## 架构

```
hipop/server/
├─ main.py            FastAPI app + 飞书 webhook（/feishu/webhook）
├─ pages.py           7 个模块页路由（overview / sales / logistics / replenish / selection / feishu / audit + role/liuhe）
├─ api.py             /api/* JSON 接口 + /api/run-workflow + /api/events/stream/<task_id> SSE
├─ data.py            统一访问 hipop.db（含 chat_messages 持久化 + agent_actions reference 表）
├─ agent.py           Anthropic tool-use chat（默认 haiku-4-5，可 ANTHROPIC_CHAT_MODEL 覆盖）
├─ _auth.py           凭据工厂：env > macOS keychain OAuth
├─ intent.py          飞书消息意图识别（旧链路，给 webhook 用）
├─ skills.py          飞书消息工作流派发（旧链路）
└─ templates/         Jinja2 模板
   ├─ base.html
   ├─ overview.html / module_*.html / role_liuhe.html
   └─ partials/chat_panel.html  右侧 chat（订阅 SSE + dispatch workflow-done）
```

## chat 协作 Agent — 8 个 tool

定义在 `server/agent.py:TOOLS`，由 Claude `messages.create(tools=...)` 自动选用。

| tool | 用途 | 写库 |
|---|---|---|
| `query_sku` | 查 SKU 健康（趋势/利润/库存可撑天/在途/告警），最多 3 个 SKU | 否 |
| `query_order` | 查货单告警 + 涉及 SKU + 处理状态 | 否 |
| `update_alert_status` | 反馈货单告警状态（已确认丢货 / 已约仓 ...）| ✅ wf6_logistics_alerts |
| `scope_overview` | 店铺概览（在售 SKU 数 / 急速下降 / 在途 / 红色告警）| 否 |
| `compute_replenishment` | 列当前店铺补货建议（来自 wf5）| 否 |
| `compute_air_freight_roi` | 单 SKU 海空运 ROI 估算 | 否 |
| `data_health_check` | 各表最新写入时间 | 否 |
| `list_products` | **双维度统计**（product 维度对齐 ERP 后台 + SKU 维度含变体），用户问"商品总数"时优先报 product | 否 |
| `run_workflow` | **异步触发后台工作流**，立即返回 task_id；前端订阅 SSE 显示 inline 进度 + 完成后自动刷新模块页 | ✅ agent_events |

**调用规则**（写在 SYSTEM_PROMPT 里）：
- 优先调工具拿真数据再回答
- `update_alert_status` 涉及写入需确认意图后再调
- `run_workflow` 不需要二次确认（页面有进度条），调完直接告诉用户 task_id + 完成后会刷新哪些页面，**不要再 query 数据**（还没跑完）

## 工作流触发链路（chat → run_workflow → SSE → 模块刷新）

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
[8] sales / logistics / replenish 模块的 init() 监听该事件 → 自动 refetch
```

`WORKFLOW_REGISTRY` 已注册的 workflow:

| name | label | affected_modules |
|---|---|---|
| `wf1_stock` | wf1 商品库存（ERP 6 仓 + noon Inventory）| sales / replenish |
| `wf2_sales` | wf2 商品总表 + 销量 | sales |
| `wf3_logistics` | wf3 物流采集 | logistics / replenish |
| `wf5_sales_cycle` | wf5 销售周期 + 补货 | sales / replenish |
| `wf6_alerts` | wf6 物流告警 | logistics / replenish |
| `daily` | 每日例行（wf3 + wf6 + 日报）| logistics / replenish |
| `weekly` | 每周例行全链路 | sales / logistics / replenish |

新增 workflow 只需在 `api.WORKFLOW_REGISTRY` 加一项，无需改 agent.py / chat_panel.html。

## SSE 协议（/api/events/stream/<task_id>）

事件 JSON 结构：
```
{ "id": ..., "task_id": ..., "step_no": 0|1..N|99, "step_name": "...",
  "status": "started"|"done"|"error"|"skipped",
  "message": "...",  // step_no=0 / 99 时含 JSON: {workflow, label, affected_modules, total_steps}
  "created_at": "..." }
```

特殊 step_no:
- `0` = 初始化（携带 affected_modules）
- `99` = 管道完成

连接保持策略（已修 30s 误关 bug）：收到 step_no=99 后再 idle 5 tick 才关；否则 30 分钟硬超时。

## chat 持久化

- 表：`chat_messages(id, store, role, who, content, tag, references_json, task_json, created_at)`
- 写入：`/api/chat` 端点保存 user 最后一条 + agent 回复（含 references / workflow_task）
- 读取：`/api/chat-history/<store>` 默认 100 条，空库时回退 `mock.CHAT_HISTORY_MOCK` 当 seed
- 自动建表：`data._ensure_chat_table()` 在首次 write/get 时调用
- 历史里的 task 在前端被 normalize 为"已完成"态（避免历史触发记录看似还在跑）

## reference 系统（agent_actions 表）

每次 tool 调用如果带 `references` 字段会被去重写入 `agent_actions` 表，前端气泡里点 📎 弹窗显示数据出处（哪张表 / where 子句 / as_of_date）。这是给运营透明性的关键 — chat 给的每个数字都能溯源到 SQL。

## 关键依赖

```
fastapi uvicorn jinja2          # web
anthropic                       # chat tool-use
sqlite3                         # 内置
playwright (chromium)           # ERP token（少数路径，多数走 CDP raw）
websocket-client                # CDP raw 抓 ERP token
```

## 已知 gotcha

1. **chrome remote debug 9222 必须带 `--remote-allow-origins='*'` 启动**，否则 ws 握手 403：
   ```
   open -na "Google Chrome" --args --remote-debugging-port=9222 \
     --remote-allow-origins='*' \
     --user-data-dir="$HOME/.openclaw/browser-profiles/erp-debug" \
     "https://www.dbuyerp.com/product/list"
   ```
2. **系统 HTTP 代理（clash/v2ray 等）会拦截 127.0.0.1:9222 请求**：所有走 chrome devtools 的代码必须用 `requests.Session().trust_env = False` + `proxies={"http":None,"https":None}`。
3. **OAuth 订阅配额**：sonnet-4-5 在 5h 窗口内容易耗尽。当前默认模型用 haiku-4-5（够用），需要换 sonnet 时 `export ANTHROPIC_CHAT_MODEL=claude-sonnet-4-5-20250929` 重启 server。
4. **chrome 多 tab 时 playwright connect_over_cdp 180s 超时**：所以 token 抓取链优先 CDP raw（websocket 直接读 localStorage）→ playwright 兜底 → wf0 headless 登录。

## 模块页 → workflow 自动刷新约定

每个 module template 在 init() 里挂 `window.addEventListener('workflow-done', ...)`，命中自家 module 名（在 `affected_modules` 数组里）就 refresh。新增模块时同步加这段：

```js
async init() {
  await this.refresh();
  window.addEventListener('workflow-done', (e) => {
    if ((e.detail.affected_modules || []).includes('YOUR_MODULE')) this.refresh();
  });
},
async refresh() { ... }  // 原 init 里的 fetch 逻辑搬到这里
```

## 开发建议

- 加新 tool：编辑 `agent.py:TOOLS` + `TOOL_FUNCS` + 实现函数；让函数返回 `references` 字段以便前端 📎 出处
- 加新 workflow：编辑 `api.py:WORKFLOW_REGISTRY` 加一项，指向 callable（`module:func` 字符串）
- 加新模块页：`pages.py` + 新 template + 在 `affected_modules` 体系里登记
- 调试 SSE：`curl -sN http://localhost:8765/api/events/stream/<task_id>` 看流
- 调试 chat：`curl -X POST http://localhost:8765/api/chat -d '{"messages":[...],"scope":{"store":"KSA",...}}'`

## 与现有 hipop 工作流的关系

agent_os 是**消费层**，所有数据都来自工作流写入的表：
- 销量/SKU → `wf2_<alias>_sku`（hipop-wf2）
- 库存 → `wf1_<alias>_stock`（hipop-wf1）
- 物流 → `wf3_logistics_hub`（hipop-wf3，daily 跑）
- 补货 → `wf5_<alias>_sales_cycle`（hipop-wf5，weekly 跑）
- 告警 → `wf6_logistics_alerts`（hipop-wf6）

agent_os 自己不写工作流逻辑，只通过 `run_workflow` tool 触发已有 workflow 入口。

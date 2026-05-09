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

**LLM provider 切换**：`LLM_PROVIDER=qwen|anthropic|deepseek|doubao`，**默认 qwen**（与产品化国内栈对齐：¥18/万次 + 阿里云内网 + ICP 备案过 + 实测防护下不 hallucinate）。
- **qwen / deepseek / doubao**：走 OpenAI 协议（`server/_provider_openai.py`），只换 `base_url + api_key + model`。
- **anthropic**（本地开发可选）：`server/_auth.py` 优先 `ANTHROPIC_API_KEY`，回退 macOS keychain Claude Code OAuth token（免费走订阅）；`/login` 后 token 轮换自动重读（捕 `AuthenticationError` 后 `_auth.reset()` 重试一次）。
- 抽象在 `server/_provider.py:chat_with_tools()`，统一返回 `{reply, tool_log, refs_collected, workflow_task}`。

**反 hallucinate 三层防护**（必须配套 Qwen 部署）：
1. **Prompt 硬约束**（`agent.py:SYSTEM_PROMPT` 6 条强制规则）：业务数据必须先调 tool；严禁宣称未做的事；禁编 URL；用户报告状态变化必须重调 tool 验证；时间戳只到日期粒度；表格列限定真实字段。
2. **3 个 stub 门控 tool**（`agent.py:TOOLS`）：`export_table` / `navigate_user_to` / `notify_via_feishu`，劫持"导出/打开页面/发飞书"这三个最常 hallucinate 的触发点。
3. **`_safety.py` 后处理**：扫 reply 里的未授权域名 / 精确时间戳 / wf5 不存在字段 / 假宣称 → 命中加 banner + 写入 `hallucination_warnings` 字段透回前端。

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

**反 hallucinate 门控类**（2026-05-08 加，拦 Qwen 类幻觉）:

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

## DB 分派（SQLite ↔ Postgres）

`server/data.py:conn()` 按 `DB_URL` env 分派：
- 不设 → 本地 SQLite at `HIPOP_DB`（默认 `hipop.db`），dev 模式
- `DB_URL=postgresql://...` → 生产 PG，`_PGConnWrapper` 包 `psycopg2 + RealDictCursor` 让 SQL 行为透明
- `_convert_sql_for_pg()` 自动转换：`?→%s` / `datetime('now','localtime')→NOW()` / `date('now')→CURRENT_DATE`
- 所有业务代码透明（`_fetch` / `_scalar` 不变）

**切到 PG 的步骤**:
```bash
docker compose up -d postgres redis     # 起 PG + Redis（schema.sql 自动执行）
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  python scripts/migrate_sqlite_to_pg.py   # 一次性迁数据
DB_URL=postgresql://... QWEN_API_KEY=... \
  python -m uvicorn hipop.server.main:app --port 8765
```

infrastructure 文件: `docker-compose.yml` / `db/schema.sql` / `scripts/migrate_sqlite_to_pg.py`

## 关键依赖

```
fastapi uvicorn jinja2          # web
anthropic                       # chat tool-use（fallback）
openai                          # qwen / deepseek / 豆包（默认）
psycopg2-binary                 # PG（DB_URL 设置时）
sqlite3                         # 内置（默认）
playwright (chromium)           # ERP token 兜底
websocket-client                # ERP token 主路径（CDP raw）
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

## 扩展点（要扩什么改哪些文件）

### 1. 加新销售主体（开新店铺）
- `config/hipop.json` → `sales_entities[]` 加一项（含 `alias / country / store / store_id / currency`）
- 跑 `python3 hipop/scripts/wf2_feishu_setup.py --alias <new>` 建对应飞书表
- 跑 ingest 链 → 表自动建好

### 2. 加新 chat tool
1. `server/agent.py:TOOLS` 加 schema
2. `TOOL_FUNCS` 注册实现函数
3. 实现函数务必返回 `references` 字段（用于前端 📎 数据出处）
4. SYSTEM_PROMPT 的"问题→工具映射表"加一行（让 Agent 知道何时调）

### 3. 加新 workflow
1. `server/api.py:WORKFLOW_REGISTRY` 加一项 `(label, [(step_no, step_name, "module:func"), ...], affected_modules)`
2. 实现 callable（一般在 `scripts/weekly_run.py` 或新文件）
3. `agent.py:run_workflow` tool 的 `enum` 列表加新名字
4. `data.py:dependency_groups` 加新 intent 时把它列入

### 4. 加新意图（用户问新类型问题）
1. `server/data.py:dependency_groups` 加 intent → 依赖源 list
2. `agent.py` SYSTEM_PROMPT 的"意图 → 依赖源 + 推荐 tool"表加一行
3. （如果数据源是新的）`get_data_health()` 的 `sources` 字典加该源（带 automation / workflow / csv_pattern）

### 5. 加新数据源
1. `data.py:get_data_health()` `sources` 加该源（最重要的字段：`stale_days / automation / workflow`）
2. 如果是 needs_csv 类，加 `csv_pattern` + `where`（导出步骤指引）
3. 在相关 intent 的 `dependency_groups` 里加它

### 6. 加新模块页
1. `server/pages.py` 加路由
2. `templates/module_<name>.html`：在 `init()` 里挂 `workflow-done` 监听，命中自家 module 名（在 `affected_modules` 数组里）就 `refresh()`
3. `partials/sidebar.html` 加导航入口
4. `WORKFLOW_REGISTRY` 里把跟这个模块相关的 workflow 的 `affected_modules` 加上新模块名

## 开发建议（调试）

- 调试 SSE：`curl -sN http://localhost:8765/api/events/stream/<task_id>` 看实时流
- 调试 chat：`curl -X POST http://localhost:8765/api/chat -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"..."}],"scope":{"store":"KSA","current_user":"Cherry","current_role":"运营"}}'`
- 调试 workflow：`curl -X POST http://localhost:8765/api/run-workflow -H 'Content-Type: application/json' -d '{"workflow":"wf6_alerts"}'`
- 看 chat 历史：`sqlite3 hipop.db "SELECT role, who, substr(content,1,80), created_at FROM chat_messages ORDER BY id DESC LIMIT 20"`
- 看最近 task：`sqlite3 hipop.db "SELECT task_id, MAX(step_no), MAX(created_at) FROM agent_events GROUP BY task_id ORDER BY MAX(id) DESC LIMIT 5"`

## 与现有 hipop 工作流的关系

agent_os 是**消费层**，所有数据都来自工作流写入的表：
- 销量/SKU → `wf2_<alias>_sku`（hipop-wf2）
- 库存 → `wf1_<alias>_stock`（hipop-wf1）
- 物流 → `wf3_logistics_hub`（hipop-wf3，daily 跑）
- 补货 → `wf5_<alias>_sales_cycle`（hipop-wf5，weekly 跑）
- 告警 → `wf6_logistics_alerts`（hipop-wf6）

agent_os 自己不写工作流逻辑，只通过 `run_workflow` tool 触发已有 workflow 入口。

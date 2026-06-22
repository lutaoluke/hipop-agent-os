---
name: agent-os-server
display_name: 点购 Agent OS — 服务端架构与扩展参考
version: 1.0.0
author: hipop
description: 服务端参考：真多租户 v2 架构、工作流触发链路（chat/UI/cron）、SSE 协议、chat 持久化、Auth+RBAC、DB 分派、扩展点、调试命令。开发者文档，不含 Agent 调用规则（见 agent-os-tools.md）。
tags: [agent-os, server, architecture, multitenant, hipop]
---

## 真多租户 v2 链路（Phase A-E，2026-05-09→11）

**业务表全部列存化**：`wf2_sku / wf2_orders / wf1_stock / wf5_sales_cycle / wf3_logistics_hub_v2 / wf6_logistics_alerts_v2 / wf6_replenishment_queue_v2`，主键 `(tenant_id, entity_alias, partner_sku)`，**老物理切表 wf2_hipop_<a>_sku 等保留作为 hipop 老 cron 用**。

**PG RLS 真隔离**: 所有 v2 表 `ENABLE + FORCE ROW LEVEL SECURITY`（`FORCE` 关键，否则 owner bypass）+ policy `tenant_id = current_setting('app.current_tenant')::BIGINT`。SQLite 不支持 RLS，靠应用层 WHERE 过滤。

**ERP 凭据加密**: `tenant_erp_credentials` 表 + `_crypto.py` Fernet 对称加密（key 派生自 JWT_SECRET）。新公司 onboarding 填密码即加密存。

**ERP 后端登录**: `_erp_auth.get_erp_token_for_tenant(tid)` — 解密凭据 + playwright headless 登 dbuyerp + 拦截 erp-api 请求拿 Bearer + 缓存 20 min。**不再依赖本机 chrome 9222**，server 自给。

**v2 ingest pipeline**（per-tenant，从 onboarding 配的 ERP 自动拉）:
- `ingest_erp_products_v2.run_v2(tenant_id)` → wf2_sku
- `ingest_erp_sales_v2.run_v2(tenant_id)` → wf2_sku 销量字段（10/30/60/90/120/180d）
- `ingest_erp_stock_v2.run_v2(tenant_id)` → wf1_stock
- `ingest_noon_csv_v2.process_csv_v2(tenant_id, path)` → wf2_orders（用户上传 noon CSV）
- `wf_sales_cycle.run_v2(tenant_id)` → wf5_sales_cycle（销售周期算法）

**WORKFLOW_REGISTRY 加 5 个 v2 workflow**（chat 可触发 / API 可调）:
| name | 内容 |
|---|---|
| `wf2_products_v2` | 商品库（per-tenant ERP 拉 + 写 v2） |
| `wf2_sales_v2` | 商品 + 销量价格 6 时间窗 |
| `wf1_stock_v2` | 6 仓库存 |
| `wf5_sales_cycle_v2` | 销售周期 + 补货决策（v2 算法） |
| **`refresh_all_v2`** | 4 步全套（一键刷新）|

**`_run_workflow(task_id, workflow, tenant_id)`** background 线程显式 `set_current_tenant(tid)`（middleware 不覆盖线程）+ `inspect.signature` 探测 fn 是否接 `tenant_id` 参数自动注入。

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

### 三种触发通道（都留痕 `actor_*`）

| 通道 | 入口 | actor_source | 备注 |
|---|---|---|---|
| chat | Agent 调 `run_workflow` tool | `chat` | user 来自 JWT，Agent 直接选 workflow |
| UI 按钮 | `sidebar.html` "数据刷新" 4 个按钮 | `ui` | 调 `POST /api/run-workflow {workflow, source:'ui'}`，Alpine `refreshPanel()` 跟进度 |
| 定时 cron | `server/scheduler.py` APScheduler 02:00 | `cron` | 每天给每个 tenant 跑 `refresh_all_v2`；env `DAILY_REFRESH_HOUR/MINUTE` 可调；`DISABLE_DAILY_REFRESH=1` 关闭 |

留痕字段：`agent_events.actor_user_id / actor_email / actor_role / actor_source`。审计查询：
```sql
SELECT task_id, status, actor_email, actor_role, actor_source, created_at
FROM agent_events
WHERE step_no = 0 AND tenant_id = <tid>
ORDER BY created_at DESC LIMIT 50;
```

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

- 表：`chat_messages(id, tenant_id, store, role, who, content, tag, references_json, task_json, created_at)`
- 写入：`/api/chat` 端点保存 user 最后一条 + agent 回复（含 references / workflow_task）。PG 模式下 INSERT 必须显式带 tenant_id（RLS WITH CHECK 要求）
- 读取：`/api/chat-history/<store>` 默认 100 条，空库时回退 `mock.CHAT_HISTORY_MOCK` 当 seed
- 自动建表：`data._ensure_chat_table()` 在首次 write/get 时调用
- 历史里的 task 在前端被 normalize 为"已完成"态（避免历史触发记录看似还在跑）

**关键失败模式**：`/api/chat-history` 一旦返 500，`Promise.all` reject → `init()` 抛错 → Alpine 整个 chat panel 组件 init 失败 → 表现为"切页面无法继承聊天记录"。

**已踩坑**：PG 模式下 `created_at` 是 `datetime` 对象，旧代码 `(r["created_at"] or "")[-8:-3]` 抛 `TypeError: 'datetime.datetime' object is not subscriptable`。修于 commit `2b265dc`：所有 datetime 字段渲染必走 `data._hhmm(v)` / `data._date10(v)` helper，禁止 inline slice。

**防回归**：`tests/smoke_chat.py` 的 `check_chat_history_endpoint(base_url)` 作为前置 case 跑——任何对 chat_messages 渲染链路的改动，必须保证它 200 且 `time` 字段格式为 `'HH:MM'`。

## reference 系统（agent_actions 表）

每次 tool 调用如果带 `references` 字段会被去重写入 `agent_actions` 表，前端气泡里点 📎 弹窗显示数据出处（哪张表 / where 子句 / as_of_date）。这是给运营透明性的关键 — chat 给的每个数字都能溯源到 SQL。

## Auth + RBAC + 多租户 (W2，2026-05-09)

### Auth (`server/auth.py`)
- `tenants(id, name, plan)` + `users(id, tenant_id, email, role, password_hash)` + `sessions`
- 4 角色：`owner` / `manager` / `ops` / `forwarder`
- 密码 pbkdf2_sha256（避免 bcrypt 5.x 与 passlib 不兼容）
- JWT cookie/header 双通道（`JWT_SECRET` env 必须生产固定）
- **兼容 fallback**：未登录 → DEFAULT_USER（Cherry, owner, tenant=1），不破坏现有 single-tenant 调用

### RBAC (`server/rbac.py`)
- `PERMISSIONS` 矩阵（12 个 action × 4 角色）
- `TOOL_PERMISSION` 把 chat tool 名映射到 action（chat 调 tool 前过 RBAC）
- `can(user, action)` / `tool_allowed(user, tool_name)` / `require_permission(action)` decorator
- 2026-05-12：**工作组所有角色（含 forwarder）都能 `trigger_workflow` / `upload_csv`**，行为靠 `agent_events.actor_*` 留痕审计而非角色阻挡。view_billing / edit_store_config 等管理类仍限 owner。

### 多租户 RLS
- 17 张业务表加 `tenant_id BIGINT NOT NULL DEFAULT 1`（旧数据自动归 HIPOP）
- Postgres `ENABLE ROW LEVEL SECURITY` + policy `tenant_id = current_setting('app.current_tenant')::BIGINT`
- `data.py` middleware 在每个请求拿 user.tenant_id → `set_current_tenant(tid)` → conn() 拿到时 `SET app.current_tenant`

## DB 分派（SQLite ↔ Postgres）

`server/data.py:conn()` 按 `DB_URL` env 分派：
- 不设 → 本地 SQLite at `HIPOP_DB`（默认 `hipop.db`），dev 模式
- `DB_URL=postgresql://...` → 生产 PG，`_PGConnWrapper` 包 `psycopg2 + RealDictCursor` 让 SQL 行为透明
- `_convert_sql_for_pg()` 自动转换：`?→%s` / `datetime('now','localtime')→NOW()` / `date('now')→CURRENT_DATE`

**切到 PG 的步骤**:
```bash
docker compose up -d postgres redis     # 起 PG + Redis（schema.sql 自动执行）
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  python scripts/migrate_sqlite_to_pg.py
DB_URL=postgresql://... QWEN_API_KEY=... \
  python -m uvicorn hipop.server.main:app --port 8765
```

## 模块页 → workflow 自动刷新约定

每个 module template 在 init() 里挂 `window.addEventListener('workflow-done', ...)`，命中自家 module 名就 refresh。新增模块时同步加这段：

```js
async init() {
  await this.refresh();
  window.addEventListener('workflow-done', (e) => {
    if ((e.detail.affected_modules || []).includes('YOUR_MODULE')) this.refresh();
  });
},
```

## 扩展点（要扩什么改哪些文件）

### 1. 加新销售主体
- `config/hipop.json` → `sales_entities[]` 加一项
- 跑 `python3 hipop/scripts/wf2_feishu_setup.py --alias <new>` 建对应飞书表

### 2. 加新 chat tool
1. `server/agent.py:TOOLS` 加 schema
2. `TOOL_FUNCS` 注册实现函数
3. 实现函数务必返回 `references` 字段
4. SYSTEM_PROMPT 的"问题→工具映射表"加一行

### 3. 加新 workflow
1. `server/api.py:WORKFLOW_REGISTRY` 加一项
2. 实现 callable（一般在 `scripts/weekly_run.py` 或新文件）
3. `agent.py:run_workflow` tool 的 `enum` 列表加新名字
4. `data.py:dependency_groups` 加新 intent 时把它列入

### 4. 加新意图
1. `server/data.py:dependency_groups` 加 intent → 依赖源 list
2. `agent.py` SYSTEM_PROMPT 的"意图 → 依赖源 + 推荐 tool"表加一行
3. （如果数据源是新的）`get_data_health()` 的 `sources` 字典加该源

### 5. 加新模块页
1. `server/pages.py` 加路由
2. `templates/module_<name>.html`：在 `init()` 里挂 `workflow-done` 监听
3. `partials/sidebar.html` 加导航入口
4. `WORKFLOW_REGISTRY` 里把相关 workflow 的 `affected_modules` 加上新模块名

## 开发建议（调试）

- 调试 SSE：`curl -sN http://localhost:8765/api/events/stream/<task_id>` 看实时流
- 调试 chat：`curl -X POST http://localhost:8765/api/chat -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"..."}],"scope":{"store":"KSA","current_user":"Cherry","current_role":"运营"}}'`
- 调试 workflow：`curl -X POST http://localhost:8765/api/run-workflow -H 'Content-Type: application/json' -d '{"workflow":"wf6_alerts"}'`
- 看 chat 历史：`sqlite3 hipop.db "SELECT role, who, substr(content,1,80), created_at FROM chat_messages ORDER BY id DESC LIMIT 20"`
- 看最近 task：`sqlite3 hipop.db "SELECT task_id, MAX(step_no), MAX(created_at) FROM agent_events GROUP BY task_id ORDER BY MAX(id) DESC LIMIT 5"`

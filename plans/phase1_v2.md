# 阶段 1：单租户云端化（v2 国内栈）— 工程任务拆解

> 起草：2026-05-08
> 目标：3-4 周内，把当前 POC 搬到云端，让 Luke 自己 + 1 个外部 alpha 客户能用，团队 5 人级别在 chat 里协同。
> 验收：alpha 客户 ERP 凭据接入 30 分钟跑出 dashboard。

---

## 总览：4 周节奏

| 周 | 重点 | 验收 |
|---|---|---|
| W1 | DeepSeek provider 抽象 + Postgres 本地迁移 | chat 能切换两个 provider；DB 跑通本地 docker postgres |
| W2 | Auth + 多用户 + RBAC | 4 角色登录跑通，团队同事在共同 chat 看到彼此 |
| W3 | Zeabur 部署 + ICP 备案启动 | 公网域名访问 demo 站点 |
| W4 | alpha 客户 onboarding + 收反馈 | 1 个外部团队的 KSA 数据接入并跑出补货建议 |

---

## W1：模型抽象 + DB 切换

### Task 1.1 — provider 抽象 ✅（已完成 2026-05-08）

**目标**：`server/_provider.py` 统一 chat 接口，`LLM_PROVIDER` env 切换 anthropic / qwen / deepseek / doubao。

**生产默认**：**Qwen-Plus**（性价比最高 ¥18/万次 + 阿里云内网集成 + ICP 备案过）。
本地开发：anthropic（OAuth keychain 免费）。

**交付**（已实现）：
- `server/_provider.py`：`chat_with_tools(messages, system, tools, tool_funcs, scope) → ChatResult`
- `_provider_anthropic.py`：Anthropic tool_use loop（含 OAuth 401 自动重读 keychain）
- `_provider_openai.py`：OpenAI 协议兼容 — Qwen/DeepSeek/豆包 三家共一份代码，只换 base_url + key + model
- `agent.py:chat()` 重构走抽象层，response 加 `provider` 字段
- `requirements.txt` 加 `openai>=1.50`
- `.env.example` 模板

**验收**:
- ✅ `LLM_PROVIDER=anthropic curl /api/chat` 现有行为不变（实测 scope_overview tool 正确）
- ✅ `LLM_PROVIDER=qwen curl /api/chat`（缺 key fail-fast 正确报错）
- ⏳ Qwen 实测命中率（要 Luke 提供 QWEN_API_KEY 后跑）

**实现要点（坑已踩）**:
- DeepSeek/Qwen 偶尔不调 tool 直接答 → SYSTEM_PROMPT 已写"必须先调 tool 拿数据"硬约束
- tool_calls 字段名差异：Anthropic `tool_use_id` vs OpenAI `tool_call_id` — 已在 _provider_openai 中处理
- 多轮 stop 条件不同：`stop_reason=tool_use` vs `finish_reason=tool_calls` — 已分别判断
- assistant 历史含 mixed content blocks（text + tool_use）→ openai 风格转换时只取 text，tool_result 转成 'tool' role 消息

### Task 1.2 — Postgres 迁移 ✅（已完成 2026-05-09）

**目标**：infrastructure 全准备 + data.py DB_URL 分派；本地 SQLite 默认不破坏。

**交付**（已实现）:
- ✅ `docker-compose.yml`：postgres 16 + redis（W2 任务队列预留）+ healthcheck
- ✅ `db/schema.sql`：所有表 PG-flavored DDL（agent_events / agent_actions / chat_messages / wf2_*_sku/orders / wf1_*_stock / wf3_logistics_hub / wf5_*_sales_cycle / wf6_alerts/queue / sa_main / feishu_digest）
- ✅ `scripts/migrate_sqlite_to_pg.py`：按表 dump → upsert（ON CONFLICT），JSON 字段自动 JSONB
- ✅ `server/data.py`：`DB_URL` env 分派 + `_convert_sql_for_pg()` 自动 `?→%s` / `datetime('now')→NOW()` / `date('now')→CURRENT_DATE` + `_PGConnWrapper` 让 RealDictCursor 行为接近 sqlite3.Row
- ✅ `requirements.txt` 加 `psycopg2-binary>=2.9`

**验收**:
- ✅ `python -m uvicorn ...`（不设 DB_URL）→ SQLite 默认，sku_count=688 正常
- ✅ `is_postgres()` 检测 + SQL 转换 unit test 通过
- ⏳ 本地起 docker postgres + 跑 migrate（用户哪天动手切）

**实施要点**:
- `INSERT OR REPLACE`（SQLite-only）→ 已统一改用 `ON CONFLICT (pk) DO UPDATE`（两边兼容）
- `_fetch` / `_scalar` 通过 `_PGConnWrapper` 适配 RealDictCursor 与 sqlite3.Row 的双向兼容
- `_scalar` 增加 dict 检测（PG RealDictCursor 返回 dict，取首值）
- DB 切换不动业务代码，所有 query 透明走 conn() 抽象

**怎么切到 PG**:
```bash
docker compose up -d postgres redis
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  python scripts/migrate_sqlite_to_pg.py
DB_URL=postgresql://... QWEN_API_KEY=... \
  python -m uvicorn hipop.server.main:app --port 8765
```

### Task 1.3 — env 配置外置（half-day）

**目标**：所有 secret 移环境变量 + .env 模板；hipop.json 只放 schema 配置不放 secret。

**交付**：
- `.env.example`：`DEEPSEEK_API_KEY` / `DB_URL` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `ERP_USERNAME` / `ERP_PASSWORD` / `ANTHROPIC_API_KEY`（可选）
- `hipop/config/hipop.json` 移除 `feishu.app_secret`（用 env）
- 启动时 fail-fast 检查必需 env

---

## W2：多用户 + RBAC + 协同

### Task 2.1 — 用户系统（1 day）

**交付**：
- 新表：
  - `tenants(id, name, plan, created_at)`
  - `users(id, tenant_id, email, password_hash, role, created_at, last_active)` — role enum: owner / manager / ops / forwarder
  - `sessions(id, user_id, token_hash, expires_at)`
- 注册流：邮箱 + 密码（先简单，magic link 后做）
- 登录返回 JWT；FastAPI middleware 验 token，注入 `request.state.user / tenant_id`
- chat scope 改成基于登录态自动注入（不再前端传 `current_user`）

**验证**：4 个角色都能登录，token 跨页面有效，过期自动跳登录页。

### Task 2.2 — 多租户 RLS（1 day）

**交付**：
- 所有业务表加 `tenant_id INT NOT NULL` 列（wf1/2/3/5/6 + chat + agent_actions + agent_events）
- Postgres RLS policy：每张表 `USING (tenant_id = current_setting('app.current_tenant')::int)`
- DB connection 拿到时 `SET app.current_tenant = <user.tenant_id>`
- 行级回归测试：tenant A 用户 query 看不到 tenant B 数据（每次 release 自动跑）

**坑**：RLS 错配 = 数据泄漏 = 信任崩塌 → day1 必须有自动测试

### Task 2.3 — RBAC 权限（half-day）

**交付**：
- 角色权限矩阵（hardcode 阶段 1）：

| 操作 | owner | manager | ops | forwarder |
|---|---|---|---|---|
| 看 dashboard | ✅ | ✅ | ✅ | ✅ |
| 触发 workflow | ✅ | ✅ | ✅ | ❌ |
| 上传 CSV | ✅ | ✅ | ✅ | ❌ |
| update_alert_status | ✅ | ✅ | ✅ | ✅ |
| 邀请用户 / 改角色 | ✅ | ✅ | ❌ | ❌ |
| 改店铺配置 | ✅ | ❌ | ❌ | ❌ |
| 看计费 / 改套餐 | ✅ | ❌ | ❌ | ❌ |

- chat tool 调用前检查权限（`require_permission("trigger_workflow")` decorator）
- 前端按角色隐藏入口（不只是后端拦）

### Task 2.4 — 共同空间 chat（half-day）

**交付**：
- chat_messages 加 `user_id` 列；who 由 `users.email` 派生
- 同 tenant + 同 store 的用户共看一个 chat
- @ 同事：`@张组长` parse → 同 tenant 用户检索 → 站内通知（先只埋点，alpha 阶段先不做推送）
- 飞书 webhook 推送可选（用 `tenants.feishu_webhook_url` 配置）

---

## W3：部署 + 备案

### Task 3.1 — Dockerize（half-day）

**交付**：
- `Dockerfile`：python:3.11-slim + 依赖 + 启动 uvicorn
- `docker-compose.yml`：app + postgres + redis（dev 用）
- `.dockerignore`：排 `inbox/` / `logs/` / `*.db`
- 健康检查 endpoint：`/health` 已经有，加 `/ready`（检查 DB 连接）

### Task 3.2 — Zeabur 部署（half-day）

**交付**：
- `zeabur.json`：包含 build / start / env 引用
- 创建 Zeabur 项目 → 接 git 仓库 → 配置 env（DEEPSEEK_API_KEY 等从 Zeabur 凭据库注入）
- 接 Zeabur 内置 Postgres（或外接阿里云 RDS）
- 自定义域名：先用 zeabur 默认域名（`hipop-os.zeabur.app`）

**坑**：Zeabur 内置 PG 对小客户够用，正式发布前换阿里云 RDS

### Task 3.3 — ICP 备案启动（持续，1-2 周）

**并行做**（不阻塞工程）：
- 注册 .cn 域名（如 dgo.com.cn / hipop-agent.cn）
- 阿里云购买 ICP 备案服务
- 公司主体（如有）/ 个人备案

阶段 1 用 zeabur 默认域名先跑，备案下来再切。

---

## W4：alpha 客户 onboarding

### Task 4.1 — 客户引导流（1 day）

**交付**：
- 注册落地页（简单 1 页）
- 注册 → 创建 tenant → "接 ERP" 表单（dbuyerp username/password）→ 后端 verify → 触发首次 ingest
- "接飞书"可跳过
- 邀请同事：发邮件链接（先简单，post-alpha 改 SSO）

### Task 4.2 — 找 1 个 alpha 客户（持续）

- 优先：跨境电商朋友 / Luke 的网络
- 必须用 dbuyerp（adapter 范围内）
- 价格：alpha 期免费 → 收反馈

### Task 4.3 — 监控 + 错误日志（half-day）

**交付**：
- 阿里云 SLS 接日志（或先用本地文件 + 定时打包发邮件）
- Sentry 集成（错误告警）
- 关键指标埋点：chat 调用次数 / 工具命中率 / workflow 成功率 / 客户活跃度

---

## 不在阶段 1 范围（后置）

- Celery 任务队列（阶段 2，多客户隔离时再上）
- 客户引导 SSO / OAuth（阶段 2）
- ERP adapter 多个（先 dbuyerp 一家）
- 平台 adapter 多个（先 noon CSV，API 留 stub）
- 计费 / 支付（阶段 2）
- 移动端（阶段 3）

---

## 风险与决策点

### 决策点 1：阶段 1 末是否切阿里云 RDS

**问题**：Zeabur 内置 PG 对 5 客户够用；阿里云 RDS 贵但稳定且数据合规。

**建议**：阶段 1 用 Zeabur PG（省事），阶段 2 上线收费时切阿里云 RDS。

### 决策点 2：ICP 备案没下来怎么办

**问题**：备案要 1-3 周，可能阶段 1 收尾时还没下。

**建议**：alpha 用 .com 域名 + zeabur，国内访问慢但能用；备案下来切 .cn。

### 决策点 3：DeepSeek 搞定 chat tool-use 需要多少调 prompt

**问题**：DeepSeek 协议兼容 OpenAI，但中文 chat tool-use 行为可能跟 Claude 略有差。需要测。

**建议**：W1 抽象做完 → 立刻把当前 KSA 数据 + 9 个 tool 测一遍 DeepSeek。如果命中率 <80%，回到 SYSTEM_PROMPT 强化。

---

## 关键工程文件清单（W1-W4 会动到哪些）

```
新增:
  server/_provider.py            (Task 1.1)
  server/_anthropic.py
  server/_openai_compat.py
  server/auth.py                 (Task 2.1)
  server/rbac.py                 (Task 2.3)
  scripts/migrate_sqlite_to_pg.py (Task 1.2)
  Dockerfile                     (Task 3.1)
  docker-compose.yml             (Task 3.1)
  zeabur.json                    (Task 3.2)
  db/schema.sql                  (Task 1.2)
  .env.example                   (Task 1.3)

改动:
  server/agent.py                (用 _provider; SYSTEM_PROMPT)
  server/api.py                  (auth middleware; tenant_id)
  server/data.py                 (psycopg2 + RLS; tenant_id)
  server/templates/*.html        (按角色隐藏入口)
  hipop/config/hipop.json        (移除 secrets)
  requirements.txt               (openai, psycopg2, python-jose, passlib)
```

---

## 出阶段 1 时的状态

- ✅ 公网可访问的 demo 站点（Zeabur）
- ✅ 团队多人登录，4 角色权限可用
- ✅ DeepSeek 跑 chat 全场景
- ✅ Postgres 多租户 + RLS 测试通过
- ✅ 1 个外部 alpha 客户（除 Luke 自己之外）跑通完整流
- 🟡 ICP 备案进行中
- 🟡 阿里云 RDS 待切

→ 进入阶段 2：多租户基线（4-6 周，参考方案文档第三节）。

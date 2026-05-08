# 点购 Agent OS 产品化方案（v2 国内版）

> v1 起草于 2026-05-07，海外栈（Postgres/Fly.io/Anthropic OAuth）。
> v2 修订于 2026-05-08，目标客户在中国（跨境电商团队），全面切国内方案。
> POC 已收口（chat 行为四象限 + 依赖图 + 自动 follow-up），具备产品化前提。

---

## 一、目标用户与价值定位

**核心用户画像**
- 跨境电商团队（5-50 人核心运营，**总用户数百级别**），有 ERP（dbuyerp 或类似）+ 海外平台店铺（noon / amazon / shopee）+ 飞书办公
- 痛点：销量/物流/补货数据散落多个系统，运营每天 30 分钟手动整理表格、被动等问题、跨部门拉群催回应

**对手 / 替代品**
- 紫鸟 / 店小秘等 ERP 自带 BI 模块（重，难定制）
- 自建 Excel / Looker Studio（轻，但要自己写 ETL + 拉数据）
- 数据中台咨询服务（贵，外包做完没人维护）

**Agent OS 卖点**
1. **chat 即指令**：发"我该补货吗"就直接得答案，Agent 自动判断需要的上游数据是否齐 — 不需要学 SQL / 不切系统
2. **数据出处可溯源**：每个数字 📎 展开看 SQL where 子句（运营信任的关键）
3. **共同空间感**：聊天里看到 Agent + 团队同事的协作（运营 / 跟单 / 主管 / 老板）
4. **决策卡片可采纳**：Agent 给的建议可一键采纳进决策表，不只是聊天聊聊就过去
5. **触发即可见**：跑完工作流页面立刻刷新，区别于"跑 cron 等明早看"
6. **真闭环**：用户拒绝上传 / 拒绝跑 workflow 时，Agent 也能基于陈旧数据给临时答案 + 警示偏向

---

## 二、当前架构离产品化的差距（v2 国内版）

### 关键缺口对照表

| 维度 | 现状 | 产品化（国内栈） | 优先级 |
|---|---|---|---|
| **DB** | 本地 SQLite | **阿里云 RDS PostgreSQL**（北京/上海/新加坡多区） | P0 |
| **部署** | 本地 uvicorn + cloudflared 临时 | **Zeabur**（alpha）/ **阿里云 SAE**（多租户阶段） | P0 |
| **模型** | Anthropic Haiku-4-5 OAuth 订阅 | **DeepSeek-V3 API**（国内直连，便宜，备案过；本地开发可保留 Anthropic） | P0 |
| **Auth** | 单人 Cherry 写死 | **OAuth + JWT 多用户**（数百人级别，day1 必须有） | P0 |
| **多租户** | hipop.json 单家 | **tenant_id + RLS**（Postgres 行级安全） | P0 |
| **RBAC** | 无 | **角色（运营 / 跟单 / 主管 / 老板）+ 资源级权限** | P0 |
| **协同** | chat 单人空间 | **店铺工作组共享空间** + @ 同事 + 站内通知 + 飞书推送 | P0 |
| **ERP 对接** | 仅 dbuyerp，靠本机 chrome 9222 抓 token | **Adapter 层**（dbuyerp / 店小秘 / 易仓...）+ 服务端定时抓 token | P1 |
| **平台对接** | 仅 noon CSV 人工导入 | **noon API / Amazon SP-API / Shopee API** adapter | P1 |
| **飞书** | base_id / app_secret 写死 | **每客户自配 / 不用飞书也能跑** | P1 |
| **凭据安全** | 明文 in config | **阿里云 KMS / Vault** + 客户自填加密 | P0 |
| **任务队列** | BackgroundTasks（FastAPI 进程内） | **Celery / RQ** + 独立 worker 集群（多客户隔离） | P1 |
| **观测** | 看 logs 文件 | **阿里云 SLS** / 阿里云监控 / 业务结构化日志 | P1 |
| **计费** | — | Stripe（海外）/ **支付宝 + 微信支付 + 企业账期** | P1 |
| **ICP 备案** | — | .cn 备案走国内云一周 | P0 |

### 直接复用的部分（不重写）
- ✅ workflow 计算逻辑（wf1/2/3/5/6 已独立可调用）
- ✅ Agent OS UI 框架（FastAPI + Jinja2 + Alpine，足够轻）
- ✅ `WORKFLOW_REGISTRY` + SSE + 模块自动刷新链路（多租户加 tenant_id 即可）
- ✅ chat tool 集（query_sku / list_products / 等 9 个）+ 9 种意图依赖图
- ✅ chat 行为四象限（数据齐 / auto 陈旧 / needs_csv 陈旧 / 用旧数据）

---

## 三、技术演进路径（三阶段）

### 阶段 1：单租户云端化（3-4 周，让 Luke 自己 + 1 个 alpha 客户用上）

> **修正**：v1 估 2 周（单用户），v2 改 3-4 周，因为 alpha 用户也是数百人级别，**day1 必须多用户**，不能偷懒。

**关键改动**
1. **DB 切阿里云 RDS PostgreSQL**：`hipop.db` SQLite → 托管 PG。SQL 改造小（兼容子集）。
2. **provider 抽象**：新增 `server/_provider.py` 统一 `chat_with_tools(...)` 接口；环境变量 `LLM_PROVIDER=anthropic|deepseek` 切换。本地默认 Anthropic（OAuth 免费），生产切 DeepSeek（OpenAI 协议兼容）。
3. **Auth 多用户**：FastAPI middleware + JWT + 邮箱 magic link；用户表 `users(id, tenant_id, email, role, ...)`；4 个角色（owner / manager / ops / forwarder）。
4. **凭据外置**：所有 secret 移阿里云 KMS / 环境变量；ERP 凭据后端 service account 抓 token。
5. **部署 Zeabur**：Dockerfile + zeabur.json 一键部署；走阿里云**香港 region**（合规友好 + 兼容 Anthropic 万一回退）+ Caddy 反代 HTTPS。
6. **协同 day1**：chat_messages 已有 `store` 字段隔离，加 `tenant_id`；店铺被多人看到 + @ 同事 + 站内通知。

**验收**：1 个外部 ERP 凭据 + 飞书 base，alpha 客户 30 分钟接入跑出 dashboard，团队 5 人能在 chat 里协同。

### 阶段 2：多租户基线（4-6 周，能开 5-10 个客户）

**关键改动**
1. **多租户 schema**：所有表加 `tenant_id` 列 + Postgres RLS；运行时 SET app.current_tenant，PG 自动过滤。
2. **客户引导流**：注册 → 验证邮箱 → 创建 tenant → 邀请同事 → 接 ERP（OAuth/API key 自填）→ 接平台 → 飞书可选。
3. **Workflow 抽象**：`workflows/` 不再 hardcode，改成"数据合约 + 计算逻辑"。客户能配置补货阈值、销售周期窗口。
4. **任务队列**：`run_workflow` 改 Celery + Redis；每 tenant 独立 queue，互不阻塞。
5. **管理后台**：sysadmin 看所有客户、停服、调试。
6. **基础计费**：支付宝 + 微信支付集成；按"店铺数 + 月活 chat 次数"两段式定价。
7. **观测**：阿里云 SLS 收日志 + 关键指标埋点（chat 次数 / workflow 次数 / 客户活跃度）。

**验收**：3 个外部团队完整跑一个月，留存 ≥ 2，月费收齐。

### 阶段 3：生态扩展（持续）

- ERP adapter 扩展（店小秘 / 易仓 / 旺店通 / Shopify）
- 平台 adapter 扩展（amazon SP-API / shopee / lazada / shopify）
- 移动端：飞书机器人 + 微信小程序 + 企微
- 工作流市场（Zapier 模式）
- AI 升级：让 Agent 自主写计算逻辑（给 code-execution 工具）

---

## 四、关键设计决策（v2 国内版）

### 决策 1：模型选型

**默认 DeepSeek-V3**（生产）+ **Anthropic Haiku-4-5**（本地开发可选）

| 维度 | DeepSeek-V3 | Claude Haiku-4-5 |
|---|---|---|
| 协议 | OpenAI 兼容 | Anthropic |
| 价格 | ¥1/M input / ¥8/M output（缓存命中 ¥0.1/M） | $1/$5/M USD（OAuth 订阅本机免费）|
| 国内访问 | ✅ 直连 | ❌ 走代理 / 香港 region |
| Tool-use | ✅ 接近 GPT-4 | ✅ 强 |
| 中文 | ✅ 优 | 强 |
| 备案 | ✅ ICP 已备 | — |

**实施**：`server/_provider.py` 抽象 `chat_with_tools()`，env `LLM_PROVIDER=anthropic|deepseek`。两个实现共享 tool 集，schema 统一。

### 决策 2：DB 选型

**阿里云 RDS PostgreSQL**（不是 MySQL）原因：
- 行级安全（RLS）多租户原生支持
- SQLite → PG 迁移基本零改动
- `COUNT(DISTINCT)` 等 SQL 表现强
- TimescaleDB / pgvector 扩展生态

阶段 1 单实例（北京 / 香港），阶段 2 加只读副本，阶段 3 给企业客户独立 instance。

### 决策 3：多租户隔离

**单 DB + tenant_id 列 + Postgres RLS**（阶段 1-2）→ **企业客户独立 instance**（阶段 3）

每条 query 在 connection pool 层 `SET app.current_tenant=<id>`，所有表的 RLS policy 自动过滤。RLS 错配 = 租户串数据，**day1 就要写自动回归测试**。

### 决策 4：部署

| 阶段 | 方案 | 原因 |
|---|---|---|
| alpha（5 客户内） | **Zeabur**（git push 即部署） | 国产 Vercel，最快验证 |
| 多租户基线（5-50 客户） | **阿里云 SAE**（Serverless 应用引擎） | docker 镜像直部署，按量计费 |
| 企业客户 | **阿里云 ECS + RDS + SLB** | 完全可控，独立资源 |

香港 region（合规 + 国内访问 200ms 级 + 可访问 Anthropic 备份）。.cn 域名国内云做 ICP 备案。

### 决策 5：定价

按"店铺数 × 工作流次数 × 用户数"三维：

| 套餐 | 店铺 | 月 workflow | 用户 | 价格 |
|---|---|---|---|---|
| Free | 1 | 100 | 3 | ¥0 |
| Starter | 3 | 1000 | 10 | ¥199/月 |
| Pro | 10 | 5000 | 50 | ¥599/月 |
| Enterprise | ∞ | ∞ | ∞ | 定制 |

参考紫鸟收 ¥几千/年/账号（按用户数），我们按团队定价更友好。

### 决策 6：Agent tool 是否对客户暴露 DIY

**先托管模式**（封闭 tool 集）→ **2026-Q4 看需求**再开 DIY。维护成本 vs 灵活性 trade-off。

---

## 五、组织 / 执行节奏

阶段 1（单租户云端化）：3-4 周 — 详见 `phase1_v2.md`
阶段 2（多租户基线）：4-6 周
阶段 3（生态扩展）：持续

**首要里程碑**（M1）：
- 时间：2026-06-05（4 周内）
- 标准：除 Luke 外，至少 1 个外部团队的 KSA 店铺数据从 ERP 自动同步进 Agent OS 并能在 chat 里得到正确响应

---

## 六、风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| ERP 适配工作量超预期（每家 ERP API 不同） | 阶段 2 延期 | 阶段 1 只支持 dbuyerp，找的 alpha 客户必须用 dbuyerp |
| DeepSeek tool-use 偶尔不调 tool 直接答 | chat 体验差 | SYSTEM_PROMPT 写硬"必须先调 tool"；监控 tool_use 命中率，<80% 自动回退 anthropic |
| 多租户数据泄漏（RLS 错配） | 信任崩塌 | day1 上 RLS + 行级测试，每次 release 自动回归 |
| Luke 时间被工作流改动牵扯 | 产品化进度不可控 | 工作流改动冻结：阶段 1 期间 hipop 内部需求走"先记需求再 batch 实现"，避免打断 |
| 备案延期 | 域名上不了 | 早申请；阶段 1 用 .com + 香港 IP，国内访问可接受 |
| Anthropic 国内访问被限 | 本地开发卡 | 已切 DeepSeek 为生产默认；本地走 OAuth + 香港代理 |

---

## 七、立即下一步

详见 `plans/phase1_v2.md`（阶段 1 工程任务拆解）。

按周拆：
- W1：DeepSeek provider 抽象 + Postgres 本地迁移
- W2：Auth + 多用户 + RBAC 框架
- W3：Zeabur 部署 + ICP 备案
- W4：alpha 客户 onboarding + 收反馈

---

*v1 → v2 修订重点*：
- 海外栈（Postgres / Fly.io / Anthropic）→ 国内栈（阿里云 RDS-PG / Zeabur / DeepSeek）
- 阶段 1 从 2 周 → 3-4 周（增加多用户 RBAC + 协同 day1）
- 加 ICP 备案 + 国内大模型选型
- 计费切支付宝 / 微信支付 / 企业账期

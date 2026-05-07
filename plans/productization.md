# 点购 Agent OS 产品化方案

> 现状：本地单租户验证（HIPOP-NOON-KSA/UAE），跑通"chat 触发工作流 + 模块自动刷新 + 数据出处可溯源"完整链路。
> 目标：沉淀成可对外发布的 SaaS / 工具，让其他跨境电商团队接入即用。
> 起草：2026-05-07

---

## 一、目标用户与价值定位

**核心用户画像**
- 跨境电商团队（5-50 人），有 ERP（dbuyerp 或类似）+ 海外平台店铺（noon / amazon / shopee）+ 飞书办公
- 痛点：销量/物流/补货数据散落多个系统，运营每天 30 分钟手动整理表格、被动等问题、跨部门拉群催回应

**对手 / 替代品**
- 紫鸟 / 店小秘等 ERP 自带 BI 模块（重，难定制）
- 自建 Excel / Looker Studio（轻，但要自己写 ETL + 拉数据）
- 数据中台咨询服务（贵，外包做完没人维护）

**Agent OS 卖点**（区别于上面）
1. **chat 即指令**：发"跑一下销售周期"就触发后台分析，进度实时回，结果落到模块页 — 不用学 SQL / 不用打开 Linear / 飞书来回切
2. **数据出处可溯源**：每个数字都能展开看到来自哪张表、哪个 where 子句（运营信任的关键）
3. **共同空间感**：聊天里看到 Agent + 团队同事的协作（运营/跟单/组长），不是个人用工具
4. **决策卡片可采纳**：Agent 给的建议可以一键采纳进决策表，不只是聊天聊聊就过去了
5. **触发即可见**：跑完工作流页面立刻刷新，区别于"跑 cron 等明早看"

---

## 二、当前架构离产品化的差距

### 关键缺口对照表

| 维度 | 现状 | 产品化要求 | 优先级 |
|---|---|---|---|
| 多租户 | hipop.json 写死单家 + sales_entities[] 单文件 | 客户表 → 多店铺，rbac 控制 | P0 |
| ERP 对接 | 仅 dbuyerp，token 靠本机 chrome 9222 | adapter 层 + service account | P0 |
| 平台对接 | 仅 noon CSV 人工导入 | adapter（noon API / amazon SP-API / shopee API） | P1 |
| 飞书绑定 | base_id / app_secret 写死 hipop.json | 每客户自配 / 也支持不用飞书 | P1 |
| Auth | 单人 Cherry 写死 + 走本机 chrome OAuth | OAuth + JWT 多用户 + 行级权限 | P0 |
| 部署 | 本地 uvicorn + cloudflared 临时 | 云端容器 + 域名 + HTTPS + DB 托管 | P0 |
| 凭据安全 | feishu_secret 明文 in config | KMS / Vault / 客户自填 + 加密 | P0 |
| 工作流执行 | 本机 launchd cron + Python subprocess | 任务队列（celery/RQ）+ worker 集群 | P1 |
| 数据库 | 本地 SQLite | 客户隔离的 Postgres / 共享 schema 多租户 | P0 |
| 观测 | 看 logs 文件 | Sentry / 结构化日志 / 监控告警 | P1 |
| 计费 | — | 按客户店铺数 / 工作流次数 / 用户数 | P1 |

### 现在能直接复用的部分（不重写）
- ✅ workflow 计算逻辑（wf1/2/3/5/6 已经独立可调用）
- ✅ Agent OS UI 框架（FastAPI + Jinja2 + Alpine，足够轻）
- ✅ `WORKFLOW_REGISTRY` + SSE + 模块自动刷新链路（设计正确，多租户加 client_id 即可）
- ✅ chat tool 集（query_sku / list_products 等）— 加 entity 参数即可多店铺

---

## 三、技术演进路径（三阶段）

### 阶段 1：单租户云端化（1-2 周，让自己第一个客户用上）

**目标**：把当前本地版搬到云端，让 Luke 自己 + 1 个外部 alpha 客户能远程访问，先跑起来收反馈。

**关键改动**
1. **DB 切 Postgres**：`hipop.db` → 托管 Postgres（Supabase / Neon）；保留 SQLite 测试模式。SQL 变化不大（SQLite 兼容子集即可）。
2. **凭据外置**：所有 secret 移到环境变量；ERP 凭据改成读云端 Vault（或 1Password CLI）。
3. **Auth 加最小层**：FastAPI middleware 验 cookie / JWT，单用户先支持 magic link。
4. **部署**：Docker + Fly.io / Railway 一键起；Caddy 反代 HTTPS；后台 worker 用 launchd 等价的 systemd timer。
5. **ERP token 远程**：Headless chrome + service account 放后端，定期登录抓 token；不再依赖本机 9222。

**验收**：能给一个外部 ERP 凭据 + 飞书 base，alpha 客户 30 分钟接入跑出 dashboard。

### 阶段 2：多租户基线（3-4 周，能开 5-10 个客户）

**目标**：客户自助注册、自配 ERP/飞书、隔离数据，开始收钱。

**关键改动**
1. **多租户 schema**：所有表加 `tenant_id` 列 + RLS（Row Level Security 在 Postgres 里），或一客户一 schema。
2. **客户引导流**：
   - 注册 → 验证邮箱
   - 接 ERP（OAuth / API key 自填）→ 自动 probe entity 列表
   - 接平台（noon API / Amazon SP-API）
   - 飞书可选（不用也能跑，仅丢失推送 + 飞书表协作）
3. **Workflow 抽象层**：`workflows/` 不再 hardcode 字段名，改成"数据合约（input schema）+ 计算逻辑"。让客户能配置补货阈值、销售周期窗口。
4. **任务队列**：`run_workflow` 不再 BackgroundTasks，改 celery / RQ + 隔离 worker。客户多时不互相阻塞。
5. **管理后台**：sysadmin 看所有客户、停服、调试。
6. **基础计费**：Stripe 集成，按"店铺数 + 月活 chat 次数"两段式定价。

**验收**：3 个外部团队完整跑一个月，留存 ≥ 2 个，月费收齐。

### 阶段 3：生态扩展（持续）

- ERP adapter 扩展：除 dbuyerp 外加店小秘 / 易仓 / 旺店通 / Shopify
- 平台 adapter 扩展：amazon SP-API / shopee / lazada / shopify
- 移动端：飞书机器人 + 微信小程序双通道
- 工作流市场：客户能自己写 / 装 workflow（类似 Zapier）
- AI 升级：把现在的 hard-coded ROI 计算 / 趋势判定让 Agent 自主写，给 Agent code-execution 工具

---

## 四、关键设计决策

### 决策 1：多租户隔离方式

| 方案 | 优点 | 缺点 |
|---|---|---|
| 单 DB + tenant_id 列 + RLS | 简单，运维成本低 | 单库爆表风险，跨租户故障辐射大 |
| 客户独立 schema（同 instance） | 备份/分析方便 | schema migration 复杂 |
| 客户独立 instance | 完全隔离 | 部署 / 计费复杂，小客户成本高 |

**推荐**：阶段 2 用方案 1（tenant_id + RLS），阶段 3 给企业客户提供方案 3 升级。

### 决策 2：ERP 抽象层

不要直接 expose ERP 内部字段。设计统一的 **canonical 商品/订单/库存模型**，每个 ERP adapter 把数据映射到这个模型。下游 workflow 都吃 canonical model，与 ERP 解耦。

```
ERP A (dbuyerp) ─┐
ERP B (店小秘) ─┼─→ ProductCanonical / OrderCanonical / StockCanonical → workflows
ERP C (易仓)   ─┘
```

### 决策 3：Agent tool 是否对客户暴露

两种模式：
- **托管模式**：tool 集封闭，客户只能用我们提供的 chat 能力（标准化）
- **DIY 模式**：客户能写自定义 tool 接入自家的数据源（强大但维护成本高）

**推荐**：先托管模式，2026-Q4 再开 DIY（要等需求验证）。

### 决策 4：定价

不要按用户数（运营人多但都该用），按"店铺数 × 工作流执行次数"：
- Free：1 店铺，每月 100 次 workflow，chat 50 条
- Starter ¥199/月：3 店铺，1000 workflow，chat 不限
- Pro ¥599/月：10 店铺，5000 workflow，飞书集成
- Enterprise 定制：独立 instance，专属 onboarding

参考紫鸟收 ¥几千/年/账号，这个空间足够。

---

## 五、组织 / 执行节奏

阶段 1（云端化）：Luke + Claude Code 配合，2 周
- 周 1：Postgres 迁移 + Auth + 单 user OAuth + Docker 化
- 周 2：部署 Fly.io + 1 个 alpha 客户接入 + 收反馈

阶段 2（多租户）：再 4 周
- 周 3-4：multi-tenant schema + 客户引导流
- 周 5-6：任务队列 + 计费 + 3 个客户上线

**首要里程碑**（M1）：
- 时间：2026-05-21（2 周内）
- 标准：除 Luke 外，至少 1 个外部团队的 KSA 店铺数据从 ERP 自动同步进 Agent OS 并能在 chat 里得到正确响应

---

## 六、风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| ERP 适配工作量超预期（每家 ERP API 不同） | 阶段 2 延期 | 阶段 1 只支持 dbuyerp，找的 alpha 客户必须用 dbuyerp |
| Anthropic API 成本失控（chat 用量 × 客户数） | 单价吃掉利润 | 客户 BYOK + 本地缓存 + 简单意图走规则不走 LLM |
| 飞书企业版限制 / 国内合规 | 部分客户不能用 | 国内站建独立 instance，飞书替换为企微 / 钉钉 |
| 多租户数据泄漏 | 信任崩塌 | 阶段 1 就上 RLS + 行级测试，每次 release 自动回归 |
| Luke 时间被工作流改动牵扯 | 产品化进度不可控 | 工作流改动冻结：阶段 1 期间任何 hipop 内部需求走"先记需求再 batch 实现"，避免打断产品化 |

---

## 七、立即可做的下一步（Luke 回来后挑一个）

A. **写阶段 1 的工程拆解**（Postgres 迁移 / Docker / Fly.io / Auth 各步），落到 `plans/phase1_<topic>.md`
B. **找 alpha 客户**：先写一个 1 页的产品介绍 + landing 页 mock，发给 2-3 个跨境电商朋友看反馈
C. **先动 Postgres**（如果你着急看效果）：把 `hipop.db` 跑通迁移到本地 docker postgres，所有功能不变 — 验证迁移可行性
D. **暂停产品化讨论，回归当前 hipop 内部需求**（如果你觉得 KSA 店铺数据还没稳定到能 demo 给外人看）

我推荐路径：**A → C → B**。A 把方向定死，C 验证最高风险技术问题（DB 迁移），B 拿到真实客户反馈再做阶段 2。

---

*本文档是出门前的一稿。Luke 回来后我们对一遍，调优先级 + 把 A 拆成具体任务。*

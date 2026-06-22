点购 Agent OS — 工作台 + 协作 Agent + 工作流触发（hipop/server）：FastAPI + Jinja2 + Alpine 的本地工作台，右侧 chat 通过 Anthropic tool-use 调用 11 个 tool；触发工作流后实时进度推到气泡 + 模块页自动刷新。

工作目录：/Users/luke/code/hipop

> **详情文档**：
> - `openclaw-skill/agent-os-tools.md` — chat 工具 + 意图路由 + Agent 调用规则
> - `openclaw-skill/agent-os-server.md` — 服务端架构 / 多租户 / 扩展点 / 调试

## 启动（三种模式）

**dev 最简（SQLite + 不强制登录）**:
```bash
QWEN_API_KEY=sk-... python -m uvicorn hipop.server.main:app --port 8765
```

**生产同款（PG + 多租户 RLS）— 当前 alpha 跑这套**:
```bash
brew services start postgresql@16
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  JWT_SECRET=hipop_alpha_stable_secret_keep_this \
  LLM_PROVIDER=deepseek \
  DEEPSEEK_API_KEY=sk-... \
  python -m uvicorn hipop.server.main:app --port 8765
```

**部署到 Zeabur**: 看 `DEPLOY.md`。`zeabur.json` 已配。

## 健康检查

- `GET /health` — liveness（进程在跑就 200）
- `GET /ready` — readiness（DB 连得上 200，附 mode: postgres|sqlite）

## smoke test（commit 前必跑）

```
bash tests/run_smoke.sh
```

12 case 覆盖：数据新鲜度 / 商品总数 / 概览 / 红色告警 / 补货 / SKU 查询 / 3 个门控 tool / 用户拒绝刷新 / 时间戳精度。

**LLM provider 切换**：`LLM_PROVIDER=qwen|anthropic|deepseek|doubao`，**默认 qwen**。

## 架构

```
hipop/server/
├─ main.py            FastAPI app + 飞书 webhook（/feishu/webhook）
├─ pages.py           7 个模块页路由
├─ api.py             /api/* JSON 接口 + /api/run-workflow + SSE
├─ data.py            统一访问 hipop.db
├─ agent.py           tool-use chat（默认 qwen，可 LLM_PROVIDER 切换）
├─ _auth.py           凭据工厂：env > macOS keychain OAuth
└─ templates/         Jinja2 模板
```

## 已知 gotcha

1. **chrome remote debug 9222 必须带 `--remote-allow-origins='*'` 启动**，否则 ws 握手 403。
2. **系统 HTTP 代理（clash/v2ray 等）会拦截 127.0.0.1:9222 请求**：所有走 chrome devtools 的代码必须用 `proxies={"http":None,"https":None}`。
3. **OAuth 订阅配额**：sonnet-4-5 在 5h 窗口内容易耗尽。当前默认模型用 haiku-4-5（够用）。
4. **chrome 多 tab 时 playwright connect_over_cdp 180s 超时**：token 抓取链优先 CDP raw → playwright 兜底 → wf0 headless 登录。

## 与现有 hipop 工作流的关系

agent_os 是**消费层**，所有数据都来自工作流写入的表：
- 销量/SKU → `wf2_<alias>_sku`（hipop-wf2）
- 库存 → `wf1_<alias>_stock`（hipop-wf1）
- 物流 → `wf3_logistics_hub`（hipop-wf3，daily 跑）
- 补货 → `wf5_<alias>_sales_cycle`（hipop-wf5，weekly 跑）
- 告警 → `wf6_logistics_alerts`（hipop-wf6）

agent_os 自己不写工作流逻辑，只通过 `run_workflow` tool 触发已有 workflow 入口。

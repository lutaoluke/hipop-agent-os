# Agent OS · 运行与运维（渐进披露引用）

> 从旧单体 `agent-os.md` 拆出的底座说明，供 [`../../agent-os.md`](../../agent-os.md) 索引引用。
> 非「9 个能力 skill」之一，按需打开。

## 启动（三种模式）

**dev 最简（SQLite + 不强制登录）**：
```bash
QWEN_API_KEY=sk-... python -m uvicorn hipop.server.main:app --port 8765
```

**生产同款（PG + 多租户 RLS）— 当前 alpha**：
```bash
brew services start postgresql@16   # 或 docker compose up -d postgres
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  JWT_SECRET=hipop_alpha_stable_secret_keep_this \
  LLM_PROVIDER=deepseek DEEPSEEK_API_KEY=sk-... \
  python -m uvicorn hipop.server.main:app --port 8765
# 公网暴露：cloudflared tunnel --url http://localhost:8765
```

**部署到 Zeabur**：看 `DEPLOY.md`，`zeabur.json` 已配。

## 健康检查

- `GET /health` — liveness（进程在跑就 200）
- `GET /ready` — readiness（DB 连得上 200，附 mode: postgres|sqlite）

## LLM provider 切换

`LLM_PROVIDER=qwen|anthropic|deepseek|doubao`，**默认 qwen**（国内栈对齐 + 实测防护下不 hallucinate）。
- qwen / deepseek / doubao 走 OpenAI 协议（`server/_provider_openai.py`），换 base_url + api_key + model。
- anthropic（本地开发可选）：`server/_auth.py` 优先 `ANTHROPIC_API_KEY`，回退 macOS keychain OAuth；
  `/login` 后 token 轮换自动重读。抽象在 `server/_provider.py:chat_with_tools()`，统一返回
  `{reply, tool_log, refs_collected, workflow_task}`。

## DB 分派（SQLite ↔ Postgres）

`server/data.py:conn()` 按 `DB_URL` 分派：
- 不设 → SQLite at `HIPOP_DB`（默认 `hipop.db`），dev 模式。
- `DB_URL=postgresql://...` → PG，`_PGConnWrapper` 包 psycopg2 + RealDictCursor。
- `_convert_sql_for_pg()` 自动转 `?→%s` / `datetime('now','localtime')→NOW()` / `date('now')→CURRENT_DATE`。

切到 PG：
```bash
docker compose up -d postgres redis
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  python scripts/migrate_sqlite_to_pg.py
DB_URL=postgresql://... QWEN_API_KEY=... python -m uvicorn hipop.server.main:app --port 8765
```
infrastructure 文件：`docker-compose.yml` / `db/schema.sql` / `scripts/migrate_sqlite_to_pg.py`。

## 关键依赖

```
fastapi uvicorn jinja2          # web
anthropic                       # chat tool-use（fallback）
openai                          # qwen / deepseek / 豆包（默认）
psycopg2-binary                 # PG（DB_URL 设置时）
playwright (chromium)           # ERP token 兜底
websocket-client                # ERP token 主路径（CDP raw）
```

## 已知 gotcha

1. **chrome remote debug 9222 必须带 `--remote-allow-origins='*'`**，否则 ws 握手 403。
2. **系统 HTTP 代理（clash/v2ray）会拦 127.0.0.1:9222**：走 devtools 的代码必须
   `requests.Session().trust_env = False` + `proxies={"http":None,"https":None}`。
3. **OAuth 订阅配额**：sonnet-4-5 在 5h 窗口易耗尽，默认模型用 haiku-4-5；
   换 sonnet 时 `export ANTHROPIC_CHAT_MODEL=claude-sonnet-4-5-20250929` 重启。
4. **chrome 多 tab 时 playwright connect_over_cdp 180s 超时**：token 抓取优先 CDP raw → playwright 兜底 → wf0 headless 登录。

## 调试

- SSE：`curl -sN http://localhost:8765/api/events/stream/<task_id>`
- chat：`curl -X POST http://localhost:8765/api/chat -d '{"messages":[...],"scope":{...}}'`
- workflow：`curl -X POST http://localhost:8765/api/run-workflow -d '{"workflow":"wf6_alerts"}'`
- chat 历史：`sqlite3 hipop.db "SELECT role,who,substr(content,1,80),created_at FROM chat_messages ORDER BY id DESC LIMIT 20"`

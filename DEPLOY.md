# 部署指南

## 本地开发模式（最简）

```bash
QWEN_API_KEY=sk-... \
  python -m uvicorn hipop.server.main:app --port 8765
```
- 用本地 SQLite (`hipop.db`)
- 不强制登录（DEFAULT_USER fallback）
- 适合调试

## 本地完整模式（PG + Auth + 多租户 RLS）

```bash
docker compose up -d postgres redis
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  python scripts/migrate_sqlite_to_pg.py    # 一次性迁老 SQLite 数据

# 起 server（用 docker compose 一并起）
docker compose up -d app
# 或本机起
DB_URL=postgresql://hipop:hipop_dev_password@localhost:5432/hipop \
  JWT_SECRET=$(openssl rand -hex 32) \
  QWEN_API_KEY=sk-... \
  python -m uvicorn hipop.server.main:app --port 8765
```

## 生产（Zeabur）

1. 在 Zeabur 控制台 import 这个 git 仓库
2. 加内置 Postgres service（自动注入 `DB_URL`）
3. 配置 env：
   - `QWEN_API_KEY`（阿里云灵积控制台）
   - `JWT_SECRET=$(openssl rand -hex 32)`
   - `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_WEBHOOK`（可选）
4. 部署后跑 `python scripts/migrate_sqlite_to_pg.py`（一次性）
5. healthcheck path `/ready`，DB 不通会自动 503 让 LB 摘流量

部署详细在 `zeabur.json`，env 模板在 `.env.example`。

## 国内备案

- .com 域名：Zeabur 默认域名先跑通（`xxx.zeabur.app`），后续切自有域名
- .cn 域名：阿里云购买备案服务（1-3 周），备案下来切阿里云 SAE 部署
- alpha 阶段免备案先用 .com + Zeabur 香港节点，国内访问 200ms 级可接受

## 切换 LLM provider

env 控制：
- `LLM_PROVIDER=qwen` (默认，¥18/万次)
- `LLM_PROVIDER=deepseek` (¥50/万次，工具调用更稳)
- `LLM_PROVIDER=anthropic` (本地开发用，OAuth keychain)
- `LLM_PROVIDER=doubao` (火山引擎)

均走 `server/_provider.py` 抽象，业务代码不变。

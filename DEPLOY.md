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

## 紫鸟 web_driver 常驻（noon 官方仓取数前置）

noon 等平台官方仓无程序化 API，取数走紫鸟超级浏览器接管已登录会话。`hipop/server/_platform_browser.py`
的 `get_platform_session` 依赖紫鸟 **web_driver 模式常驻在 `127.0.0.1:18080`**。这条以前是口头
"先手动 `open -na ziniao …`"，属隐性人工动作 —— 紫鸟退出 / 端口断 / Mac 重启后就静默断链。
**不要再手动找 chromium debug port**：端口由紫鸟自动分配，业务侧一律走 `get_platform_session`。

运维入口（本机 macOS，需已装并登录紫鸟客户端）：

```bash
# 真实健康检查：检 127.0.0.1:18080 + 每个 live session 的 CDP /json/version 可达性
python3 -m hipop.scripts.ziniao_webdriver healthcheck

# 手动拉起 / 重启（pkill -TERM -i ziniao + open -na … --run_type=web_driver --port=18080）
python3 -m hipop.scripts.ziniao_webdriver start
python3 -m hipop.scripts.ziniao_webdriver restart

# 常驻守护：开机自启 + keepalive（断了自动 restart）
bash hipop/launchd/install.sh install     # 装 com.hipop.ziniao（连同周期任务一并装）
bash hipop/launchd/install.sh status      # 看是否在跑
```

- 进程层只负责把紫鸟 web_driver **拉起 / 守活**；紫鸟**账号认证**仍由每次调用带
  company/username/password 完成（env `ZINIAO_COMPANY/ZINIAO_USERNAME/ZINIAO_PASSWORD`），
  **不引入"紫鸟 token"状态**。
- `healthcheck` 端口未监听时退出码 3（blocked）并打印拉起命令；缺紫鸟 app / 端口起不来 →
  blocked，不静默假成功。
- 旧的 `hipop/scripts/probe_ziniao_webdriver.py` 已 **deprecated**（手传 `debuggPort` 会被紫鸟
  -10000 拒绝），运行只会转交本入口。

## 切换 LLM provider

env 控制：
- `LLM_PROVIDER=qwen` (默认，¥18/万次)
- `LLM_PROVIDER=deepseek` (¥50/万次，工具调用更稳)
- `LLM_PROVIDER=anthropic` (本地开发用，OAuth keychain)
- `LLM_PROVIDER=doubao` (火山引擎)

均走 `server/_provider.py` 抽象，业务代码不变。

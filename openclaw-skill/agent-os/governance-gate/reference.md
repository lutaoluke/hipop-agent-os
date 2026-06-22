# 治理门 · 完整明细（渐进披露）

供 [`SKILL.md`](SKILL.md) 引用。

## Auth（`server/auth.py`）

- `tenants(id, name, plan)` + `users(id, tenant_id, email, role, password_hash)` + `sessions`
- 4 角色：`owner` / `manager` / `ops` / `forwarder`
- 密码 pbkdf2_sha256（避免 bcrypt 5.x 与 passlib 不兼容）
- JWT cookie/header 双通道（`JWT_SECRET` env 生产必固定）
- 兼容 fallback：未登录 → DEFAULT_USER（Cherry, owner, tenant=1），不破坏 single-tenant

Endpoints：
- `POST /api/auth/register {email, password, tenant_name?, display_name?}` — 建 tenant + owner + 自动登录
- `POST /api/auth/login {email, password}` — 返回 JWT + 设 cookie
- `POST /api/auth/logout`
- `GET /api/auth/me` — 当前 user
- `GET /api/auth/permissions` — role → action map（前端按角色隐藏入口）

## RBAC（`server/rbac.py`，CODEOWNERS 锁）

- `PERMISSIONS` 矩阵（12 个 action × 4 角色）
- `TOOL_PERMISSION` 把 chat tool 名映射到 action（chat 调 tool 前过 RBAC）
- `can(user, action)` / `tool_allowed(user, tool_name)` / `require_permission(action)` decorator
- 工作组所有角色（含 forwarder）都能 `trigger_workflow` / `upload_csv`，靠 `agent_events.actor_*`
  留痕审计而非角色阻挡；view_billing / edit_store_config 等管理类仍限 owner。

## 多租户 RLS（`db/schema.sql` + `data.py`，CODEOWNERS 锁）

- 17 张业务表加 `tenant_id BIGINT NOT NULL DEFAULT 1`（旧数据自动归 HIPOP）
- Postgres `ENABLE ROW LEVEL SECURITY` + policy `tenant_id = current_setting('app.current_tenant')::BIGINT`
- `data.py` middleware 每请求拿 user.tenant_id → `set_current_tenant(tid)` → conn() 时 `SET app.current_tenant`
- SQLite 不支持 RLS，本地开发跳过（多租户必须切 PG）
- 实测隔离：tenant=1 看自己 1788+1787 SKU；tenant=6 看 219；tenant=999（假）看 0 行；
  SQL injection 也无法跨租户读（PG RLS 强制）

## 门控 stub tool（反 hallucinate 第 2 层）

| tool | 用途 | 必调场景 |
|---|---|---|
| `export_table` | 用户问"导出/Excel/给我表格" | stub 引导浏览器另存。严禁绕过宣称「已生成 Excel」 |
| `navigate_user_to` | 用户问"打开 X 页面" | 返真实 `localhost:8765/module/<name>`。严禁编造虚构域名 |
| `notify_via_feishu` | 用户问"发飞书/通知同事" | stub 返"只读集成"。严禁宣称「已发到飞书」 |

## actor 留痕审计（SQL）

```sql
SELECT task_id, status, actor_email, actor_role, actor_source, created_at
FROM agent_events WHERE step_no = 0 AND tenant_id = <tid>
ORDER BY created_at DESC LIMIT 50;
```

governance 动作门 / 决策 pipeline 在 `server/governance.py` + `governance_actions.yaml`（CODEOWNERS 锁）；
回归 `tests/smoke_governance.py`（硬编 PG，CI 起 postgres 服务跑）。

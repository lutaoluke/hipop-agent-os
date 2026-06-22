---
name: agent-os-governance-gate
display_name: Agent OS · 治理门
version: 0.1.0
author: hipop
description: Agent OS 的治理与权限门——Auth（JWT + 4 角色）、RBAC（tool→action 映射，chat 调 tool 前过 can()）、多租户 RLS（PG FORCE ROW LEVEL SECURITY 真隔离）、门控 stub tool（拦导出/打开页面/发飞书三类幻觉），以及全通道 actor_* 留痕审计。改这些要害文件走 CODEOWNERS 审批。详表见 reference.md。
tags: [hipop, agent-os, rbac, tenant, governance]
---

# 治理门

chat 的每个动作都过：**RBAC 准入 → 租户隔离 → 门控 → 留痕**。

## Auth（`server/auth.py`）

JWT cookie/header 双通道（`JWT_SECRET` 生产必固定）；4 角色 `owner / manager / ops / forwarder`；
密码 pbkdf2_sha256；未登录回退 DEFAULT_USER（Cherry, owner, tenant=1）不破坏 single-tenant。

## RBAC（`server/rbac.py`，CODEOWNERS 锁）

`PERMISSIONS` 矩阵 + `TOOL_PERMISSION` 把 chat tool 名映射到 action；**chat 调 tool 前过 `can()`**。
工作组所有角色（含 forwarder）都能 `trigger_workflow` / `upload_csv`，靠 `actor_*` 留痕审计而非角色阻挡；
view_billing / edit_store_config 等管理类仍限 owner。

## 多租户 RLS（`db/schema.sql`，CODEOWNERS 锁）

业务表带 `tenant_id`；PG `ENABLE + FORCE ROW LEVEL SECURITY`（**FORCE 关键，否则 owner bypass**）+
policy `tenant_id = current_setting('app.current_tenant')`；middleware 每请求 `set_current_tenant(tid)`。
SQLite 不支持 RLS（多租户必须切 PG）。实测：假 tenant 看 0 行，SQL 注入也跨不了租户。

## 门控 stub tool（反 hallucinate 第 2 层）

| tool | 拦什么 | 红线 |
|---|---|---|
| `export_table` | "导出/Excel" | 严禁绕过宣称「已生成 Excel」 |
| `navigate_user_to` | "打开 X 页面" | 返真实 `localhost:8765/module/<name>`，**禁编域名** |
| `notify_via_feishu` | "发飞书/通知" | 只读集成，**禁宣称「已发飞书」** |

## actor 留痕（审计）

三通道（chat / UI / cron）都写 `agent_events.actor_user_id / actor_email / actor_role / actor_source`。
审计 SQL + governance 动作门见 [`reference.md`](reference.md)。

回归：`tests/smoke_governance.py`（连 PG）/ `smoke_safety.py` /
`smoke_ws121_ops_evidence_safety.py` / `smoke_lifecycle_gate.py`。

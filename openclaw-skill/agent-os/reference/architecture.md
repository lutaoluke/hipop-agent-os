# Agent OS · 架构与扩展点（渐进披露引用）

> 从旧单体 `agent-os.md` 拆出，供 [`../../agent-os.md`](../../agent-os.md) 索引引用。

## server 目录结构

```
hipop/server/
├─ main.py            FastAPI app + 飞书 webhook（/feishu/webhook）
├─ pages.py           模块页路由（overview / sales / logistics / replenish / selection / feishu / audit + role/liuhe）
├─ api.py             /api/* JSON + /api/run-workflow + /api/events/stream/<task_id> SSE
├─ data.py            统一访问 hipop.db（chat_messages 持久化 + agent_actions reference 表）
├─ agent.py           tool-use chat（默认 haiku-4-5，可 ANTHROPIC_CHAT_MODEL 覆盖）【CODEOWNERS 锁】
├─ _auth.py           凭据工厂：env > macOS keychain OAuth
├─ intent.py / skills.py   飞书消息旧链路（webhook 用）
└─ templates/         Jinja2（base / overview / module_* / partials/chat_panel.html）
```

## 真多租户 v2 链路（Phase A-E）

业务表列存化：`wf2_sku / wf2_orders / wf1_stock / wf5_sales_cycle / wf3_logistics_hub_v2 /
wf6_logistics_alerts_v2 / wf6_replenishment_queue_v2`，主键 `(tenant_id, entity_alias, partner_sku)`。

per-tenant ingest（从 onboarding 配的 ERP 自动拉）：
- `ingest_erp_products_v2.run_v2(tid)` → wf2_sku
- `ingest_erp_sales_v2.run_v2(tid)` → wf2_sku 销量字段（10/30/60/90/120/180d）
- `ingest_erp_stock_v2.run_v2(tid)` → wf1_stock
- `ingest_noon_csv_v2.process_csv_v2(tid, path)` → wf2_orders
- `wf_sales_cycle.run_v2(tid)` → wf5_sales_cycle

v2 WORKFLOW：`wf2_products_v2 / wf2_sales_v2 / wf1_stock_v2 / wf5_sales_cycle_v2 / refresh_all_v2`。
`_run_workflow(task_id, workflow, tenant_id)` 后台线程显式 `set_current_tenant(tid)` +
`inspect.signature` 探测 fn 是否接 `tenant_id` 自动注入。

ERP 凭据加密：`tenant_erp_credentials` + `_crypto.py` Fernet（key 派生自 JWT_SECRET）。
ERP 后端登录：`_erp_auth.get_erp_token_for_tenant(tid)` 解密 + playwright headless 登 dbuyerp +
拦 erp-api 拿 Bearer + 缓存 20min（server 自给，不依赖本机 chrome 9222）。

onboarding 用户体验：register → onboarding 配 sales_entities + ERP 凭据 → chat 提问 →
data_health_check 看陈旧 → run_workflow(refresh_all_v2) → 解密登 ERP 拉数写自己 v2 表 → step_no=99 → 自动续答。

## 模块页 → workflow 自动刷新约定

每个 module template 在 `init()` 挂 `workflow-done` 监听，命中自家 module 名（在 affected_modules）就 refresh：
```js
async init() {
  await this.refresh();
  window.addEventListener('workflow-done', (e) => {
    if ((e.detail.affected_modules || []).includes('YOUR_MODULE')) this.refresh();
  });
}
```

## 扩展点（改哪些文件）

1. **加销售主体**：`config/hipop.json:sales_entities[]` 加项 → `wf2_feishu_setup.py --alias <new>` → 跑 ingest。
2. **加 chat tool**：`agent.py:TOOLS` 加 schema → `TOOL_FUNCS` 注册 → 实现返回 `references` → SYSTEM_PROMPT 映射表加行。
3. **加 workflow**：`api.WORKFLOW_REGISTRY` 加项 → 实现 callable → `agent.py:run_workflow` enum 加名 → `data.py:dependency_groups` 列入。
4. **加意图**：`data.py:dependency_groups` 加 intent→依赖源 → SYSTEM_PROMPT 意图表加行 → （新源）`get_data_health().sources` 加该源。
5. **加数据源**：`get_data_health().sources` 加（`stale_days/automation/workflow`；needs_csv 类加 `csv_pattern/where`）→ 相关 intent 的 dependency_groups 加它。
6. **加模块页**：`pages.py` 加路由 → `module_<name>.html` 挂 workflow-done 监听 → `sidebar.html` 加导航 → REGISTRY 相关 workflow 的 affected_modules 加新模块名。

"""
LLM Coordinator - 把 hipop 的 5 个 handler + 2 个新工具包装成 Anthropic tool-use API。

7 个 tool:
  1. query_sku                   - 查 SKU 健康（来自 wf2/wf3/wf5/wf6）
  2. query_order                 - 查货单告警 + 涉及 SKU
  3. update_alert_status         - 反馈货单状态（已确认丢货 / 已约仓 / ...）
  4. scope_overview              - 店铺概览（指定国家+平台）
  5. compute_replenishment       - 列出当前店铺的补货建议
  6. compute_air_freight_roi     - 海运 vs 空运 ROI 估算（基于 SKU 利润 + 销量）
  7. data_health_check           - 数据新鲜度检查（最新 imported_at / updated_at）

每次 tool 调用都会写入 agent_actions 表 (action_type='execute')，并把 references_json
回传给前端用于"📎 出处"展示。
"""
import os, sys, json
from typing import List, Dict, Any, Optional

from ._agent_context import (
    _chat_tenant,
    _chat_scope,
    _chat_question,
    _last_replenishment_stock_status,
    _last_sku_rate_stats,
    _chat_intent,
    _get_tenant,
    _resolve_entity_alias,
)

# 让 hipop/scripts/* import
HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(HIPOP_ROOT)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from . import data as _data
from ._workflow_reply import _workflow_receipt_reply
from ._inventory_constraint_rule import handle_inventory_constraint_rule_chat as _handle_icr_chat

import anthropic
from . import _auth
from .tools_registry import load_tools_from_yaml


def _get_client():
    return _auth.get_client()


# ── 工具定义（Anthropic tool schema）────────────────────
TOOLS = load_tools_from_yaml()


# ── 工具实现（v2 列存：按 tenant_id + entity_alias 过滤）──

# T03 injection slot remains on agent for existing tests/tools_impl patches.
_sku_sales_live_fn: Optional[Any] = None

from ._tool_runtime import (
    _erp_sku_stats_live,
    _normalize_replenishment_rows,
    _write_xlsx_and_return,
    _erp_token_or_error,
    _patch_wls_token,
    _fetch_logistics_nodes,
    _physical_tracking_url,
    _utc_now_iso,
)

# ── Tool 派发 ─────────────────────────────────────────
# ── 工具实现已外移到 tools_impl（WS-166）。agent.py 仅保留注册/分发/治理入口。
# 重新导出工具实现名，保持 `agent.tool_*` 外部契约（api.py / 测试 / TOOL_FUNCS 投影）不变。
from .tools_impl import (
    tool_query_sku,
    tool_query_order,
    tool_update_alert_status,
    tool_scope_overview,
    tool_compute_replenishment,
    tool_query_replenishment_sku,
    tool_compute_air_freight_roi,
    tool_data_health_check,
    tool_list_products,
    tool_top_sales_by_window,
    tool_export_table,
    tool_navigate_user_to,
    tool_notify_via_feishu,
    tool_run_workflow,
    tool_query_1688_similar,
    tool_query_sku_live,
    tool_query_order_live,
    _tool_tenant_notes_get,
    _tool_tenant_notes_append,
    _tool_confirm_proposal,
    tool_capture_feedback,
    tool_explain_status_enum,
    tool_query_stock_split,
    tool_total_stock_topn,
)

TOOL_FUNCS = {
    "query_sku": tool_query_sku,
    "query_order": tool_query_order,
    "update_alert_status": tool_update_alert_status,
    "scope_overview": tool_scope_overview,
    "compute_replenishment": tool_compute_replenishment,
    "query_replenishment_sku": tool_query_replenishment_sku,
    "compute_air_freight_roi": tool_compute_air_freight_roi,
    "data_health_check": tool_data_health_check,
    "list_products": tool_list_products,
    "top_sales_by_window": tool_top_sales_by_window,
    "export_table": tool_export_table,
    "navigate_user_to": tool_navigate_user_to,
    "notify_via_feishu": tool_notify_via_feishu,
    "run_workflow": tool_run_workflow,
    "confirm_proposal": lambda proposal_id, user_decision: _tool_confirm_proposal(proposal_id, user_decision),
    "tenant_notes_get": lambda section="": _tool_tenant_notes_get(section),
    "tenant_notes_append": lambda note, section="通用": _tool_tenant_notes_append(note, section),
    "query_sku_live": tool_query_sku_live,
    "query_order_live": tool_query_order_live,
    "query_1688_similar": tool_query_1688_similar,
    "explain_status_enum": tool_explain_status_enum,
    "capture_feedback": tool_capture_feedback,
    "query_stock_split": tool_query_stock_split,
    "total_stock_topn": tool_total_stock_topn,
    }


def _exec_tool(name: str, args: dict, user: dict = None) -> dict:
    """tool 执行前先过 RBAC + governance pipeline。

    - RBAC: user.role → tool 入口权限
    - Governance (Phase 0.2 半 MSCL): destructive tool 走
      ActionProposal → Decision (Haiku) → ExecToken → Execute → ExecutionRecord
      read-only tool 跳过 governance，直调

    ⚠️ INVARIANT (2026-05-26)：
    所有 LLM tool 调用必须经此函数。provider 层（_provider_anthropic /
    _provider_openai）禁止自己实现 _exec_tool —— 历史上 5/21 把 _exec_tool
    复制到 provider 文件只做 RBAC，导致 destructive tool 全部裸跑（governance
    pipeline 形同虚设）。新增 provider 时：from . import agent; agent._exec_tool(...).
    smoke_governance.py 会跑 inspect.getsource 检查 provider 没自定义 _exec_tool。
    """
    try:
        # WS-145 肯定执行意图门:非执行语气（否定/询问/假设/只问影响面）下，
        # LLM 不许偷偷 run_workflow 落任务 —— 在工具执行入口确定性拦死，不靠 prompt。
        if name == "run_workflow":
            _intent = _chat_intent.get()
            if _intent is not None and getattr(_intent, "blocks_llm_execution", False):
                return {
                    "ok": False,
                    "error": "execution_intent_gate_blocked",
                    "blocked_by": "execution_intent_gate",
                    "message": (
                        "本轮是非执行语气（否定/询问/假设/只问影响面），未创建任何后台任务。"
                        "需要执行请明确说「帮我刷新/重算…」。"
                    ),
                }
        from . import rbac as _rbac
        if user and not _rbac.tool_allowed(user, name):
            return {
                "error": "permission_denied",
                "tool": name,
                "user_role": user.get("role"),
                "message": f"当前角色 {user.get('role')} 不能调用 {name}（请向 owner/manager 申请权限）",
            }
        if name not in TOOL_FUNCS:
            return {"error": f"unknown tool: {name}"}
        # ── Governance pipeline（仅 destructive） ──
        from . import governance as _gov
        if _gov.is_destructive(name):
            sc = _chat_scope.get() or {}
            actor = {
                "user_id": (user or {}).get("id") or sc.get("user_id"),
                "email": (user or {}).get("email") or sc.get("current_user_email"),
                "role": (user or {}).get("role") or sc.get("current_role"),
                "tenant_id": (user or {}).get("tenant_id") or sc.get("tenant_id") or _get_tenant(),
                "source": sc.get("source") or "chat",
            }
            return _gov.propose_and_execute(name, args, actor, sc, TOOL_FUNCS)
        # read-only 直调
        fn = TOOL_FUNCS[name]
        return fn(**args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── Chat 主入口 ──────────────────────────────────────
# Prompt 文本已外移到 _prompts.py（WS-168）；agent.py 只保留接线，不承载 prompt 文本本体。
from ._prompts import SYSTEM_PROMPT_LEGACY, SYSTEM_PROMPT, _JUDGE_SYSTEM_PROMPT


from ._chat_pipeline import (
    _OFFER_MARK,
    _OFFER_LINE,
    _OFFER_SEEN,
    _run_llm_judge,
    _compute_judge_confidence,
    _strip_safety_banner,
    _clean_history,
    _needs_feedback_offer,
    _maybe_append_feedback_offer,
    _maybe_append_stock_readiness_warning,
    _ensure_export_download_link,
    _maybe_inject_missing_rates,
    _asks_workflow_impact,
    _workflow_business_impact_reply,
    _maybe_append_oldest_data_health_date,
    _maybe_append_order_lookup_negative_hint,
    _maybe_append_navigation_url,
    _dedup_refs,
)

from ._deterministic_routes import (  # WS-167: 确定性路由/formatter 外移到非锁模块
    _deterministic_data_freshness_request,
    _deterministic_erp_refresh_time_request,
    _deterministic_export_request,
    _deterministic_multi_workflow_request,
    _deterministic_product_sales_topn_request,
    _deterministic_window_sales_topn_request,
    _deterministic_products_count_request,
    _deterministic_readonly_reply,
    _deterministic_readonly_request,
    _deterministic_replenishment_list_request,
    _deterministic_replenishment_sku_request,
    _deterministic_scope_overview_request,
    _deterministic_sku_metric_request,
    _deterministic_stock_split_request,
    _deterministic_total_stock_topn_request,
    _deterministic_workflow_request,
    _extract_live_order_no,
    _fmt_int,
    _format_data_freshness_reply,
    _format_erp_refresh_time_reply,
    _format_metric_value,
    _format_order_live_reply,
    _format_pct,
    _format_product_sales_topn_reply,
    _format_window_sales_topn_reply,
    _format_products_count_reply,
    _format_replenishment_list_reply,
    _format_scope_overview_reply,
    _format_sku_metric_reply,
    _format_stock_split_reply,
    _format_total_stock_topn_reply,
    _has_stock_refresh_intent,
    _procurement_rate_rule_response,
    _stock_refresh_refusal_reply,
    _stock_refresh_refused,
    _window_sales_topn_route,
)


from ._chat_workflows import (
    _current_workflow_task,
    _existing_workflow_task_id,
    _workflow_registry_summary,
    _active_workflow_task,
    _logistics_task_evidence_check,
    _extract_freshness_target_date,
    _detect_operational_domain,
    _freshness_gate_route,
    _execute_workflow_route,
    _msg_text,
    _inventory_refresh_feasibility,
    _pending_inventory_refresh_inquiry,
    _inventory_refresh_no_task_result,
    _inventory_refresh_confirm_gate,
)

def chat(messages: List[Dict], scope: Dict) -> Dict:
    """
    messages: [{role: 'user'|'assistant', content: '...'}]
    scope: {store, current_user, current_role, tenant_id, user_id, ...}
    返回: {reply, clean_reply, references, action_id, tag, workflow_tasks, tools_used, provider, confidence}

    走 _provider 抽象层，通过 LLM_PROVIDER env 切换 anthropic / qwen / deepseek / doubao。
    """
    from . import _provider

    # 把 scope.tenant_id 注入 contextvars，让所有 tool 函数（同线程）能拿到
    _chat_tenant.set(scope.get("tenant_id") or 1)
    _chat_scope.set(scope)
    _last_replenishment_stock_status.set(None)
    _last_sku_rate_stats.set(None)
    # 同时设给 data 层（PG RLS 用）
    _data.set_current_tenant(scope.get("tenant_id") or 1)

    question = messages[-1].get("content") if messages else ""
    if isinstance(question, list):  # content 可能是 blocks
        question = " ".join(b.get("text", "") for b in question if isinstance(b, dict))
    _chat_question.set(question or "")

    # WS-145 肯定执行意图门:本轮句式语气 + 风险分层一次求出，注入 contextvar，
    # 供 _exec_tool 在非执行语气下拒绝 run_workflow（LLM 不许绕）。
    from . import _execution_intent_gate as _intent_gate
    _intent_decision = _intent_gate.evaluate(question or "")
    _chat_intent.set(_intent_decision)

    # WS-150: 工作台不支持主动发飞书/通知群（确定性拒绝，不进 confirm-first）
    if _intent_decision.unsupported_feishu_notify:
        reply = _intent_gate.unsupported_feishu_notify_reply()
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "拒绝",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "execution_intent_gate_unsupported_feishu_notify",
            "hallucination_warnings": None,
        }

    # 高风险动作（外部通知/交易·采购·订单/不可回滚/跨店批量覆盖）即使肯定句也不自动执行:
    # 先 confirm，不自动补调。确定性短路，绝不落任务。
    if _intent_decision.needs_confirm_first:
        reply = _intent_gate.confirm_first_reply(question or "")
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "确认",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "execution_intent_gate_confirm_first",
            "hallucination_warnings": None,
        }

    # WS-159 库存刷新询问式确认门:跨轮 pending 解锁。
    #   询问轮（能不能帮我刷新库存?）→ 提议 + 反问，不落任务;
    #   裸确认轮（好/可以/确认）且上一轮有提议 → 只执行一次真实 wf1_stock_v2;
    #   取消/换题/模糊/无 pending 裸确认 → 不执行。
    # 高风险询问不入此门（由上方 confirm-first / _exec_tool 兜），一句「好」不解锁高风险。
    inv_refresh = _inventory_refresh_confirm_gate(messages, question, scope)
    if inv_refresh is not None:
        return inv_refresh

    direct_export = _deterministic_export_request(question)
    if direct_export:
        store = scope.get("store") or "KSA"
        tool_args = {
            "view": direct_export["view"],
            "store": store,
            "filter_desc": direct_export["filter_desc"],
        }
        tool_result = _exec_tool("export_table", tool_args, user=scope)
        if isinstance(tool_result, dict) and tool_result.get("ok") and tool_result.get("download_url"):
            filename = tool_result.get("filename") or "export.xlsx"
            reply = (
                f"已生成 {tool_result.get('row_count', 0)} 行表格："
                f"[{filename}]({tool_result['download_url']})"
            )
        else:
            reply = (tool_result or {}).get("message") or (tool_result or {}).get("error") or "导出失败。"
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": ["export_table"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_export_router",
            "hallucination_warnings": None,
        }

    direct_readonly = _deterministic_readonly_request(question)
    if direct_readonly:
        store = (scope.get("store") or "KSA").upper()
        tool_args = {"store": store}
        tool_result = _exec_tool(direct_readonly["tool"], tool_args, user=scope)
        reply = _deterministic_readonly_reply(
            direct_readonly["intent"], tool_result or {}, store
        )
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": [direct_readonly["tool"]],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_readonly_router",
            "hallucination_warnings": None,
        }

    # T37 round-15（Luke 指令①）：库存刷新副作用动作，用户明确拒绝（任意语序）→
    # 确定性回复，绝不调 run_workflow，绝不伪造任务号/已启动声明。
    stock_refusal_reply = _stock_refresh_refusal_reply(question)
    if stock_refusal_reply:
        return {
            "reply": stock_refusal_reply,
            "clean_reply": stock_refusal_reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "执行",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_stock_refusal_router",
            "hallucination_warnings": None,
        }

    if r := _procurement_rate_rule_response(question, _provider.get_provider()):
        return r

    if _deterministic_erp_refresh_time_request(question):
        store = (scope.get("store") or "KSA").upper()
        reply = _format_erp_refresh_time_reply(store, _data.get_data_health(store))
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "查询",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_erp_refresh_time_router",
            "hallucination_warnings": None,
        }

    if _deterministic_data_freshness_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("data_health_check", {"store": store}, user=scope)
        reply = _format_data_freshness_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["data_health_check"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_data_freshness_router",
            "hallucination_warnings": None,
        }

    # WS-98 Round-2：非执行语气的刷新/重算意图（询问/假设/只问影响面）确定性短路，
    # 交回 WS-145 结构门的干净解释，绝不落 LLM。否则 LLM 会去试 run_workflow——
    # 虽被 _exec_tool 拦下不落任务，却会污染 tools_used 且触发 _safety 假活 banner，
    # 把「能不能帮我刷新…?」误渲染成带警告/「启动失败」的回复（验门人 Round-2 红队洞）。
    # NEGATED 不在此短路：它常是「不用刷新，但告诉我哪些要补」这类仍要数据答案的句子，
    # 留给 LLM 给陈旧警示 + 答案（smoke「用户拒绝刷新」）。
    if (
        _intent_decision.has_refresh_trigger
        and _intent_decision.mood in (
            _intent_gate.IntentMood.INTERROGATIVE,
            _intent_gate.IntentMood.HYPOTHETICAL,
            _intent_gate.IntentMood.IMPACT_QUERY,
        )
        and not _deterministic_sku_metric_request(question)
    ):
        reply = _intent_gate.explain_reply(_intent_decision.mood, question)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": [],
            "action_id": None,
            "tools_used": [],
            "tag": "查询",
            "workflow_task": None,
            "workflow_tasks": [],
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "execution_intent_gate_explain_non_executory",
            "hallucination_warnings": None,
        }

    direct_workflows = _deterministic_multi_workflow_request(question)
    if direct_workflows:
        workflow_tasks = []
        reply_parts = []
        for direct_workflow in direct_workflows:
            workflow = direct_workflow["workflow"]
            label = direct_workflow["label"]
            tool_args = {"workflow": workflow, "followup_prompt": question}
            tool_result = _exec_tool("run_workflow", tool_args, user=scope)
            if isinstance(tool_result, dict) and tool_result.get("ok"):
                task_id = tool_result["task_id"]
                workflow_tasks.append({
                    "ok": True,
                    "task_id": task_id,
                    "workflow": tool_result.get("workflow", workflow),
                    "label": tool_result.get("label", label),
                    "total_steps": tool_result.get("total_steps", 0),
                    "affected_modules": tool_result.get("affected_modules", []),
                    "followup_prompt": tool_result.get("followup_prompt"),
                })
                reply_parts.append(_workflow_receipt_reply(
                    task_id, tool_result.get("workflow", workflow), label
                ))
                continue

            existing_task_id = None
            if (
                isinstance(tool_result, dict)
                and tool_result.get("action_type") == "denied"
                and "已有运行中实例" in (tool_result.get("reason") or "")
            ):
                existing = _current_workflow_task(workflow)
                if existing:
                    existing_task_id = existing["task_id"]
                    workflow_tasks.append({
                        "ok": True,
                        "task_id": existing_task_id,
                        "workflow": existing["workflow"],
                        "label": label,
                        "total_steps": None,
                        "affected_modules": [],
                        "followup_prompt": question,
                        "state": existing.get("state"),
                        "already_running": True,
                    })
                    reply_parts.append(
                        f"{label}（{workflow}）已有运行中的后台任务 `{existing_task_id}`，"
                        "本轮未新建重复任务。请在工作台任务面板查看进度。"
                    )
                    continue
                existing_task_id = _existing_workflow_task_id(tool_result)

            if existing_task_id:
                label2, total_steps, affected = _workflow_registry_summary(workflow, label)
                workflow_tasks.append({
                    "ok": True,
                    "task_id": existing_task_id,
                    "workflow": workflow,
                    "label": label2,
                    "total_steps": total_steps,
                    "affected_modules": affected,
                    "followup_prompt": question,
                    "already_running": True,
                })
                reply_parts.append(
                    f"{label2}（{workflow}）已有同类后台任务在运行，未新建重复任务。\n"
                    f"任务 ID：{existing_task_id}｜当前状态：运行中或排队。\n"
                    "请在工作台任务面板查看进度；任务结束后如仍需刷新，可以再重试。"
                )
                continue

            reason = (
                (tool_result or {}).get("message")
                or (tool_result or {}).get("error")
                or (tool_result or {}).get("reason")
                or "触发失败"
            )
            workflow_tasks.append({
                "ok": False,
                "workflow": workflow,
                "label": label,
                "error": reason,
                "task_id": None,
            })
            reply_parts.append(f"{label}（{workflow}）启动失败：{reason}。")

        return {
            "reply": "\n\n".join(reply_parts),
            "clean_reply": "\n\n".join(reply_parts),
            "references": [],
            "action_id": None,
            "tools_used": ["run_workflow"],
            "tag": "执行",
            "workflow_tasks": workflow_tasks,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_multi_workflow_router",
            "hallucination_warnings": None,
        }

    direct_workflow = _deterministic_workflow_request(question)
    if direct_workflow:
        return _execute_workflow_route(direct_workflow, question, scope)

    win_resp = _window_sales_topn_route(question, scope, _exec_tool, _provider.get_provider())
    if win_resp is not None:  # WS-120：指定日期窗口 / 近N天 → top_sales_by_window（先于 WS-148 裸 TopN）
        return win_resp

    direct_sales_topn_n = _deterministic_product_sales_topn_request(question)
    if direct_sales_topn_n is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "list_products",
            {"store": store, "listing": "all", "limit": direct_sales_topn_n},
            user=scope,
        )
        reply = _format_product_sales_topn_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["list_products"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if not (tool_result or {}).get("error") else 0.8,
            "judge_method": "deterministic_product_sales_topn_router",
            "hallucination_warnings": None,
        }

    if _provider.get_provider() != "smoke" and _deterministic_products_count_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("list_products", {"store": store, "listing": "all", "limit": 0}, user=scope)
        reply = _format_products_count_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["list_products"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_products_count_router",
            "hallucination_warnings": None,
        }

    if _deterministic_scope_overview_request(question):
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("scope_overview", {"store": store}, user=scope)
        reply = _format_scope_overview_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["scope_overview"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_scope_overview_router",
            "hallucination_warnings": None,
        }

    direct_replenishment_sku = _deterministic_replenishment_sku_request(question)
    if direct_replenishment_sku:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "query_replenishment_sku",
            {"sku": direct_replenishment_sku, "store": store},
            user=scope,
        )
        from . import replenishment_evidence as _rep
        reply = _rep.format_replenishment_sku_reply(tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_replenishment_sku"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if (tool_result or {}).get("ok") else 0.9,
            "judge_method": "deterministic_replenishment_sku_router",
            "hallucination_warnings": None,
        }

    direct_replenishment_limit = _deterministic_replenishment_list_request(question)
    if direct_replenishment_limit is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "compute_replenishment",
            {"store": store, "limit": direct_replenishment_limit},
            user=scope,
        )
        reply = _format_replenishment_list_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["compute_replenishment"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0 if not (tool_result or {}).get("fail_closed") else 0.9,
            "judge_method": "deterministic_replenishment_list_router",
            "hallucination_warnings": None,
        }

    direct_stock_split_sku = _deterministic_stock_split_request(question)
    if direct_stock_split_sku:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("query_stock_split", {"sku": direct_stock_split_sku, "store": store}, user=scope)
        reply = _format_stock_split_reply(direct_stock_split_sku, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_stock_split"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_stock_split_router",
            "hallucination_warnings": None,
        }

    direct_sku_metric = _deterministic_sku_metric_request(question)
    if direct_sku_metric:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool(
            "query_sku",
            {"skus": [direct_sku_metric], "store": store},
            user=scope,
        )
        reply = _format_sku_metric_reply(direct_sku_metric, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_sku"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_sku_metric_router",
            "hallucination_warnings": None,
        }

    direct_order_no = _extract_live_order_no(question)
    if direct_order_no:
        tool_result = _exec_tool("query_order_live", {"order_no": direct_order_no}, user=scope)
        reply = _format_order_live_reply(direct_order_no, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["query_order_live"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_order_live_router",
            "hallucination_warnings": None,
        }

    # T15 — 总库存 TopN 确定性路由（WS-139）
    direct_topn_n = _deterministic_total_stock_topn_request(question)
    if direct_topn_n is not None:
        store = scope.get("store") or "KSA"
        tool_result = _exec_tool("total_stock_topn", {"store": store, "n": direct_topn_n}, user=scope)
        reply = _format_total_stock_topn_reply(store, tool_result)
        return {
            "reply": reply,
            "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None,
            "tools_used": ["total_stock_topn"],
            "tag": "查询",
            "workflow_task": None,
            "provider": _provider.get_provider(),
            "confidence": 1.0,
            "judge_method": "deterministic_total_stock_topn_router",
            "hallucination_warnings": None,
        }

    if r := _handle_icr_chat(question, _provider.get_provider()):
        return r

    # T07: freshness gate — 运营查询（TopN 销量等）在 LLM 前先检业务日覆盖
    store = scope.get("store", "KSA")
    gate_result = _freshness_gate_route(store, question, scope)
    _t07_stale_suffix = ""
    if gate_result is not None:
        if gate_result.get("_stale_skip"):
            # sales_skip 场景：继续走 LLM，事后追加确定性陈旧提示
            _t07_stale_suffix = gate_result.get("_stale_suffix", "")
        else:
            return gate_result

    sys_text = SYSTEM_PROMPT.format(scope=json.dumps(scope, ensure_ascii=False))
    result = _provider.chat_with_tools(
        messages=_clean_history(messages),   # 清掉历史里残留的 banner，断自激
        system=sys_text,
        tools=TOOLS,
        tool_funcs=TOOL_FUNCS,
        scope=scope,
    )
    clean_reply  = result["reply"]           # LLM 原文（无 banner）— 用于持久化 + 喂未来历史
    tool_log     = result["tool_log"]
    refs_collected = result["refs_collected"]
    workflow_tasks = result.get("workflow_tasks", [])
    tools_used     = [t["name"] for t in tool_log]

    # T07-2 sales_skip: 确定性陈旧后缀（代码级注入，不依赖 LLM wording）
    if _t07_stale_suffix:
        clean_reply += _t07_stale_suffix

    # Layer 3 hallucinate 后处理（上移自 api.py — 一处产生 warnings，既喂 confidence 又 sanitize）
    # final_text = 展示版（可能带 banner）；clean_reply = 持久化版（无 banner，防历史自激）
    from . import _safety
    final_text, hallu_warnings = _safety.sanitize_reply(clean_reply, tools_used, tool_log=tool_log, question=question)
    clean_reply = _strip_safety_banner(final_text)

    # WS-117 采购议价率口径生产接线（deterministic verifier，非 prompt）
    from hipop.rules.procurement_rate import check_procurement_rate_reply as _check_procurement_rate
    _procurement_warns = _check_procurement_rate(clean_reply)
    if _procurement_warns:
        hallu_warnings = list(hallu_warnings or []) + _procurement_warns
        if not final_text.startswith("⚠️"):
            _proc_banner = (
                "⚠️ **系统检测到采购议价率口径可能有误**：\n"
                + "\n".join(f"- {w}" for w in _procurement_warns)
                + "\n\n---\n\n"
            )
            final_text = _proc_banner + final_text

    final_text = _maybe_append_stock_readiness_warning(final_text)
    clean_reply = _maybe_append_stock_readiness_warning(clean_reply)
    final_text = _ensure_export_download_link(final_text, tool_log)
    clean_reply = _ensure_export_download_link(clean_reply, tool_log)
    final_text = _maybe_inject_missing_rates(final_text, question)
    clean_reply = _maybe_inject_missing_rates(clean_reply, question)
    final_text = _maybe_append_oldest_data_health_date(final_text, question, tools_used, scope)
    clean_reply = _maybe_append_oldest_data_health_date(clean_reply, question, tools_used, scope)
    final_text = _maybe_append_order_lookup_negative_hint(final_text, question, tools_used)
    clean_reply = _maybe_append_order_lookup_negative_hint(clean_reply, question, tools_used)
    final_text = _maybe_append_navigation_url(final_text, tool_log)
    clean_reply = _maybe_append_navigation_url(clean_reply, tool_log)

    # judge + confidence 真逻辑（混合：启发式 + 低置信/destructive 触发 LLM judge）
    judge, confidence, judge_method = _compute_judge_confidence(
        question, final_text, tool_log, refs_collected, hallu_warnings)

    # 低置信自动在 reply 头部加提示（_safety 已加 banner 时不重复，避免双 banner）
    if confidence < 0.6 and not hallu_warnings:
        final_text = (
            f"⚠️ 我对这个回答的置信度较低（{int(confidence*100)}%），"
            "建议你核实关键数字，或换个更明确的问法。\n\n---\n\n"
        ) + final_text

    # WS-26: 撞限（做不到/超范围）回复确定性补一句『要记成需求吗』offer。
    # display 版 + 持久化版都补，保证下一轮用户回『记一下』时对话连贯。
    final_text  = _maybe_append_feedback_offer(final_text, tools_used)
    clean_reply = _maybe_append_feedback_offer(clean_reply, tools_used)

    # 写入 agent_actions（reference 系统）
    action_id = None
    if final_text and (refs_collected or tool_log):
        try:
            first_tool_args = _safety._normalize_args(tool_log[0].get("args") or {}) if tool_log else {}
            action_id = _data.write_agent_action(
                store=scope.get("store", "KSA"),
                module="chat",
                action_type="execute",
                subject=(first_tool_args.get("sku") or first_tool_args.get("order_no")) if tool_log else None,
                judge=judge,
                pill_text="执行" if _safety._is_substantive_action(tool_log) else ("查询" if tool_log else "信息"),
                pill="info",
                confidence=confidence,
                options=[],
                references=_dedup_refs(refs_collected),
                owner=scope.get("current_user", "Cherry"),
            )
        except Exception:
            pass

    return {
        "reply": final_text.strip() or "(无回复)",          # 展示版（带 banner）给前端当场看
        "clean_reply": (clean_reply or "").strip() or "(无回复)",  # 无 banner 版给持久化，防历史自激
        "references": _dedup_refs(refs_collected),
        "action_id": action_id,
        "tools_used": tools_used,
        "tag": ("hallucinate" if hallu_warnings else ("执行" if _safety._is_substantive_action(tool_log) else ("查询" if tool_log else None))),
        "workflow_tasks": workflow_tasks,
        "provider": _provider.get_provider(),
        "confidence": round(confidence, 2),
        "judge_method": judge_method,
        "hallucination_warnings": hallu_warnings or None,
}

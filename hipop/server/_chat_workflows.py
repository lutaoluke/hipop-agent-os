"""Workflow, freshness, and inventory-confirmation helpers for chat().

These functions are business/runtime logic used by the chat entrypoint. WS-169
moves their bodies out of `agent.py`; where historical tests patch
`agent._exec_tool` or `agent._workflow_receipt_reply`, small delegates below keep
those patches effective.
"""
import re
import re as _re
from typing import Dict, List, Optional

from . import data as _data
from ._agent_context import _get_tenant
from ._chat_pipeline import _dedup_refs, _workflow_business_impact_reply


def _agent_module():
    from . import agent
    return agent


def _exec_tool(name: str, args: dict, user: dict = None) -> dict:
    return _agent_module()._exec_tool(name, args, user=user)


def _workflow_receipt_reply(task_id: str, workflow: str, label: str) -> str:
    return _agent_module()._workflow_receipt_reply(task_id, workflow, label)


def _current_workflow_task(workflow: str) -> Optional[dict]:
    try:
        rows = _data._fetch(
            "SELECT task_id, workflow, state FROM tasks WHERE tenant_id=? AND workflow=? "
            "AND state IN ('running','queued') ORDER BY COALESCE(last_heartbeat, started_at) DESC LIMIT 1",
            (_get_tenant(), workflow),
        )
        return dict(rows[0]) if rows else None
    except Exception:
        return None


_RUNNING_WORKFLOW_TASK_RE = re.compile(
    r"已有运行中实例:\s*\[\s*['\"]?([0-9a-fA-F]{8})"
)


def _existing_workflow_task_id(tool_result: dict) -> Optional[str]:
    """Return the real task id when governance denies only because the workflow is already running."""
    if not isinstance(tool_result, dict) or tool_result.get("action_type") != "denied":
        return None
    reason = tool_result.get("reason") or ""
    m = _RUNNING_WORKFLOW_TASK_RE.search(reason)
    return m.group(1).lower() if m else None


def _workflow_registry_summary(workflow: str, fallback_label: str) -> tuple[str, int, list]:
    try:
        from . import api as _api
        label, steps, affected = _api.WORKFLOW_REGISTRY.get(workflow, (fallback_label, [], []))
        return label, len(steps), affected
    except Exception:
        return fallback_label, 0, []


def _active_workflow_task(workflow: str) -> Optional[Dict]:
    rows = _data._fetch(
        "SELECT task_id, workflow, state FROM tasks "
        "WHERE tenant_id=? AND workflow=? AND state IN ('running', 'queued') "
        "ORDER BY COALESCE(last_heartbeat, started_at) DESC NULLS LAST LIMIT 1",
        (_get_tenant(), workflow),
    )
    if not rows:
        return None
    from . import api as _api
    label, steps, affected = _api.WORKFLOW_REGISTRY.get(workflow, (workflow, [], []))
    task = rows[0]
    return {
        "task_id": task["task_id"],
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "followup_prompt": None,
        "state": task.get("state"),
    }


def _logistics_task_evidence_check(task_id: str) -> Optional[str]:
    """物流入口证据检查（T21-SUB-3）：用 SUB-1 统一回读接口验证 durable 任务证据。

    返回 None → 证据完整（task row 存在 + ≥1 queued/started 事件），无需降级。
    返回字符串 → 证据缺失，调用方将此字符串作为降级回复（替换「已触发」）。

    任务表报错或事件缺失 → 回复降级为「未确认创建成功」，绝不返回假成功。
    孤儿事件（agent_events 有记录但 tasks 行不存在）同样降级，不放行假成功。
    """
    try:
        evidence = _data.get_task_with_events(task_id)
    except Exception:
        return ("物流后台任务**未确认创建成功**（任务表查询出错）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    if evidence is None:
        # task row 不存在（含孤儿事件场景：agent_events 有记录但 tasks 行缺失）
        return ("物流后台任务**未确认创建成功**（任务行不存在）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    if not evidence.get("events"):
        return ("物流后台任务**未确认创建成功**（任务记录或事件缺失）。"
                "请稍后在工作台任务面板确认任务状态，或重试。")
    return None


# ── T07 freshness gate（确定性运营查询预检）────────────────────────────────
# 在 LLM 调用前插入：识别"最新/今天/TopN 销量"类运营查询 → 检查业务日覆盖 → 数据不足时
# 直接触发 workflow 或返回结构化不可用，禁止 LLM 自由补数（workflow_task=null + 模拟数事故源）。
_FRESHNESS_GATE_SALES_RE = _re.compile(
    # 显式时间窗 + 销售意图
    r"(?:今天|今日|最新|本周|这周|最近[0-9一两三四五六七八九十]+天?).*?(?:卖|销量|销售|热销|top\s*\d|前\s*\d|排名)"
    # 纯销售排名短语（无时间也隐含"最近"）
    r"|(?:卖得最好|卖得最多|热销|热门|销量最高|销量最多|最畅销|最好卖)"
    r"|(?:前[0-9]+|top\s*[0-9]+).*?(?:销量|卖|热销)"
    r"|哪[些个].*?(?:卖得最好|卖得最多|销量最高|最畅销|最好卖)",
    _re.IGNORECASE | _re.DOTALL,
)
# 明确拒绝刷新的否定短语 → 不触发 gate（用户想用现有数据答）
_FRESHNESS_GATE_SKIP_RE = _re.compile(
    r"(?:不用|不要|无需|先别).{0,8}(?:刷新|更新|同步)|就用现在的|先告诉我|不用等",
)
# WS-119: 库存类批量/榜单/约束查询（排行、可售、缺货、积压…）需 freshness gate 路由。
# 只匹配「批量/排序/数量约束」意图；单 SKU 实时问题由编码排除（见 _SKU_OR_ORDER_CODE_RE）。
_FRESHNESS_GATE_STOCK_RE = _re.compile(
    r"(?:库存|可售|缺货|断货|积压|备货).*?(?:排行|排名|榜|最多|最高|最大|最低|最少|多少|够不够|不够|缺口|清单|哪[些个]|top\s*\d|前\s*\d)"
    r"|(?:哪[些个]|多少).*?(?:库存|可售|缺货|断货|积压)"
    r"|(?:库存|可售|积压).*?(?:排行|排名|top\s*\d|前\s*\d)",
    _re.IGNORECASE | _re.DOTALL,
)
# WS-119: 物流类批量/榜单查询（在途/卡单/滞留排行、汇总）需 freshness gate 路由。
# 单 SKU/单货单实时问题继续优先走 query_sku_live/query_order_live（编码排除 + 既有 order_live 路由）。
_FRESHNESS_GATE_LOGISTICS_RE = _re.compile(
    r"(?:在途|卡单|滞留|压货|物流|货单|发货).*?(?:排行|排名|榜|最多|最高|多少|总量|汇总|清单|哪[些个]|top\s*\d|前\s*\d)"
    r"|(?:哪[些个]|多少).*?(?:在途|卡单|滞留|货单)"
    r"|(?:卡单|滞留).*?(?:货单|批次|sku|SKU)",
    _re.IGNORECASE | _re.DOTALL,
)
# WS-119 验收③：带明确 SKU/货单编码的单点实时问题 → 不当批量榜单 gate，交既有 live 工具（防退化成旧缓存）。
# 复用 _extract_live_order_no 的编码形态（2+ 字母 + 5+ 位字母数字/连字符），避免误吞 "top5"/"SKU" 这类非编码词。
_SKU_OR_ORDER_CODE_RE = _re.compile(r"\b[A-Z]{2,}[A-Z0-9-]{5,}\b")
_FRESHNESS_TARGET_ISO_DATE_RE = _re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_FRESHNESS_TARGET_CN_DATE_RE = _re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?")


def _extract_freshness_target_date(question: str) -> Optional[str]:
    """Extract an explicit target business date/window end_date from a question."""
    import datetime as _dt

    candidates = []
    for rx in (_FRESHNESS_TARGET_ISO_DATE_RE, _FRESHNESS_TARGET_CN_DATE_RE):
        for m in rx.finditer(question or ""):
            try:
                candidates.append(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                continue
    if not candidates:
        return None
    return max(candidates).isoformat()


def _detect_operational_domain(question: str) -> Optional[str]:
    """T07: 识别需要 freshness gate 的运营查询类型。

    返回值:
      'sales'      — 销量类查询，需要 freshness gate 路由
      'sales_skip' — 用户明确说不用刷新，但仍在问销量排名；gate 跳过但需注入陈旧提示
      'stock'      — 库存类批量/榜单/约束查询（WS-119）
      'logistics'  — 物流类批量/榜单查询（WS-119）
      None         — 无需 gate（非运营查询、明确说不刷、或带 SKU/货单编码的单点实时问题）
    """
    q = question or ""
    skip = bool(_FRESHNESS_GATE_SKIP_RE.search(q))
    sales = bool(_FRESHNESS_GATE_SALES_RE.search(q))
    if skip and sales:
        return "sales_skip"
    if skip:
        return None
    if sales:
        return "sales"
    # WS-119：库存/物流批量榜单查询接入同一 freshness gate。
    # 验收③：带明确 SKU/货单编码的单点实时问题不被批量 gate 捕获——交既有
    # query_sku_live / query_order_live（防退化成旧缓存）。
    has_code = bool(_SKU_OR_ORDER_CODE_RE.search(q.upper()))
    if not has_code:
        if _FRESHNESS_GATE_STOCK_RE.search(q):
            return "stock"
        if _FRESHNESS_GATE_LOGISTICS_RE.search(q):
            return "logistics"
    return None


def _freshness_gate_route(store: str, question: str, scope: Dict) -> Optional[Dict]:
    """T07: LLM 调用前确定性 freshness 路由。
    返回完整 chat response dict（直接返给调用方）；若数据已覆盖/无法匹配则返 None（继续走 LLM）。
    特例：返回 {"_stale_skip": True, "_stale_suffix": "..."} 时，调用方应继续走 LLM 并在
    LLM 回复后追加 _stale_suffix（确定性陈旧警示，避免依赖 LLM wording 导致 T07-2 flaky）。
    """
    from . import _provider as _prov
    domain = _detect_operational_domain(question)
    if not domain:
        return None
    target_date = _extract_freshness_target_date(question)

    # sales_skip: 用户明确说不刷新但仍问销量排名。不拦截 LLM，但检查数据新鲜度，
    # 若数据陈旧则返回确定性陈旧后缀供调用方追加（代码级注入，不依赖 LLM wording）。
    if domain == "sales_skip":
        freshness = _data.check_freshness_coverage(store, "sales", target_date)
        latest = freshness.get("latest_date") or ""
        target = freshness.get("target_date") or ""
        if freshness.get("covered"):
            suffix = (
                "\n\n（提示：按你的要求本轮没有刷新，直接使用当前销量数据"
                + (f"，最新到 {latest}" if latest else "")
                + "；noon 销量同步可能滞后，结果偏保守。）"
            )
            return {"_stale_skip": True, "_stale_suffix": suffix}
        target_s = f"目标日期 {target} 暂未覆盖" if target_date else "未更新到今天"
        suffix = (
            f"\n\n（⚠️ 提示：当前销量数据{target_s}"
            + (f"，最新到 {latest}" if latest else "")
            + "，如需最新数据请随时刷新。）"
        )
        return {"_stale_skip": True, "_stale_suffix": suffix}

    freshness = _data.check_freshness_coverage(store, domain, target_date)
    if freshness.get("covered"):
        return None  # 数据新鲜 → 继续走 LLM/既有确定性路由（用最新业务日算）

    # WS-119：文案按域走，库存/物流不串"销量"措辞。
    domain_label = {"sales": "销量", "stock": "库存", "logistics": "物流"}.get(domain, domain)
    action = freshness.get("action") or "unavailable"
    latest = freshness.get("latest_date") or ""
    target = freshness.get("target_date") or ""
    wf = freshness.get("workflow")
    when_s = f"最新到 {latest}" if latest else "暂无数据"

    if action == "run_workflow" and wf:
        tool_result = _exec_tool("run_workflow", {"workflow": wf, "followup_prompt": question}, user=scope)
        workflow_task = None
        if isinstance(tool_result, dict) and tool_result.get("ok"):
            workflow_task = {
                "task_id": tool_result["task_id"],
                "workflow": tool_result["workflow"],
                "label": tool_result["label"],
                "total_steps": tool_result["total_steps"],
                "affected_modules": tool_result["affected_modules"],
                "followup_prompt": tool_result.get("followup_prompt"),
            }
            reply = (
                f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，"
                f"已触发更新（{wf}）。跑完后我会接着告诉你。"
            )
        else:
            err = (tool_result or {}).get("error") or ""
            reply = f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，更新触发失败：{err}"
        return {
            "reply": reply, "clean_reply": reply,
            "references": _dedup_refs((tool_result or {}).get("references", [])),
            "action_id": None, "tools_used": ["run_workflow"], "tag": "执行",
            "workflow_task": workflow_task,
            "provider": _prov.get_provider(), "confidence": 1.0,
            "judge_method": "freshness_gate", "freshness_gate": freshness,
            "hallucination_warnings": None,
        }

    if action == "upload_csv":
        csv_hint = freshness.get("csv_hint") or {}
        reply = (
            f"{domain_label}数据{when_s}，目标日期 {target} 暂未覆盖，无法自动刷新此部分。\n\n"
            f"👉 请到{csv_hint.get('where', '紫鸟 noon 后台')}导出 CSV，"
            f"文件名形如 `{csv_hint.get('csv_pattern', 'sales_noon_*.csv')}`，"
            "拖到工作台 📤 上传区。上传后我会接着告诉你。"
        )
        return {
            "reply": reply, "clean_reply": reply, "references": [],
            "action_id": None, "tools_used": [], "tag": "信息",
            "workflow_task": None,
            "provider": _prov.get_provider(), "confidence": 1.0,
            "judge_method": "freshness_gate", "freshness_gate": freshness,
            "hallucination_warnings": None,
        }

    # fallback: 数据不足，无 workflow 可跑
    reply = f"数据不足：{domain_label}{when_s}，无法提供 {target} 的查询结果。"
    return {
        "reply": reply, "clean_reply": reply, "references": [],
        "action_id": None, "tools_used": [], "tag": "信息",
        "workflow_task": None,
        "provider": _prov.get_provider(), "confidence": 1.0,
        "judge_method": "freshness_gate", "freshness_gate": freshness,
        "hallucination_warnings": None,
    }


def _execute_workflow_route(
    direct_workflow: Dict[str, str],
    question: str,
    scope: Dict,
    judge_method: str = "deterministic_workflow_router",
) -> Dict:
    """真实工作流执行路由（确定性）：调 run_workflow、构造 workflow_tasks + 三态受理回执。

    WS-145 肯定执行 + WS-159 库存刷新确认门共用此路由 —— 一处真实触发逻辑，避免确认轮另写
    一份「执行」分支导致接线/回执口径漂移。绝不返回「已触发/已完成」假证据，task_id 来自真实
    tool_result / 既有运行实例。
    """
    tool_args = {"workflow": direct_workflow["workflow"], "followup_prompt": question}
    tool_result = _exec_tool("run_workflow", tool_args, user=scope)
    workflow_tasks = []
    if isinstance(tool_result, dict) and tool_result.get("ok"):
        task_id = tool_result["task_id"]
        workflow_tasks.append({
            "ok": True,
            "task_id": task_id,
            "workflow": tool_result.get("workflow", direct_workflow["workflow"]),
            "label": tool_result.get("label", direct_workflow.get("label", direct_workflow["workflow"])),
            "total_steps": tool_result.get("total_steps", 0),
            "affected_modules": tool_result.get("affected_modules", []),
            "followup_prompt": tool_result.get("followup_prompt"),
        })
        # T21-SUB-2: 三态受理回执（已排队/已开始/已完成·失败），
        # 直接回答「是否已创建」并附 task_id/workflow/状态，禁止只说「已触发」。
        reply = _workflow_receipt_reply(
            task_id, tool_result["workflow"], direct_workflow["label"]
        )
        impact = _workflow_business_impact_reply(tool_result["workflow"], question)
        if impact:
            reply = f"{reply}\n\n{impact}"
        # T21-SUB-3 物流入口专项降级：用回读接口验证 durable 任务证据；
        # 任务表报错或事件缺失时降级回复，不返回假成功。
        if direct_workflow.get("workflow") == "wf3_logistics_v2":
            degrade_msg = _logistics_task_evidence_check(task_id)
            if degrade_msg:
                reply = degrade_msg
    elif (
        isinstance(tool_result, dict)
        and tool_result.get("action_type") == "denied"
        and "已有运行中实例" in (tool_result.get("reason") or "")
    ):
        existing = _current_workflow_task(direct_workflow["workflow"])
        if existing:
            workflow_tasks.append({
                "ok": True,
                "task_id": existing["task_id"],
                "workflow": existing["workflow"],
                "label": direct_workflow["label"],
                "total_steps": None,
                "affected_modules": [],
                "followup_prompt": question,
                "state": existing.get("state"),
                "already_running": True,
            })
            reply = (
                f"{direct_workflow['label']}已有运行中的后台任务 "
                f"`{existing['task_id']}`，我不重复触发。"
            )
        else:
            # DB not yet updated; parse ID from denial reason string
            extracted_id = _existing_workflow_task_id(tool_result)
            if extracted_id:
                workflow = direct_workflow["workflow"]
                label, total_steps, affected = _workflow_registry_summary(
                    workflow, direct_workflow["label"]
                )
                workflow_tasks.append({
                    "ok": True,
                    "task_id": extracted_id,
                    "workflow": workflow,
                    "label": label,
                    "total_steps": total_steps,
                    "affected_modules": affected,
                    "followup_prompt": question,
                })
                reply = (
                    f"{direct_workflow['label']}已有同类后台任务在运行，未新建重复任务。\n"
                    f"任务 ID：{extracted_id}｜workflow：{workflow}｜当前状态：运行中或排队。\n"
                    "请在工作台任务面板查看进度；任务结束后如仍需刷新，可以再重试。"
                )
            else:
                reply = tool_result.get("reason") or "工作流触发失败。"
    else:
        existing_task_id = _existing_workflow_task_id(tool_result or {})
        if existing_task_id:
            workflow = direct_workflow["workflow"]
            label, total_steps, affected = _workflow_registry_summary(
                workflow, direct_workflow["label"]
            )
            workflow_tasks.append({
                "ok": True,
                "task_id": existing_task_id,
                "workflow": workflow,
                "label": label,
                "total_steps": total_steps,
                "affected_modules": affected,
                "followup_prompt": question,
            })
            reply = (
                f"{direct_workflow['label']}已有同类后台任务在运行，未新建重复任务。\n"
                f"任务 ID：{existing_task_id}｜workflow：{workflow}｜当前状态：运行中或排队。\n"
                "请在工作台任务面板查看进度；任务结束后如仍需刷新，可以再重试。"
            )
        else:
            workflow_tasks.append({
                "ok": False,
                "workflow": direct_workflow["workflow"],
                "label": direct_workflow.get("label", direct_workflow["workflow"]),
                "error": (tool_result or {}).get("error") or "触发失败",
                "task_id": None,
            })
            reason = (
                (tool_result or {}).get("message")
                or (tool_result or {}).get("error")
                or (tool_result or {}).get("reason")
            )
            if reason:
                reply = reason
            elif direct_workflow.get("workflow") == "wf3_logistics_v2":
                reply = ("物流后台任务**未确认创建成功**（工作流触发失败）。"
                         "请稍后在工作台任务面板确认任务状态，或重试。")
            else:
                reply = "本轮没有创建后台任务：工作流触发失败，请稍后重试。"
            # WS-145 自动补调策略:确定性路由这一次触发即「自动补调一次」。
            # 失败后 policy 判定转 plan→confirm（不无限重试），追加下一步 + 需确认，
            # 绝不返回「已触发/已完成」假证据。
            from . import _execution_intent_gate as _intent_gate
            if (
                _intent_gate.decide_recovery(_intent_gate.RiskTier.LOW_AUTO, 1)
                == _intent_gate.RecoveryAction.PLAN_CONFIRM
            ):
                reply = reply.rstrip() + (
                    "\n\n下一步:这步自动补调一次仍未成功，我不再自动重复触发——"
                    "回「确认」我再试一次，或回「取消」改用上传/手动核对。"
                )
    from . import _provider
    return {
        "reply": reply,
        "clean_reply": reply,
        "references": _dedup_refs((tool_result or {}).get("references", [])),
        "action_id": None,
        "tools_used": ["run_workflow"],
        "tag": "执行",
        "workflow_tasks": workflow_tasks,
        "provider": _provider.get_provider(),
        "confidence": 1.0,
        "judge_method": judge_method,
        "hallucination_warnings": None,
    }


def _msg_text(m) -> str:
    """从一条 message 取纯文本（content 可能是 blocks 列表）。"""
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return c or ""


def _inventory_refresh_feasibility(scope: Dict):
    """库存刷新「可执行性」稳口径判断（WS-159）。

    不仅判低风险，还要能锁定范围 + 无明显执行前阻断：
      - 缺店铺范围（不知道刷哪个店铺）→ 不可执行；
      - 已有正在运行的库存刷新任务（冲突）→ 不可执行（不重复触发）。
    其余视为可执行。返回 (ok: bool, reason: str, next_step: str)。
    """
    store = (scope or {}).get("store")
    if not store:
        return (
            False,
            "缺少店铺范围（不知道要刷哪个店铺的库存）",
            "请先在工作台选好店铺（如 KSA），再让我刷新",
        )
    existing = _current_workflow_task("wf1_stock_v2")
    if existing:
        return (
            False,
            f"{store} 已有一个正在运行的库存刷新任务（task `{existing['task_id']}`）",
            "等当前任务跑完再刷，或在任务面板查看进度",
        )
    return (True, "", "")


def _pending_inventory_refresh_inquiry(messages: List[Dict]) -> Optional[str]:
    """从消息历史结构性推出「上一轮存在可执行库存刷新提议」(pending)。

    判据（只看紧接上一轮，pending 只对下一轮有效）：
      messages[-3] 是 user 且为询问式库存刷新请求，
      messages[-2] 是 assistant 且含本门的 PROPOSAL_MARKER（=确实提过可执行刷新）。
    满足则返回那条询问文本（用作 followup_prompt）；否则 None。
    换题后 messages[-3] 不再是询问句 → 自然返回 None（pending 失效）。
    """
    from . import _inventory_refresh_gate as _inv_gate
    if not messages or len(messages) < 3:
        return None
    prev_assistant = messages[-2]
    prev_user = messages[-3]
    if not isinstance(prev_assistant, dict) or prev_assistant.get("role") != "assistant":
        return None
    if not isinstance(prev_user, dict) or prev_user.get("role") != "user":
        return None
    inquiry = _msg_text(prev_user)
    if not _inv_gate.is_inventory_refresh_inquiry(inquiry):
        return None
    if _inv_gate.PROPOSAL_MARKER not in _msg_text(prev_assistant):
        return None
    return inquiry


def _inventory_refresh_no_task_result(reply: str, judge_method: str) -> Dict:
    from . import _provider
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
        "judge_method": judge_method,
        "hallucination_warnings": None,
    }


def _inventory_refresh_confirm_gate(
    messages: List[Dict], question: str, scope: Dict
) -> Optional[Dict]:
    """WS-159 库存刷新询问式确认门。返回 chat 结果 dict 即短路；返回 None 表示不介入。

    三类轮次：
      1) 裸确认轮：有 pending → 执行一次真实 wf1_stock_v2；无 pending → 要求说明要执行什么。
      2) 取消轮：有 pending → 作废、不执行；无 pending → 交既有流程（普通否定句）。
      3) 询问轮：可执行 → 提议 + 反问确认（挂 pending）；不可/缺信息 → 说明缺口（不挂 pending）。
    """
    from . import _inventory_refresh_gate as _inv_gate
    q = question or ""

    # 1) 裸确认轮
    if _inv_gate.is_confirmation(q):
        inquiry = _pending_inventory_refresh_inquiry(messages)
        if inquiry is not None:
            ok, reason, next_step = _inventory_refresh_feasibility(scope)
            if not ok:
                return _inventory_refresh_no_task_result(
                    _inv_gate.pending_now_infeasible_reply(reason, next_step),
                    "inventory_refresh_confirm_now_infeasible",
                )
            # 消费 pending：执行一次真实库存刷新（复用统一执行路由）。
            return _execute_workflow_route(
                {"workflow": "wf1_stock_v2", "label": "库存刷新"},
                inquiry,
                scope,
                judge_method="inventory_refresh_confirm_consumed",
            )
        return _inventory_refresh_no_task_result(
            _inv_gate.bare_confirm_no_pending_reply(),
            "inventory_refresh_no_pending",
        )

    # 2) 取消轮
    if _inv_gate.is_cancellation(q):
        if _pending_inventory_refresh_inquiry(messages) is not None:
            return _inventory_refresh_no_task_result(
                _inv_gate.cancelled_reply(), "inventory_refresh_cancelled"
            )
        return None  # 无 pending 的否定句 → 交既有 WS-145 流程

    # 3) 询问轮（turn 1）
    if _inv_gate.is_inventory_refresh_inquiry(q):
        ok, reason, next_step = _inventory_refresh_feasibility(scope)
        if ok:
            store = (scope or {}).get("store")
            return _inventory_refresh_no_task_result(
                _inv_gate.proposal_reply(store), "inventory_refresh_proposed"
            )
        return _inventory_refresh_no_task_result(
            _inv_gate.infeasible_reply(reason, next_step),
            "inventory_refresh_infeasible",
        )

    return None

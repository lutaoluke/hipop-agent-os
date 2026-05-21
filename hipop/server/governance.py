"""半 MSCL 治理 pipeline — Phase 0.2（2026-05-21）

拦截 destructive tool 调用，走完整的:
  ActionProposal → Decision (Claude Haiku) → ExecToken → Execute → ExecutionRecord

跟 Anthropic 范式对应：
  Writing Tools for Agents              — actionable error / schema 强约束
  Agentic Misalignment Research         — "do not X" prompt 无效 → 结构性约束
  Claude Code Sandboxing                — execute boundary enforcement
  叶小钗 Harness + Palantir AIP / MSCL  — Action Governance 行动语义化

核心 API（agent.py 调）：
  propose_and_execute(tool_name, args, user, scope) -> dict
    - read tool → 直接 fn(**args)（跳过 governance）
    - destructive → 走完整 pipeline

风险分级：
  read     直调，不入 pipeline
  medium   Decision Agent 单独 OK → auto execute
  high     Decision Agent OK + 给用户 Plan 等 OK
  critical OOB approval（推飞书 manager 审批，挂起 task）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None


# ── Action Registry ──────────────────────────────────────────

_ACTIONS_YAML = Path(__file__).parent / "governance_actions.yaml"
_REGISTRY: dict = {}


def _load_registry() -> dict:
    global _REGISTRY
    if _REGISTRY:
        return _REGISTRY
    if not yaml:
        # 极简 fallback：硬编码 2 个
        _REGISTRY = {
            "update_alert_status": {"risk_level": "high"},
            "run_workflow": {"risk_level": "medium"},
        }
        return _REGISTRY
    with open(_ACTIONS_YAML) as f:
        _REGISTRY = yaml.safe_load(f) or {}
    return _REGISTRY


def is_destructive(tool_name: str) -> bool:
    """tool 是否走 governance pipeline。"""
    reg = _load_registry()
    spec = reg.get(tool_name)
    if not spec:
        return False
    return spec.get("risk_level") in ("medium", "high", "critical")


def get_action_spec(tool_name: str) -> Optional[dict]:
    return _load_registry().get(tool_name)


# ── Data classes ─────────────────────────────────────────────

@dataclass
class ActionProposal:
    """模型提出的行动意图（normalized + bound + state-validated）。"""
    proposal_id: str
    tool_name: str
    risk_level: str
    raw_args: dict
    bound_args: dict
    target_object: dict          # {table, identifier, current_state}
    actor: dict                  # {user_id, email, role, tenant_id, source}
    expected_effects: list
    decision_context: dict       # 给 Decision Agent 用
    spec: dict                   # registry 里的 spec
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """裁决结果。"""
    kind: str                    # Allow / AllowWithConstraints / AskUser / AskOOB / Deny
    reason: str
    constraints: dict = field(default_factory=dict)
    plan_text: Optional[str] = None       # AskUser 时给用户看的 plan
    approval_target: Optional[str] = None  # AskOOB 时谁审批


@dataclass
class ExecToken:
    """一次性受限授权。"""
    token_id: str
    proposal_id: str
    tool_name: str
    boundary: dict               # 允许的参数白名单
    expires_at: float            # +30s
    issued_at: float
    issued_by: str = "decision_agent"

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def verify_call(self, tool_name: str, args: dict) -> Optional[str]:
        """返回错误信息或 None（通过）。"""
        if self.is_expired():
            return f"token expired ({int(time.time()-self.expires_at)}s ago)"
        if tool_name != self.tool_name:
            return f"token for {self.tool_name}, not {tool_name}"
        # boundary 检查：每个 boundary key 都必须跟 args 匹配
        for k, v in self.boundary.items():
            if k in args and args[k] != v:
                return f"arg {k!r} violates token boundary: expected {v!r}, got {args[k]!r}"
        return None


# ── Build ActionProposal ─────────────────────────────────────

def make_proposal(tool_name: str, args: dict, actor: dict, scope: dict) -> ActionProposal:
    """从 LLM 输出 + tool args 构造 ActionProposal。
    包含 normalize、object binding、state validate、effect annotation。
    """
    spec = get_action_spec(tool_name) or {}
    risk_level = spec.get("risk_level", "medium")

    # Normalize args（去 None、strip 字符串等）
    bound = {k: (v.strip() if isinstance(v, str) else v) for k, v in args.items() if v is not None}

    # Bind target_object（per-tool 逻辑）
    target = _bind_target_object(tool_name, bound, actor)

    # Decision context — 给 Decision Agent 看的事实
    ctx = {
        "actor": {
            "user_id": actor.get("user_id"),
            "email": actor.get("email"),
            "role": actor.get("role"),
            "tenant_id": actor.get("tenant_id"),
            "source": actor.get("source") or scope.get("source") or "chat",
        },
        "target": target,
        "tool_name": tool_name,
        "risk_level": risk_level,
        "scope_store": scope.get("store"),
        "scope_module": scope.get("module"),
        "irreversible": spec.get("irreversible", False),
    }

    return ActionProposal(
        proposal_id=uuid.uuid4().hex[:12],
        tool_name=tool_name,
        risk_level=risk_level,
        raw_args=dict(args),
        bound_args=bound,
        target_object=target,
        actor=ctx["actor"],
        expected_effects=spec.get("expected_effects", []),
        decision_context=ctx,
        spec=spec,
        created_at=time.time(),
    )


def _bind_target_object(tool_name: str, args: dict, actor: dict) -> dict:
    """per-tool 把 args 绑定到具体 business object + current state。"""
    from . import data as _data
    tid = actor.get("tenant_id")
    if tid:
        _data.set_current_tenant(tid)

    if tool_name == "update_alert_status":
        order_no = args.get("order_no")
        if not order_no:
            return {"table": "wf6_logistics_alerts_v2", "error": "missing order_no"}
        rows = _data._fetch(
            "SELECT alert_id, alert_level, alert_reason, ops_status, resolved_at "
            "FROM wf6_logistics_alerts_v2 "
            "WHERE tenant_id=? AND order_no=? AND resolved_at IS NULL LIMIT 1",
            (tid, order_no),
        )
        if not rows:
            return {"table": "wf6_logistics_alerts_v2", "order_no": order_no,
                    "error": f"no active alert for {order_no}"}
        r = rows[0]
        return {
            "table": "wf6_logistics_alerts_v2",
            "identifier": {"alert_id": r["alert_id"], "order_no": order_no},
            "current_state": {
                "ops_status": r["ops_status"],
                "alert_level": r["alert_level"],
                "resolved_at": r.get("resolved_at"),
                "alert_reason": r.get("alert_reason"),
            },
        }

    if tool_name == "run_workflow":
        wf = args.get("workflow")
        # 看是否有同 workflow 在跑（防并发）
        running = _data._fetch(
            "SELECT task_id FROM tasks WHERE tenant_id=? AND workflow=? "
            "AND state IN ('running', 'queued') LIMIT 1",
            (tid, wf),
        )
        return {
            "table": "tasks",
            "identifier": {"workflow": wf, "tenant_id": tid},
            "current_state": {
                "concurrent_running": [r["task_id"] for r in running],
            },
        }

    return {"table": "unknown", "identifier": args}


# ── Decision Agent（Claude Haiku 4.5） ──────────────────────

_DECISION_SYSTEM_PROMPT = """你是企业 Agent 行动治理裁决官。你的唯一任务是基于 ActionProposal + DecisionContext 决定：是否允许这个行动发生。

输出严格 JSON：
{
  "kind": "Allow" | "AskUser" | "AskOOB" | "Deny",
  "reason": "一句话裁决理由",
  "plan_text": "(仅当 AskUser) 给用户看的执行计划，包含预期影响 + 需要用户确认什么"
}

裁决规则（按 risk_level）：

**medium（可逆，触发后台 workflow 类）**：
- 默认 Allow（actor 已通过 RBAC）
- 但 concurrent_running 已存在同 workflow → Deny（"已有相同 workflow 正在跑"）
- target.error 存在 → Deny（业务对象绑定失败）

**high（可逆但影响业务链，如改 alert 状态）**：
- 默认 AskUser，写 plan_text 说明：
  - 当前 target 对象状态（current_state）
  - 这次会改成什么
  - 预期影响（expected_effects）
  - 让用户回复 OK / 改
- 但若 target.error → Deny

**critical（不可逆 / 跨 tenant / 大额）**：
- 永远 AskOOB（推飞书 manager 审批）— 不让 LLM 自己批

其他 Deny 情形：
- actor.role 不在 preconditions 允许范围 → Deny
- 业务对象不存在 / 已结案 → Deny
- 时间戳精度禁忌 / 模型已知 hallucinate 模式 → Deny

严禁在 reason 里编不存在的字段名 / 用户 id / 时间戳。事实只能来自 DecisionContext。"""


def _decide_with_llm(proposal: ActionProposal) -> Decision:
    """Claude Haiku 4.5 跑一个独立 prompt 做裁决。"""
    from . import _provider

    user_msg = (
        f"Tool: {proposal.tool_name}\n"
        f"Risk: {proposal.risk_level}\n"
        f"Args: {json.dumps(proposal.bound_args, ensure_ascii=False)}\n"
        f"Target object: {json.dumps(proposal.target_object, ensure_ascii=False, default=str)}\n"
        f"Actor: {json.dumps(proposal.actor, ensure_ascii=False)}\n"
        f"Expected effects: {proposal.expected_effects}\n"
        f"Spec preconditions: {proposal.spec.get('preconditions', [])}\n\n"
        f"裁决（严格 JSON）："
    )

    try:
        # 尽量用便宜的 Haiku；fallback 到当前 provider
        model_override = os.environ.get("DECISION_MODEL", "claude-haiku-4-5")
        result = _provider.chat_with_tools(
            messages=[{"role": "user", "content": user_msg}],
            system=_DECISION_SYSTEM_PROMPT,
            tools=[],
            tool_funcs={},
            scope={"_decision_only": True, "model_override": model_override},
        )
        reply = (result or {}).get("reply") or ""
        decision_json = _extract_json(reply)
        return Decision(
            kind=decision_json.get("kind", "Deny"),
            reason=decision_json.get("reason") or "(no reason)",
            plan_text=decision_json.get("plan_text"),
        )
    except Exception as e:
        # 模型挂了 → 保守 Deny（don't ship action when system is broken）
        return Decision(
            kind="Deny",
            reason=f"decision agent error: {type(e).__name__}: {str(e)[:120]}",
        )


def _extract_json(text: str) -> dict:
    """从 LLM 输出中抽 JSON（可能包 ```json 围栏或前缀文字）。"""
    import re
    # 找第一个 { 到最后一个 }
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def decide(proposal: ActionProposal) -> Decision:
    """根据 risk_level 路由：deterministic 预校验 → critical 直接 OOB / 其他走 Haiku。

    设计原则（Anthropic Agentic Misalignment 教训）：
    LLM 决策不可信。必须先用 deterministic 规则把绝对 deny 的情形挡掉，
    LLM 只是次级校验 + 用户友好的 plan_text 生成器。
    """
    # 1. 业务对象错误优先 Deny（不调 LLM）
    if proposal.target_object.get("error"):
        return Decision(kind="Deny", reason=proposal.target_object["error"])

    # 2. Deterministic precondition 校验（这层不靠 LLM，最稳）
    detached = _check_deterministic_preconditions(proposal)
    if detached:
        return Decision(kind="Deny", reason=detached)

    # 3. critical 强制 OOB（按 Agentic Misalignment 研究：irreversible 不靠 LLM）
    if proposal.risk_level == "critical":
        return Decision(
            kind="AskOOB",
            reason="critical action 必须人工 OOB approval",
            approval_target="manager_via_feishu",
        )

    # 4. medium/high 跑 Haiku（生成 plan_text 给用户 / 做次级校验）
    return _decide_with_llm(proposal)


def _check_deterministic_preconditions(proposal: ActionProposal) -> Optional[str]:
    """Deterministic 校验。返回 deny 原因或 None（通过）。
    跟 LLM 平行的一层结构性约束。"""
    spec = proposal.spec
    args = proposal.bound_args
    tool = proposal.tool_name

    # 1. actor role precondition
    actor_role = (proposal.actor.get("role") or "").lower()
    # 简单解析"actor.role in [..]"模式（spec preconditions 字符串）
    for cond in spec.get("preconditions", []):
        if "actor.role in" in cond:
            import re
            m = re.search(r"\[([^\]]+)\]", cond)
            if m:
                allowed = [x.strip() for x in m.group(1).split(",")]
                if actor_role not in allowed:
                    return f"actor role={actor_role} 不在 {allowed} 内"

    # 2. allowed_workflows / allowed_statuses 白名单校验
    if "allowed_workflows" in spec and "workflow" in args:
        if args["workflow"] not in spec["allowed_workflows"]:
            return (f"workflow={args['workflow']!r} 不在白名单。"
                    f"可选: {spec['allowed_workflows']}")

    if "allowed_statuses" in spec and "status" in args:
        if args["status"] not in spec["allowed_statuses"]:
            return (f"status={args['status']!r} 不在白名单。"
                    f"可选: {spec['allowed_statuses']}")

    # 3. concurrent_running 并发防护（run_workflow 专用）
    if tool == "run_workflow":
        running = proposal.target_object.get("current_state", {}).get("concurrent_running", [])
        if running:
            return f"该 workflow 已有运行中实例: {running[:3]}（防并发抢资源）"

    # 4. required_fields 完整性
    for f in spec.get("required_fields", []):
        if not args.get(f):
            return f"required field {f!r} 缺失"

    return None


# ── ExecToken ────────────────────────────────────────────────

_TOKEN_TTL_SEC = int(os.environ.get("EXEC_TOKEN_TTL_SEC", "30"))

_PROPOSALS_DIR = Path(os.environ.get(
    "HIPOP_PROPOSALS_DIR", os.path.expanduser("~/hipop/proposals")
))
_PROPOSAL_TTL_SEC = int(os.environ.get("PROPOSAL_TTL_SEC", "300"))  # 5 min


def _save_pending_proposal(proposal: ActionProposal, decision: Decision) -> None:
    """AskUser 时持久化 proposal，等用户回 OK 时读出来 execute。"""
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    p = _PROPOSALS_DIR / f"{proposal.proposal_id}.json"
    with open(p, "w") as f:
        json.dump({
            "proposal": proposal.to_dict(),
            "decision": asdict(decision),
            "expires_at": time.time() + _PROPOSAL_TTL_SEC,
        }, f, ensure_ascii=False, default=str, indent=2)


def _load_pending_proposal(proposal_id: str) -> Optional[dict]:
    """读暂存的 proposal。返回 None 如果不存在 / 过期。"""
    p = _PROPOSALS_DIR / f"{proposal_id}.json"
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
    except Exception:
        return None
    if data.get("expires_at", 0) < time.time():
        try: p.unlink()
        except Exception: pass
        return None
    return data


def confirm_proposal(proposal_id: str, user_decision: str, actor: dict, tool_funcs: dict) -> dict:
    """用户在 chat 回 "OK" / "cancel" 后，由 chat tool 调本函数推进。

    - user_decision='ok'     → issue_token + execute_with_token
    - user_decision='cancel' → 写 denied record，删暂存
    """
    data = _load_pending_proposal(proposal_id)
    if not data:
        return {"error": "proposal_not_found_or_expired",
                "proposal_id": proposal_id,
                "hint": "proposal 5 分钟内有效；超时请用原 query 重新发起"}

    proposal_data = data["proposal"]
    # 验 actor 是同一个用户（防越权 confirm 别人的 proposal）
    if proposal_data["actor"].get("user_id") != actor.get("user_id"):
        return {"error": "actor_mismatch",
                "reason": f"only original proposer can confirm; "
                          f"proposal user_id={proposal_data['actor'].get('user_id')}, "
                          f"current user_id={actor.get('user_id')}"}

    proposal = ActionProposal(**{
        k: v for k, v in proposal_data.items()
        if k in ActionProposal.__dataclass_fields__
    })

    p = _PROPOSALS_DIR / f"{proposal_id}.json"
    try: p.unlink()
    except Exception: pass

    if user_decision.lower() in ("cancel", "no", "不要", "取消"):
        write_execution_record(
            proposal,
            token=ExecToken(token_id="-", proposal_id=proposal_id,
                             tool_name=proposal.tool_name, boundary={}, expires_at=0, issued_at=0),
            result=None, status="cancelled_by_user",
            error="用户取消",
        )
        return {"action_type": "cancelled", "proposal_id": proposal_id,
                "message": "已取消"}

    # OK → issue token + execute
    fake_decision = Decision(kind="Allow", reason="user_confirmed")
    token = issue_token(proposal, fake_decision)
    return execute_with_token(proposal, token, tool_funcs)


def issue_token(proposal: ActionProposal, decision: Decision) -> ExecToken:
    """Allow 后立刻 issue ExecToken。boundary = bound_args 全锁定。"""
    return ExecToken(
        token_id=uuid.uuid4().hex[:16],
        proposal_id=proposal.proposal_id,
        tool_name=proposal.tool_name,
        boundary=dict(proposal.bound_args),
        expires_at=time.time() + _TOKEN_TTL_SEC,
        issued_at=time.time(),
    )


# ── Execution Adapter ───────────────────────────────────────

def execute_with_token(proposal: ActionProposal, token: ExecToken, tool_funcs: dict) -> dict:
    """验 token + 执行真 tool function + 写 ExecutionRecord."""
    # 1. 验 token
    err = token.verify_call(proposal.tool_name, proposal.bound_args)
    if err:
        write_execution_record(proposal, token, None, status="token_invalid", error=err)
        return {"error": "exec_token_invalid", "reason": err}

    # 2. 执行真 tool
    fn = tool_funcs.get(proposal.tool_name)
    if not fn:
        return {"error": "unknown_tool", "tool": proposal.tool_name}
    try:
        result = fn(**proposal.bound_args)
    except Exception as e:
        write_execution_record(proposal, token, None, status="exec_error", error=str(e)[:300])
        return {"error": f"{type(e).__name__}: {e}"}

    # 3. 写 ExecutionRecord (审计)
    write_execution_record(proposal, token, result, status="done")
    return result


def write_execution_record(
    proposal: ActionProposal,
    token: ExecToken,
    result: Any,
    status: str,
    error: Optional[str] = None,
) -> None:
    """audit log — 写 agent_events 表 step 0=proposal / step 1=execute。"""
    from . import data as _data
    tid = proposal.actor.get("tenant_id")
    if tid:
        _data.set_current_tenant(tid)

    actor_for_event = {
        "user_id": proposal.actor.get("user_id"),
        "email": proposal.actor.get("email"),
        "role": proposal.actor.get("role"),
        "source": proposal.actor.get("source") or "chat",
    }
    task_id = f"action_{proposal.proposal_id}"
    _data.write_event(
        task_id, 0, f"propose:{proposal.tool_name}", "done",
        json.dumps({
            "proposal": proposal.to_dict(),
            "decision": "Allow",   # 走到这就是 Allow
            "token_id": token.token_id,
        }, ensure_ascii=False, default=str)[:8000],
        actor=actor_for_event,
    )
    _data.write_event(
        task_id, 1, f"execute:{proposal.tool_name}", status,
        json.dumps({
            "result_preview": str(result)[:500] if result else None,
            "error": error,
        }, ensure_ascii=False, default=str),
        actor=actor_for_event,
    )


# ── Public 主入口 ────────────────────────────────────────────

def propose_and_execute(
    tool_name: str, args: dict, actor: dict, scope: dict, tool_funcs: dict,
) -> dict:
    """destructive tool 唯一调用入口。
    - 非 destructive → 直接 fn(**args)
    - destructive → Proposal → Decision → Token → Execute → Record
    """
    if not is_destructive(tool_name):
        fn = tool_funcs.get(tool_name)
        return fn(**args) if fn else {"error": "unknown_tool"}

    # build proposal
    proposal = make_proposal(tool_name, args, actor, scope)

    # decide
    decision = decide(proposal)

    # 路由
    if decision.kind == "Allow":
        token = issue_token(proposal, decision)
        return execute_with_token(proposal, token, tool_funcs)

    if decision.kind == "AskUser":
        # 持久化 proposal 等用户回 OK（5 min TTL）
        _save_pending_proposal(proposal, decision)
        return {
            "action_type": "plan",
            "needs_user_confirmation": True,
            "tool": tool_name,
            "args": proposal.bound_args,
            "plan_text": decision.plan_text or _default_plan_text(proposal),
            "expected_effects": proposal.expected_effects,
            "target_object": proposal.target_object,
            "proposal_id": proposal.proposal_id,
            "reason": decision.reason,
            "hint": "用户回复确认（OK / 改 / 不要）后，请用 confirm_proposal(proposal_id=..., user_decision='ok|cancel') 推进。",
        }

    if decision.kind == "AskOOB":
        # 推飞书 + 暂存（先 stub，飞书集成在 Phase 1 真接）
        _stage_oob_approval(proposal, decision)
        return {
            "action_type": "oob_approval_pending",
            "tool": tool_name,
            "args": proposal.bound_args,
            "proposal_id": proposal.proposal_id,
            "approval_target": decision.approval_target,
            "reason": decision.reason,
            "message": "此 action 需要管理员审批（已推送飞书 manager）。审批通过后系统自动执行。",
        }

    # Deny
    write_execution_record(
        proposal,
        token=ExecToken(token_id="-", proposal_id=proposal.proposal_id,
                         tool_name=tool_name, boundary={}, expires_at=0, issued_at=0),
        result=None,
        status="denied",
        error=decision.reason,
    )
    return {
        "action_type": "denied",
        "tool": tool_name,
        "reason": decision.reason,
        "proposal_id": proposal.proposal_id,
    }


def _default_plan_text(proposal: ActionProposal) -> str:
    """Decision Agent 没给 plan_text 时的 fallback。"""
    target = proposal.target_object
    return (
        f"我准备执行：{proposal.tool_name}({json.dumps(proposal.bound_args, ensure_ascii=False)})\n"
        f"作用对象：{json.dumps(target, ensure_ascii=False, default=str)}\n"
        f"预期影响：{'; '.join(proposal.expected_effects)}\n"
        f"确认请回复 OK，不要请回复"
    )


def _stage_oob_approval(proposal: ActionProposal, decision: Decision) -> None:
    """暂存 OOB approval — 写 agent_events status='pending_oob'。
    Phase 1 接飞书时这里 + 真推消息。"""
    from . import data as _data
    tid = proposal.actor.get("tenant_id")
    if tid:
        _data.set_current_tenant(tid)
    actor_event = {
        "user_id": proposal.actor.get("user_id"),
        "email": proposal.actor.get("email"),
        "role": proposal.actor.get("role"),
        "source": proposal.actor.get("source") or "chat",
    }
    task_id = f"action_{proposal.proposal_id}"
    _data.write_event(
        task_id, 0, f"propose:{proposal.tool_name}", "pending_oob",
        json.dumps({
            "proposal": proposal.to_dict(),
            "decision_reason": decision.reason,
            "approval_target": decision.approval_target,
        }, ensure_ascii=False, default=str)[:8000],
        actor=actor_event,
    )

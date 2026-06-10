"""Smoke test: governance pipeline 必须真生效。

历史教训（2026-05-21 → 5/26）：
  早期版本拆 _provider 抽象层时，把 agent.py 的 _exec_tool 复制到两个 provider 文件
  只做 RBAC。后来在 agent.py 加 governance 集成时，provider 副本没同步更新，
  结果 agent._exec_tool 沦为死代码，所有 destructive tool 裸跑。

  本 smoke test 保证：
  1) destructive tool 走 governance pipeline（返 plan 等 confirm，不直接执行）
  2) provider 文件无 _exec_tool 副本（防再被复制粘贴绕过）

跑法：
  python3 -m pytest tests/smoke_governance.py -v
  或 make test-governance
"""
import inspect
import os
import sys

# 让 import 能找到 hipop 包
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("DB_URL", "postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
os.environ.setdefault("JWT_SECRET", "hipop_alpha_stable_secret_keep_this")


def test_provider_files_have_no_local_exec_tool():
    """Invariant：provider 不能自己实现 _exec_tool，必须委托给 agent._exec_tool。"""
    from hipop.server import _provider_anthropic, _provider_openai
    for mod in (_provider_anthropic, _provider_openai):
        src = inspect.getsource(mod)
        assert "def _exec_tool" not in src, (
            f"{mod.__name__} 不应自己定义 _exec_tool —— 必须 "
            f"`from . import agent; agent._exec_tool(...)`. "
            f"详见 agent._exec_tool 的 INVARIANT docstring。"
        )


def test_agent_exec_tool_has_governance_dispatch():
    """agent._exec_tool 必须包含 governance.is_destructive + propose_and_execute 逻辑。"""
    from hipop.server import agent
    src = inspect.getsource(agent._exec_tool)
    assert "is_destructive" in src, "agent._exec_tool 缺 governance.is_destructive 调用"
    assert "propose_and_execute" in src, "agent._exec_tool 缺 governance.propose_and_execute 调用"


def test_governance_registry_has_destructive_tools():
    """governance_actions.yaml 至少注册 update_alert_status (high) + run_workflow (medium)"""
    from hipop.server import governance
    assert governance.is_destructive("update_alert_status"), \
        "update_alert_status 应在 governance_actions.yaml 标 high"
    assert governance.is_destructive("run_workflow"), \
        "run_workflow 应在 governance_actions.yaml 标 medium"
    assert not governance.is_destructive("query_sku"), \
        "query_sku 是 read-only，不应进 governance"


def test_tools_registry_manifest_is_single_source_of_truth():
    """WS-162: tool schema/access/risk/role/scope/smoke must live in tools_registry.yaml."""
    from hipop.runtime.verifiers import verify_tools_registry_manifest_contract

    result = verify_tools_registry_manifest_contract()
    assert result["ok"], result


def test_medium_risk_decide_allows_without_api_key():
    """medium-risk decide() 必须在无 LLM API key 的环境下返回 Allow。

    FAIL（修前）：decide() 调 _decide_with_llm → DEEPSEEK_API_KEY 缺失 → RuntimeError
                 → Decision(kind="Deny") → run_workflow 被静默拦截，UI 显示「工作流触发失败。」
    PASS（修后）：medium-risk 走确定性路径，直接 Allow，不调 LLM。

    根因背景：governance 规格明确"medium 默认 Allow"，concurrent_running / 目标错误
    已在 deterministic 前置层处理，LLM 裁决对 medium 是冗余的 + 引入环境依赖。
    """
    import dataclasses
    from hipop.server import governance
    from hipop.server.governance import ActionProposal, Decision

    import time
    proposal = ActionProposal(
        proposal_id="smoke_medium_decide_test",
        tool_name="run_workflow",
        risk_level="medium",
        raw_args={"workflow": "wf3_logistics_v2"},
        bound_args={"workflow": "wf3_logistics_v2"},
        target_object={
            "table": "tasks",
            "identifier": {"workflow": "wf3_logistics_v2", "tenant_id": 1},
            "current_state": {"concurrent_running": []},
        },
        actor={"role": "ops", "tenant_id": 1, "source": "smoke_test"},
        expected_effects=["triggers background task"],
        decision_context={},
        spec={"risk_level": "medium"},
        created_at=time.time(),
    )

    saved = {}
    for k in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "DECISION_MODEL"):
        saved[k] = os.environ.pop(k, None)
    try:
        result = governance.decide(proposal)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    assert result.kind == "Allow", (
        f"medium-risk decide() 在无 API key 时应返回 Allow，实际: {result.kind} — {result.reason}\n"
        f"(FAIL 期望: Deny；PASS 期望: Allow)"
    )


def test_destructive_tool_goes_through_governance_pipeline():
    """端到端：高风险 tool 必须经 governance pipeline，不能裸跑。

    pipeline 任一阶段（Decision/Allow/Deny/AskUser/AskOOB/Execute/Audit）触发即算通过。
    Decision Agent 可能 Allow、Deny 或 AskUser；都必须留下 governance 印记。
    """
    from hipop.server import agent, data as _data
    _data.set_current_tenant(1)
    result = agent._exec_tool(
        "update_alert_status",
        {"order_no": "TEST_FAKE_ORDER_DO_NOT_EXECUTE", "status": "已确认丢货"},
        user={"role": "owner", "tenant_id": 1, "id": 1,
              "email": "smoke@test.local"},
    )
    # governance 印记任一即可：action_type / proposal_id / token_id / "decision"
    governance_signals = (
        result.get("action_type") in ("plan", "denied", "deny", "oob_approval_pending"),
        "proposal_id" in result,
        "token_id" in result,
        # 失败时 error string 也能证明 pipeline 跑过（如 Decision Allow → exec 失败）
        "decision" in str(result).lower(),
        "proposal" in str(result).lower(),
    )
    assert any(governance_signals), (
        f"high-risk tool 没走 governance！结果无任何 governance signal: {result}"
    )


if __name__ == "__main__":
    # 允许直接 python3 tests/smoke_governance.py 跑
    import traceback
    tests = [
        test_provider_files_have_no_local_exec_tool,
        test_agent_exec_tool_has_governance_dispatch,
        test_governance_registry_has_destructive_tools,
        test_tools_registry_manifest_is_single_source_of_truth,
        test_medium_risk_decide_allows_without_api_key,
        test_destructive_tool_goes_through_governance_pipeline,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

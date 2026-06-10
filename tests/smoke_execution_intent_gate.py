"""smoke_execution_intent_gate.py — WS-145 fail-then-pass smoke

肯定执行意图门:否定/询问/假设/只问影响面 → 不执行;肯定 → 真实执行路由;
低风险自动补调一次失败 → plan→confirm;高风险 → 先 confirm 不自动补调。

FAIL（修前）:
  - hipop/server/_execution_intent_gate 不存在 → import 失败
  - _deterministic_workflow_request 只挡「不用/不要刷新」字面，挡不住「能不能刷新库存？」
    /「如果刷新库存会影响什么」这类询问/假设句 → 会误进 run_workflow
  - chat() 对高风险「下采购单并提交」无 confirm-first 门
  - _exec_tool 不挡 LLM 在非执行语气下偷偷 run_workflow

PASS（修后）:
  - 上述四类语气在门里被正确分类、不进真实执行路由
  - 「帮我刷库存」肯定句进 wf1_stock_v2 真实路由
  - 高风险 chat() 走 confirm-first，不创建任务
  - 非执行语气下 _exec_tool 拒绝 run_workflow（LLM 不许绕）
  - 自动补调策略 decide_recovery 三态正确
"""

import os
import re
import sys
import traceback
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server import _execution_intent_gate as gate
from hipop.server._execution_intent_gate import IntentMood, RiskTier, RecoveryAction
from hipop.server.agent import _deterministic_workflow_request
from hipop.server import agent as _agent
from hipop.server import _provider as _prov

_SCOPE = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}


# ── 1) 句式语气结构判别 ──────────────────────────────────────────────────────

def test_negation_not_execute():
    """否定句:不要刷新库存 → NEGATED，不进执行。"""
    assert gate.classify_mood("不要刷新库存") == IntentMood.NEGATED
    assert gate.enters_execution("不要刷新库存") is False


def test_interrogative_not_execute():
    """询问句:能不能刷新库存？ → INTERROGATIVE，不进执行。"""
    assert gate.classify_mood("能不能刷新库存？") == IntentMood.INTERROGATIVE
    assert gate.enters_execution("能不能刷新库存？") is False


def test_hypothetical_not_execute():
    """假设句:如果刷新库存会影响什么？ → HYPOTHETICAL，不进执行。"""
    assert gate.classify_mood("如果刷新库存会影响什么？") == IntentMood.HYPOTHETICAL
    assert gate.enters_execution("如果刷新库存会影响什么？") is False


def test_impact_query_not_execute():
    """只问影响面:刷新库存有什么影响 → IMPACT_QUERY，不进执行。"""
    assert gate.classify_mood("刷新库存有什么影响") == IntentMood.IMPACT_QUERY
    assert gate.enters_execution("刷新库存有什么影响") is False


def test_affirmative_executes():
    """肯定句:帮我刷库存，ERP 6 仓 → EXECUTE，进执行。"""
    assert gate.classify_mood("帮我刷库存，ERP 6 仓") == IntentMood.EXECUTE
    assert gate.enters_execution("帮我刷库存，ERP 6 仓") is True


def test_plain_query_is_none():
    """无执行动词:给我看一下销售周期分析 → NONE（门不介入）。"""
    assert gate.classify_mood("给我看一下销售周期分析") == IntentMood.NONE
    assert gate.enters_execution("给我看一下销售周期分析") is False


def test_negation_inside_mixed_message():
    """混合句:不用上传 不用刷新 现在就告诉我哪些要补 → NEGATED（不进执行）。

    回归 chat case 11:这句必须不触发 run_workflow（用户明确说不用刷新）。
    """
    assert gate.classify_mood("不用上传 不用刷新 现在就告诉我哪些要补") == IntentMood.NEGATED
    assert gate.enters_execution("不用上传 不用刷新 现在就告诉我哪些要补") is False


def test_interrogative_with_imperative_not_execute():
    """红队（验门人 14:42 打回点）:询问句里带「帮我/请/能否」仍是询问，不执行。

    修前 bug:classify_mood 先判祈使（帮我/请）为 EXECUTE，再判疑问 —— 所以这些
    「能不能帮我刷新库存？」红队句全被误判执行并路由到 wf1_stock_v2。
    修后:执行动词分句带疑问情态/疑问助词 → INTERROGATIVE，压过祈使。
    """
    for q in (
        "能不能帮我刷新库存？",
        "可以帮我刷新库存吗？",
        "能否帮我刷库存？",
        "帮我刷新库存吗？",
        "可不可以帮我刷新一下库存？",
    ):
        assert gate.classify_mood(q) == IntentMood.INTERROGATIVE, f"{q!r} 应判询问句"
        assert gate.enters_execution(q) is False, f"{q!r} 不应进执行路由"
        assert gate.evaluate(q).blocks_llm_execution is True, f"{q!r} 应拦 LLM 偷跑"


def test_imperative_with_reporting_subclause_still_executes():
    """回归护栏:真命令带汇报性从句（…告诉我是否成功）仍是执行，不被疑问词误伤。

    执行动词分句「帮我刷库存」是祈使命令；「是否」在另一汇报分句，不该把整句拉成询问。
    含末尾问号的变体也必须执行（执行分句是命令，整句问号属汇报语气）。
    """
    assert gate.classify_mood("帮我刷库存，并告诉我是否真的创建了任务") == IntentMood.EXECUTE
    assert gate.enters_execution("帮我刷库存，并告诉我是否真的创建了任务") is True
    assert gate.classify_mood("帮我刷库存，告诉我是否成功？") == IntentMood.EXECUTE
    assert gate.enters_execution("帮我刷库存，告诉我是否成功？") is True


def test_router_blocks_interrogative_with_imperative():
    """路由层:「能不能帮我刷新库存？」不进 wf 路由（修前会因含「帮我刷新」误进）。"""
    for q in ("能不能帮我刷新库存？", "可以帮我刷新库存吗？", "能否帮我刷库存？"):
        assert _deterministic_workflow_request(q) is None, f"{q!r} 不应路由"


# ── 2) 风险分层 + 自动补调策略 ──────────────────────────────────────────────

def test_low_risk_internal_action():
    assert gate.classify_risk("帮我刷库存") == RiskTier.LOW_AUTO


def test_high_risk_external_and_txn():
    for q in ("帮我下采购单并提交", "发飞书通知群", "帮我取消订单", "全店批量覆盖价格"):
        assert gate.classify_risk(q) == RiskTier.HIGH_CONFIRM, f"{q!r} 应判高风险"


def test_external_notify_phrasings_confirm_first():
    """WS-145 红队契约（WS-150 收敛后仍成立）：**通用「人对人」外部通知** ——
    notify_via_feishu schema 明写的运营说法「通知刘鹤 / @同事 / 通知运营」，**无显式
    飞书渠道词**，仍是高风险外部副作用 → confirm-first，飞书拒绝门不得吞它。

    这正是 PR #92 Round-2 被打回的点：飞书不支持检测过宽，把这些通用通知误判成
    unsupported_feishu_notify / needs_confirm_first=False。收敛后它们必须 confirm-first。
    """
    for q in (
        "帮我通知刘鹤这批货到了",
        "@同事看一下这个库存",
        "通知运营一下",
        "推送消息给张三",
    ):
        assert gate.classify_risk(q) == RiskTier.HIGH_CONFIRM, f"{q!r} 外部通知应判高风险"
        d = gate.evaluate(q)
        assert d.has_exec_verb is True, f"{q!r} 应识别为动作（含执行动词）"
        assert d.unsupported_feishu_notify is False, f"{q!r} 无飞书渠道词 → 不归飞书拒绝"
        assert d.needs_confirm_first is True, f"{q!r} 应 confirm-first 不自动执行"
        assert d.enters_execution is False, f"{q!r} 不应直接进执行路由"


def test_explicit_feishu_channel_unsupported():
    """WS-150 收敛：**显式飞书渠道 / 群广播**请求（发飞书 / 发到飞书群 / 推到群里 /
    通知群）→ 工作台只读、不支持主动发 → 确定性拒绝（非通用 confirm-first）。

    本产品对外「群」只有飞书群一条，故群广播归入飞书拒绝。WS-145 安全内核保留：
    has_exec_verb=True、绝不直接进执行路由。
    """
    for q in (
        "帮我发飞书通知大家",
        "把补货建议发到飞书群",
        "把库存情况推到群里",
        "把库存情况推送消息到群里",   # 码长 Round-4 漏判点（群广播自然说法）
        "通知群里这批货到了",
        "同步到群",
    ):
        d = gate.evaluate(q)
        assert d.has_exec_verb is True, f"{q!r} 应识别为动作（含执行动词）"
        assert d.unsupported_feishu_notify is True, f"{q!r} 显式飞书/群广播 → 确定性拒绝"
        assert d.needs_confirm_first is False, f"{q!r} 不走通用 confirm-first（已被确定性拒绝取代）"
        assert d.enters_execution is False, f"{q!r} 不应直接进执行路由"


def test_notify_plus_transaction_still_confirm_first():
    """WS-150 边界护栏：主动飞书通知与真高风险交易/采购/批量同句出现时，
    交易不能被「通知不支持」顺带放过 —— 飞书拒绝让位，交易仍 confirm-first。
    """
    for q in (
        "帮我下采购单并通知刘鹤",
        "下采购单并推到群里",
        "全店批量覆盖价格再发飞书通知大家",
    ):
        d = gate.evaluate(q)
        assert d.risk == RiskTier.HIGH_CONFIRM, f"{q!r} 含交易/批量应判高风险"
        assert d.unsupported_feishu_notify is False, f"{q!r} 夹带交易 → 飞书拒绝让位"
        assert d.needs_confirm_first is True, f"{q!r} 交易仍须 confirm-first，不被通知不支持放过"
        assert d.enters_execution is False, f"{q!r} 高风险不直接执行"


def test_plain_notification_query_not_misfired():
    """护栏:查询型「飞书有没有新通知」不应被误判为外部通知动作而拦 confirm。"""
    assert gate.classify_mood("查一下飞书有没有新通知") == IntentMood.NONE
    assert gate.evaluate("查一下飞书有没有新通知").needs_confirm_first is False


def test_recovery_low_risk_first_attempt_retries_once():
    assert gate.decide_recovery(RiskTier.LOW_AUTO, 0) == RecoveryAction.AUTO_RETRY_ONCE


def test_recovery_low_risk_second_attempt_plan_confirm():
    """低风险自动补调失败 → 不无限补，转 plan→confirm。"""
    assert gate.decide_recovery(RiskTier.LOW_AUTO, 1) == RecoveryAction.PLAN_CONFIRM
    assert gate.decide_recovery(RiskTier.LOW_AUTO, 5) == RecoveryAction.PLAN_CONFIRM


def test_recovery_high_risk_always_confirm_first():
    """高风险任何时候都先 confirm，不自动补调。"""
    assert gate.decide_recovery(RiskTier.HIGH_CONFIRM, 0) == RecoveryAction.CONFIRM_FIRST
    assert gate.decide_recovery(RiskTier.HIGH_CONFIRM, 1) == RecoveryAction.CONFIRM_FIRST


def test_high_risk_affirmative_needs_confirm_first():
    d = gate.evaluate("帮我下采购单并提交")
    assert d.mood == IntentMood.EXECUTE
    assert d.risk == RiskTier.HIGH_CONFIRM
    assert d.needs_confirm_first is True
    assert d.enters_execution is False  # 高风险即使肯定也不直接执行


def test_replies_carry_no_fake_evidence():
    """门的所有回复都不得含「已触发/已启动/已完成」等假证据。"""
    bad = ("已触发", "已启动", "已完成", "已刷新", "accepted")
    texts = [
        gate.explain_reply(IntentMood.NEGATED),
        gate.explain_reply(IntentMood.INTERROGATIVE),
        gate.explain_reply(IntentMood.HYPOTHETICAL),
        gate.confirm_first_reply(),
        gate.recovery_plan_confirm_reply("库存刷新", "wf1 正在运行中"),
    ]
    for t in texts:
        assert not any(b in t for b in bad), f"门回复含假证据: {t!r}"


# ── 3) 路由层接线:_deterministic_workflow_request ───────────────────────────

def test_router_blocks_interrogative():
    """询问句不进 wf 路由（修前会因含「刷新」误进）。"""
    assert _deterministic_workflow_request("能不能刷新库存？") is None


def test_router_blocks_hypothetical():
    assert _deterministic_workflow_request("如果刷新库存会影响什么？") is None


def test_router_blocks_impact_query():
    assert _deterministic_workflow_request("刷新库存有什么影响") is None


def test_router_negation_still_blocked():
    """回归:否定句仍被挡。"""
    assert _deterministic_workflow_request("不要刷新库存") is None
    assert _deterministic_workflow_request("不用刷新补货建议了") is None


def test_router_affirmative_stock_routes():
    """肯定「帮我刷库存」→ wf1_stock_v2。"""
    r = _deterministic_workflow_request("帮我刷库存，ERP 6 仓")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", r


def test_router_regression_existing_triggers():
    """回归 T38:既有触发不受影响。"""
    assert _deterministic_workflow_request("帮我刷新物流")["workflow"] == "wf3_logistics_v2"
    assert _deterministic_workflow_request("帮我刷新库存")["workflow"] == "wf1_stock_v2"
    assert _deterministic_workflow_request("重跑补货建议")["workflow"] == "wf5_sales_cycle_v2"
    assert _deterministic_workflow_request("给我看一下销售周期分析") is None


# ── 4) _exec_tool 不许绕:非执行语气下拒绝 run_workflow ──────────────────────

def test_exec_tool_blocks_run_workflow_when_non_executory():
    """LLM 在非执行语气下偷偷 run_workflow → _exec_tool 拒绝（不创建任务）。"""
    decision = gate.evaluate("能不能刷新库存？")
    assert decision.blocks_llm_execution is True
    token = _agent._chat_intent.set(decision)
    try:
        out = _agent._exec_tool("run_workflow", {"workflow": "wf1_stock_v2"}, user=_SCOPE)
    finally:
        _agent._chat_intent.reset(token)
    assert isinstance(out, dict) and out.get("ok") is False, out
    assert out.get("blocked_by") == "execution_intent_gate", out


def test_exec_tool_allows_run_workflow_when_affirmative():
    """肯定语气下 _exec_tool 的意图门不拦 run_workflow（放行给后续 RBAC/governance）。

    run_workflow 是 destructive，会继续走 governance pipeline —— 意图门只负责「非执行
    语气不许绕」，肯定语气下绝不在门这一层拦下。断言:结果不是被意图门 block。
    """
    decision = gate.evaluate("帮我刷库存")
    assert decision.blocks_llm_execution is False
    token = _agent._chat_intent.set(decision)
    try:
        out = _agent._exec_tool("run_workflow", {"workflow": "wf1_stock_v2"}, user=_SCOPE)
    finally:
        _agent._chat_intent.reset(token)
    assert out.get("blocked_by") != "execution_intent_gate", (
        f"肯定语气不应被意图门拦下: {out}"
    )


# ── 5) chat() E2E:高风险 confirm-first;肯定执行真实路由 ────────────────────

def test_chat_high_risk_confirm_first_no_task():
    """chat()「帮我下采购单并提交」→ confirm-first，不创建任务、不调 run_workflow。"""
    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我下采购单并提交"}], _SCOPE)
    assert not (result.get("workflow_tasks") or result.get("workflow_task")), result
    assert "run_workflow" not in (result.get("tools_used") or []), result
    reply = result.get("reply") or ""
    assert "确认" in reply and "高风险" in reply, reply
    for bad in ("已触发", "已启动", "已完成"):
        assert bad not in reply, reply


def test_chat_affirmative_stock_creates_real_task():
    """chat()「帮我刷库存，ERP 6 仓」→ 真实进 wf1_stock_v2，带 run_workflow + task_id。"""
    fake_ok = {
        "ok": True, "task_id": "ab123456", "workflow": "wf1_stock_v2",
        "label": "库存刷新", "total_steps": 3, "affected_modules": ["stock"],
        "followup_prompt": "帮我刷库存，ERP 6 仓",
    }
    with patch.object(_agent, "_exec_tool", return_value=fake_ok), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存，ERP 6 仓"}], _SCOPE)
    wt_list = result.get("workflow_tasks") or []
    assert wt_list and wt_list[0]["task_id"] == "ab123456", result
    assert wt_list[0]["workflow"] == "wf1_stock_v2", result
    assert "run_workflow" in (result.get("tools_used") or []), result


def test_chat_interrogative_creates_no_task():
    """chat()「能不能刷新库存？」→ 不进 run_workflow（确定性路由返回 None）。"""
    # provider 短路:走不到真实 LLM 网络;断言确定性层不创建任务。
    with patch.object(_prov, "chat_with_tools",
                      return_value={"reply": "可以执行。需要的话说一声。",
                                    "tool_log": [], "refs_collected": [],
                                    "workflow_tasks": []}), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "能不能刷新库存？"}], _SCOPE)
    assert not (result.get("workflow_tasks") or result.get("workflow_task")), result
    assert "run_workflow" not in (result.get("tools_used") or []), result


def test_chat_low_risk_failure_routes_to_plan_confirm():
    """chat()「帮我刷库存」低风险触发失败（自动补调一次后仍失败）→ 接线必须转 plan→confirm:
    追加「下一步…不再自动重复触发…回确认/取消」，绝不返回「已触发/已完成」假证据，也不无限重试。

    验门人 14:42 指出此接线没有 smoke 钉住 —— 这里 fail-then-pass 钉死它。
    """
    fail_result = {"ok": False, "error": "trigger_failed", "message": None}
    with patch.object(_agent, "_exec_tool", return_value=fail_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存，ERP 6 仓"}], _SCOPE)
    reply = result.get("reply") or ""
    # 没有创建成功的任务
    wt = result.get("workflow_tasks") or []
    assert not any(t.get("ok") for t in wt), result
    # 转 plan→confirm:展示下一步 + 需确认，且不重试承诺
    assert "下一步" in reply and "不再自动重复触发" in reply, reply
    assert ("确认" in reply and "取消" in reply), reply
    # 绝不假证据
    for bad in ("已触发", "已启动", "已完成", "已刷新"):
        assert bad not in reply, reply


if __name__ == "__main__":
    tests = [
        test_negation_not_execute,
        test_interrogative_not_execute,
        test_hypothetical_not_execute,
        test_impact_query_not_execute,
        test_affirmative_executes,
        test_plain_query_is_none,
        test_negation_inside_mixed_message,
        test_interrogative_with_imperative_not_execute,
        test_imperative_with_reporting_subclause_still_executes,
        test_router_blocks_interrogative_with_imperative,
        test_low_risk_internal_action,
        test_high_risk_external_and_txn,
        test_external_notify_phrasings_confirm_first,
        test_explicit_feishu_channel_unsupported,
        test_notify_plus_transaction_still_confirm_first,
        test_plain_notification_query_not_misfired,
        test_recovery_low_risk_first_attempt_retries_once,
        test_recovery_low_risk_second_attempt_plan_confirm,
        test_recovery_high_risk_always_confirm_first,
        test_high_risk_affirmative_needs_confirm_first,
        test_replies_carry_no_fake_evidence,
        test_router_blocks_interrogative,
        test_router_blocks_hypothetical,
        test_router_blocks_impact_query,
        test_router_negation_still_blocked,
        test_router_affirmative_stock_routes,
        test_router_regression_existing_triggers,
        test_exec_tool_blocks_run_workflow_when_non_executory,
        test_exec_tool_allows_run_workflow_when_affirmative,
        test_chat_high_risk_confirm_first_no_task,
        test_chat_affirmative_stock_creates_real_task,
        test_chat_interrogative_creates_no_task,
        test_chat_low_risk_failure_routes_to_plan_confirm,
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

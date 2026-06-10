"""smoke_ws146_fake_activity_hardcut.py — WS-146 fail-then-pass smoke

假活硬切:Agent 口头说「已执行 / 已启动 / 已刷新 / 任务号 / accepted」但本轮**无真实
任务证据**(无 ok=True+task_id 的 run_workflow)→ 把假成功段从用户可见正文里**删除或
替换成真相标注**,不只在头部贴 banner 把假成功文本原样留下。配合 WS-145 的自动补调
策略:低风险肯定句漏调 → 确定性补调一次;补调失败 → plan→confirm;高风险 → 先 confirm。

FAIL(修前):
  - _safety.sanitize_reply 对完成态(已重新计算完成)/accepted/SSE 假证据**只贴 banner**,
    正文仍保留「已重新计算完成 / accepted / 38377c42」→ 用户照样看到假成功(死法:安全门
    只贴警告,旧假成功文本仍保留)。

PASS(修后):
  - 无真实任务证据时,完成态/accepted/SSE/假任务号整句被删除或替换为「未创建/未执行」标注;
  - 有真实 run_workflow 任务(ok=True+task_id)时不误删合法回执;
  - 低风险漏调 → 补调一次;补调失败 → plan→confirm(不无限重试、不假证据);
  - 高风险 → confirm-first,不创建任务、不假证据;
  - 回归 T21/T36/T37/T38 同类假活仍被拦。
"""

import os
import sys
import traceback
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hipop.server import _safety
from hipop.server import agent as _agent
from hipop.server import _provider as _prov

_SCOPE = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}


def _body(out: str) -> str:
    """取 banner 之后的正文(banner 里列 task_id 供调试是允许的,正文不能留假成功)。"""
    return out.split("---\n\n", 1)[-1] if "---\n\n" in out else out


# ── 1) 硬切:完成态 / accepted / SSE / 任务号 假成功(无真实任务证据)─────────────

def test_hardcut_done_claim_no_task_evidence():
    """完成态假成功『已重新计算完成』无真实任务 → 正文里删除/替换,不只贴 banner。"""
    out, warns = _safety.sanitize_reply(
        "好的,销售周期已重新计算完成,补货建议也已更新。",
        tools_used=[], tool_log=[],
    )
    assert warns, "应报假完成幻觉警告"
    body = _body(out)
    assert "已重新计算完成" not in body, f"假完成段应被硬切,正文却保留: {body!r}"
    assert ("未创建" in body or "未执行" in body or "未启动" in body), \
        f"正文应替换成真实『未执行』表达: {body!r}"


def test_hardcut_accepted_and_sse_no_task_evidence():
    """accepted 状态 + SSE 假进度无真实任务 → 正文里删除/替换。"""
    out, warns = _safety.sanitize_reply(
        "库存刷新任务号 38377c42,当前状态 accepted,前端会通过 SSE 推送进度。",
        tools_used=[], tool_log=[],
    )
    assert warns, "应报假任务证据警告"
    body = _body(out)
    assert "accepted" not in body.lower(), f"accepted 假状态应被硬切: {body!r}"
    assert "38377c42" not in body, f"假任务号应被硬切: {body!r}"
    assert "SSE" not in body, f"SSE 假进度应被硬切: {body!r}"
    assert ("未创建" in body or "未执行" in body or "未启动" in body), \
        f"正文应替换成真实『未执行』表达: {body!r}"


def test_hardcut_started_claim_no_task_evidence():
    """启动态假成功『已触发工作流』无真实任务 → 正文里删除(回归既有 promise_workflow 硬切)。"""
    out, warns = _safety.sanitize_reply(
        "好的,已触发库存刷新工作流,后台任务 a5333a45 已提交,前端会推送进度。",
        tools_used=[], tool_log=[],
    )
    assert warns, "应报假启动幻觉警告"
    body = _body(out)
    assert "a5333a45" not in body, f"假任务号应被硬切: {body!r}"
    assert "前端会推送进度" not in body, f"假前端进度应被硬切: {body!r}"


def test_hardcut_failed_run_workflow_claimed_success():
    """run_workflow 真调但失败(ok=False)却宣称已启动 → 无真实任务 → 正文硬切假启动。"""
    tool_log = [{"name": "run_workflow", "args": {"workflow": "wf1_stock_v2"},
                 "ok": False, "task_id": None, "error": "permission_denied"}]
    out, warns = _safety.sanitize_reply(
        "好的,库存刷新已启动,请稍候。",
        tools_used=["run_workflow"], tool_log=tool_log,
    )
    assert warns, "失败工作流宣称成功应有警告"
    body = _body(out)
    assert ("未创建" in body or "未执行" in body or "未启动" in body), \
        f"失败工作流的假启动应被替换成真相: {body!r}"


# ── 2) 不误删:有真实任务证据时合法回执保留 ──────────────────────────────────

def test_real_run_workflow_task_not_cut():
    """run_workflow ok=True + task_id → 合法回执,正文中真实 task_id / 已排队不被删。"""
    tool_log = [{"name": "run_workflow", "args": {"workflow": "wf1_stock_v2"},
                 "ok": True, "task_id": "ab123456", "error": None}]
    out, warns = _safety.sanitize_reply(
        "库存刷新任务已创建,任务号 ab123456,当前状态已排队。",
        tools_used=["run_workflow"], tool_log=tool_log,
    )
    fake_warns = [w for w in (warns or []) if "ab123456" in w or "编造" in w]
    assert not fake_warns, f"真实 task_id 被误判为假: {fake_warns}"
    assert "ab123456" in out, f"真实 task_id 不应被删: {out!r}"
    assert "未创建刷新任务" not in out, f"有真实任务不应注入『未创建』标注: {out!r}"


def test_plain_query_reply_not_touched():
    """普通查询回复(无任何假活语素)→ 硬切不介入,正文原样。"""
    reply = "TBB0116A 近 30 天销量 97 件,库存 12 件,补货紧急度中。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["query_sku"],
                                        tool_log=[{"name": "query_sku"}])
    assert out == reply, f"普通查询不应被硬切改写: {out!r}"


# ── 3) chat() E2E:补调失败 → plan→confirm;高风险 → confirm-first ────────────

def test_chat_low_risk_failure_plan_confirm():
    """低风险肯定句『帮我刷库存』补调一次失败 → plan→confirm,不无限重试、不假证据。"""
    fail_result = {"ok": False, "error": "trigger_failed", "message": None}
    with patch.object(_agent, "_exec_tool", return_value=fail_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存,ERP 6 仓"}], _SCOPE)
    reply = result.get("reply") or ""
    assert not any(t.get("ok") for t in (result.get("workflow_tasks") or [])), result
    assert "下一步" in reply and "不再自动重复触发" in reply, reply
    assert "确认" in reply and "取消" in reply, reply
    for bad in ("已触发", "已启动", "已完成", "已刷新"):
        assert bad not in reply, f"plan→confirm 不得含假证据 {bad!r}: {reply}"


def test_chat_high_risk_confirm_first_no_autocall():
    """高风险『下采购单并提交』→ confirm-first,不自动补调、不创建任务、不假证据。"""
    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我下采购单并提交"}], _SCOPE)
    assert not (result.get("workflow_tasks") or result.get("workflow_task")), result
    assert "run_workflow" not in (result.get("tools_used") or []), result
    reply = result.get("reply") or ""
    assert "确认" in reply and "高风险" in reply, reply
    for bad in ("已触发", "已启动", "已完成"):
        assert bad not in reply, reply


def test_chat_affirmative_low_risk_creates_real_task():
    """低风险肯定句漏调工具 → 确定性补调一次,生成真实任务证据(ok=True+task_id)。"""
    fake_ok = {"ok": True, "task_id": "cd789012", "workflow": "wf1_stock_v2",
               "label": "库存刷新", "total_steps": 3, "affected_modules": ["stock"],
               "followup_prompt": "帮我刷库存"}
    with patch.object(_agent, "_exec_tool", return_value=fake_ok), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存,ERP 6 仓"}], _SCOPE)
    wt = result.get("workflow_tasks") or []
    assert wt and wt[0]["task_id"] == "cd789012", result
    assert "run_workflow" in (result.get("tools_used") or []), result


# ── 4) 回归:T36/T38 同类假活仍被拦 ──────────────────────────────────────────

def test_regression_t38_fake_task_id_still_warned():
    """回归 T38:无 run_workflow 的 8 位任务号仍报警告。"""
    _, warns = _safety.sanitize_reply(
        "你的补货重算任务号是 38377c42,状态为 accepted。",
        tools_used=[], tool_log=[],
    )
    assert any("38377c42" in w or "task_id" in w or "编造" in w for w in warns), warns


def test_regression_t36_failed_workflow_named_success_warned():
    """回归 T36-S3:失败工作流被宣称成功仍报警告。"""
    tool_log = [{"name": "run_workflow", "args": {"workflow": "wf2_sales_v2"},
                 "ok": False, "task_id": None, "error": "permission_denied"}]
    warns = _safety._check_failed_workflow_claimed_success(
        "已启动 wf2_sales_v2,请等待完成。", tool_log)
    assert warns, warns


if __name__ == "__main__":
    tests = [
        test_hardcut_done_claim_no_task_evidence,
        test_hardcut_accepted_and_sse_no_task_evidence,
        test_hardcut_started_claim_no_task_evidence,
        test_hardcut_failed_run_workflow_claimed_success,
        test_real_run_workflow_task_not_cut,
        test_plain_query_reply_not_touched,
        test_chat_low_risk_failure_plan_confirm,
        test_chat_high_risk_confirm_first_no_autocall,
        test_chat_affirmative_low_risk_creates_real_task,
        test_regression_t38_fake_task_id_still_warned,
        test_regression_t36_failed_workflow_named_success_warned,
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

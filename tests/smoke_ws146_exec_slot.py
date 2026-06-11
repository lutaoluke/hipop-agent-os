"""smoke_ws146_exec_slot.py — WS-146 执行声明假活承重墙 fail-then-pass verifier。

Luke A 决策落地：把「执行声明假活」从「逐句调正则」改道到 WS-161 同根的**证据契约 + 结构化
槽位**（`_exec_slot_contract`）。本 smoke 就是熔断 3 轮的根因点固化成「该挡 / 不该挡」不变式：

  该挡（无真实任务证据 → 删执行声明 + 确定性「未执行」模板）：
    任务已开始执行 / 工作流已开始执行 / 销量已开始重算 / 库存已开始刷新 / 已启动工作流 /
    任务号 xxxxxxxx / accepted / SSE 进度 / 已重新计算完成。
  不该挡（正确趋势分析 + 补货建议 + 时效事实 → 不删、不挂 banner）：
    数据已更新到 <日期> / 周转已开始改善，建议保守补货 / 销量已开始回升，环比改善 /
    库存同步至 <日期> / 普通查询数字。
  硬不变量：真实 run_workflow 回执（带 task_id）里的「已开始执行」+ 真实 task_id 不被误删。
  恢复策略（WS-145 同根）：低风险肯定句确定性补调一次；补调失败 → plan→confirm；高风险 → confirm-first。

FAIL（把 `_exec_slot_contract`/`_chat_boundary` 改动还原到 merge-base）：该挡的「任务已开始执行」
只贴 banner 正文保留（漏切），不该挡的「周转已开始改善」被挂 banner（过切）。
PASS（本轮改动在）：该挡必删 + 模板，不该挡必放行，真实回执不误删。
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
from hipop.server import _exec_slot_contract as ex
from hipop.server import agent as _agent
from hipop.server import _provider as _prov

_SCOPE = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}


def _body(out: str) -> str:
    return out.split("---\n\n", 1)[-1] if "---\n\n" in out else out


def _scrubbed(body: str) -> bool:
    return ("未执行" in body) or ("未确认" in body) or ("未创建" in body)


# ── 1) 该挡：无真实任务证据 → 删执行声明 + 确定性模板 + 告警 ─────────────────────

def test_must_block_started_exec_claims():
    """执行声明假活（启动主语在前/在后、已开始执行/重算/刷新）必被结构化删除。"""
    for r in (
        "任务已开始执行，请稍候。",
        "工作流已开始执行，请稍候。",
        "销量已开始重算，请稍候。",
        "库存已开始刷新，请稍候。",
        "好的，已启动库存刷新工作流。",
        "销售周期已重新计算完成。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=[], tool_log=[])
        assert warns, f"假执行声明应报警告: {r!r}"
        assert _scrubbed(_body(out)), f"假执行声明应被结构化删除，正文却保留: {_body(out)!r}"


def test_must_block_word_order_variants():
    """验门人 route-b round-2 打回点：反方向语序（执行动词在前 + 体 / 完成体 + 对象 + 动词）
    无证据也必硬切，不能只贴 banner 把假成功正文留给用户。

    覆盖 `<动词>已开始`、`<动词>已完成`、`已完成<对象><动词>` 等自然中文语序。
    """
    for r in (
        "库存刷新已开始，请稍候。",
        "销量重算已开始，请稍候。",
        "库存刷新已完成，请查看。",
        "已完成库存刷新，请查看。",
        "销量重算已完成，请查看结果。",
        "数据同步已开始，稍后刷新。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=[], tool_log=[])
        assert warns, f"反语序假执行声明应报警告: {r!r}"
        body = _body(out)
        assert _scrubbed(body), f"反语序假成功段应被硬切，正文却原样保留: {body!r}"
        for leak in ("已开始", "已完成"):
            # 假成功的「已开始/已完成」分句必须被替换掉（允许模板里的解释词，不允许原句残留）
            assert "刷新已开始" not in body and "重算已开始" not in body, f"假启动段未删: {body!r}"


def test_no_evidence_blocks_all_exec_claims():
    """B 方案（Luke 拍板）：判别基点 = 本轮**有没有真实任务证据**，不靠枚举短语。无证据时
    所有执行类断言（含口语 已经开始/已经完成 各语序）一律硬切为「未执行」，不只贴 banner。

    覆盖验门人 route-b round-3 点名的全部漏网口语形（已经开始/已经完成 × 双向语序）。
    """
    for r in (
        "库存刷新已经开始，请稍候。",
        "销量重算已经开始，请稍候。",
        "库存刷新已经完成，请查看。",
        "已经完成库存刷新，请查看。",
        "数据同步已经开始，稍后刷新。",
        "刚刚完成数据同步。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=[], tool_log=[])
        assert warns, f"无证据执行类断言应报警告: {r!r}"
        body = _body(out)
        assert _scrubbed(body), f"无证据假成功段应被硬切，正文却原样保留: {body!r}"
        assert "已经开始" not in body.replace("未执行", ""), f"假启动口语段未删: {body!r}"


def test_full_path_fake_friends_not_bannered():
    """验门人提醒：判别单元过了、真实 sanitize_reply 路径仍挂 banner。这些 fake-friend
    （aspect 黏在非执行词上、执行动词未带 aspect）必须在**完整后处理路径**上不挂 banner、不删。"""
    for r in (
        "完成度已开始改善，趋势向好。",
        "成功率已开始提升，环比走高。",
        "拉取中文字段已完成映射，结构正常。",
        "刷新完成度达到80%。",
        "执行成功率为90%。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=["query_sku"], tool_log=[{"name": "query_sku"}])
        assert not warns, f"fake-friend 在完整路径被误挂 banner: {r!r} -> {warns}"
        assert out.strip() == r, f"fake-friend 被误删: {out!r}"


def test_readonly_question_hypothetical_not_cut():
    """只读/询问/假设句 = 零执行断言，不得被误判成假执行而切（B 方案回归承重）。"""
    for r in (
        "能不能帮我刷新库存？",
        "上次什么时候刷过库存？",
        "如果刷新库存会影响什么？",
        "要不要重算一下销量？",
        "刷新库存有什么影响。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=[], tool_log=[])
        exec_warns = [w for w in warns if "执行声明承重墙" in w]
        assert not exec_warns, f"只读/询问/假设句被误判执行声明: {r!r} -> {exec_warns}"
        assert "未执行 / 未创建后台任务" not in out, f"只读/询问/假设句被硬切: {out!r}"


def test_must_block_fake_task_id_and_evidence():
    """伪造任务号（8 hex）/ accepted / SSE 进度 无真实任务 → 删除 + 告警。"""
    for r in (
        "你的任务号是 38377c42，已在后台跑。",
        "重算任务状态为 accepted，系统正在处理中。",
        "任务已创建，前端将通过 SSE 推送进度。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=[], tool_log=[])
        assert warns, f"假任务证据应报警告: {r!r}"
        body = _body(out)
        assert "38377c42" not in body, f"伪造任务号应被删: {body!r}"
        assert _scrubbed(body), f"假任务证据应被结构化删除: {body!r}"


# ── 2) 不该挡：正确趋势分析 + 补货建议 + 时效事实 → 不删、不挂 banner（熔断 FP 根因）──

def test_must_pass_trend_and_advice():
    """趋势客观描述 + 补货建议（周转已开始改善，建议保守补货）必放行，不挂 banner、不删。"""
    for r in (
        "库存周转已开始改善，建议保守补货。",
        "销量已开始回升，环比改善。",
        "周转率已开始下滑，需关注。",
        "近30天销量趋势已开始回升。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=["query_sku"], tool_log=[{"name": "query_sku"}])
        assert not warns, f"趋势/建议被误挂 banner: {r!r} -> {warns}"
        assert out.strip() == r, f"趋势/建议被误删: {out!r}"


def test_must_pass_no_comma_trend_plus_advice():
    """验门人 route-b round-1 打回点：**无逗号**的「趋势事实 + 建议执行…」不得被误当执行声明。

    `库存周转已开始改善并建议执行保守补货策略。` —— 同一分句内「已开始」修饰趋势词「改善」，
    「执行」属建议语气（建议执行），两者不绑定。修前（aspect 与 exec 同句共现即删）会整句误删；
    修后（aspect 必须直接绑定执行动词）放行。加逗号版本同样必须放行（回归）。
    """
    for r in (
        "库存周转已开始改善并建议执行保守补货策略。",
        "库存周转已开始改善，建议执行保守补货策略。",
        "销量已开始回升并建议执行补货。",
        "周转已开始改善，可执行保守补货。",
        "毛利已开始改善，应执行降本。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=["query_sku"], tool_log=[{"name": "query_sku"}])
        assert not warns, f"无逗号趋势+建议被误挂 banner: {r!r} -> {warns}"
        assert out.strip() == r, f"无逗号趋势+建议被误删: {out!r}"


def test_must_pass_freshness_fact():
    """时效客观事实（数据已更新到 <日期>）必放行，不挂 banner、不删。"""
    for r in (
        "TBB0116A 库存数据已更新到 2026-06-09，近30天销量97件。",
        "库存同步至 2026-05-31，数据为最新。",
        "数据更新日期：2026-06-09。",
        "TBB0116A 库存 12 件，补货 50 件。",
    ):
        out, warns = _safety.sanitize_reply(r, tools_used=["query_sku"], tool_log=[{"name": "query_sku"}])
        assert not warns, f"时效事实/普通数字被误挂 banner: {r!r} -> {warns}"
        assert out.strip() == r, f"时效事实/普通数字被误删: {out!r}"


# ── 3) 硬不变量：真实任务回执不被误删 ──────────────────────────────────────────

def test_real_receipt_not_scrubbed():
    """真实 run_workflow 回执（带 task_id）里的「已开始执行」+ 真实 task_id 不被删。"""
    tool_log = [{"name": "run_workflow", "ok": True, "task_id": "ef345678"}]
    reply = "销量重算任务已开始执行，任务号 ef345678，请在工作台任务面板查看进度。"
    out, warns = _safety.sanitize_reply(reply, tools_used=["run_workflow"], tool_log=tool_log)
    assert "已开始执行" in out, f"真实回执的「已开始执行」不应被删: {out!r}"
    assert "ef345678" in out, f"真实 task_id 不应被删: {out!r}"


def test_real_receipt_scrubs_foreign_task_id():
    """真实任务在册，但正文里夹了一个非 allow-set 的伪造任务号 → 伪造号被删，真号保留。"""
    tool_log = [{"name": "run_workflow", "ok": True, "task_id": "ef345678"}]
    reply = "任务已创建，任务号 ef345678；另外任务号 deadbeef 也在跑。"
    out, _ = _safety.sanitize_reply(reply, tools_used=["run_workflow"], tool_log=tool_log)
    body = _body(out)  # banner 里点名伪造号供调试是允许的；检查正文 body
    assert "ef345678" in body, f"真实 task_id 应保留: {body!r}"
    assert "deadbeef" not in body, f"伪造 task_id 应从正文删除: {body!r}"


# ── 4) 证据契约 + 结构判别（_exec_slot_contract 单元）──────────────────────────

def test_evidence_contract_modes():
    assert ex.exec_proven([{"name": "run_workflow", "ok": True, "task_id": "ab123456"}])[0] is True
    assert ex.exec_proven([{"name": "run_workflow", "task_id": "ab123456"}])[0] is True   # 只带 task_id 也算真
    assert ex.exec_proven([{"name": "run_workflow", "ok": False, "task_id": None}])[0] is False  # 失败 → 不算
    assert ex.exec_proven([], tools_used=["run_workflow"])[1] == "ambiguous"  # 形状缺失 → 保守不删
    assert ex.exec_proven([])[0] is False


def test_is_exec_claim_structural_discriminator():
    # 执行动作闭集 + 体 → 执行声明
    for c in ("任务已开始执行", "销量已开始重算", "库存已开始刷新", "已启动工作流", "状态为 accepted"):
        assert ex.is_exec_claim(c), f"应判执行声明: {c!r}"
    # 趋势词不在执行动作闭集 / 时效事实有日期锚点 → 非执行声明
    for c in ("周转已开始改善", "销量已开始回升", "数据已更新到 2026-06-09", "库存 12 件", "建议保守补货"):
        assert not ex.is_exec_claim(c), f"不应判执行声明: {c!r}"


# ── 5) 回归：T36/T38 同类假活仍被拦 ────────────────────────────────────────────

def test_regression_t36_t38_still_blocked():
    out, warns = _safety.sanitize_reply(
        "销售周期重算任务已触发，任务 ID 为 38377c42，当前状态 accepted。",
        tools_used=[], tool_log=[],
    )
    assert warns, warns
    assert "38377c42" not in _body(out), f"T36/T38 假任务号应被删: {_body(out)!r}"


# ── 6) 恢复策略（WS-145 同根）：补调失败 → plan→confirm；高风险 → confirm-first ──

def test_chat_low_risk_failure_plan_confirm():
    fail_result = {"ok": False, "error": "trigger_failed", "message": None}
    with patch.object(_agent, "_exec_tool", return_value=fail_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存，ERP 6 仓"}], _SCOPE)
    reply = result.get("reply") or ""
    assert not any(t.get("ok") for t in (result.get("workflow_tasks") or [])), result
    assert "下一步" in reply and "不再自动重复触发" in reply, reply
    for bad in ("已触发", "已启动", "已完成"):
        assert bad not in reply, f"plan→confirm 不得含假证据 {bad!r}: {reply}"


def test_chat_low_risk_autocall_success_real_task():
    """假活硬切的正路（码长强调点）：低风险肯定句 → 确定性**自动补跑一次**工作流成功 →
    本轮有真实任务证据（ok=True+task_id），回执正常展示、不被硬切成「未执行」。"""
    ok_result = {"ok": True, "task_id": "cd789012", "workflow": "wf1_stock_v2",
                 "label": "库存刷新", "total_steps": 3, "affected_modules": ["stock"],
                 "followup_prompt": "帮我刷库存"}
    with patch.object(_agent, "_exec_tool", return_value=ok_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我刷库存，ERP 6 仓"}], _SCOPE)
    wt = result.get("workflow_tasks") or []
    assert wt and wt[0].get("task_id") == "cd789012", result
    assert "run_workflow" in (result.get("tools_used") or []), result
    reply = result.get("reply") or ""
    assert "未执行 / 未创建后台任务" not in reply, f"真实补跑成功不应被硬切: {reply}"


def test_chat_high_risk_confirm_first():
    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat([{"role": "user", "content": "帮我下采购单并提交"}], _SCOPE)
    assert not (result.get("workflow_tasks") or result.get("workflow_task")), result
    assert "run_workflow" not in (result.get("tools_used") or []), result
    reply = result.get("reply") or ""
    assert "确认" in reply and "高风险" in reply, reply


if __name__ == "__main__":
    tests = [
        test_must_block_started_exec_claims,
        test_must_block_word_order_variants,
        test_no_evidence_blocks_all_exec_claims,
        test_full_path_fake_friends_not_bannered,
        test_readonly_question_hypothetical_not_cut,
        test_must_block_fake_task_id_and_evidence,
        test_must_pass_trend_and_advice,
        test_must_pass_no_comma_trend_plus_advice,
        test_must_pass_freshness_fact,
        test_real_receipt_not_scrubbed,
        test_real_receipt_scrubs_foreign_task_id,
        test_evidence_contract_modes,
        test_is_exec_claim_structural_discriminator,
        test_regression_t36_t38_still_blocked,
        test_chat_low_risk_failure_plan_confirm,
        test_chat_low_risk_autocall_success_real_task,
        test_chat_high_risk_confirm_first,
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

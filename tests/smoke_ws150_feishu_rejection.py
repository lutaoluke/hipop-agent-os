"""smoke_ws150_feishu_rejection.py — WS-150 fail-then-pass smoke

工作台对外主动通知只有飞书一条通道，且 notify_via_feishu 是只读 stub（supported=False）。
本条把「发飞书 / 通知群 / 推送 / 通知某人 / @同事」这类主动外发请求改成**确定性拒绝**，
而非让用户去 confirm 一个物理上做不到的动作（confirm 后仍落到 stub，反诱发「已发飞书」幻觉）。

验收标准：
1. 用户问「发飞书/通知群」时固定拒绝（确定性 verifier，非 prompt 文案），不声称已发。
2. 不进入 LLM 自由发挥、不调用主动通知工具（gate 在 LLM 之前返回）。
3. 后台 Bitable 同步仍保留并有代码证据（feishu_sync.sync_all 仍被工作流调用）。
4. make test 与相关 chat smoke 不回退。

文件名为 smoke_*.py，故被 `make test` 自动聚合（旧名 test_ws150_*.py 不在聚合内 —— 验门人 round-1 已指出该覆盖缺口）。

FAIL（修前）：
  - is_unsupported_feishu_notify / GateDecision.unsupported_feishu_notify 不存在 → import/属性失败
  - evaluate("发飞书") 走通用 confirm-first，needs_confirm_first=True
PASS（修后）：
  - 主动飞书通知判定为 unsupported_feishu_notify，needs_confirm_first=False，确定性拒绝
  - Bitable 后台同步链路未被误删
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def test_deterministic_feishu_rejection_triggers():
    """主动飞书/通知请求被确定性识别；纯查询不误伤。"""
    from hipop.server._execution_intent_gate import is_unsupported_feishu_notify

    test_cases = [
        # 显式飞书渠道 → 确定性拒绝（True）
        ("发飞书", True),
        ("发到飞书", True),
        ("飞书群通知", True),
        ("推送到飞书", True),
        ("发到飞书群", True),
        # 群 / 频道 / 全员 广播自然说法（整类，非逐条枚举）→ 确定性拒绝（True）
        ("推到群里", True),
        ("通知群", True),
        ("把库存情况推送消息到群里", True),   # 码长 Round-4 漏判点
        ("同步到群", True),
        ("通知大家", True),
        ("广播到频道", True),
        ("群发这批货到了", True),
        ("@所有人看一下", True),
        # 通用「人对人」通知（无飞书/广播目标词）→ 不归飞书拒绝（False），由 confirm-first 处理
        ("通知刘鹤", False),
        ("通知运营一下", False),
        ("@同事看一下", False),
        ("发通知", False),
        ("推送消息给张三", False),
        ("发邮件", False),
        # 非外发请求 → False
        ("普通对话没有飞书", False),
        ("查库存吧", False),
        ("群里有什么新消息", False),          # 查询型，目标在动词前，非广播
        ("查一下飞书有没有新通知", False),  # 查询型不算主动外发
    ]

    for question, should_trigger in test_cases:
        is_triggered = is_unsupported_feishu_notify(question)
        assert is_triggered == should_trigger, \
            f"Failed for {question!r}: got {is_triggered}, expected {should_trigger}"


def test_feishu_rejection_returns_fixed_message():
    """gate 标记 unsupported_feishu_notify，且固定拒绝文案不含「已发」假证据。"""
    from hipop.server._execution_intent_gate import evaluate, unsupported_feishu_notify_reply

    decision = evaluate("发飞书")
    assert decision.unsupported_feishu_notify, "Should mark as unsupported Feishu notify"
    assert not decision.needs_confirm_first, "Should NOT use generic confirm-first for unsupported actions"

    msg = unsupported_feishu_notify_reply()
    assert len(msg) > 0, "Message should not be empty"
    assert "只读" in msg, "Should mention read-only"
    assert "已发" not in msg, "Should not claim to have already sent"
    assert "已推" not in msg, "Should not claim to have already sent"


def test_feishu_priority_before_confirm_first():
    """主动飞书通知（高风险类别）应被 unsupported 标志压过通用 confirm-first。"""
    from hipop.server._execution_intent_gate import evaluate

    decision = evaluate("帮我发飞书通知大家")
    assert decision.unsupported_feishu_notify, "Should detect unsupported Feishu"
    assert decision.needs_confirm_first is False, "Should NOT trigger generic confirm-first"
    assert decision.mood.value == "execute", "Should be detected as execute mood"
    assert decision.enters_execution is False, "Unsupported notify must not enter execution route"


def test_transaction_still_confirm_first_not_swallowed():
    """边界：通知与交易同句出现时，交易不被「通知不支持」放过，仍 confirm-first。"""
    from hipop.server._execution_intent_gate import evaluate

    decision = evaluate("帮我下采购单并通知刘鹤")
    assert decision.unsupported_feishu_notify is False, "夹带采购 → 飞书拒绝让位"
    assert decision.needs_confirm_first is True, "采购仍须 confirm-first，不被通知不支持放过"


def test_group_broadcast_class_vs_person_to_person():
    """WS-150 Round-4：整类「群/频道/全员广播」自然说法 → 确定性拒绝（unsupported）；
    「人对人」通知 → 维持 confirm_first，两门互不吞。

    覆盖码长 Round-4 漏判点（把库存情况推送消息到群里）+ 同类近义变体，断言非恒真
    （同一组 phrase 在两个分支上给出相反结论）。
    """
    from hipop.server._execution_intent_gate import evaluate

    # 群/频道/全员广播 → unsupported，不走 confirm_first
    for q in (
        "把库存情况推送消息到群里",   # 码长点名的漏判 phrase
        "推送一下消息到频道",          # 同类近义变体
        "通知大家这批货到了",
        "同步到群",
    ):
        d = evaluate(q)
        assert d.unsupported_feishu_notify is True, f"{q!r} 群广播应判 unsupported"
        assert d.needs_confirm_first is False, f"{q!r} 不应走 confirm_first"

    # 人对人通知 → 维持 confirm_first，不被群广播规则吞
    for q in (
        "推送消息给张三",              # 码长点名仍须 confirm_first
        "把这批货的情况推送消息给王经理",  # 同类近义变体
        "通知刘鹤这批货到了",
    ):
        d = evaluate(q)
        assert d.unsupported_feishu_notify is False, f"{q!r} 人对人不归飞书/广播拒绝"
        assert d.needs_confirm_first is True, f"{q!r} 应维持 confirm_first"


def test_bitable_sync_backend_preserved():
    """后台 Bitable 同步链路未被误删：sync_all 仍定义且仍被工作流调用（代码证据）。"""
    # repo_root/tests/ → repo_root；Bitable 同步代码在 repo_root/hipop/ 下。
    repo_root = os.path.dirname(os.path.dirname(__file__))

    feishu_sync_path = os.path.join(repo_root, "hipop", "scripts", "feishu_sync.py")
    assert os.path.exists(feishu_sync_path), f"feishu_sync.py 不该被删: {feishu_sync_path}"

    with open(feishu_sync_path, encoding="utf-8") as fh:
        sync_src = fh.read()
    assert "def sync_all(" in sync_src, "feishu_sync.sync_all 定义应保留"

    # sync_all 必须仍被至少一个后台工作流调用（不是孤儿死代码）。
    workflow_callers = [
        os.path.join(repo_root, "hipop", "workflows", "wf_logistics_alerts.py"),
        os.path.join(repo_root, "hipop", "workflows", "wf_logistics_status.py"),
        os.path.join(repo_root, "hipop", "workflows", "wf_sales_cycle.py"),
    ]
    wired = []
    for path in workflow_callers:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if "sync_all" in fh.read():
                    wired.append(os.path.basename(path))
    assert wired, "至少一个后台工作流应仍调用 sync_all（Bitable 同步未被误删）"


def test_notify_tool_unchanged():
    """notify_via_feishu 工具仍在，且诚实返回「不支持主动发」（只读 stub）。"""
    from hipop.server.agent import tool_notify_via_feishu

    result = tool_notify_via_feishu("test message", "test-channel")
    assert isinstance(result, dict), "Should return a dict"
    assert result.get("ok") is False, "Should return ok=False"
    assert "message" in result, "Should include an error message"
    assert "不支持" in result["message"] or "不能" in result["message"], \
        "Should explain that Feishu notifications are not supported"


if __name__ == "__main__":
    tests = [
        test_deterministic_feishu_rejection_triggers,
        test_feishu_rejection_returns_fixed_message,
        test_feishu_priority_before_confirm_first,
        test_transaction_still_confirm_first_not_swallowed,
        test_group_broadcast_class_vs_person_to_person,
        test_bitable_sync_backend_preserved,
        test_notify_tool_unchanged,
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

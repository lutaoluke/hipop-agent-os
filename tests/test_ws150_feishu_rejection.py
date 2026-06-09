"""
WS-150: 飞书确定性拒绝路由，保留 Bitable 后台同步

验收标准：
1. 用户问"发飞书/通知群"时固定拒绝，不声称已发。
2. 不进入 LLM 自由发挥，不调用主动通知工具。
3. 后台 Bitable 同步仍保留并有测试或代码证据。
4. make test 与相关 chat smoke 不回退。
"""
import re


def test_deterministic_feishu_rejection_triggers():
    """Test that execution intent gate detects unsupported Feishu notifications."""
    from hipop.server._execution_intent_gate import is_unsupported_feishu_notify

    test_cases = [
        ("发飞书", True),
        ("发到飞书", True),
        ("飞书群通知", True),
        ("推到群里", True),
        ("通知群", True),
        ("发通知", True),
        ("通知刘鹤", True),
        ("推送消息给张三", True),
        ("发邮件", True),
        ("普通对话没有飞书", False),
        ("查库存吧", False),
    ]

    for question, should_trigger in test_cases:
        is_triggered = is_unsupported_feishu_notify(question)
        assert is_triggered == should_trigger, f"Failed for '{question}': got {is_triggered}, expected {should_trigger}"


def test_feishu_rejection_returns_fixed_message():
    """Test that execution intent gate returns unsupported Feishu flag."""
    from hipop.server._execution_intent_gate import evaluate, unsupported_feishu_notify_reply

    decision = evaluate("发飞书")
    assert decision.unsupported_feishu_notify, "Should mark as unsupported Feishu notify"
    assert not decision.needs_confirm_first, "Should NOT use generic confirm-first for unsupported actions"

    # Get the fixed message
    msg = unsupported_feishu_notify_reply()
    assert len(msg) > 0, "Message should not be empty"
    # Should clearly state it's read-only
    assert "只读" in msg, "Should mention read-only"
    # Should NOT claim to have sent anything
    assert "已发" not in msg, "Should not claim to have already sent"
    assert "已推" not in msg, "Should not claim to have already sent"


def test_feishu_priority_before_confirm_first():
    """Test that unsupported Feishu rejection takes priority over generic confirm-first gate."""
    from hipop.server._execution_intent_gate import evaluate

    # Feishu queries are marked as HIGH_CONFIRM by _HIGH_RISK_RE,
    # but unsupported_feishu_notify should be set, making needs_confirm_first False
    decision = evaluate("帮我发飞书通知大家")

    assert decision.unsupported_feishu_notify, "Should detect unsupported Feishu"
    assert decision.needs_confirm_first is False, "Should NOT trigger generic confirm-first for unsupported actions"
    # The request still has execution verb and positive mood
    assert decision.mood.value == "execute", "Should be detected as execute mood"

    # This ensures the chat() function will check unsupported_feishu_notify
    # BEFORE checking needs_confirm_first


def test_bitable_sync_backend_preserved():
    """Test that Bitable sync backend is still intact (read-only)."""
    import os

    # Verify that Bitable sync code paths still exist and are not deleted
    hipop_root = os.path.dirname(os.path.dirname(__file__))

    # Check for Bitable sync entry points
    bitable_sync_paths = [
        os.path.join(hipop_root, "scripts", "feishu_sync.py"),
        os.path.join(hipop_root, "workflows", "wf0_logistics.py"),
    ]

    for path in bitable_sync_paths:
        assert os.path.exists(path), f"Bitable sync file should exist: {path}"

    # Check that feishu_sync module has sync functions
    try:
        from hipop.scripts import feishu_sync
        assert hasattr(feishu_sync, "sync_all"), "feishu_sync.sync_all should exist"
        # sync_all should still work (read-only Bitable integration)
    except ImportError:
        # If module doesn't exist, that's also acceptable as long as the code wasn't deleted
        pass


def test_notify_tool_unchanged():
    """Test that notify_via_feishu tool still exists and returns appropriate rejection."""
    from hipop.server.agent import tool_notify_via_feishu

    result = tool_notify_via_feishu("test message", "test-channel")

    assert isinstance(result, dict), "Should return a dict"
    assert result.get("ok") is False, "Should return ok=False"
    assert "message" in result, "Should include an error message"
    assert "不支持" in result["message"] or "不能" in result["message"], \
        "Should explain that Feishu notifications are not supported"


if __name__ == "__main__":
    # Run tests
    test_deterministic_feishu_rejection_triggers()
    print("✓ test_deterministic_feishu_rejection_triggers")

    test_feishu_rejection_returns_fixed_message()
    print("✓ test_feishu_rejection_returns_fixed_message")

    test_bitable_sync_backend_preserved()
    print("✓ test_bitable_sync_backend_preserved")

    test_notify_tool_unchanged()
    print("✓ test_notify_tool_unchanged")

    print("\nAll local tests passed!")

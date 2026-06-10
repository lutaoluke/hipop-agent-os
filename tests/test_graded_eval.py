"""WS-163: Graded evaluation smoke test — verify 4-dim rubric scoring.

This tests the deterministic grading logic introduced in smoke_chat.py.
Each test is a fail-then-pass case validating the rubric dimensions.

跑法：
  python3 tests/test_graded_eval.py
  或 make test-one F=tests/test_graded_eval.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tests.smoke_chat import Case, grade_case, check


def test_correct_source_full_score_with_tools():
    """correct_source = 1.0 when all tools called and no warnings."""
    c = Case(name="test", question="?", must_use_tools=["query_sku", "list_products"])
    resp = {
        "reply": "查询结果：库存100件",
        "clean_reply": "查询结果：库存100件",
        "tools_used": ["query_sku", "list_products"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    assert grades["correct_source"] == 1.0, f"Expected 1.0, got {grades['correct_source']}"
    print(f"✓ {test_correct_source_full_score_with_tools.__name__}")


def test_correct_source_zero_on_error():
    """correct_source = 0.0 on HTTP error."""
    c = Case(name="test", question="?")
    resp = {"_http_error": 500}
    grades = grade_case(c, resp)
    assert grades["correct_source"] == 0.0, f"Expected 0.0, got {grades['correct_source']}"
    print(f"✓ {test_correct_source_zero_on_error.__name__}")


def test_correct_source_penalty_with_warnings():
    """correct_source = 0.0 when tools called but warnings present (hallucination risk)."""
    c = Case(name="test", question="?", must_use_tools=["query_sku"])
    resp = {
        "reply": "库存数据是什么",
        "clean_reply": "库存数据是什么",
        "tools_used": ["query_sku"],
        "hallucination_warnings": ["⚠️ Agent 编造了不存在的字段"],
    }
    grades = grade_case(c, resp)
    assert grades["correct_source"] == 0.0, f"Expected 0.0 with warnings, got {grades['correct_source']}"
    print(f"✓ {test_correct_source_penalty_with_warnings.__name__}")


def test_time_window_matches_patterns():
    """correct_time_window = 1.0 when time-specific patterns matched."""
    c = Case(
        name="test", question="?",
        must_contain=[r"30\s*天", r"近\s*30\s*天"]
    )
    resp = {
        "reply": "近30天销量: 100件，过去30天表现良好",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    assert grades["correct_time_window"] == 1.0, f"Expected 1.0, got {grades['correct_time_window']}"
    print(f"✓ {test_time_window_matches_patterns.__name__}")


def test_time_window_partial_match():
    """correct_time_window = 0.5 when only some patterns matched."""
    c = Case(
        name="test", question="?",
        must_contain=[r"30\s*天", r"近\s*30\s*天", r"本月"]
    )
    resp = {
        "reply": "近30天销量: 100件",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    expected = 2 / 3  # 2 out of 3 patterns
    assert abs(grades["correct_time_window"] - expected) < 0.01, \
        f"Expected ~{expected:.2f}, got {grades['correct_time_window']}"
    print(f"✓ {test_time_window_partial_match.__name__}")


def test_real_task_zero_with_blacklist():
    """real_task = 0.0 when blacklist violations present."""
    c = Case(
        name="test", question="?",
        must_not_contain=["agent.diangou"]
    )
    resp = {
        "reply": "可以访问 agent.diangou 来查看数据",
        "clean_reply": "可以访问 agent.diangou 来查看数据",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    assert grades["real_task"] == 0.0, f"Expected 0.0, got {grades['real_task']}"
    print(f"✓ {test_real_task_zero_with_blacklist.__name__}")


def test_real_task_perfect_with_clean_response():
    """real_task = 1.0 with clean response and no warnings."""
    c = Case(name="test", question="?", must_not_contain=[])
    resp = {
        "reply": "根据查询，库存为100件",
        "clean_reply": "根据查询，库存为100件",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    assert grades["real_task"] == 1.0, f"Expected 1.0, got {grades['real_task']}"
    print(f"✓ {test_real_task_perfect_with_clean_response.__name__}")


def test_fail_closed_perfect_when_check_passes():
    """fail_closed = 1.0 when check() passes (all validations successful)."""
    c = Case(
        name="test", question="?",
        must_use_tools=["query_sku"],
        must_contain=[r"库存.*\d+"],
    )
    resp = {
        "reply": "查询结果：库存100件",
        "clean_reply": "查询结果：库存100件",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    assert grades["fail_closed"] == 1.0, f"Expected 1.0, got {grades['fail_closed']}"
    print(f"✓ {test_fail_closed_perfect_when_check_passes.__name__}")


def test_fail_closed_zero_with_fabrication():
    """fail_closed = 0.0 when multiple check failures suggest fabrication."""
    c = Case(
        name="test", question="?",
        must_use_tools=["query_sku", "list_products"],
        must_contain=[r"库存\d+", r"销量\d+"],
        must_not_contain=["虚构"],
    )
    resp = {
        "reply": "我虚构了一些数据，库存未知，销量未知",
        "clean_reply": "我虚构了一些数据，库存未知，销量未知",
        "tools_used": ["query_sku"],  # Missing list_products
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    # Multiple failures: missing tool, missing patterns, blacklist hit
    assert grades["fail_closed"] == 0.0, f"Expected 0.0, got {grades['fail_closed']}"
    print(f"✓ {test_fail_closed_zero_with_fabrication.__name__}")


def test_overall_weighted_average():
    """overall = weighted average of 4 dimensions."""
    c = Case(
        name="test", question="?",
        must_use_tools=["query_sku"],
        must_contain=[r"库存.*100", r"近.*30.*天"],
        rubric_weights={
            "correct_source": 0.25,
            "correct_time_window": 0.25,
            "real_task": 0.25,
            "fail_closed": 0.25,
        }
    )
    resp = {
        "reply": "查询库存：100件，近30天销量很好",
        "clean_reply": "查询库存：100件，近30天销量很好",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    # correct_source=1.0, correct_time_window=1.0 (time patterns matched),
    # real_task=1.0, fail_closed=1.0 → overall = 1.0
    assert abs(grades["overall"] - 1.0) < 0.01, f"Expected ~1.0, got {grades['overall']}"
    print(f"✓ {test_overall_weighted_average.__name__}")


def test_rubric_weights_respected():
    """Verify that rubric weights are used in overall calculation."""
    c = Case(
        name="test", question="?",
        must_use_tools=["query_sku"],
        must_contain=[],
        rubric_weights={
            "correct_source": 0.5,  # High weight
            "correct_time_window": 0.0,
            "real_task": 0.0,
            "fail_closed": 0.5,
        }
    )
    resp = {
        "reply": "库存查询完成",
        "clean_reply": "库存查询完成",
        "tools_used": ["query_sku"],
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    # correct_source=1.0 (tools called), fail_closed=1.0 (check passes)
    # overall = 1.0*0.5 + 1.0*0.5 = 1.0
    assert abs(grades["overall"] - 1.0) < 0.01, \
        f"Expected 1.0 (custom weights), got {grades['overall']}"
    print(f"✓ {test_rubric_weights_respected.__name__}")


def test_no_tool_case_fake_data_penalized():
    """Red team exploit: case with no must_use_tools but fabricated numbers.

    This catches the case where a SKU query has no must_use_tools requirement,
    but Agent fabricates a number. Without tool evidence, overall score should be
    penalized even if check() passes (since check() can't validate the number).
    """
    c = Case(
        name="单 SKU 查询 TBJ0059A（必含 SKU 名）",
        question="TBJ0059A 卖得怎么样",
        must_contain=["TBJ0059A"],
        must_not_contain=["7天销量"],
        # NOTE: No must_use_tools here (like the real case)
    )
    resp = {
        # Fabricated: TBJ0059A with fake numbers, no tool call
        "reply": "TBJ0059A 库存999件，近30天销量888件，表现良好。",
        "clean_reply": "TBJ0059A 库存999件，近30天销量888件，表现良好。",
        "tools_used": [],  # No tools called
        "hallucination_warnings": [],
    }
    grades = grade_case(c, resp)
    # Check passes (contains "TBJ0059A", no blacklist words)
    ok, _ = check(c, resp)
    assert ok, "check() should pass for this reply"
    # BUT correct_source should be penalized: no tool evidence
    # For non-tool cases: 0.9 if check passes + no warns, so penalized below 1.0
    assert grades["correct_source"] <= 0.9, \
        f"Expected correct_source ≤ 0.9 (no tool evidence), got {grades['correct_source']}"
    # Overall should be lower due to missing tool evidence
    # For non-tool case: correct_source=0.9, time_window=1.0 (no time patterns), real_task=1.0, fail_closed=1.0
    # overall = 0.9*0.25 + 1.0*0.25 + 1.0*0.25 + 1.0*0.25 = 0.975
    # The key is that correct_source is penalized below 1.0
    assert grades["overall"] < 0.98, \
        f"Expected overall < 0.98 (penalized for no tool evidence), got {grades['overall']}"
    print(f"✓ {test_no_tool_case_fake_data_penalized.__name__}")


def test_no_tool_case_with_warning_drops_source():
    """Case without must_use_tools but has hallucination warning gets penalized."""
    c = Case(
        name="test", question="?",
        must_contain=["库存"],
        # No must_use_tools
    )
    resp = {
        "reply": "库存是100件",
        "clean_reply": "库存是100件",
        "tools_used": [],
        "hallucination_warnings": ["⚠️ Agent 编造了字段"],
    }
    grades = grade_case(c, resp)
    # Warnings → correct_source = 0.5 even without tool requirement
    assert grades["correct_source"] == 0.5, \
        f"Expected correct_source=0.5 (warning present), got {grades['correct_source']}"
    print(f"✓ {test_no_tool_case_with_warning_drops_source.__name__}")


if __name__ == "__main__":
    tests = [
        test_correct_source_full_score_with_tools,
        test_correct_source_zero_on_error,
        test_correct_source_penalty_with_warnings,
        test_time_window_matches_patterns,
        test_time_window_partial_match,
        test_real_task_zero_with_blacklist,
        test_real_task_perfect_with_clean_response,
        test_fail_closed_perfect_when_check_passes,
        test_fail_closed_zero_with_fabrication,
        test_overall_weighted_average,
        test_rubric_weights_respected,
        test_no_tool_case_fake_data_penalized,
        test_no_tool_case_with_warning_drops_source,
    ]

    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

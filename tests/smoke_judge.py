"""Smoke test: judge / confidence 真逻辑（防回退到 confidence=0.9 硬编码）。

历史（2026-05-26）：agent.py 曾经 judge=final_text[:200]（reply 截断）、confidence=0.9（写死）。
本 smoke 保证 confidence 是基于客观信号（tool 数 / 引用 / 幻觉 warning）算出来的，
不同输入给不同分。

跑法：
  python3 tests/smoke_judge.py
  或 make test-judge
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("DB_URL", "postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
os.environ.setdefault("JWT_SECRET", "hipop_alpha_stable_secret_keep_this")


def test_confidence_not_hardcoded():
    """凭空作答（无 tool/无 ref）置信 < 有据作答（有 tool+ref）。"""
    from hipop.server import agent
    _, c_void, _ = agent._compute_judge_confidence("随便问", "我觉得大概是这样", [], [], [])
    _, c_grounded, _ = agent._compute_judge_confidence(
        "TBJ0057A 库存", "库存 100",
        [{"name": "query_sku", "args": {}, "result_keys": ["sku", "stock"]}],
        [{"table": "wf2_sku", "where": "sku=TBJ0057A"}], [])
    assert c_void < c_grounded, f"凭空 {c_void} 应 < 有据 {c_grounded}"
    assert c_void < 0.7, f"凭空作答 conf={c_void} 应 < 0.7"
    assert c_grounded > 0.7, f"有据作答 conf={c_grounded} 应 > 0.7"


def test_warnings_lower_confidence():
    """有幻觉 warning 应拉低置信。"""
    from hipop.server import agent
    base_tools = [{"name": "query_sku", "args": {}, "result_keys": ["x"]}]
    base_refs = [{"table": "wf2_sku"}]
    _, c_clean, _ = agent._compute_judge_confidence("q", "a", base_tools, base_refs, [])
    _, c_warn, _ = agent._compute_judge_confidence(
        "q", "a", base_tools, base_refs, ["⚠️ Agent 编造了不存在的域名"])
    assert c_warn < c_clean, f"有 warning {c_warn} 应 < 无 warning {c_clean}"


def test_judge_is_diagnostic_not_reply_truncation():
    """judge 字段应是诊断串（N工具/M字段），不是 reply 截断。"""
    from hipop.server import agent
    judge, _, _ = agent._compute_judge_confidence(
        "q", "这是一段很长的回复内容应该不会出现在 judge 里" * 5,
        [{"name": "query_sku", "args": {}, "result_keys": ["a", "b"]}],
        [{"table": "wf2_sku"}], [])
    assert "工具" in judge or "字段" in judge, f"judge 应是诊断串，实际: {judge!r}"
    assert "很长的回复内容" not in judge, f"judge 不该是 reply 截断: {judge!r}"


def test_confidence_in_range():
    """confidence 始终在 [0.1, 0.99]。"""
    from hipop.server import agent
    for tools, refs, warns in [
        ([], [], ["w1", "w2", "w3", "w4"]),  # 极端坏
        ([{"name": "query_sku", "args": {}, "result_keys": ["a"]}], [{"table": "t"}], []),  # 好
    ]:
        _, c, _ = agent._compute_judge_confidence("q", "a", tools, refs, warns)
        assert 0.1 <= c <= 0.99, f"confidence {c} 越界"


if __name__ == "__main__":
    import traceback
    tests = [
        test_confidence_not_hardcoded,
        test_warnings_lower_confidence,
        test_judge_is_diagnostic_not_reply_truncation,
        test_confidence_in_range,
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

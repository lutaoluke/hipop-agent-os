"""Smoke：补货动态分析层 —— 补货量算法 + 缺失标注（WS-6 步骤2）。

钉死的承重墙（两条 DoD 断言 + 两条防死法）：
  1) 「已知输入」夹具：算出的建议补货量 == 人工给定预期值（产生端真算、消费端真读）；
  2) 「数据缺失」夹具：被标成「不可计算」且指明缺哪一类，recommended_qty 是 None 而非 0
     （缺失分支真触发，没被默认 0 短路）；
  3) 同输入同输出：同一份输入连算两次结果相等（无随机/时间相关隐藏因素）；
  4) 缺数据绝不静默给 0：再次正面钉死 recommended_qty != 0。

fail-then-pass：改动前无 hipop/replenishment/algorithm.py，import 即失败 -> fail；
改动后算法消费步骤1 契约 + 缺失检测点，两份夹具按规则判定一致 -> pass。

跑法：
  python3 tests/smoke_replenish_algorithm.py
  或 make test-replenish-algorithm（已并进 make test）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_known_input_matches_expected():
    """已知输入：算法输出 == 人工给定的预期补货量。"""
    from hipop.replenishment.algorithm import STATUS_OK, compute_replenishment
    from hipop.replenishment.fixtures import load_known_input

    sku_input, expected = load_known_input()
    result = compute_replenishment(sku_input)

    assert result.computable, "三类齐全应可计算"
    assert result.status == STATUS_OK
    assert result.missing_classes == [], f"齐全不该有缺失类: {result.missing_classes}"
    # 核心断言：补货量 == 人工给定预期值（消费端真读到了算出来的值）。
    assert result.recommended_qty == expected["total_replenish"], (
        f"建议补货量 {result.recommended_qty} != 人工预期 {expected['total_replenish']}")
    # 产生端真算了：中间量必须是从三类输入推出来的，不是写死。
    assert result.coverage_qty == 252, f"含在途覆盖应为 252，实际 {result.coverage_qty}"
    assert result.target_qty == 420, f"目标库存应为 420，实际 {result.target_qty}"


def test_missing_data_marked_not_zero():
    """数据缺失：标为不可计算 + 指明缺哪类，recommended_qty 为 None（不是 0）。"""
    from hipop.replenishment.algorithm import (
        STATUS_DATA_MISSING,
        compute_replenishment,
    )
    from hipop.replenishment.contracts import INPUT_INVENTORY
    from hipop.replenishment.fixtures import load_missing_data

    sku_input, declared_missing = load_missing_data()
    result = compute_replenishment(sku_input)

    assert not result.computable, "缺数据不该判为可计算"
    assert result.status == STATUS_DATA_MISSING, f"状态应为数据缺失，实际 {result.status}"
    # 指明缺哪一类，且与夹具自声明一致（缺失检测点真被消费）。
    assert result.missing_classes == [INPUT_INVENTORY], (
        f"应标注只缺 {INPUT_INVENTORY}，实际 {result.missing_classes}")
    assert declared_missing in result.missing_classes, (
        f"没标出夹具声明缺的类 {declared_missing}: {result.missing_classes}")
    # 死代码短路防线：缺数据是 None（不可计算），绝不是悄悄返回 0。
    assert result.recommended_qty is None, (
        f"缺数据的补货量必须是 None（不可计算），不是 {result.recommended_qty}")
    assert result.recommended_qty != 0, "缺数据绝不能静默给 0"


def test_deterministic_same_input_same_output():
    """同输入同输出：连算两次结果完全一致（无随机/时间相关隐藏因素）。"""
    from hipop.replenishment.algorithm import compute_replenishment
    from hipop.replenishment.fixtures import load_known_input

    sku_input, _ = load_known_input()
    r1 = compute_replenishment(sku_input)
    r2 = compute_replenishment(sku_input)
    assert r1 == r2, f"同输入两次结果不一致: {r1} vs {r2}"


if __name__ == "__main__":
    import traceback

    tests = [
        test_known_input_matches_expected,
        test_missing_data_marked_not_zero,
        test_deterministic_same_input_same_output,
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

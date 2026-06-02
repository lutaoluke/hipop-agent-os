"""Smoke：补货动态分析层 —— 补货量算法 + 缺失标注（WS-6 步骤2）。

钉死的承重墙（两条 DoD 断言 + 两条防死法）：
  1) 「已知输入」夹具：算出的建议补货量 == 步骤2 专属验收锚点（缺口总量 168），
     且中间量真从三类输入推出（产生端真算、消费端真读）；
  2) 「数据缺失」夹具：缺三类中**任意一类**都被标成「不可计算」且指明缺哪一类，
     recommended_qty 是 None 而非 0（缺失分支真触发，没被默认 0 短路）——
     inventory / logistics / recent_sales 三类各一份夹具、各一条断言，逐一钉死；
  3) 同输入同输出：同一份输入连算两次结果相等（无随机/时间相关隐藏因素）；
  4) 缺数据绝不静默给 0：再次正面钉死 recommended_qty != 0。

fail-then-pass：改动前无 hipop/replenishment/algorithm.py，import 即失败 -> fail；
改动后算法消费步骤1 契约 + 缺失检测点，各份夹具按规则判定一致 -> pass。

跑法：
  python3 tests/smoke_replenish_algorithm.py
  或 make test-replenish-algorithm（已并进 make test）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_known_input_matches_expected():
    """已知输入：算法输出 == 步骤2 专属验收锚点（缺口总量）。"""
    from hipop.replenishment.algorithm import STATUS_OK, compute_replenishment
    from hipop.replenishment.fixtures import load_known_step2_expected

    sku_input, expected = load_known_step2_expected()
    result = compute_replenishment(sku_input)

    assert result.computable, "三类齐全应可计算"
    assert result.status == STATUS_OK
    assert result.missing_classes == [], f"齐全不该有缺失类: {result.missing_classes}"
    # 核心断言：补货量 == 步骤2 专属锚点 gap_total（消费端真读到了算出来的值）。
    # 用步骤2 自己的字段，不挪 main(WS-5) 既有的首批锚点 total_replenish。
    assert result.recommended_qty == expected["gap_total"], (
        f"建议补货量 {result.recommended_qty} != 步骤2 预期缺口 {expected['gap_total']}")
    # 产生端真算了：中间量必须是从三类输入推出来的，不是写死。
    assert result.coverage_qty == 252, f"含在途覆盖应为 252，实际 {result.coverage_qty}"
    assert result.target_qty == 420, f"目标库存应为 420，实际 {result.target_qty}"


def _assert_missing_marked(loader, expected_class):
    """通用断言：缺某一类的夹具被标成不可计算 + 指明缺哪类，且 recommended_qty 为 None。"""
    from hipop.replenishment.algorithm import (
        STATUS_DATA_MISSING,
        compute_replenishment,
    )

    sku_input, declared_missing = loader()
    result = compute_replenishment(sku_input)

    assert not result.computable, f"缺 {expected_class} 不该判为可计算"
    assert result.status == STATUS_DATA_MISSING, f"状态应为数据缺失，实际 {result.status}"
    # 指明缺哪一类，且与夹具自声明一致（缺失检测点真被消费）。
    assert result.missing_classes == [expected_class], (
        f"应标注只缺 {expected_class}，实际 {result.missing_classes}")
    assert declared_missing == expected_class, (
        f"夹具自声明的缺失类 {declared_missing} 与预期 {expected_class} 不符")
    assert declared_missing in result.missing_classes, (
        f"没标出夹具声明缺的类 {declared_missing}: {result.missing_classes}")
    # 死代码短路防线：缺数据是 None（不可计算），绝不是悄悄返回 0。
    assert result.recommended_qty is None, (
        f"缺数据的补货量必须是 None（不可计算），不是 {result.recommended_qty}")
    assert result.recommended_qty != 0, "缺数据绝不能静默给 0"


def test_missing_inventory_marked_not_zero():
    """缺『各仓现有库存 inventory』：标不可计算 + 指明缺 inventory，非返 0。"""
    from hipop.replenishment.contracts import INPUT_INVENTORY
    from hipop.replenishment.fixtures import load_missing_data

    _assert_missing_marked(load_missing_data, INPUT_INVENTORY)


def test_missing_logistics_marked_not_zero():
    """缺『物流 logistics』：标不可计算 + 指明缺 logistics，非返 0。"""
    from hipop.replenishment.contracts import INPUT_LOGISTICS
    from hipop.replenishment.fixtures import load_missing_logistics

    _assert_missing_marked(load_missing_logistics, INPUT_LOGISTICS)


def test_missing_sales_marked_not_zero():
    """缺『近N天销量 recent_sales』：标不可计算 + 指明缺 recent_sales，非返 0。"""
    from hipop.replenishment.contracts import INPUT_SALES
    from hipop.replenishment.fixtures import load_missing_sales

    _assert_missing_marked(load_missing_sales, INPUT_SALES)


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
        test_missing_inventory_marked_not_zero,
        test_missing_logistics_marked_not_zero,
        test_missing_sales_marked_not_zero,
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

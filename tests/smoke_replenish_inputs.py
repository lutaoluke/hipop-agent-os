"""Smoke：补货静态数据层 —— 三类输入契约 + 缺失检测点（WS-5）。

钉死的承重墙：
  1) 「已知输入」夹具被识别为三类齐全（is_complete 真、无缺失类）；
  2) 「数据缺失」夹具被缺失检测点正确判出缺哪一类（缺 inventory，且物流=0 不算缺）；
  3) 检测点真能被消费 —— 这正是算法层显式标注缺数据 SKU 的入口。

fail-then-pass：改动前（无 hipop/replenishment 契约/检测点/夹具）import 即失败 -> fail；
改动后两份夹具按契约判定一致 -> pass。

跑法：
  python3 tests/smoke_replenish_inputs.py
  或 make test-replenish-inputs（已并进 make test）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_known_input_is_complete():
    """已知输入夹具：三类齐全，无缺失。"""
    from hipop.replenishment.contracts import is_complete, missing_input_classes
    from hipop.replenishment.fixtures import load_known_input

    sku_input, expected = load_known_input()
    assert missing_input_classes(sku_input) == [], (
        f"已知输入不该缺任何类，实际缺: {missing_input_classes(sku_input)}")
    assert is_complete(sku_input), "已知输入应判为三类齐全"
    # 人工给定的预期补货量是步骤2 的验收锚点，必须真的在夹具里、能被下游读到。
    assert expected and expected.get("total_replenish", 0) > 0, (
        f"已知输入夹具缺人工预期补货量: {expected!r}")


def test_missing_data_detected_by_detection_point():
    """数据缺失夹具：缺失检测点判出缺『inventory』这一类，且只缺这一类。"""
    from hipop.replenishment.contracts import (
        INPUT_INVENTORY,
        is_complete,
        missing_input_classes,
    )
    from hipop.replenishment.fixtures import load_missing_data

    sku_input, declared_missing = load_missing_data()
    missing = missing_input_classes(sku_input)
    assert missing == [INPUT_INVENTORY], (
        f"缺失检测点应判出只缺 {INPUT_INVENTORY}，实际: {missing}")
    # 检测点判出的缺失类，必须和夹具自声明的缺失类一致 —— 检测点真被消费、真对上。
    assert declared_missing in missing, (
        f"检测点没判出夹具声明缺的类 {declared_missing}: {missing}")
    assert not is_complete(sku_input), "数据缺失夹具不该判为齐全"


def test_present_zero_is_not_missing():
    """边界：物流在途=0/待发=0 是真实数据（present），不能被误判为缺失。

    钉死「占位假数据」死法的另一面 —— 缺失 = 整段没拿到（None），而非值为 0。
    数据缺失夹具的 logistics 全是 0，但它在场，所以缺失里不该出现 logistics。
    """
    from hipop.replenishment.contracts import INPUT_LOGISTICS, missing_input_classes
    from hipop.replenishment.fixtures import load_missing_data

    sku_input, _ = load_missing_data()
    assert sku_input.logistics is not None, "在途=0 的物流是真实数据，不该是 None"
    assert sku_input.logistics.in_transit_qty == 0
    assert INPUT_LOGISTICS not in missing_input_classes(sku_input), (
        "物流值为 0 但在场，不该被判为缺失")


if __name__ == "__main__":
    import traceback

    tests = [
        test_known_input_is_complete,
        test_missing_data_detected_by_detection_point,
        test_present_zero_is_not_missing,
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

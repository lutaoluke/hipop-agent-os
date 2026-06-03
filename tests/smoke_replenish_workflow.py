"""Smoke：补货接线层 —— 三类静态数据 → 可执行入口 → 每 SKU 补货量表（WS-7 步骤3）。

这一层不发明算法，只把步骤1 的契约 + 步骤2 的算法接成一个真能跑的入口：
三类静态数据源（物流 / 销量 / 库存，各按 SKU 索引）进 → join 成每 SKU 三类输入 →
**调用步骤2 的 compute_many** → 出每个 SKU 的补货量表（缺数据的 SKU 被标注）。

钉死的承重墙（DoD + 防三死法）：
  1) 端到端·目标 SKU 真算对：从三类数据夹具走入口，输出表里 TBJ0059A 的
     建议补货量 == 步骤2 专属锚点 gap_total（168，从 known 夹具读，不写魔数）；
  2) 端到端·缺失 SKU 真标注：TBA0210A 在库存源里整段缺省，输出表里它被标成
     不可计算、指明缺 inventory、补货量是 None（不是静默 0）；
  3) 防「接线缺失 / 死代码短路」：用 spy 包住入口依赖的 compute_many，断言
     入口**确实调到了步骤2 的算法**，且表里每一行都来自算法产物
     （computable 行带算法中间量 coverage_qty/target_qty，旁路拼不出来）;
  4) 同输入同输出：同一组静态数据连跑两次，表完全一致。

fail-then-pass：改动前无 hipop/replenishment/workflow.py，import 即失败 -> fail；
改动后入口 join 三源 + 真调步骤2 算法 + 按夹具判定一致 -> pass。

跑法：
  python3 tests/smoke_replenish_workflow.py
  （make test 自动聚合 tests/smoke_*.py，本文件自动并入，无需改 Makefile）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

DATASET_DIR = os.path.join(HERE, "fixtures", "replenish_dataset")
TARGET_SKU = "TBJ0059A"      # 三类齐全，应算出补货量 == gap_total(168)
MISSING_SKU = "TBA0210A"     # 库存源缺省，应标『缺 inventory、不可计算、qty=None』


def _expected_gap_total():
    """从 known 夹具读步骤2 锚点 gap_total，作为目标 SKU 的预期补货量（不写魔数）。"""
    from hipop.replenishment.fixtures import load_known_step2_expected

    _, expected = load_known_step2_expected()
    return expected["gap_total"]


def _row_by_sku(table, sku):
    rows = [r for r in table if r["sku"] == sku]
    assert len(rows) == 1, f"SKU {sku} 在输出表里应恰好一行，实际 {len(rows)}"
    return rows[0]


def test_target_sku_replenish_matches_anchor():
    """端到端：目标 SKU 的补货量 == 步骤2 锚点（消费端真读到算出来的值）。"""
    from hipop.replenishment.workflow import replenish_from_dataset_dir

    table = replenish_from_dataset_dir(DATASET_DIR)
    row = _row_by_sku(table, TARGET_SKU)

    assert row["computable"] is True, f"{TARGET_SKU} 三类齐全应可计算: {row}"
    assert row["recommended_qty"] == _expected_gap_total(), (
        f"{TARGET_SKU} 补货量 {row['recommended_qty']} != 步骤2 锚点 {_expected_gap_total()}")
    assert not row["missing_classes"], f"{TARGET_SKU} 不该有缺失类: {row['missing_classes']}"


def test_missing_sku_is_flagged_not_zeroed():
    """端到端：缺库存的 SKU 被标不可计算 + 指明缺 inventory，补货量 None（非静默 0）。"""
    from hipop.replenishment.contracts import INPUT_INVENTORY
    from hipop.replenishment.workflow import replenish_from_dataset_dir

    table = replenish_from_dataset_dir(DATASET_DIR)
    row = _row_by_sku(table, MISSING_SKU)

    assert row["computable"] is False, f"{MISSING_SKU} 缺库存不该可计算: {row}"
    assert row["missing_classes"] == [INPUT_INVENTORY], (
        f"{MISSING_SKU} 应只缺 {INPUT_INVENTORY}，实际 {row['missing_classes']}")
    assert row["recommended_qty"] is None, (
        f"缺数据 SKU 的补货量必须是 None，不是 {row['recommended_qty']}")
    assert row["recommended_qty"] != 0, "缺数据绝不能静默给 0"


def test_entry_actually_calls_step2_algorithm():
    """防『接线缺失/死代码短路』：spy 证明入口真调到步骤2 的 compute_many，
    且表里 computable 行带算法中间量（旁路拼不出来）。"""
    import hipop.replenishment.workflow as wf
    from hipop.replenishment.algorithm import compute_many as real_compute_many

    calls = {"n": 0, "skus": []}

    def spy(sku_inputs):
        calls["n"] += 1
        calls["skus"].extend(s.sku for s in sku_inputs)
        return real_compute_many(sku_inputs)

    orig = wf.compute_many
    wf.compute_many = spy
    try:
        table = wf.replenish_from_dataset_dir(DATASET_DIR)
    finally:
        wf.compute_many = orig

    assert calls["n"] >= 1, "入口没有调用步骤2 的 compute_many —— 接线缺失（黑屏）"
    assert TARGET_SKU in calls["skus"] and MISSING_SKU in calls["skus"], (
        f"入口没把全部 SKU 喂给算法: {calls['skus']}")
    # 算法中间量只可能由步骤2 真算产生，旁路短路给不出 coverage/target。
    row = _row_by_sku(table, TARGET_SKU)
    assert row.get("coverage_qty") == 252, f"含在途覆盖应为 252，实际 {row.get('coverage_qty')}"
    assert row.get("target_qty") == 420, f"目标库存应为 420，实际 {row.get('target_qty')}"


def test_deterministic_same_dataset_same_table():
    """同输入同输出：同一组静态数据连跑两次，表完全一致。"""
    from hipop.replenishment.workflow import replenish_from_dataset_dir

    t1 = replenish_from_dataset_dir(DATASET_DIR)
    t2 = replenish_from_dataset_dir(DATASET_DIR)
    assert t1 == t2, "同一组静态数据两次跑出的补货表不一致"


if __name__ == "__main__":
    import traceback

    tests = [
        test_target_sku_replenish_matches_anchor,
        test_missing_sku_is_flagged_not_zeroed,
        test_entry_actually_calls_step2_algorithm,
        test_deterministic_same_dataset_same_table,
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

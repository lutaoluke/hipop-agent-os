"""smoke_fact_source_contract.py — WS-129/P0-S1 事实源契约承重墙。

fail-then-pass 验收（与本契约一起落地）：
  ① INVENTORY_TOTAL_COMPONENTS 与 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS 严格一致
     —— 口径单一真相，漂移立即红灯。
  ② NOT_IN_INVENTORY_TOTAL（in_transit_total_qty）不在库存合计里
     —— 在途货物不是可售库存，禁止加入 total_stock。
  ③ 缺时间戳的行不能作为事实 —— assert_data_has_timestamp 对缺失值 raise。
  ④ 缺来源标签的行不能作为事实 —— assert_data_has_source 对缺失值 raise。
  ⑤ 所有权威来源枚举覆盖预期数据类型。
  ⑥ AUTHORITATIVE_SOURCES 中每个来源只能是 SOURCE_NOON 或 SOURCE_ERP（没有第三种）。

守三种死法：
  - 占位假数据：合法行（含时间戳 + 来源）不会被误拒；空时间戳必须被拒。
  - 接线缺失：契约常量缺失或类型错误直接 import 失败/断言失败。
  - 死代码短路：每条验证函数都有正例+反例，无法用空实现绕过。

跑法：
  python3 tests/smoke_fact_source_contract.py
  make test-one F=tests/smoke_fact_source_contract.py
  （也被 make test 自动聚合）
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "hipop" / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "hipop" / "scripts"))


def _expect_raise(fn, exc_type, what):
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"应红灯却没 raise {exc_type.__name__}: {what}")


def test_import():
    """契约模块可 import，核心常量均已定义。"""
    print("== ① 契约模块 import + 核心常量存在 ==")
    import fact_source_contract as C

    assert hasattr(C, "SOURCE_NOON"), "SOURCE_NOON 未定义"
    assert hasattr(C, "SOURCE_ERP"), "SOURCE_ERP 未定义"
    assert hasattr(C, "AUTHORITATIVE_SOURCES"), "AUTHORITATIVE_SOURCES 未定义"
    assert hasattr(C, "INVENTORY_TOTAL_COMPONENTS"), "INVENTORY_TOTAL_COMPONENTS 未定义"
    assert hasattr(C, "NOT_IN_INVENTORY_TOTAL"), "NOT_IN_INVENTORY_TOTAL 未定义"
    assert hasattr(C, "REQUIRED_TIMESTAMPS"), "REQUIRED_TIMESTAMPS 未定义"
    assert hasattr(C, "ContractViolation"), "ContractViolation 未定义"
    assert isinstance(C.INVENTORY_TOTAL_COMPONENTS, tuple), \
        "INVENTORY_TOTAL_COMPONENTS 应为 tuple"
    assert isinstance(C.NOT_IN_INVENTORY_TOTAL, tuple), \
        "NOT_IN_INVENTORY_TOTAL 应为 tuple"
    print(f"  ✓ INVENTORY_TOTAL_COMPONENTS = {C.INVENTORY_TOTAL_COMPONENTS}")
    print(f"  ✓ NOT_IN_INVENTORY_TOTAL = {C.NOT_IN_INVENTORY_TOTAL}")


def test_source_enums_cover_expected_types():
    """AUTHORITATIVE_SOURCES 覆盖销量/noon库存/ERP库存/在途等必要类型，且只用 SOURCE_NOON/SOURCE_ERP。"""
    print("== ② 权威来源枚举覆盖所有必要数据类型 ==")
    import fact_source_contract as C

    expected_types = {
        "noon_inventory", "sales",
        "domestic_inventory", "overseas_inventory",
        "pending_inbound", "in_transit", "logistics_nodes",
    }
    actual_types = set(C.AUTHORITATIVE_SOURCES.keys())
    missing = expected_types - actual_types
    assert not missing, f"AUTHORITATIVE_SOURCES 缺少数据类型: {missing}"

    valid_sources = {C.SOURCE_NOON, C.SOURCE_ERP}
    for dtype, src in C.AUTHORITATIVE_SOURCES.items():
        assert src in valid_sources, \
            f"AUTHORITATIVE_SOURCES['{dtype}'] = '{src}' 不是合法权威来源（应为 noon/erp）"
    print(f"  ✓ {len(C.AUTHORITATIVE_SOURCES)} 个数据类型均已映射到 noon/erp")

    # 关键来源检查
    assert C.AUTHORITATIVE_SOURCES["noon_inventory"] == C.SOURCE_NOON, \
        "noon 官方仓库存权威源必须是 noon"
    assert C.AUTHORITATIVE_SOURCES["sales"] == C.SOURCE_NOON, \
        "销量权威源必须是 noon"
    assert C.AUTHORITATIVE_SOURCES["domestic_inventory"] == C.SOURCE_ERP, \
        "国内仓库存权威源必须是 ERP"
    assert C.AUTHORITATIVE_SOURCES["in_transit"] == C.SOURCE_ERP, \
        "国际在途权威源必须是 ERP"
    print("  ✓ noon 库存/销量→noon；国内仓/在途→ERP")


def test_inventory_total_matches_merge():
    """INVENTORY_TOTAL_COMPONENTS 必须与 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS 严格一致。"""
    print("== ③ INVENTORY_TOTAL_COMPONENTS 与 merge_stock_snapshot_v2 口径一致 ==")
    import fact_source_contract as C
    from hipop.scripts import merge_stock_snapshot_v2 as merge

    # 正例：调用验证函数，应不 raise
    try:
        C.assert_inventory_total_matches_merge(merge.TOTAL_STOCK_COMPONENTS)
    except C.ContractViolation as e:
        raise AssertionError(
            f"INVENTORY_TOTAL_COMPONENTS 与 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS"
            f" 口径不一致（WS-129 契约漂移）：{e}"
        )
    print(f"  ✓ 两处口径一致：{sorted(C.INVENTORY_TOTAL_COMPONENTS)}")

    # 反例：篡改一个列表看是否能检测到漂移
    fake_components = list(merge.TOTAL_STOCK_COMPONENTS) + ["some_extra_col"]
    _expect_raise(
        lambda: C.assert_inventory_total_matches_merge(fake_components),
        C.ContractViolation,
        "加了额外列应触发 ContractViolation",
    )
    print("  ✓ 反例：多一列触发 ContractViolation（漂移检测有效）")

    fake_components2 = [c for c in merge.TOTAL_STOCK_COMPONENTS if c != "yiwu_qty"]
    _expect_raise(
        lambda: C.assert_inventory_total_matches_merge(fake_components2),
        C.ContractViolation,
        "少一列应触发 ContractViolation",
    )
    print("  ✓ 反例：少一列触发 ContractViolation（漂移检测有效）")


def test_in_transit_not_in_inventory_total():
    """in_transit_total_qty 不在库存合计里；若错误加入必须红灯。"""
    print("== ④ in_transit_total_qty 不在库存合计里（在途单列）==")
    import fact_source_contract as C
    from hipop.scripts import merge_stock_snapshot_v2 as merge

    # 正例：当前 merge 不含 in_transit，应通过
    for col in C.NOT_IN_INVENTORY_TOTAL:
        assert col not in merge.TOTAL_STOCK_COMPONENTS, \
            f"'{col}' 不应在 merge.TOTAL_STOCK_COMPONENTS 里（在途不得计入库存合计）"
    try:
        C.assert_in_transit_not_in_inventory_total(merge.TOTAL_STOCK_COMPONENTS)
    except C.ContractViolation as e:
        raise AssertionError(f"在途未计入库存合计却报 ContractViolation：{e}")
    print(f"  ✓ {C.NOT_IN_INVENTORY_TOTAL} 均不在合计里")

    # 反例：若强行加入 in_transit，必须红灯
    with_transit = list(merge.TOTAL_STOCK_COMPONENTS) + ["in_transit_total_qty"]
    _expect_raise(
        lambda: C.assert_in_transit_not_in_inventory_total(with_transit),
        C.ContractViolation,
        "在途被加入库存合计应触发 ContractViolation",
    )
    print("  ✓ 反例：在途加入合计触发 ContractViolation（在途单列规则有效）")


def test_no_timestamp_is_red_light():
    """缺时间戳的数据不能作为事实：assert_data_has_timestamp 对空值 raise。"""
    print("== ⑤ 缺时间戳的数据不能作为事实 ==")
    import fact_source_contract as C

    # 反例 1：时间戳字段为 None
    _expect_raise(
        lambda: C.assert_data_has_timestamp({"noon_total_qty": 10}, "imported_at", "noon库存"),
        C.ContractViolation,
        "imported_at=None 应红灯",
    )
    # 反例 2：时间戳字段为空字符串
    _expect_raise(
        lambda: C.assert_data_has_timestamp({"noon_total_qty": 10, "imported_at": ""}, "imported_at"),
        C.ContractViolation,
        "imported_at='' 应红灯",
    )
    # 反例 3：时间戳字段不存在（键缺失）
    _expect_raise(
        lambda: C.assert_data_has_timestamp({}, "updated_at", "ERP库存"),
        C.ContractViolation,
        "键缺失应红灯",
    )
    # 正例：时间戳有效
    try:
        C.assert_data_has_timestamp(
            {"noon_total_qty": 10, "imported_at": "2026-06-08 10:00:00"},
            "imported_at", "noon库存"
        )
    except C.ContractViolation as e:
        raise AssertionError(f"有效时间戳不应红灯：{e}")
    print("  ✓ None / '' / 键缺失 → 红灯；有效时间戳 → 通过")


def test_no_source_is_red_light():
    """缺来源标签的数据不能作为事实：assert_data_has_source 对空值/未知值 raise。"""
    print("== ⑥ 缺来源标签的数据不能作为事实 ==")
    import fact_source_contract as C

    # 反例 1：source 为 None
    _expect_raise(
        lambda: C.assert_data_has_source({"qty": 10}, "source", "午仓库存"),
        C.ContractViolation,
        "source=None 应红灯",
    )
    # 反例 2：来源标签未知
    _expect_raise(
        lambda: C.assert_data_has_source({"qty": 10, "source": "unknown_system"}, "source"),
        C.ContractViolation,
        "未知来源应红灯",
    )
    # 正例 noon
    try:
        C.assert_data_has_source(
            {"qty": 10, "source": "noon"},
            "source", "noon官方仓"
        )
    except C.ContractViolation as e:
        raise AssertionError(f"source=noon 不应红灯：{e}")
    # 正例 erp
    try:
        C.assert_data_has_source(
            {"qty": 10, "source": "erp"},
            "source", "ERP国内仓"
        )
    except C.ContractViolation as e:
        raise AssertionError(f"source=erp 不应红灯：{e}")
    print("  ✓ None / 未知 → 红灯；noon / erp → 通过")


def test_validate_stock_row():
    """validate_stock_row：noon 数据有 imported_at 合格，没有时间戳 raise 问题。"""
    print("== ⑦ validate_stock_row：noon 数据必须携带 imported_at ==")
    import fact_source_contract as C

    # 正例：noon_total_qty 非 NULL + imported_at 有值
    row_ok = {
        "partner_sku": "TSKUA",
        "noon_total_qty": 50,
        "noon_saleable_qty": 48,
        "imported_at": "2026-06-08 10:00:00",
        "updated_at": "2026-06-08 10:00:00",
        "total_stock": 50,
    }
    problems = C.validate_stock_row(row_ok)
    assert not problems, f"合规行不应有问题: {problems}"
    print("  ✓ 合规行（noon_total_qty + imported_at）无问题")

    # 反例：noon_total_qty 非 NULL 但 imported_at 为 NULL
    row_bad = {
        "partner_sku": "TSKUB",
        "noon_total_qty": 30,
        "imported_at": None,
        "updated_at": "2026-06-08 10:00:00",
        "total_stock": 30,
    }
    problems_bad = C.validate_stock_row(row_bad)
    assert any("imported_at" in p for p in problems_bad), \
        f"noon 有数据但 imported_at 为 NULL 应报问题: {problems_bad}"
    print("  ✓ noon_total_qty 非 NULL 但 imported_at=NULL → 问题被识别")

    # 边界：noon_total_qty 为 NULL（ERP-only 行）可以没有 imported_at
    row_erp_only = {
        "partner_sku": "TSKUC",
        "noon_total_qty": None,
        "yiwu_qty": 100,
        "imported_at": None,
        "updated_at": "2026-06-08 09:00:00",
        "total_stock": 100,
    }
    problems_erp = C.validate_stock_row(row_erp_only)
    assert not problems_erp, \
        f"ERP-only 行（noon_total_qty=NULL）不应被误报 noon 问题: {problems_erp}"
    print("  ✓ ERP-only 行（noon_total_qty=NULL）不被误报")


def main():
    failures = []
    tests = [
        test_import,
        test_source_enums_cover_expected_types,
        test_inventory_total_matches_merge,
        test_in_transit_not_in_inventory_total,
        test_no_timestamp_is_red_light,
        test_no_source_is_red_light,
        test_validate_stock_row,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failures.append((fn.__name__, str(e)))
            print(f"  ✗ {fn.__name__}: {e}")
        print()

    if failures:
        print(f"✗ {len(failures)} 项失败: {[n for n,_ in failures]}")
        return 1
    print("✓ 事实源契约 smoke 全过（WS-129）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

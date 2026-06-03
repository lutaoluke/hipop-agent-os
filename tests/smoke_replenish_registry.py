"""Smoke：补货 workflow 真注册进触发面 —— registry/runner 能解析触发（WS-7 返工）。

验门人打回点不是代码质量，是**接线缺失的另一层**：步骤3 的入口 `workflow.py`
原本只能单跑（自身 smoke/CLI），真实触发面（UI/chat/scheduler 经 `/run-workflow`
→ runner）解析不到它。本 smoke 把这条接线钉死：

  1) `wf4_replenish_suggest` 真在 `WORKFLOW_REGISTRY` 里 —— 否则 `/run-workflow`
     直接 400「unknown workflow」，UI/chat/scheduler 根本触发不了；
  2) 它的 step callable 路径能被 runner 的 `_resolve_callable` 解析成真 callable
     （不是写了个解析不到的死字符串）；
  3) 经 registry 解析出来的 callable 真跑通到**步骤2 算法**：调它产出的补货表里，
     目标 SKU 补货量 == 步骤2 锚点 168、缺库存 SKU 被标注不可计算（qty=None）。
     —— 证明「registry → runner → 解析 callable → 步骤2 算法」整条触发路径活的，
     而不是注册了一个空壳/旁路（防「接线缺失 / 死代码短路」死法）。

fail-then-pass：去掉 `WORKFLOW_REGISTRY` 里的 `wf4_replenish_suggest` 项时，
test_registered 红（KeyError/断言失败）→ 接上后绿。

跑法：
  python3 tests/smoke_replenish_registry.py
  （make test 自动聚合 tests/smoke_*.py，本文件自动并入，无需改 Makefile）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

WORKFLOW_NAME = "wf4_replenish_suggest"
TARGET_SKU = "TBJ0059A"
MISSING_SKU = "TBA0210A"


def _expected_gap_total():
    from hipop.replenishment.fixtures import load_known_step2_expected

    _, expected = load_known_step2_expected()
    return expected["gap_total"]


def test_registered_in_workflow_registry():
    """补货 workflow 真在 WORKFLOW_REGISTRY 里（否则 /run-workflow 400，触发不到）。"""
    import hipop.server.api as api

    assert WORKFLOW_NAME in api.WORKFLOW_REGISTRY, (
        f"{WORKFLOW_NAME} 不在 WORKFLOW_REGISTRY → /run-workflow 会 400「unknown workflow」，"
        f"UI/chat/scheduler 触发不到。现有: {list(api.WORKFLOW_REGISTRY)}")
    label, steps, affected = api.WORKFLOW_REGISTRY[WORKFLOW_NAME]
    assert steps, f"{WORKFLOW_NAME} 没有 step，runner 无活可跑"
    assert "replenish" in affected, f"affected_modules 应含 replenish，实际 {affected}"


def test_step_callable_resolves_via_runner():
    """runner 的 _resolve_callable 能把 step 路径解析成真 callable（非死字符串）。"""
    import hipop.server.api as api

    _, steps, _ = api.WORKFLOW_REGISTRY[WORKFLOW_NAME]
    path = steps[0][2]
    fn = api._resolve_callable(path)  # 解析不到会直接抛 ImportError/AttributeError
    assert callable(fn), f"step 路径 {path} 没解析成 callable"


def test_triggered_callable_runs_through_step2_algorithm():
    """经 registry 解析出的 callable 真跑通到步骤2 算法：补货量对、缺失被标注。

    用 spy 包住步骤2 的 compute_many，证明触发路径确实调到算法，而非旁路拼输出。
    """
    import hipop.server.api as api
    import hipop.replenishment.workflow as wf
    from hipop.replenishment.algorithm import compute_many as real_compute_many

    _, steps, _ = api.WORKFLOW_REGISTRY[WORKFLOW_NAME]
    fn = api._resolve_callable(steps[0][2])

    called = {"n": 0}

    def spy(sku_inputs):
        called["n"] += 1
        return real_compute_many(sku_inputs)

    orig = wf.compute_many
    wf.compute_many = spy
    try:
        table = fn(tenant_id=1)  # runner 对接受 tenant_id 的 callable 就是这么调的
    finally:
        wf.compute_many = orig

    assert called["n"] >= 1, "触发路径没调到步骤2 的 compute_many —— 接线缺失（空壳注册）"

    by_sku = {r["sku"]: r for r in table}
    assert by_sku[TARGET_SKU]["recommended_qty"] == _expected_gap_total(), (
        f"经触发路径算出的 {TARGET_SKU} 补货量 {by_sku[TARGET_SKU]['recommended_qty']} "
        f"!= 步骤2 锚点 {_expected_gap_total()}")
    assert by_sku[MISSING_SKU]["computable"] is False, f"{MISSING_SKU} 缺库存应标不可计算"
    assert by_sku[MISSING_SKU]["recommended_qty"] is None, "缺数据 SKU 不能静默给 0"


if __name__ == "__main__":
    import traceback

    tests = [
        test_registered_in_workflow_registry,
        test_step_callable_resolves_via_runner,
        test_triggered_callable_runs_through_step2_algorithm,
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

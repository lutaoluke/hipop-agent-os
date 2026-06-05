"""Smoke: WS-74 假活门 — 声称查询/拉取但无工具调用证据时 _safety 应拦。

fail-then-pass 结构：
- 改前：新增的 _check_fake_query_claims 不存在 → test_*_caught 类测试 FAIL
- 改后：规则写入 _safety + tool_log 传入 → 全绿
- 回归：已有 export_table/run_workflow/feishu 检测不受影响
"""
import os, sys, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from hipop.server import _safety

# ── 新规则：声称查了商品但无工具证据 ─────────────────────────────────────

def test_fake_query_no_tool_caught():
    """完全没调工具，声称查了商品 → 必须拦。"""
    _, warns = _safety.sanitize_reply("我查了你的商品，总共有 1798 个 SKU。", [])
    assert any("hallucinate" in w or "查询" in w or "list_products" in w for w in warns), \
        f"声称查商品但无工具调用未被拦: {warns}"

def test_fake_query_count_only_caught():
    """只调了 list_products(limit=0)（仅计数），声称查了商品 → 必须拦。"""
    tool_log = [{"name": "list_products", "args": {"store": "KSA", "limit": 0}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的商品，总共有 1798 个 SKU。", ["list_products"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "查询" in w or "list_products" in w for w in warns), \
        f"只做计数但声称查商品未被拦: {warns}"

def test_real_query_limit_positive_passes():
    """真调了 list_products(limit=20)（有实际数据），声称查了商品 → 放行。"""
    tool_log = [{"name": "list_products", "args": {"store": "KSA", "limit": 20}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的商品，总共有 1798 个 SKU，以下是前 20 条。", ["list_products"], tool_log=tool_log
    )
    assert not any("list_products" in w or "查询" in w for w in warns), \
        f"真实查询被误报: {warns}"

def test_sku_query_passes():
    """调了 query_sku，声称查了 SKU → 放行。"""
    _, warns = _safety.sanitize_reply(
        "我查了这个 SKU，库存还有 30 件。", ["query_sku"]
    )
    assert not any("query_sku" in w for w in warns), f"合法 query_sku 被误报: {warns}"

def test_count_statement_no_claim_passes():
    """只说了总数，没有完成态动词 → 不触发。"""
    tool_log = [{"name": "list_products", "args": {"limit": 0}}]
    _, warns = _safety.sanitize_reply(
        "店铺共有 1798 个 SKU。", ["list_products"], tool_log=tool_log
    )
    assert not any("list_products" in w for w in warns), \
        f"纯计数陈述被误触发: {warns}"

def test_order_query_fake_caught():
    """声称查了货单但没调 query_order → 拦。"""
    _, warns = _safety.sanitize_reply("我查了这个货单，状态正常。", [])
    assert any("query_order" in w or "hallucinate" in w or "货单" in w for w in warns), \
        f"声称查货单但无工具调用未被拦: {warns}"

def test_order_query_real_passes():
    """真调了 query_order → 放行。"""
    _, warns = _safety.sanitize_reply("我查了这个货单，状态正常。", ["query_order"])
    assert not any("query_order" in w for w in warns), f"合法 query_order 被误报: {warns}"

# ── 回归：已有规则不受影响 ────────────────────────────────────────────────

def test_regression_export_table_still_works():
    _, warns = _safety.sanitize_reply("已为你导出 Excel。", [])
    assert any("export_table" in w or "导出" in w for w in warns), "export_table 检测被破坏"

def test_regression_run_workflow_still_works():
    _, warns = _safety.sanitize_reply("已触发物流刷新。", [])
    assert any("run_workflow" in w for w in warns), "run_workflow 检测被破坏"


if __name__ == "__main__":
    tests = [
        test_fake_query_no_tool_caught,
        test_fake_query_count_only_caught,
        test_real_query_limit_positive_passes,
        test_sku_query_passes,
        test_count_statement_no_claim_passes,
        test_order_query_fake_caught,
        test_order_query_real_passes,
        test_regression_export_table_still_works,
        test_regression_run_workflow_still_works,
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

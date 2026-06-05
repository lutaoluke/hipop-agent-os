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

# ── 其他真实查询工具：scope_overview/compute_replenishment/data_health_check 均放行 ──

def test_scope_overview_passes():
    """调了 scope_overview（真实数据查询），说了查了数据 → 放行（非假活）。"""
    _, warns = _safety.sanitize_reply(
        "我查了一下你的店铺数据，当前共有 1,048 个在售 SKU，20 个急速下降。",
        ["scope_overview"]
    )
    assert not any("hallucinate" in w or ("查询" in w and "list_products" in w) for w in warns), \
        f"scope_overview 合法查询被误报为假活: {warns}"

def test_compute_replenishment_passes():
    """调了 compute_replenishment（真实计算），说了查了数据 → 放行。"""
    _, warns = _safety.sanitize_reply(
        "我查了一下补货数据，建议补货的 SKU 共 15 个。",
        ["compute_replenishment"]
    )
    assert not any("hallucinate" in w or ("查询" in w and "list_products" in w) for w in warns), \
        f"compute_replenishment 合法查询被误报为假活: {warns}"

def test_data_health_check_passes():
    """调了 data_health_check（真实查询），说了查了数据 → 放行。"""
    _, warns = _safety.sanitize_reply(
        "我查了一下你的数据，最新 imported_at 是 2026-06-03。",
        ["data_health_check"]
    )
    assert not any("hallucinate" in w or ("查询" in w and "list_products" in w) for w in warns), \
        f"data_health_check 合法查询被误报为假活: {warns}"

# ── 回归：已有规则不受影响 ────────────────────────────────────────────────

def test_regression_export_table_still_works():
    _, warns = _safety.sanitize_reply("已为你导出 Excel。", [])
    assert any("export_table" in w or "导出" in w for w in warns), "export_table 检测被破坏"

def test_regression_run_workflow_still_works():
    _, warns = _safety.sanitize_reply("已触发物流刷新。", [])
    assert any("run_workflow" in w for w in warns), "run_workflow 检测被破坏"

# ── 显式白名单：非查询工具不能作为查询证据 ────────────────────────────────

def test_export_table_not_proof_of_query():
    """export_table 不能作为'查了商品'的证据。"""
    tool_log = [{"name": "export_table", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的商品，库存都正常。", ["export_table"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "查询" in w for w in warns), \
        f"export_table 被当成查询证据，漏拦: {warns}"

def test_run_workflow_not_proof_of_query():
    """run_workflow 不能作为'查了数据'的证据。"""
    tool_log = [{"name": "run_workflow", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了一下数据，都正常。", ["run_workflow"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "查询" in w for w in warns), \
        f"run_workflow 被当成查询证据，漏拦: {warns}"

def test_notify_feishu_not_proof_of_query():
    """notify_via_feishu 不能作为'查了SKU'的证据。"""
    tool_log = [{"name": "notify_via_feishu", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了这个SKU，库存有30件。", ["notify_via_feishu"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "query_sku" in w or "查询" in w for w in warns), \
        f"notify_via_feishu 被当成查询证据，漏拦: {warns}"


# ── 对象级证据：查询工具必须能证明声明对象 ────────────────────────────────

def test_query_order_not_proof_of_product_inventory():
    """query_order 不能作为'查了商品/库存'的证据。"""
    tool_log = [{"name": "query_order", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的商品，库存都正常。", ["query_order"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"query_order 被当成商品/库存查询证据，漏拦: {warns}"


def test_data_health_not_proof_of_sku_inventory():
    """data_health_check 不能作为'查了具体 SKU 库存'的证据。"""
    tool_log = [{"name": "data_health_check", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了这个SKU，库存有30件。", ["data_health_check"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"data_health_check 被当成 SKU/库存查询证据，漏拦: {warns}"


def test_scope_overview_not_proof_of_sku_inventory():
    """scope_overview 不能作为'查了具体 SKU 库存'的证据。"""
    tool_log = [{"name": "scope_overview", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了这个SKU，库存有30件。", ["scope_overview"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"scope_overview 被当成 SKU/库存查询证据，漏拦: {warns}"


def test_data_health_not_proof_when_broad_data_claim_wraps_sku_inventory():
    """data_health_check 不能用'查了数据'包装具体 SKU 库存结论。"""
    tool_log = [{"name": "data_health_check", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，这个SKU库存有30件。", ["data_health_check"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"data_health_check 用宽泛数据声明包装 SKU 库存，漏拦: {warns}"


def test_scope_overview_not_proof_when_broad_data_claim_wraps_product_inventory():
    """scope_overview 不能用'查了数据'包装商品库存正常结论。"""
    tool_log = [{"name": "scope_overview", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了一下数据，商品库存都正常。", ["scope_overview"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"scope_overview 用宽泛数据声明包装商品库存，漏拦: {warns}"


def test_replenishment_not_proof_when_broad_data_claim_wraps_sku_inventory():
    """compute_replenishment 不能用'查了数据'包装具体 SKU 库存结论。"""
    tool_log = [{"name": "compute_replenishment", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了一下补货数据，这个SKU库存有30件。", ["compute_replenishment"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"compute_replenishment 用宽泛数据声明包装 SKU 库存，漏拦: {warns}"


# ── 第5轮：SKU + 量词 + 库存后缀语序 ─────────────────────────────────────

def test_sku_quantity_suffix_inventory_caught():
    """'SKU ABC 有 30 件库存' — 库存为后缀语序，data_health_check 不能作证据。"""
    tool_log = [{"name": "data_health_check", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，SKU ABC 有 30 件库存。", ["data_health_check"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"SKU+量词+库存后缀语序漏拦: {warns}"


def test_sku_quantity_haiyou_suffix_inventory_caught():
    """'SKU ABC 还有 30 件库存' — 还有变体，data_health_check 不能作证据。"""
    tool_log = [{"name": "data_health_check", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，SKU ABC 还有 30 件库存。", ["data_health_check"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"SKU+还有+库存后缀语序漏拦: {warns}"


def test_sku_quantity_suffix_inventory_with_scope_overview_caught():
    """scope_overview 不能用'查了数据'包装 'SKU ABC 有 30 件库存' 结论。"""
    tool_log = [{"name": "scope_overview", "args": {}}]
    _, warns = _safety.sanitize_reply(
        "我查了一下数据，SKU ABC 有 30 件库存。", ["scope_overview"], tool_log=tool_log
    )
    assert any("hallucinate" in w or "list_products" in w or "query_sku" in w for w in warns), \
        f"scope_overview 用宽泛数据包装 SKU 量词后缀库存，漏拦: {warns}"


def test_real_query_sku_with_suffix_inventory_passes():
    """真调了 query_sku，'SKU ABC 还有 30 件库存' → 放行。"""
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，SKU ABC 还有 30 件库存。", ["query_sku"]
    )
    assert not any("hallucinate" in w or "list_products" in w for w in warns), \
        f"query_sku 合法证据被误报: {warns}"


# ── 第6轮（码长指定）：货品/货 + N件库存后缀，及通用 N件库存 ─────────────

def test_sku_plus_id_inventory_caught():
    """SKU+标识符+有N件库存，data_health_check 不能作证据 → 必须拦。"""
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，SKU ABC 有 30 件库存。",
        ["data_health_check"],
        tool_log=[{"name": "data_health_check", "args": {}}],
    )
    assert any("hallucinate" in w or "查询" in w or "list_products" in w for w in warns), \
        f"SKU+标识符库存声明未被拦: {warns}"


def test_n_jian_kucun_suffix_caught():
    """N件库存后缀写法（货品+还有+N件库存），compute_replenishment 不能作证据 → 必须拦。"""
    _, warns = _safety.sanitize_reply(
        "我查了补货数据，这批货还有 50 件库存。",
        ["compute_replenishment"],
        tool_log=[{"name": "compute_replenishment", "args": {}}],
    )
    assert any("hallucinate" in w or "查询" in w or "list_products" in w for w in warns), \
        f"N件库存后缀写法未被拦: {warns}"


def test_sku_id_real_query_passes():
    """SKU+标识符+有N件库存，真调了 query_sku → 放行。"""
    _, warns = _safety.sanitize_reply(
        "我查了你的数据，SKU ABC 有 30 件库存。",
        ["query_sku"],
    )
    assert not any("list_products" in w or "query_sku" in w for w in warns), \
        f"合法 query_sku 被误报: {warns}"


# ── 第7轮（码长指定）：_is_substantive_action 直接覆盖 ──────────────────────
# fail-then-pass 说明：函数已在 _safety.py 定义，故 import 即可通过；
# 若函数被删或逻辑退化，断言立即 FAIL。

def test_is_substantive_action_count_only_false():
    """list_products(limit=0) 单独调用 → _is_substantive_action 返回 False。"""
    tool_log = [{"name": "list_products", "args": {"limit": 0}}]
    assert not _safety._is_substantive_action(tool_log), \
        "limit=0 应返回 False（只计数，非真执行）"


def test_is_substantive_action_limit_positive_true():
    """list_products(limit=20) → _is_substantive_action 返回 True。"""
    tool_log = [{"name": "list_products", "args": {"limit": 20}}]
    assert _safety._is_substantive_action(tool_log), \
        "limit>0 应返回 True（有行返回，是真执行）"


def test_is_substantive_action_other_tool_true():
    """export_table（非 list_products）→ _is_substantive_action 返回 True。"""
    tool_log = [{"name": "export_table", "args": {}}]
    assert _safety._is_substantive_action(tool_log), \
        "非 list_products 工具应返回 True"


def test_is_substantive_action_mixed_true():
    """list_products(limit=0) + export_table 混合 → _is_substantive_action 返回 True。"""
    tool_log = [
        {"name": "list_products", "args": {"limit": 0}},
        {"name": "export_table", "args": {}},
    ]
    assert _safety._is_substantive_action(tool_log), \
        "混合工具（含非 list_products）应返回 True"


# ── 第8轮：GPT provider raw JSON string args 跨 provider 兼容 ────────────────

def test_is_substantive_action_gpt_string_args_positive():
    """GPT provider 传 string args with limit>0 → True（真实查询，非计数）。"""
    tool_log = [{"name": "list_products", "args": '{"store":"KSA","limit":20}'}]
    assert _safety._is_substantive_action(tool_log), \
        "GPT string args limit=20 应为 True（真实查询）"


def test_is_substantive_action_gpt_string_args_zero():
    """GPT provider 传 string args with limit=0 → False（只计数，非真执行）。"""
    tool_log = [{"name": "list_products", "args": '{"store":"KSA","limit":0}'}]
    assert not _safety._is_substantive_action(tool_log), \
        "GPT string args limit=0 应为 False（只计数）"


if __name__ == "__main__":
    tests = [
        test_fake_query_no_tool_caught,
        test_fake_query_count_only_caught,
        test_real_query_limit_positive_passes,
        test_sku_query_passes,
        test_count_statement_no_claim_passes,
        test_order_query_fake_caught,
        test_order_query_real_passes,
        test_scope_overview_passes,
        test_compute_replenishment_passes,
        test_data_health_check_passes,
        test_regression_export_table_still_works,
        test_regression_run_workflow_still_works,
        test_export_table_not_proof_of_query,
        test_run_workflow_not_proof_of_query,
        test_notify_feishu_not_proof_of_query,
        test_query_order_not_proof_of_product_inventory,
        test_data_health_not_proof_of_sku_inventory,
        test_scope_overview_not_proof_of_sku_inventory,
        test_data_health_not_proof_when_broad_data_claim_wraps_sku_inventory,
        test_scope_overview_not_proof_when_broad_data_claim_wraps_product_inventory,
        test_replenishment_not_proof_when_broad_data_claim_wraps_sku_inventory,
        test_sku_quantity_suffix_inventory_caught,
        test_sku_quantity_haiyou_suffix_inventory_caught,
        test_sku_quantity_suffix_inventory_with_scope_overview_caught,
        test_real_query_sku_with_suffix_inventory_passes,
        test_sku_plus_id_inventory_caught,
        test_n_jian_kucun_suffix_caught,
        test_sku_id_real_query_passes,
        test_is_substantive_action_count_only_false,
        test_is_substantive_action_limit_positive_true,
        test_is_substantive_action_other_tool_true,
        test_is_substantive_action_mixed_true,
        test_is_substantive_action_gpt_string_args_positive,
        test_is_substantive_action_gpt_string_args_zero,
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

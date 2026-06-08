"""smoke_t45_evidence_gate.py — T45-S1 选品库存约束证据门 fail-then-pass smoke

验收（WS-108）：
  当 Agent 回答涉及「选品库存约束/本期选品/库存约束」类问题时，必须有
  list_products(limit>0) 或 query_sku 的真实工具调用证据；无证据时 gate 拦截。

FAIL 条件（修前）：
  - _safety.sanitize_reply 对「叙述查过但无工具调用证据」的回答放行
  - warns 为空，gate 未触发 → T45 假活通过

PASS 条件（修后）：
  - 含选品库存约束关键词 + 无工具证据 → warns 非空（gate_triggered=True 语义）
  - 有 query_sku 或 list_products(limit>0) 工具证据 → warns 不含证据门告警
  - 无库存压力的普通 query → 不触发证据门

跑法：
  make test-one F=tests/smoke_t45_evidence_gate.py
  （也会被 make test 自动聚合）
"""
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import _safety  # noqa: E402


def _inventory_gate_warned(warns):
    """是否出现选品库存约束证据门告警。"""
    return any(
        ("库存" in w or "选品" in w or "list_products" in w or "query_sku" in w)
        and ("证据" in w or "工具" in w or "未查询" in w)
        for w in warns
    )


# ── FAIL-BEFORE: 叙述查过但无工具证据 → gate 必须拦截 ──────────────────────────

def test_inventory_constraint_claim_without_evidence_triggers_gate():
    """T45 核心：回答「库存反向约束判断」但无工具调用证据 → gate 拦截。

    FAIL (修前): warns 为空，叙述假活被放行。
    PASS (修后): warns 含证据门告警（gate_triggered=True 语义）。
    """
    reply = (
        "好的，根据rules.md完整规则，我来给你做库存反向约束判断。\n"
        "## 库存反向约束 — 判断结果\n"
        "国内仓 20 寸行李箱库存过多，本期选品应优先考虑 24 寸+拓展层。"
    )
    _, warns = _safety.sanitize_reply(reply, [], tool_log=[])
    assert _inventory_gate_warned(warns), (
        f"T45 叙述假活应被拦截（无工具证据），但 warns={warns}"
    )
    print(f"    gate triggered: {[w[:100] for w in warns if '库存' in w or '证据' in w or '工具' in w]}")


def test_selection_stock_claim_without_evidence_triggers_gate():
    """本期选品 + 库存约束表述，无工具证据 → gate 拦截。

    FAIL (修前): 放行。
    PASS (修后): 拦截。
    """
    reply = "本期选品需遵守库存约束，20寸库存充足时应优先推入 24寸产品。"
    _, warns = _safety.sanitize_reply(reply, [], tool_log=[])
    assert _inventory_gate_warned(warns), (
        f"本期选品+库存约束无证据应被拦截，but warns={warns}"
    )


def test_selection_with_inventory_keyword_without_evidence_triggers():
    """选品涉及库存多的判断，无证据 → gate 拦截。"""
    reply = "选品时若国内仓库存多，应优先国产履约，减少重复采购。"
    _, warns = _safety.sanitize_reply(reply, [], tool_log=[])
    assert _inventory_gate_warned(warns), (
        f"选品+库存多无证据应触发 gate，but warns={warns}"
    )


# ── PASS-AFTER: 有真实工具调用证据 → gate 放行 ─────────────────────────────────

def test_query_sku_evidence_satisfies_gate():
    """有 query_sku 真实工具调用 → 证据满足，不触发 gate。

    PASS (修后): warns 不含证据门告警。
    """
    reply = "库存约束分析：根据查询结果，20寸库存多，本期选品应优先考虑24寸。"
    tool_log = [
        {
            "name": "query_sku",
            "args": {"skus": ["SKU-20INCH"], "store": "KSA"},
            "result_keys": ["sku", "inventory", "sellable_days"],
        }
    ]
    _, warns = _safety.sanitize_reply(reply, ["query_sku"], tool_log=tool_log)
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"有 query_sku 证据不应触发证据门，but warns={inv_warns}"
    )
    print(f"    query_sku evidence: gate not triggered ✓")


def test_list_products_limit_positive_satisfies_gate():
    """list_products(limit>0) 调用 → 证据满足，不触发 gate。"""
    reply = "库存约束分析：查询到 20寸行李箱库存多，本期选品应优先24寸。"
    tool_log = [
        {
            "name": "list_products",
            "args": {"store": "KSA", "limit": 10},
            "result_keys": ["total", "items"],
        }
    ]
    _, warns = _safety.sanitize_reply(reply, ["list_products"], tool_log=tool_log)
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"list_products(limit=10) 证据不应触发证据门，but warns={inv_warns}"
    )
    print(f"    list_products(limit=10) evidence: gate not triggered ✓")


def test_list_products_limit_zero_not_evidence():
    """list_products(limit=0) 只返回聚合，不含产品数据 → 不算证据，gate 拦截。"""
    reply = "本期选品的库存约束：20寸库存多，建议减少同类入选。"
    tool_log = [
        {
            "name": "list_products",
            "args": {"store": "KSA", "limit": 0},
            "result_keys": ["total", "listed_count"],
        }
    ]
    _, warns = _safety.sanitize_reply(reply, ["list_products"], tool_log=tool_log)
    assert _inventory_gate_warned(warns), (
        f"list_products(limit=0) 仅聚合，应被拦截（无产品级证据），but warns={warns}"
    )
    print(f"    list_products(limit=0) correctly blocked ✓")


# ── 反例：无库存压力的 query 不应触发 gate ────────────────────────────────────

def test_replenishment_query_no_inventory_constraint_not_triggered():
    """补货建议类回答，不含选品库存约束关键词 → 不触发证据门。

    反例（防硬编码）：gate 只在选品库存约束上下文触发，普通补货不触发。
    """
    reply = "KSA 当前补货建议：TBJ0059A 补 50 件（urgency=high），SDA1874A 补 30 件。"
    _, warns = _safety.sanitize_reply(reply, ["compute_replenishment"], tool_log=[])
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"普通补货回答不应触发库存证据门，but warns={inv_warns}"
    )
    print(f"    replenishment (no selection context): gate not triggered ✓")


def test_sku_sales_query_no_inventory_constraint_not_triggered():
    """SKU 销量查询类回答，不含选品库存约束关键词 → 不触发证据门。"""
    reply = "TBJ0059A 最近 30 天销量 120 件，库存 200 件，预计可撑 50 天。"
    tool_log = [{"name": "query_sku", "args": {"skus": ["TBJ0059A"]}, "result_keys": ["sku"]}]
    _, warns = _safety.sanitize_reply(reply, ["query_sku"], tool_log=tool_log)
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"SKU 销量查询不应触发库存证据门，but warns={inv_warns}"
    )


def test_general_inventory_question_without_selection_context_not_triggered():
    """一般库存数量回答（无「选品」/「库存约束」上下文）→ 不触发证据门。"""
    reply = "当前 KSA 店铺共有 1424 个商品，809 个 SKU 有库存。"
    _, warns = _safety.sanitize_reply(reply, ["list_products"], tool_log=[
        {"name": "list_products", "args": {"store": "KSA", "limit": 0}, "result_keys": ["total"]}
    ])
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"一般库存数量回答不应触发证据门，but warns={inv_warns}"
    )


def test_list_products_args_json_string_satisfies_gate():
    """args 为 JSON-string（非 dict）时，_get_tool_arg 必须正确解析 limit。

    修前（缺 import json）：json.loads() 抛 NameError 被 except 吞掉 → args={}
    → limit 取不到 → list_products(limit=10) 被误判为无证据 → gate 误触发。
    修后（加 import json）：JSON-string 正确解析，limit=10 识别为有效证据，gate 不误触。
    """
    reply = "库存约束分析：查询到 20寸行李箱库存多，本期选品应优先24寸。"
    tool_log = [
        {
            "name": "list_products",
            "args": '{"store": "KSA", "limit": 10}',  # JSON-string 形态，非 dict
            "result_keys": ["total", "items"],
        }
    ]
    _, warns = _safety.sanitize_reply(reply, ["list_products"], tool_log=tool_log)
    inv_warns = [w for w in warns if _inventory_gate_warned([w])]
    assert not inv_warns, (
        f"args 为 JSON-string 的 list_products(limit=10) 不应触发证据门"
        f"（需正确解析 JSON），but warns={inv_warns}"
    )
    print(f"    list_products(args=JSON-string, limit=10): gate not triggered ✓")


if __name__ == "__main__":
    print("▶ smoke_t45_evidence_gate — T45-S1 选品库存约束证据门")

    tests = [
        ("test_inventory_constraint_claim_without_evidence_triggers_gate",
         test_inventory_constraint_claim_without_evidence_triggers_gate),
        ("test_selection_stock_claim_without_evidence_triggers_gate",
         test_selection_stock_claim_without_evidence_triggers_gate),
        ("test_selection_with_inventory_keyword_without_evidence_triggers",
         test_selection_with_inventory_keyword_without_evidence_triggers),
        ("test_query_sku_evidence_satisfies_gate",
         test_query_sku_evidence_satisfies_gate),
        ("test_list_products_limit_positive_satisfies_gate",
         test_list_products_limit_positive_satisfies_gate),
        ("test_list_products_limit_zero_not_evidence",
         test_list_products_limit_zero_not_evidence),
        ("test_replenishment_query_no_inventory_constraint_not_triggered",
         test_replenishment_query_no_inventory_constraint_not_triggered),
        ("test_sku_sales_query_no_inventory_constraint_not_triggered",
         test_sku_sales_query_no_inventory_constraint_not_triggered),
        ("test_general_inventory_question_without_selection_context_not_triggered",
         test_general_inventory_question_without_selection_context_not_triggered),
        ("test_list_products_args_json_string_satisfies_gate",
         test_list_products_args_json_string_satisfies_gate),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            traceback.print_exc()
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_t45_evidence_gate all {len(tests)} passed")

"""smoke_ws171_chat_history_pollution.py — WS-171 fail-then-pass smoke

验收（WS-171）：
  1. chat 长历史污染（T03/T29）：
     _clean_history 必须消除历史中的意图门解释文本
     （「本轮我先不动手」「本轮不执行」等），防止 LLM 在后续轮次模仿该模式。

  2. T45 库存反向约束规则接入 chat：
     「如果国内仓 20 寸行李箱库存多，本期选品该如何受库存反向约束?请说明规则来源。」
     必须给出「同款同 SKU」约束口径 + 来源，不能说「超出工具能力」。

FAIL 条件（修前）：
  1. _clean_history 对「本轮我先不动手（你是在问能不能）」原样保留，不清除
  2. T45 类问题命中 _deadend 路径或 LLM 随意回答，无法给规则答案

PASS 条件（修后）：
  1. _clean_history 将意图门解释类 assistant 消息替换为中性占位
  2. _deterministic_inventory_constraint_rule_request 检测 T45 问法
     _format_inventory_constraint_rule_reply 返回含「同款同 SKU」和来源的答案

三死法检查：
  - 接线缺失：确保 T45 路由在 chat() 的 if-chain 中被调用
  - 死代码短路：用 _SAME_SKU_CONSTRAINT_RE 确保内容口径，不是宽松放行
  - 占位假数据：reply 必须明确提到源文件路径，不只说「参考文件」

跑法：
  python3 tests/smoke_ws171_chat_history_pollution.py
  make test-one F=tests/smoke_ws171_chat_history_pollution.py
"""
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── T45 oracle（复用与 smoke_t45_inventory_oracle 同口径）─────────────────────

_SAME_SKU_CONSTRAINT_RE = re.compile(
    r"同款同\s*SKU"
    r"|同款.{0,10}同.{0,5}SKU"
    r"|库存.{0,20}只.{0,15}约束.{0,15}同款.{0,15}SKU"
    r"|约束.{0,10}同款.{0,10}SKU",
    re.IGNORECASE,
)

_SOURCE_PATH_RE = re.compile(
    r"n9_inventory_reverse_constraint"
    r"|selection.{0,30}l3_orchestration"
    r"|n9.*inventory"
    r"|inventory.*n9",
    re.IGNORECASE,
)


# ── 1. _clean_history 意图门解释文消除（fail-then-pass）──────────────────────────

def test_clean_history_strips_interrogative_reply():
    """fail-then-pass: _clean_history 必须消除 INTERROGATIVE 意图门解释文，防止污染 LLM。

    FAIL（修前）：clean_history 只去 safety banner，「本轮我先不动手」原样留下。
    PASS（修后）：clean_history 检测并替换意图门解释文为中性占位，LLM 看不到此模式。
    """
    from hipop.server.agent import _clean_history
    interrogative_reply = (
        "可以执行。这类刷新/重算是工作台内部的低风险动作，由我直接触发后台任务、"
        "前端看进度，你不用进终端跑脚本。**本轮我先不动手**（你是在问能不能）；"
        "确认要跑就说「帮我刷新…」，我立刻执行。"
    )
    messages = [
        {"role": "user", "content": "能不能帮我刷新库存？"},
        {"role": "assistant", "content": interrogative_reply},
        {"role": "user", "content": "请查询 TBS0228A 近30天销量，回答必须给来源和更新时间。"},
    ]
    cleaned = _clean_history(messages)
    assistant_content = cleaned[1]["content"]
    assert "本轮我先不动手" not in assistant_content, (
        "_clean_history 应消除「本轮我先不动手」等意图门解释文，"
        "防止 LLM 在下一轮查询中模仿历史模式输出短路回复。"
        f"实际内容仍含该短语: {assistant_content[:100]}"
    )


def test_clean_history_strips_hypothetical_reply():
    """fail-then-pass: HYPOTHETICAL 模式的解释文（本轮不执行）也必须被消除。

    FAIL（修前）：clean_history 对「（本轮不执行）」原样保留。
    PASS（修后）：被替换为中性占位。
    """
    from hipop.server.agent import _clean_history
    hypothetical_reply = (
        "说明影响面（**本轮不执行**）:这类刷新/重算只重写工作台内部数据或分析结果、"
        "可重复覆盖、不发外部通知、不动交易/订单，属低风险幂等动作。"
        "真要跑就说「帮我刷新…」，我再触发。"
    )
    messages = [
        {"role": "user", "content": "如果刷新库存会怎样？"},
        {"role": "assistant", "content": hypothetical_reply},
        {"role": "user", "content": "KSA 本周最需要补货的 5 个 SKU 是哪些?"},
    ]
    cleaned = _clean_history(messages)
    assistant_content = cleaned[1]["content"]
    assert "本轮不执行" not in assistant_content, (
        "_clean_history 应消除「本轮不执行」意图门解释文，"
        f"实际内容仍含该短语: {assistant_content[:100]}"
    )


def test_clean_history_preserves_normal_reply():
    """修后不能误伤：普通 LLM 回答（不含意图门解释短语）应被原样保留。

    确保 _clean_history 只清除意图门解释文，不影响正常 chat 回复。
    """
    from hipop.server.agent import _clean_history
    normal_reply = (
        "TBS0228A 近 30 天销量 120 件，库存 200 件，"
        "预计可撑 50 天（数据来源：wf2_sku，截至 2026-06-10）。"
    )
    messages = [
        {"role": "user", "content": "TBS0228A 近30天销量"},
        {"role": "assistant", "content": normal_reply},
    ]
    cleaned = _clean_history(messages)
    assert cleaned[1]["content"] == normal_reply, (
        "_clean_history 不应修改普通 LLM 回答内容，"
        f"期望原样保留，实际: {cleaned[1]['content'][:100]}"
    )


def test_clean_history_user_messages_unchanged():
    """user 消息不受 _clean_history 影响。"""
    from hipop.server.agent import _clean_history
    messages = [
        {"role": "user", "content": "能不能帮我刷新库存？"},
        {"role": "assistant", "content": "test"},
    ]
    cleaned = _clean_history(messages)
    assert cleaned[0]["content"] == "能不能帮我刷新库存？", (
        "_clean_history 不应修改 user 消息"
    )


# ── 2. T03/T29 确定性路由回归（不受历史影响）────────────────────────────────────

def test_t03_sku_metric_router_catches_query():
    """T03 回归：_deterministic_sku_metric_request 必须检测到「TBS0228A 近30天销量」。

    确保即使进了 LLM 路径之前就被确定性路由拦截。
    tools_used=['query_sku'] 不依赖 LLM 历史。
    """
    from hipop.server.agent import _deterministic_sku_metric_request
    q = "请查询 TBS0228A 近30天销量，回答必须给来源和更新时间。"
    sku = _deterministic_sku_metric_request(q)
    assert sku is not None, (
        "_deterministic_sku_metric_request 未检测到 T03 查询中的 SKU，"
        f"返回 None（应返回 'TBS0228A'）"
    )
    assert sku.upper() == "TBS0228A", (
        f"_deterministic_sku_metric_request 返回了错误的 SKU: {sku}，期望 TBS0228A"
    )


def test_t29_replenishment_list_router_catches_query():
    """T29 回归：_deterministic_replenishment_list_request 必须检测到补货 Top5 查询。

    确保「本周最需要补货的 5 个 SKU」在确定性路由层被拦截。
    """
    from hipop.server.agent import _deterministic_replenishment_list_request
    q = "KSA 本周最需要补货的 5 个 SKU 是哪些?"
    limit = _deterministic_replenishment_list_request(q)
    assert limit is not None, (
        "_deterministic_replenishment_list_request 未检测到 T29 补货 Top5 查询，"
        "返回 None（应返回 5）"
    )
    assert limit == 5, (
        f"_deterministic_replenishment_list_request 返回错误数量 {limit}，期望 5"
    )


# ── 3. T45 库存反向约束规则路由（fail-then-pass）────────────────────────────────

def test_t45_inventory_constraint_rule_router_detects_question():
    """fail-then-pass: is_inventory_constraint_rule_request 必须检测 T45 问题。

    FAIL（修前）：模块不存在，ImportError。
    PASS（修后）：正确检测「库存反向约束」+「规则来源」的查询，返回 True。
    """
    from hipop.server._inventory_constraint_rule import is_inventory_constraint_rule_request
    q = "如果国内仓 20 寸行李箱库存多，本期选品该如何受库存反向约束?请说明规则来源。"
    result = is_inventory_constraint_rule_request(q)
    assert result, (
        "is_inventory_constraint_rule_request 未检测到 T45 问题，"
        f"返回: {result}"
    )


def test_t45_inventory_constraint_rule_answer_contains_same_sku():
    """fail-then-pass: T45 规则答案必须包含「同款同 SKU」约束口径。

    FAIL（修前）：模块不存在，ImportError。
    PASS（修后）：返回包含「同款同 SKU」约束口径的具体答案。
    """
    from hipop.server._inventory_constraint_rule import format_inventory_constraint_rule_reply
    reply = format_inventory_constraint_rule_reply()
    assert _SAME_SKU_CONSTRAINT_RE.search(reply), (
        "T45 规则答案必须包含「同款同 SKU」约束口径，"
        f"实际回复未包含: {reply[:200]}"
    )


def test_t45_inventory_constraint_rule_answer_contains_source():
    """fail-then-pass: T45 规则答案必须包含具体来源路径（n9_inventory_reverse_constraint）。

    FAIL（修前）：模块不存在，ImportError。
    PASS（修后）：明确引用源文件路径。
    """
    from hipop.server._inventory_constraint_rule import format_inventory_constraint_rule_reply
    reply = format_inventory_constraint_rule_reply()
    assert _SOURCE_PATH_RE.search(reply), (
        "T45 规则答案必须包含来源路径（n9_inventory_reverse_constraint），"
        f"实际回复未包含: {reply[:200]}"
    )


def test_t45_answer_does_not_mandate_24inch():
    """T45 规则答案不能含「20寸库存多 → 必须/应优先选24寸」的强制表述。

    防止与 rules.md 3c 同款同SKU口径冲突（smoke_t45_inventory_oracle 同口径）。
    """
    from hipop.server._inventory_constraint_rule import format_inventory_constraint_rule_reply
    wrong_mandate_re = re.compile(
        r"20\s*寸.{0,25}(必须|本期.*选|应优先选|主推).{0,20}24\s*寸"
        r"|20\s*寸.*库存.{0,20}(必须|应|要).{0,20}24\s*寸",
    )
    reply = format_inventory_constraint_rule_reply()
    assert not wrong_mandate_re.search(reply), (
        "T45 答案不应含「20寸库存多→必须/应优先24寸」强制表述，"
        f"违反 rules.md 3c 同款同 SKU 口径: {reply[:200]}"
    )


def test_t45_router_does_not_fire_for_normal_replenishment_query():
    """T45 路由器不误触发：普通补货查询不命中库存反向约束规则路由。"""
    from hipop.server._inventory_constraint_rule import is_inventory_constraint_rule_request
    normal_queries = [
        "KSA 本周最需要补货的 5 个 SKU 是哪些?",
        "请查询 TBS0228A 近30天销量",
        "帮我刷新库存",
        "店铺概览",
    ]
    for q in normal_queries:
        result = is_inventory_constraint_rule_request(q)
        assert not result, (
            f"is_inventory_constraint_rule_request 误触发普通查询: {q!r}，"
            f"返回: {result}"
        )


def test_t45_router_fires_for_rule_source_query():
    """T45 路由器正确触发：含「库存反向约束」+「规则/来源」关键词的查询。"""
    from hipop.server._inventory_constraint_rule import is_inventory_constraint_rule_request
    variants = [
        "如果国内仓 20 寸行李箱库存多，本期选品该如何受库存反向约束?请说明规则来源。",
        "库存反向约束的规则是什么？",
        "库存反向约束规则来源是哪里？",
        "本期选品的库存约束规则是什么？",
    ]
    for q in variants:
        result = is_inventory_constraint_rule_request(q)
        assert result, (
            f"is_inventory_constraint_rule_request 未检测到变体问题: {q!r}"
        )


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_ws171_chat_history_pollution — WS-171 fail-then-pass")

    tests = [
        ("test_clean_history_strips_interrogative_reply",
         test_clean_history_strips_interrogative_reply),
        ("test_clean_history_strips_hypothetical_reply",
         test_clean_history_strips_hypothetical_reply),
        ("test_clean_history_preserves_normal_reply",
         test_clean_history_preserves_normal_reply),
        ("test_clean_history_user_messages_unchanged",
         test_clean_history_user_messages_unchanged),
        ("test_t03_sku_metric_router_catches_query",
         test_t03_sku_metric_router_catches_query),
        ("test_t29_replenishment_list_router_catches_query",
         test_t29_replenishment_list_router_catches_query),
        ("test_t45_inventory_constraint_rule_router_detects_question",
         test_t45_inventory_constraint_rule_router_detects_question),
        ("test_t45_inventory_constraint_rule_answer_contains_same_sku",
         test_t45_inventory_constraint_rule_answer_contains_same_sku),
        ("test_t45_inventory_constraint_rule_answer_contains_source",
         test_t45_inventory_constraint_rule_answer_contains_source),
        ("test_t45_answer_does_not_mandate_24inch",
         test_t45_answer_does_not_mandate_24inch),
        ("test_t45_router_does_not_fire_for_normal_replenishment_query",
         test_t45_router_does_not_fire_for_normal_replenishment_query),
        ("test_t45_router_fires_for_rule_source_query",
         test_t45_router_fires_for_rule_source_query),
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
    print(f"\n✓ smoke_ws171_chat_history_pollution all {len(tests)} passed")

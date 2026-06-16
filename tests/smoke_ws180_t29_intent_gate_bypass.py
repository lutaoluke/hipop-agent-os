"""smoke_ws180_t29_intent_gate_bypass.py — WS-180 fail-then-pass smoke

验收（WS-180）：
  T29「Top 补货建议」在长历史 session 中被意图门短路的修复。

根因：
  1. _deterministic_replenishment_list_request 的 SKU 守卫 regex 把「Top5/Top10」
     (uppercase → TOP5/TOP10) 误判为业务 SKU 代码，返回 None。
  2. agent.py 意图门缺少 T29 bypass（只有 T03 的 _deterministic_sku_metric_request bypass）。
  当问题含「更新时间」（触发 is_refresh_time_query → has_refresh_trigger=True + INTERROGATIVE）
  且形如「补货建议更新时间是什么，给我看 Top5」时，两项缺陷叠加导致意图门错误短路，
  tools_used=[], judge_method=execution_intent_gate_explain_non_executory。

FAIL 条件（修前）：
  1. _deterministic_replenishment_list_request("KSA 补货建议更新时间是什么，给我看 Top5") == None
  2. chat() + 含「更新时间」的补货 Top5 问法 → judge_method=execution_intent_gate_explain_non_executory

PASS 条件（修后）：
  1. _deterministic_replenishment_list_request 把 Top5/Top10 视为 TopN 而非 SKU，返回 5
  2. chat() 走 deterministic_replenishment_list_router，tools_used=['compute_replenishment']

三死法检查：
  - 接线缺失：验证 agent.py 的意图门 if-block 中含 T29 bypass（通过全路径 chat() 测试）
  - 死代码短路：test_t29_negative_control 确保真执行/刷新 refresh 请求仍被正确处理
  - 占位假数据：chat() result 必须含真正 judge_method，不是 magic string

跑法：
  python3 tests/smoke_ws180_t29_intent_gate_bypass.py
  make test-one F=tests/smoke_ws180_t29_intent_gate_bypass.py
"""
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── 1. SKU regex 修复（fail-then-pass）────────────────────────────────────────

def test_t29_top5_not_treated_as_sku():
    """fail-then-pass: Top5 在补货 Top-N 查询中不得被 SKU regex 误判为业务 SKU。

    FAIL（修前）：r"\\b[A-Z]{2,}[A-Z0-9_]*\\d[A-Z0-9_]*\\b" 匹配 TOP5，返回 None。
    PASS（修后）：TOP\\d+ 模式被排除，函数返回 5。
    """
    from hipop.server.agent import _deterministic_replenishment_list_request
    q = "KSA 补货建议更新时间是什么，给我看 Top5"
    limit = _deterministic_replenishment_list_request(q)
    assert limit is not None, (
        "修前（FAIL）: _deterministic_replenishment_list_request 把「Top5」误判为业务 SKU，"
        "返回 None。需修复 SKU 守卫 regex，排除 TOP\\d+ 模式。"
    )
    assert limit == 5, (
        f"_deterministic_replenishment_list_request 应返回 5（Top5），实际: {limit}"
    )


def test_t29_top10_variant_not_treated_as_sku():
    """Top10 变体同样不误触发 SKU 守卫。"""
    from hipop.server.agent import _deterministic_replenishment_list_request
    q = "本周该补货的 Top10 SKU 有哪些"
    limit = _deterministic_replenishment_list_request(q)
    assert limit is not None, (
        "_deterministic_replenishment_list_request 不应把 TOP10 当 SKU，返回 None。"
        f"实际: {limit}"
    )
    assert limit == 10, f"应返回 10，实际: {limit}"


def test_t29_real_sku_still_blocked():
    """负控制：含真实业务 SKU（字母+数字+字母）的补货查询仍被 SKU 守卫拦截，返回 None。"""
    from hipop.server.agent import _deterministic_replenishment_list_request
    q = "TBU0010A 这个 SKU 补货建议 Top5"
    limit = _deterministic_replenishment_list_request(q)
    assert limit is None, (
        f"含真实 SKU TBU0010A 的问题应返回 None（走单 SKU 路由），实际: {limit}"
    )


# ── 2. 全路径 chat() 意图门 bypass 测试（fail-then-pass）──────────────────────

def test_t29_chat_update_time_with_top5_bypasses_intent_gate():
    """fail-then-pass: chat() + 含「更新时间」补货 Top5 → 意图门不短路，走 compute_replenishment。

    FAIL（修前）：
      - judge_method='execution_intent_gate_explain_non_executory'
      - tools_used=[]
      - 根因①: _deterministic_replenishment_list_request("...Top5") == None（SKU regex bug）
      - 根因②: agent.py 意图门缺 T29 bypass

    PASS（修后）：
      - judge_method='deterministic_replenishment_list_router'
      - 'compute_replenishment' in tools_used
    """
    from hipop.server import agent as _agent
    from hipop.server import _provider as _prov

    interrogative_reply = (
        "可以执行。这类刷新/重算是工作台内部的低风险动作，由我直接触发后台任务。"
        "**本轮我先不动手**（你是在问能不能）；"
        "确认要跑就说「帮我刷新…」，我立刻执行。"
    )
    messages = [
        {"role": "user", "content": "能不能帮我刷新库存？"},
        {"role": "assistant", "content": interrogative_reply},
        {"role": "user", "content": "KSA 补货建议更新时间是什么，给我看 Top5"},
    ]
    scope = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}

    fake_replenishment_result = {
        "ok": True,
        "store": "KSA",
        "items": [
            {"sku": "TBS0001A", "gap": 50, "priority": "high"},
            {"sku": "TBS0002B", "gap": 40, "priority": "high"},
            {"sku": "TBS0003C", "gap": 30, "priority": "medium"},
            {"sku": "TBS0004D", "gap": 20, "priority": "medium"},
            {"sku": "TBS0005E", "gap": 10, "priority": "low"},
        ],
        "as_of": "2026-06-12",
        "references": [],
    }

    with patch.object(_agent, "_exec_tool", return_value=fake_replenishment_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(messages, scope)

    judge = result.get("judge_method", "")
    tools = result.get("tools_used") or []

    assert judge == "deterministic_replenishment_list_router", (
        f"T29 含「更新时间」的补货 Top5 应走 deterministic_replenishment_list_router，"
        f"实际 judge_method={judge!r}。"
        f"修前根因：意图门缺 T29 bypass + TOP5 被误判为 SKU → 短路 tools_used=[]。"
        f" reply={result.get('reply', '')[:120]}"
    )
    assert "compute_replenishment" in tools, (
        f"T29 chat() 必须调用 compute_replenishment，实际 tools_used={tools}"
    )


def test_t29_chat_without_polluted_history():
    """回归保护：无污染历史时含「更新时间」的 T29 同样走 deterministic_replenishment_list_router。"""
    from hipop.server import agent as _agent
    from hipop.server import _provider as _prov

    messages = [
        {"role": "user", "content": "KSA 补货建议更新时间是什么，给我看 Top5"},
    ]
    scope = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}

    fake_replenishment_result = {
        "ok": True,
        "store": "KSA",
        "items": [{"sku": "TBS0001A", "gap": 50, "priority": "high"}],
        "as_of": "2026-06-12",
        "references": [],
    }

    with patch.object(_agent, "_exec_tool", return_value=fake_replenishment_result), \
         patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(messages, scope)

    judge = result.get("judge_method", "")
    tools = result.get("tools_used") or []
    assert judge == "deterministic_replenishment_list_router", (
        f"T29 无污染历史也应走 deterministic_replenishment_list_router，实际={judge!r}"
    )
    assert "compute_replenishment" in tools


# ── 3. 负控制（修前修后都必须 PASS）────────────────────────────────────────────

def test_t29_negative_interrogative_refresh_still_gated():
    """负控制：询问式刷新补货建议（无 Top-N 意图）仍被意图门正确拦截，不执行。

    「能不能帮我刷新补货建议？」→ refresh verb（刷新）+ INTERROGATIVE → 门应 fire。
    此处 _deterministic_replenishment_list_request 因含「刷新」refresh verb 返回 None，
    所以 T29 bypass 不生效，门正常拦截。
    """
    from hipop.server import agent as _agent
    from hipop.server import _provider as _prov

    messages = [
        {"role": "user", "content": "能不能帮我刷新补货建议？"},
    ]
    scope = {"tenant_id": 1, "current_user": "test", "current_role": "admin", "store": "KSA"}

    with patch.object(_prov, "get_provider", return_value="smoke"):
        result = _agent.chat(messages, scope)

    judge = result.get("judge_method", "")
    tools = result.get("tools_used") or []
    assert judge == "execution_intent_gate_explain_non_executory", (
        f"询问式刷新补货建议应被意图门拦截（非执行意图），实际 judge={judge!r}，tools={tools}"
    )
    assert tools == [], f"意图门拦截后 tools_used 应为空，实际: {tools}"


def test_t29_negative_real_sku_not_caught_by_replenishment_list():
    """负控制：含真实 SKU 的补货查询走单 SKU 路由，不走 replenishment list 路由。

    「TBU0010A 补货建议 Top5」→ _deterministic_replenishment_list_request 因 SKU 守卫返回 None，
    走 _deterministic_replenishment_sku_request 路径。T29 bypass 不影响此行为。
    """
    from hipop.server.agent import _deterministic_replenishment_list_request
    from hipop.server.agent import _deterministic_replenishment_sku_request
    q = "TBU0010A 补货建议 Top5"
    assert _deterministic_replenishment_list_request(q) is None, (
        "含真实 SKU TBU0010A 的问题，replenishment_list 应返回 None（走 SKU 路由）"
    )
    sku = _deterministic_replenishment_sku_request(q)
    assert sku is not None and "TBU0010A" in sku.upper(), (
        f"含 TBU0010A 的问题应被 replenishment_sku_request 检出，实际: {sku}"
    )


# ── 4. source/time_window 口径回退守卫（WS-180 round-2 live graded regression）──────
# 背景：PR #118 旧分支落后 main 8 个 commit，live graded 门红在 correct_source /
# correct_time_window。根因不是 T29 逻辑劫持销量类问题，而是旧分支缺 main 的确定性
# 销量/窗口 TopN 路由（WS-120 等）。下面三个守卫把「T29 加宽的补货 TopN 匹配器绝不
# 吞掉销量 TopN / 窗口 TopN 问题」钉成确定性断言——这正是 source/time_window 口径不
# 回退的承重墙：销量类问题必须留给各自的确定性工具（correct_source 才拿真 tool 证据），
# 不能被误路由到 compute_replenishment（错 tool → source=0 → time_window=0）。

def test_t29_does_not_hijack_sales_topn():
    """守卫：裸销量 TopN（近30天销量最高的N个）不得被补货 TopN 匹配器吞掉。"""
    from hipop.server.agent import _deterministic_replenishment_list_request as rl
    from hipop.server._deterministic_routes import (
        _deterministic_product_sales_topn_request as st,
        _deterministic_window_sales_topn_request as wt,
    )
    # 核心承重墙：补货清单匹配器对销量 TopN 一律 None（不劫持 → 不回退 source/time_window）。
    no_hijack = ("KSA 近30天销量最高的3个商品", "近30天销量 Top10",
                 "KSA 销量排行 Top5", "近30天销量最高的5个商品")
    for q in no_hijack:
        assert rl(q) is None, (
            f"销量 TopN 问题不得进补货清单路由（会回退 source/time_window 口径）："
            f"{q!r} → rl={rl(q)!r}"
        )
    # 附加正向：典型销量 TopN 仍由销量/窗口确定性路由接住，correct_source 拿真 tool 证据。
    for q in ("KSA 近30天销量最高的3个商品", "近30天销量 Top10"):
        assert st(q) or wt(q), f"销量 TopN 问题应被销量/窗口 TopN 确定性路由接住：{q!r}"


def test_t29_does_not_hijack_window_sales_topn():
    """守卫：指定窗口销量 TopN 不得被补货 TopN 匹配器吞掉（时间窗口口径承重墙）。"""
    from hipop.server.agent import _deterministic_replenishment_list_request as rl
    from hipop.server._deterministic_routes import (
        _deterministic_window_sales_topn_request as wt,
    )
    for q in ("KSA 2026-05-01 到 2026-05-31 销量 Top5", "过去30天卖得最好的5个 SKU"):
        assert rl(q) is None, f"窗口销量 TopN 不得进补货清单路由：{q!r} → rl={rl(q)!r}"
        assert wt(q), f"窗口销量 TopN 应被窗口确定性路由接住：{q!r}"


def test_t29_replenishment_match_is_narrow():
    """守卫：加宽只命中「补货触发词 + 纯 TopN（无真 SKU）」，不放大到无补货意图的问题。"""
    from hipop.server.agent import _deterministic_replenishment_list_request as rl
    assert rl("Top5 是什么意思") is None
    assert rl("给我看 Top10") is None
    assert rl("本周必补 Top5") == 5
    assert rl("TBS0228A 补货建议 Top5") is None


# ── main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_ws180_t29_intent_gate_bypass — WS-180 fail-then-pass")

    tests = [
        ("test_t29_top5_not_treated_as_sku", test_t29_top5_not_treated_as_sku),
        ("test_t29_top10_variant_not_treated_as_sku", test_t29_top10_variant_not_treated_as_sku),
        ("test_t29_real_sku_still_blocked", test_t29_real_sku_still_blocked),
        ("test_t29_chat_update_time_with_top5_bypasses_intent_gate",
         test_t29_chat_update_time_with_top5_bypasses_intent_gate),
        ("test_t29_chat_without_polluted_history", test_t29_chat_without_polluted_history),
        ("test_t29_negative_interrogative_refresh_still_gated",
         test_t29_negative_interrogative_refresh_still_gated),
        ("test_t29_negative_real_sku_not_caught_by_replenishment_list",
         test_t29_negative_real_sku_not_caught_by_replenishment_list),
        ("test_t29_does_not_hijack_sales_topn", test_t29_does_not_hijack_sales_topn),
        ("test_t29_does_not_hijack_window_sales_topn",
         test_t29_does_not_hijack_window_sales_topn),
        ("test_t29_replenishment_match_is_narrow",
         test_t29_replenishment_match_is_narrow),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: UNEXPECTED {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'PASS' if failed == 0 else 'FAIL'} — {passed}/{passed + failed} tests passed")
    sys.exit(0 if failed == 0 else 1)

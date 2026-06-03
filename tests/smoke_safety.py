"""Smoke: WS-55 反幻觉黑名单误报修复 — 合法字段名/措辞放行，真幻觉仍拦。

背景
----
test-chat 全局门把两类**合法措辞**当成幻觉误报：
  1. `可撑天数` = 真实字段 `sellable_days` 的中文人话说法，deepseek 描述补货页时
     自然带出 → 被 _safety / 黑名单当幻觉拦（还会二次回贴告警再踩一次）。
  2. 散文里提到旧表名 `wf3_logistics`，但**实际 tool 调用是 wf3_logistics_v2（正确）**
     → reply 正则 `wf3_logistics(?!_v2)` 误报。

WS-55(方案 A，Luke sign-off) 精修黑名单：合法字段名/措辞**放行**，黑名单只在
"真被当成编造字段（与真幻觉字段同框）/ 真选错老 workflow"时触发 —— 修误报=纠正性
提升，不是挖空反幻觉门。

fail-then-pass（钉死本修复，防回退 + 防把门挖空）
-----------------------------------------------
- 改动前：`_safety.HALLUCINATED_FIELDS` 含 `可撑天数` → 单独合法提及被误报 →
  test_legit_* FAIL。
- 改动前：smoke_chat 用 reply 正则 `wf3_logistics(?!_v2)` → 散文提旧名（真跑 v2）
  被误报 → test_wf3_prose_* FAIL。
- 改动后：合法提及放行、真幻觉字段 / 真选错老 workflow 仍拦 → 全 PASS。

跑法：python3 tests/smoke_safety.py （也会被 `make test` 自动聚合）
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # repo root → import hipop.server
sys.path.insert(0, HERE)                      # tests/ → import smoke_chat

from hipop.server import _safety  # noqa: E402
import smoke_chat                  # noqa: E402


def _field_warned(warns):
    """是否出现『字段不在 wf2/wf5 表中』这条幻觉告警。"""
    return any("字段" in w and "不在" in w for w in warns)


# ── 可撑天数：真实字段 sellable_days 的人话，合法提及必须放行 ──────────────
def test_legit_sellable_days_with_anchor_passes():
    reply, warns = _safety.sanitize_reply(
        "TBJ0059A 当前可撑天数约 30 天（即 sellable_days 字段），建议关注。", [])
    assert not any("可撑天数" in w for w in warns), \
        f"合法提及『可撑天数』被误报为幻觉: {warns}"
    assert not reply.startswith("⚠️"), f"不该加幻觉 banner: {reply[:80]}"


def test_legit_sellable_days_no_anchor_passes():
    # deepseek 描述补货页时自然带出，不一定同时写 sellable_days —— 仍应放行
    reply, warns = _safety.sanitize_reply("这个 SKU 可撑天数还有 12 天。", [])
    assert not any("可撑天数" in w for w in warns), f"误报: {warns}"


# ── 真幻觉字段：无任何真实字段背书，必须仍拦（不挖空门）────────────────────
def test_real_hallucinated_field_still_flagged():
    _, warns = _safety.sanitize_reply("该 SKU 海运ROI预估 18%，推荐物流方式空运。", [])
    assert _field_warned(warns), f"真幻觉字段未被拦: {warns}"


def test_alias_in_fabrication_context_still_flagged():
    # 可撑天数 出现在"编造字段堆"里（与真幻觉字段同框）→ 整段仍被拦
    _, warns = _safety.sanitize_reply(
        "字段汇总：海运ROI预估 18%、可撑天数 30 天、weekly_priority 高。", [])
    assert _field_warned(warns), f"编造字段上下文应被拦: {warns}"


# ── 回归：精确时间戳幻觉守卫不受影响 ─────────────────────────────────────
def test_timestamp_guard_intact():
    _, warns = _safety.sanitize_reply("数据更新于 2026-06-03T12:00:00Z。", [])
    assert any("时间戳" in w for w in warns), f"时间戳守卫被破坏: {warns}"


# ── smoke_chat: wf3 选 workflow 用真实 workflow_task 判定，不再靠散文正则 ──
def _wf3_case():
    return next(c for c in smoke_chat.CASES if "刷新物流" in c.name)


def test_wf3_prose_mention_with_correct_v2_passes():
    # Agent 散文里提了旧表名，但真跑的是 v2 → 应 PASS（修误报）
    c = _wf3_case()
    resp = {
        "reply": "好的，我用物流采集工作流（内部表 wf3_logistics_hub_v2，"
                 "旧称 wf3_logistics）帮你刷新。",
        "tools_used": ["run_workflow"],
        "workflow_task": {"workflow": "wf3_logistics_v2"},
    }
    ok, reasons = smoke_chat.check(c, resp)
    assert ok, f"散文提旧名但真跑 v2 被误报: {reasons}"


def test_wf3_real_old_workflow_selection_still_fails():
    # 真选错：实际触发老 wf3_logistics（非 v2）→ 必须 FAIL（不挖空门）
    c = _wf3_case()
    resp = {
        "reply": "已触发物流刷新。",
        "tools_used": ["run_workflow"],
        "workflow_task": {"workflow": "wf3_logistics"},
    }
    ok, reasons = smoke_chat.check(c, resp)
    assert not ok, "真选错老 workflow 应被拦，但通过了"


def test_wf3_no_workflow_triggered_still_fails():
    c = _wf3_case()
    resp = {"reply": "好的。", "tools_used": [], "workflow_task": None}
    ok, _ = smoke_chat.check(c, resp)
    assert not ok, "没真触发 workflow 应 FAIL"


def test_wf3_wrong_v2_workflow_still_fails():
    # 门2 红队洞：endswith("_v2") 会放行任意 v2。精确等值后，Agent 跑错别的 v2
    # 工作流（wf6_alerts_v2 / wf2_products_v2）在"刷新物流"这条 case 下必须 FAIL。
    c = _wf3_case()
    for wrong in ("wf6_alerts_v2", "wf2_products_v2", "wf5_sales_cycle_v2"):
        resp = {
            "reply": "已触发物流刷新。",
            "tools_used": ["run_workflow"],
            "workflow_task": {"workflow": wrong},
        }
        ok, reasons = smoke_chat.check(c, resp)
        assert not ok, f"跑错 v2 工作流 {wrong} 应被拦（精确等值），但通过了"
        assert any(wrong in r for r in reasons), \
            f"失败原因应点名跑错的 workflow {wrong}: {reasons}"


def test_wf3_exact_correct_v2_passes():
    # 精确正确的 wf3_logistics_v2 仍 PASS（不误伤正解）
    c = _wf3_case()
    resp = {
        "reply": "好的，已触发物流采集刷新。",
        "tools_used": ["run_workflow"],
        "workflow_task": {"workflow": "wf3_logistics_v2"},
    }
    ok, reasons = smoke_chat.check(c, resp)
    assert ok, f"精确正确的 wf3_logistics_v2 被误判: {reasons}"


def test_global_blacklist_drops_legit_alias():
    assert "可撑天数" not in smoke_chat.GLOBAL_BLACKLIST, \
        "可撑天数 是真实字段 sellable_days 的人话，不该在全局黑名单"


# ── smoke_chat case 11「拒绝刷新」：陈旧警示口径补同义词，但仍要求真报警示 ──
def _refuse_refresh_case():
    return next(c for c in smoke_chat.CASES if "用户拒绝刷新" in c.name)


def test_case11_stale_synonyms_pass():
    # 实跑 deepseek 各报了 "偏旧" / "有些旧"——都是合法数据陈旧警示，应放行。
    # 再加几种数据陈旧措辞（数据…旧 / 旧数据），确认收紧后仍不误伤合法说法。
    c = _refuse_refresh_case()
    legit = (
        "noon 销量数据是 5 月 5 日的（偏旧），可以用。",
        "noon 数据有些旧，但 ERP 是今天的。",
        "销量数据有点旧了，先用着。",
        "这是陈旧的 noon 数据，仅供参考。",
        "数据较旧（5/5），结果偏保守。",
        "用的是旧数据（noon 5/5），ERP 今天。",
        "noon 数据老旧（5/5），结果偏保守。",   # "老旧" 作数据陈旧（后接标点，非款/品）
    )
    for reply in legit:
        resp = {"reply": reply + " KSA 当前 20 个 SKU 需要补货：…",
                "tools_used": [], "workflow_task": None}
        ok, reasons = smoke_chat.check(c, resp)
        assert ok, f"合法数据陈旧措辞被门误判: {reply!r} -> {reasons}"


def test_case11_no_stale_warning_still_fails():
    # 完全不提任何陈旧警示 → 必须仍 FAIL（不挖空：门仍要求陈旧警示）
    c = _refuse_refresh_case()
    resp = {
        "reply": "KSA 当前 20 个 SKU 需要补货：SDA1874A 补 7 件、TBJ0059A 补 5 件…",
        "tools_used": [], "workflow_task": None,
    }
    ok, _ = smoke_chat.check(c, resp)
    assert not ok, "未报任何陈旧警示应被拦，但通过了（门被挖空）"


def test_case11_non_stale_jiu_words_still_fail():
    # 门2 红队洞：裸 "旧" 会把 "旧款 / 旧品 / 旧链接" 这类非陈旧词当成已警示。
    # 收紧后，回复里只有这类词、无任何数据陈旧警示 → 必须 FAIL（不挖空）。
    c = _refuse_refresh_case()
    traps = (
        "这些旧款 SKU 里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",   # 红队原 case
        "旧品 TBJ0059A 建议补 5 件，新品 SDA1874A 补 7 件。",
        "我看了数据，这些旧款 SKU 需要补货：SDA1874A 7 件。",      # 含"数据"但"旧款"非陈旧
        "参考旧链接里的清单，补 SDA1874A 7 件。",
        "这些老旧款 SKU 里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",  # 门2 三轮红队: 老旧款
        "这些老旧产品里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",     # 门2 三轮红队: 老旧产品
        "我看了数据，这些老旧款式需要补货：SDA1874A 7 件。",        # 含"数据"但"老旧款"非陈旧
    )
    for reply in traps:
        resp = {"reply": reply, "tools_used": [], "workflow_task": None}
        ok, _ = smoke_chat.check(c, resp)
        assert not ok, f"非陈旧的'旧X'被误判为已警示（门被挖空）: {reply!r}"


if __name__ == "__main__":
    tests = [
        test_legit_sellable_days_with_anchor_passes,
        test_legit_sellable_days_no_anchor_passes,
        test_real_hallucinated_field_still_flagged,
        test_alias_in_fabrication_context_still_flagged,
        test_timestamp_guard_intact,
        test_wf3_prose_mention_with_correct_v2_passes,
        test_wf3_real_old_workflow_selection_still_fails,
        test_wf3_no_workflow_triggered_still_fails,
        test_wf3_wrong_v2_workflow_still_fails,
        test_wf3_exact_correct_v2_passes,
        test_global_blacklist_drops_legit_alias,
        test_case11_stale_synonyms_pass,
        test_case11_no_stale_warning_still_fails,
        test_case11_non_stale_jiu_words_still_fail,
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

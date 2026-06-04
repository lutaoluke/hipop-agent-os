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


def test_legit_official_warehouse_stock_passes():
    # Luke 验收口径：「官方仓库存」是合法仓别说法（非编造字段），不得被反幻觉拦。
    for reply in ("官方仓库存还有 1200 件，建议补货。",
                  "noon 官方仓的可售库存 809 个 SKU。"):
        out, warns = _safety.sanitize_reply(reply, [])
        assert not warns, f"合法『官方仓库存』被误报为幻觉: {reply!r} -> {warns}"
        assert not out.startswith("⚠️"), f"不该加幻觉 banner: {out[:60]}"


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
        "noon 数据偏旧。",                       # 验门人指定 PASS
        "同步时间较旧，结果偏保守。",            # 验门人指定 PASS（同步时间 + 较旧）
        "数据已经旧了，建议刷新。",              # 数据 + 连接字 + 旧
        "老旧数据仅供参考。",                    # 老旧 直接修饰 数据
        "旧的口径，数据偏保守。",                # 旧 紧贴 口径
        "库存数据偏旧，建议刷新。",              # round5: 库存数据 + 偏旧（合法）
        "用的是旧 noon 数据。",                  # 旧 + noon + 数据（间隔无产品对象）
        # round6 clause-break：陈旧词收尾、产品对象另起一句（标点分隔）→ 仍是合法警示
        "数据较旧，SKU 需要补 5 件。",
        "数据偏旧。SKU 列表如下。",
        "库存数据较旧，款式很多。",
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
    # 门2 四轮红队同一族洞：陈旧形容词若隔着/前接**产品对象**（款/品/机型/SKU/版…），
    # 修饰的是那个对象而非数据本身，不算"数据陈旧警示"。这些只含"旧对象"、无任何数据
    # 陈旧提醒的回复 → 必须 FAIL（不挖空）。结构判别后一次性覆盖全族。
    c = _refuse_refresh_case()
    traps = (
        # round2: 裸"旧"+对象
        "这些旧款 SKU 里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",
        "旧品 TBJ0059A 建议补 5 件，新品 SDA1874A 补 7 件。",
        "我看了数据，这些旧款 SKU 需要补货：SDA1874A 7 件。",
        "参考旧链接里的清单，补 SDA1874A 7 件。",
        # round3: "老旧"+对象 / 旧对象再蹭数据名词
        "这些老旧款 SKU 里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",
        "这些老旧产品里，SDA1874A 补 7 件、TBJ0059A 补 5 件。",
        "我看了数据，这些老旧款式需要补货：SDA1874A 7 件。",
        "这些旧款的数据都在表里，补 SDA1874A 7 件。",
        "旧款数据线 SKU 补 5 件。",
        "看了旧版口径，补 7 件。",
        # round4（验门人 14:42）：旧对象 + 数据/口径（.{0,3} 间隔逃逸）
        "旧机型数据里，SDA1874A 补 7 件。",
        "旧机型的数据里，SDA1874A 补 7 件。",
        "旧商品数据里，SDA1874A 补 5 件。",
        "旧SKU数据里，SDA1874A 补 5 件。",
        "旧机型口径下，SDA1874A 补 7 件。",
        # round4 自查：老旧的+对象 / 数据线很旧（数据线=产品）
        "这些老旧的款式，SDA1874A 补 7 件。",
        "数据线很旧，SDA1874A 补 7 件。",
        # round5（码长首审）：数据名词在左、旧 修饰右边产品对象（镜像洞）
        "库存旧机型数据补货。",          # "库存旧" 不是数据陈旧，旧 修饰 机型
        "同步旧机型补 5 件。",
        "口径旧版补货。",
        # round5 自查（out-of-domain 也焊死）+ 同族新探
        "较旧的车型方案，补 7 件。",
        "老旧的页面布局，补 7 件。",
        "库存旧版机型，补 7 件。",
        "销量旧款图表，补 7 件。",
        "数据旧链接补 7 件。",
        # round6（码长首审）：旧 右侧用**空白/顿号**接产品对象（连接符缺口），全 in-domain
        # 词、无任何数据陈旧警示 → 必 FAIL。
        "销量里旧 SKU 补 5 件。",
        "库存里旧 机型补 7 件。",
        "数据里旧  SKU 补货。",          # 双空格
        "同步的旧 SKU 数据补 5 件。",
        "口径里旧、SKU 补货。",          # 顿号（码长 [的\s] 补丁漏的那条，已用 [的\s、] 焊死）
        "数据里旧　机型补货。",          # 全角空格
    )
    for reply in traps:
        resp = {"reply": reply, "tools_used": [], "workflow_task": None}
        ok, _ = smoke_chat.check(c, resp)
        assert not ok, f"非陈旧的'旧对象'被误判为已警示（门被挖空）: {reply!r}"


if __name__ == "__main__":
    tests = [
        test_legit_sellable_days_with_anchor_passes,
        test_legit_sellable_days_no_anchor_passes,
        test_legit_official_warehouse_stock_passes,
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

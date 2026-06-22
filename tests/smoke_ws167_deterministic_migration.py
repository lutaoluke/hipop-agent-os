"""Smoke：WS-167 —— 确定性路由 + 配套 formatter 外移到 _deterministic_routes + 接线不变。

为什么存在
---------
WS-167（WS-164/S3）把 `agent.py` 内的**确定性意图路由**（`_deterministic_*`）与**配套用户
可见回复 formatter**（`_format_*`）整体物理外移到 `hipop/server/_deterministic_routes.py`。
`agent.py`（CODEOWNERS 锁定的共享热点文件）只保留 `chat()` 主编排里对这些函数的调用接线
（`from ._deterministic_routes import (...)` 再导出投影）。本 smoke 把「外移成功且没引入三种
死法」钉成 CI：

  1) 结构判据（fail-then-pass 的钉子）—— `agent.py` 不再定义任何 `def _deterministic_*`
     路由 / formatter；它们现在定义在 `_deterministic_routes.py`。迁移前定义都在 agent.py，
     本断言 **FAIL**；外移后 **PASS**。（与 smoke_agent_antiregress_ratchet 的结构棘轮口径
     一致，但这里额外断言「实现确实落到了 _deterministic_routes」，挡「搬出去但没人接 /
     接错地方」。）
  2) 再导出契约（防接线缺失 / 死代码短路）—— 每个被外移的 `_deterministic_*` / `_format_*` /
     私有辅助，`agent.X is _deterministic_routes.X`，且其定义模块就是 `_deterministic_routes`
     （既有测试与 chat 都按 `agent.*` / 裸名取实现，外移后必须仍解析到同一函数对象）。
  3) chat() 真调（防死代码短路）—— `chat()` 函数体内对关键路由 / formatter 仍有真实调用点
     （静态 AST 在 chat 体内找到对它们的 Call），证明生产路径走的是外移后的函数，而不是
     旧死函数或没走到。
  4) 行为等价（防口径回退）—— 覆盖验收点名的代表路径：workflow request / export request /
     readonly 告警计数 / stock split / 总库存 TopN / 销量 TopN / 补货清单 / SKU 指标；并抽查
     用户可见 formatter 关键字段不回退（拆分四仓 / 概览 / 商品总数 / 货单实时 / readonly 回复 /
     TopN fail-closed 提示）。缺数据 / 空结果走 fail-closed 文案，不编数字。

fail-then-pass（对真实工件，开发期已跑过、输出贴在 PR）：
  - 把 git HEAD（外移前）的 agent.py 喂给结构判据 → 检出 15 个 `def _deterministic_` →
    test_routes_live_in_new_module 红；外移后 agent.py 0 个、新模块 15 个 → 绿。

接线：文件名匹配 `tests/smoke_*.py`，被 Makefile 自动聚合进 `make test`（required PR check）。

跑法：
  python3 tests/smoke_ws167_deterministic_migration.py
"""
import ast
import inspect
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

AGENT_PY = os.path.join(REPO, "hipop", "server", "agent.py")
ROUTES_PY = os.path.join(REPO, "hipop", "server", "_deterministic_routes.py")

# 外移前后稳定的确定性路由集（15 个 `def _deterministic_*`，ratchet 同口径）。
EXPECTED_ROUTERS = {
    "_deterministic_workflow_request",
    "_deterministic_multi_workflow_request",
    "_deterministic_erp_refresh_time_request",
    "_deterministic_export_request",
    "_deterministic_data_freshness_request",
    "_deterministic_total_stock_topn_request",
    "_deterministic_product_sales_topn_request",
    "_deterministic_window_sales_topn_request",
    "_deterministic_scope_overview_request",
    "_deterministic_products_count_request",
    "_deterministic_sku_metric_request",
    "_deterministic_replenishment_sku_request",
    "_deterministic_replenishment_list_request",
    "_deterministic_stock_split_request",
    "_deterministic_readonly_request",
    "_deterministic_readonly_reply",
}

# 配套用户可见回复 formatter（10 个），外移后必须仍从 agent 再导出解析到新模块。
EXPECTED_FORMATTERS = {
    "_format_erp_refresh_time_reply",
    "_format_product_sales_topn_reply",
    "_format_window_sales_topn_reply",
    "_format_total_stock_topn_reply",
    "_format_products_count_reply",
    "_format_scope_overview_reply",
    "_format_data_freshness_reply",
    "_format_stock_split_reply",
    "_format_replenishment_list_reply",
    "_format_sku_metric_reply",
    "_format_order_live_reply",
}

# 私有辅助（路由 / formatter 共用），也随簇外移，agent 仍需再导出（chat 与既有测试按 agent.* 取）。
EXPECTED_HELPERS = {
    "_has_stock_refresh_intent",
    "_stock_refresh_refused",
    "_stock_refresh_refusal_reply",
    "_fmt_int",
    "_format_pct",
    "_format_metric_value",
    "_extract_live_order_no",
    "_window_sales_topn_route",
}

EXPECTED_ALL = EXPECTED_ROUTERS | EXPECTED_FORMATTERS | EXPECTED_HELPERS

_DET_DEF_RE = re.compile(r'^def (_deterministic_\w+)\s*\(', re.MULTILINE)


def _det_defs(src: str):
    return set(_DET_DEF_RE.findall(src))


def test_routes_live_in_new_module():
    """结构判据：agent.py 不再定义 _deterministic_* 路由；_deterministic_routes.py 承载它们。"""
    agent_src = open(AGENT_PY, encoding="utf-8").read()
    routes_src = open(ROUTES_PY, encoding="utf-8").read()

    in_agent = _det_defs(agent_src)
    assert not in_agent, (
        f"agent.py 仍定义 {len(in_agent)} 个 def _deterministic_*（{sorted(in_agent)}）—— "
        f"WS-167 要求全部外移到 _deterministic_routes.py（迁移前此断言应 FAIL，迁移后 PASS）。")

    in_routes = _det_defs(routes_src)
    assert in_routes == EXPECTED_ROUTERS, (
        f"_deterministic_routes.py 定义的路由集漂移：缺 {EXPECTED_ROUTERS - in_routes}，"
        f"多 {in_routes - EXPECTED_ROUTERS} —— 外移漏搬 / 多搬都说明接线缺失或集合漂移。")

    # detector self-test：把一个 def _deterministic_fake 喂回 agent 源码，必须被识别（防门是死的）
    assert _det_defs(agent_src + "\ndef _deterministic_fake_regress(q):\n    return None\n") == {
        "_deterministic_fake_regress"}, "结构检测器自检失败：新增 def _deterministic_ 未被计入"
    print(f"  ✓ 结构：agent.py 0 个 _deterministic_ 路由；_deterministic_routes.py {len(in_routes)} 个"
          f"（检测器自检通过）")


def test_reexport_contract():
    """再导出契约：每个外移符号 agent.X is _deterministic_routes.X，定义模块为新模块。"""
    from hipop.server import agent, _deterministic_routes as routes

    missing = []
    for name in sorted(EXPECTED_ALL):
        a = getattr(agent, name, None)
        r = getattr(routes, name, None)
        if r is None:
            missing.append(f"{name}（新模块缺定义）")
            continue
        if a is not r:
            missing.append(f"{name}（agent 未再导出 / 取到别的对象）")
            continue
        mod = inspect.getmodule(a).__name__
        if not mod.endswith("_deterministic_routes"):
            missing.append(f"{name}（定义模块={mod}，没真外移）")
    assert not missing, "再导出契约断裂：\n    " + "\n    ".join(missing)

    # WS-169 thin shell：chat 编排 DB 辅助也已离开 agent.py；agent 只保留再导出契约。
    from hipop.server import _chat_workflows
    assert agent._current_workflow_task is _chat_workflows._current_workflow_task, \
        "_current_workflow_task 应从 _chat_workflows 再导出（agent.py 不承载实现体）"
    print(f"  ✓ 再导出：{len(EXPECTED_ALL)} 个路由/formatter/辅助 agent.X is routes.X；"
          f"_current_workflow_task 从 _chat_workflows 再导出")


def _chat_call_names():
    """静态解析 agent.py 的 chat() 函数体，收集所有被调用的函数名（Call 节点）。"""
    tree = ast.parse(open(AGENT_PY, encoding="utf-8").read())
    chat = next((n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "chat"), None)
    assert chat is not None, "agent.py 未找到 chat() 函数 —— 编排接线无法验证"
    called = set()
    for node in ast.walk(chat):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def test_chat_wires_routes():
    """chat() 真调：生产编排路径仍调用关键外移路由 / formatter（防死代码短路）。"""
    called = _chat_call_names()
    # 验收点名的代表路径 + 对应 formatter，必须在 chat 体内有真实调用点。
    must_call = {
        "_deterministic_export_request",
        "_deterministic_readonly_request", "_deterministic_readonly_reply",
        "_deterministic_multi_workflow_request",
        "_deterministic_workflow_request",
        "_deterministic_product_sales_topn_request", "_format_product_sales_topn_reply",
        "_deterministic_total_stock_topn_request", "_format_total_stock_topn_reply",
        "_deterministic_replenishment_list_request", "_format_replenishment_list_reply",
        "_deterministic_stock_split_request", "_format_stock_split_reply",
        "_deterministic_sku_metric_request", "_format_sku_metric_reply",
        "_extract_live_order_no", "_format_order_live_reply",
        "_stock_refresh_refusal_reply",
    }
    missing = sorted(must_call - called)
    assert not missing, (
        f"chat() 未调用以下外移路由 / formatter（接线缺失 / 死代码短路）：{missing}")
    print(f"  ✓ chat 接线：{len(must_call)} 个关键路由 / formatter 在 chat() 体内有真实调用点")


def test_routing_behavior_equivalent():
    """行为等价：代表确定性路由返回外移前口径（结构判别，非穷举词表）。"""
    from hipop.server import agent as A

    # workflow request：肯定祈使 → 路由；否定 → 不路由（安全侧）。
    assert A._deterministic_workflow_request("帮我刷库存，ERP 6 仓")["workflow"] == "wf1_stock_v2"
    assert A._deterministic_workflow_request("帮我刷新物流")["workflow"] == "wf3_logistics_v2"
    assert A._deterministic_workflow_request("重跑补货建议")["workflow"] == "wf5_sales_cycle_v2"
    assert A._deterministic_workflow_request("不要刷新库存") is None
    assert A._deterministic_workflow_request("能不能刷新库存？") is None

    # export request：补货 / 物流 / 默认销售视图。
    assert A._deterministic_export_request("帮我导出补货表格")["view"] == "replenish"
    assert A._deterministic_export_request("导出物流货单 excel")["view"] == "logistics"
    assert A._deterministic_export_request("导出销售表格")["view"] == "sales"
    assert A._deterministic_export_request("今天天气怎么样") is None

    # readonly 告警计数：只读意图不得升级成 run_workflow。
    assert A._deterministic_readonly_request("红色告警有几个") == {
        "tool": "scope_overview", "intent": "alert_count"}
    assert A._deterministic_readonly_request("帮我刷新库存") is None  # 含刷新动词 → 不接管

    # stock split / topN / 补货清单 / SKU 指标。
    assert A._deterministic_stock_split_request("ABC123 四仓库存拆分") == "ABC123"
    assert A._deterministic_total_stock_topn_request("库存最多的5个SKU") == 5
    assert A._deterministic_total_stock_topn_request("总库存最高的SKU") == 10  # 缺数字 → 默认 10
    assert A._deterministic_product_sales_topn_request("销量最高的商品 top5") == 5
    assert A._deterministic_replenishment_list_request("本周必补前10个") == 10
    assert A._deterministic_replenishment_sku_request("ABC123 补货 pipeline") == "ABC123"
    assert A._deterministic_sku_metric_request("ABC123 近30天销量") == "ABC123"
    print("  ✓ 行为等价：workflow/export/readonly/stock-split/topN/补货/SKU 路由口径不回退")


def test_formatter_fields_no_regress():
    """口径不回退：用户可见 formatter 关键字段保留；缺数据走 fail-closed，不编数字。"""
    from hipop.server import agent as A

    # readonly 回复：红色告警 + 待处理。
    r = A._deterministic_readonly_reply("alert_count", {"alerts_red": 3, "alerts_pending": 2}, "ksa")
    assert "红色告警 3 个" in r and "待处理告警 2 个" in r, r

    # 四仓拆分：义乌 / 沙特一号 / noon / 在途 / 合计。
    s = A._format_stock_split_reply("SKU9", {
        "split": {"yiwu": 1, "overseas_saudi_1": 2, "noon": 3, "inbound": 4}, "total": 10,
        "updated_at": "2026-06-10"})
    for token in ("义乌仓：1", "沙特一号仓：2", "noon仓：3", "在途：4", "**合计：10**"):
        assert token in s, f"stock split 字段回退，缺 {token}：{s}"

    # 店铺概览：红色 / 待处理 / 在售 SKU。
    o = A._format_scope_overview_reply("KSA", {"alerts_red": 2, "alerts_pending": 1, "sku_count": 50})
    assert "红色告警 2 个" in o and "待处理告警 1 个" in o and "在售 SKU 50 个" in o, o

    # 商品总数：product / SKU 双维度。
    p = A._format_products_count_reply("KSA", {
        "summary_products": {"total": 100, "listed": 60, "unlisted": 40},
        "summary_skus": {"total": 200, "listed": 120, "unlisted": 80}})
    assert "product 维度 100 个" in p and "SKU 维度 200 个" in p, p

    # 货单实时：状态 / 承运商 / 跟踪号。
    od = A._format_order_live_reply("PD-1", {
        "ok": True, "forwarder": "F", "tracking_no": "T1", "status": "在途"})
    assert "PD-1" in od and "在途" in od and "承运商 F" in od and "跟踪号 T1" in od, od

    # 缺数据边界：TopN fail-closed → 给出不能出数原因，不编数字。
    fc = A._format_total_stock_topn_reply("KSA", {"fail_closed": True, "max_age_days": 3})
    assert "不能出数" in fc and "3 天" in fc, fc
    assert not re.search(r"总库存\s*\d", fc), f"fail-closed 文案不应出现库存数字：{fc}"
    print("  ✓ formatter 字段：readonly/拆分/概览/商品总数/货单/TopN-failclosed 关键字段不回退")


def run():
    tests = [test_routes_live_in_new_module, test_reexport_contract, test_chat_wires_routes,
             test_routing_behavior_equivalent, test_formatter_fields_no_regress]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__} 异常: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n✗ WS-167 确定性路由外移 smoke：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ WS-167 确定性路由外移 smoke 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(run())

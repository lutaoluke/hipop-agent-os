"""Smoke：WS-166 —— tool_* 业务实现外移到 tools_impl + 投影不变 + 读写工具 parity。

为什么存在
---------
WS-166（WS-164/S2）把 `agent.py` 内 23 个 `tool_* / _tool_*` 业务实现**整体物理外移**
到 `hipop/server/tools_impl.py`。`agent.py`（CODEOWNERS 锁定的共享热点文件）只保留：
工具注册（`TOOLS` schema）、分发表投影（`TOOL_FUNCS`）、统一治理执行入口（`_exec_tool`）
与对话编排（`chat`）。本 smoke 把「外移成功且没引入三种死法」钉成 CI：

  1) 结构判据（fail-then-pass 的钉子）—— `agent.py` 不再定义任何 `def tool_ / def _tool_`，
     这些实现现在定义在 `tools_impl.py`。迁移前实现都在 agent.py，本断言 **FAIL**；
     外移后 **PASS**。（与 smoke_agent_antiregress_ratchet 的结构棘轮口径一致，但这里
     额外断言「实现确实落到了 tools_impl」，挡「搬出去但没人接 / 接错地方」。）
  2) 投影不变（防接线缺失 / 死代码短路）—— `TOOL_FUNCS` 键集合、`TOOLS` schema 工具名
     集合都等于外移前的稳定工具集；且 `agent.tool_X is tools_impl.tool_X`（再导出契约，
     api.py / 既有测试都按 `agent.tool_*` 取实现，外移后必须仍解析到同一函数对象）。
  3) 只读工具 parity —— `explain_status_enum` 直调与经 `_exec_tool` 路由，关键字段一致
     （证明外移没改 read-only 行为，且 read-only 仍走 `_exec_tool` 直调路径）。
  4) destructive 工具仍经 funnel —— `update_alert_status`（destructive）经 `_exec_tool`
     调用时，必须落到 `governance.propose_and_execute`，且治理拿到的分发表就是
     `agent.TOOL_FUNCS`。证明外移没让破坏性实现裸跑、绕过治理（权限绕过死法）。

fail-then-pass（对真实工件，开发期已跑过、输出贴在 PR）：
  - 把 git HEAD（外移前）的 agent.py 喂给 `_agent_defines_no_business_tools()` → 检出
    20+ 个 `def tool_` → test_impls_live_in_tools_impl 红；外移后 agent.py 0 个 → 绿。

接线：文件名匹配 `tests/smoke_*.py`，被 Makefile 自动聚合进 `make test`（required PR check）。

跑法：
  python3 tests/smoke_ws166_tool_migration.py
"""
import inspect
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

AGENT_PY = os.path.join(REPO, "hipop", "server", "agent.py")
TOOLS_IMPL_PY = os.path.join(REPO, "hipop", "server", "tools_impl.py")

# 外移前后稳定的工具集（registry / manifest 投影口径不许变）。
EXPECTED_TOOLS = {
    "query_sku", "query_order", "update_alert_status", "scope_overview",
    "compute_replenishment", "query_replenishment_sku", "compute_air_freight_roi",
    "data_health_check", "list_products", "export_table", "navigate_user_to",
    "notify_via_feishu", "run_workflow", "confirm_proposal", "tenant_notes_get",
    "tenant_notes_append", "query_sku_live", "query_order_live", "query_1688_similar",
    "explain_status_enum", "capture_feedback", "query_stock_split", "total_stock_topn",
}

_TOOL_DEF_RE = re.compile(r'^def (tool_|_tool_)\w+\s*\(', re.MULTILINE)


def _count_business_tool_defs(src: str) -> int:
    return len(_TOOL_DEF_RE.findall(src))


def test_impls_live_in_tools_impl():
    """结构判据：agent.py 不再定义业务 tool 实现；tools_impl.py 承载它们。"""
    agent_src = open(AGENT_PY, encoding="utf-8").read()
    impl_src = open(TOOLS_IMPL_PY, encoding="utf-8").read()

    n_agent = _count_business_tool_defs(agent_src)
    assert n_agent == 0, (
        f"agent.py 仍定义 {n_agent} 个 def tool_/def _tool_ 业务实现 —— WS-166 要求全部外移到 "
        f"tools_impl.py（迁移前此断言应 FAIL，迁移后 PASS）。")

    n_impl = _count_business_tool_defs(impl_src)
    assert n_impl == len(EXPECTED_TOOLS), (
        f"tools_impl.py 定义 {n_impl} 个 tool 实现，期望 {len(EXPECTED_TOOLS)} —— "
        f"外移漏搬 / 多搬都说明接线缺失或集合漂移。")

    # detector self-test：把一个 def tool_fake 喂回 agent 源码，必须被识别（防门是死的）
    assert _count_business_tool_defs(agent_src + "\ndef tool_fake_regress():\n    return {}\n") == 1, \
        "结构检测器自检失败：新增 def tool_ 未被计入"
    print(f"  ✓ 结构：agent.py 0 个业务 tool 实现；tools_impl.py {n_impl} 个（检测器自检通过）")


def test_runtime_projection_unchanged():
    """投影不变：TOOL_FUNCS / TOOLS schema 工具名集合不变；再导出契约成立。"""
    from hipop.server import agent, tools_impl

    assert set(agent.TOOL_FUNCS) == EXPECTED_TOOLS, (
        f"TOOL_FUNCS 键集合漂移：缺 {EXPECTED_TOOLS - set(agent.TOOL_FUNCS)}，"
        f"多 {set(agent.TOOL_FUNCS) - EXPECTED_TOOLS}")

    schema_names = {t["name"] for t in agent.TOOLS}
    assert schema_names == EXPECTED_TOOLS, (
        f"TOOLS schema 工具名集合与 TOOL_FUNCS 不一致：schema 缺 {EXPECTED_TOOLS - schema_names}，"
        f"多 {schema_names - EXPECTED_TOOLS}")

    # 再导出契约：agent.tool_X 必须就是 tools_impl.tool_X（api.py / 既有测试按 agent.tool_* 取）
    sampled = ["tool_query_sku", "tool_run_workflow", "tool_export_table",
               "tool_capture_feedback", "tool_total_stock_topn", "tool_update_alert_status"]
    for name in sampled:
        a = getattr(agent, name)
        t = getattr(tools_impl, name)
        assert a is t, f"agent.{name} 不是 tools_impl.{name}（再导出断裂，api.py/测试会取到错对象）"
        assert inspect.getmodule(a).__name__.endswith("tools_impl"), \
            f"{name} 的定义模块不是 tools_impl（实现没真外移）"
    print(f"  ✓ 投影：TOOL_FUNCS / TOOLS schema = {len(EXPECTED_TOOLS)} 个工具不变；"
          f"agent.tool_* 再导出契约成立")


def test_readonly_parity_via_exec_tool():
    """只读工具 parity：explain_status_enum 直调 == 经 _exec_tool 路由（关键字段不回退）。"""
    from hipop.server import agent

    direct = agent.tool_explain_status_enum("alert_status")
    routed = agent._exec_tool("explain_status_enum", {"field": "alert_status"})
    assert direct.get("ok") is True, f"直调 explain_status_enum 未返回 ok：{direct}"
    for key in ("ok", "current_allowed", "from_erp_api", "field"):
        assert direct.get(key) == routed.get(key), (
            f"只读 parity 回退：字段 {key} 直调={direct.get(key)!r} != 路由={routed.get(key)!r}")
    print(f"  ✓ 只读 parity：explain_status_enum 直调与 _exec_tool 路由关键字段一致")


def test_destructive_still_funnels_through_governance():
    """destructive 仍经 funnel：update_alert_status 经 _exec_tool 必落 governance，分发表为 TOOL_FUNCS。"""
    from hipop.server import agent
    from hipop.server import governance as gov

    assert gov.is_destructive("update_alert_status"), "update_alert_status 应被判为 destructive"
    assert gov.is_destructive("run_workflow"), "run_workflow 应被判为 destructive"

    calls = []
    orig = gov.propose_and_execute

    def _spy(name, args, actor, sc, tool_funcs):
        calls.append((name, tool_funcs))
        return {"ok": True, "_funnelled": True}

    gov.propose_and_execute = _spy
    try:
        r = agent._exec_tool("update_alert_status",
                             {"order_no": "PD-WS166-TEST", "status": "已确认丢货", "note": ""})
    finally:
        gov.propose_and_execute = orig

    assert calls, "destructive 工具未经 governance.propose_and_execute（funnel 被绕过！）"
    assert calls[0][0] == "update_alert_status", f"funnel 收到的工具名错：{calls[0][0]}"
    assert calls[0][1] is agent.TOOL_FUNCS, \
        "governance 拿到的分发表不是 agent.TOOL_FUNCS（destructive 实现可能被旁路调用）"
    assert r.get("_funnelled") is True, f"_exec_tool 未透传 governance 结果：{r}"
    print(f"  ✓ destructive funnel：update_alert_status 经 _exec_tool → governance.propose_and_execute"
          f"（分发表 = agent.TOOL_FUNCS）")


def run():
    tests = [test_impls_live_in_tools_impl, test_runtime_projection_unchanged,
             test_readonly_parity_via_exec_tool, test_destructive_still_funnels_through_governance]
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
        print(f"\n✗ WS-166 tool 外移 smoke：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ WS-166 tool 外移 smoke 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(run())

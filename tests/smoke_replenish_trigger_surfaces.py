"""Smoke：补货 workflow 真接进**用户可达的触发面**（WS-7 门2 二次返工）。

门2 第三次打回点仍是「接线缺失」死法的最外一层：`wf4_replenish_suggest` 虽已
在 `WORKFLOW_REGISTRY`（见 smoke_replenish_registry.py），但只有「知道 workflow
名直接 POST /api/run-workflow」这条原始 API 能命中。普通用户的真实入口——

  · chat：LLM 只能从 run_workflow tool 的 enum 里挑 workflow（enum 外的名字模型
    根本生成不出来，等于 chat 触发不到）；
  · UI 侧边栏「刷新」面板：只渲染 sidebar.html 里 refreshes 数组列出的按钮。

——都还点不到它，又是「测试绿、线上不触发」黑屏。本 smoke 把这两条 allowlist
钉死：

  1) chat run_workflow tool 的 workflow enum **包含** wf4_replenish_suggest
     （否则模型选不出 → chat 永远触发不了），且 tool 说明里点名了它（让模型知道
     何时该选）；
  2) UI 侧边栏 refreshes 面板**包含** wf4_replenish_suggest 的刷新按钮
     （否则用户在侧边栏点不到）。

fail-then-pass：把 enum / sidebar 里的 wf4_replenish_suggest 去掉时，对应 test
变红 → 接上后绿。本文件只断言 allowlist 真包含该 workflow，不塞 prompt、不改算法。

跑法：
  python3 tests/smoke_replenish_trigger_surfaces.py
  （make test 自动聚合 tests/smoke_*.py，本文件自动并入，无需改 Makefile）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

WORKFLOW_NAME = "wf4_replenish_suggest"
SIDEBAR_HTML = os.path.join(REPO, "hipop", "server", "templates", "partials", "sidebar.html")


def _run_workflow_tool():
    import hipop.server.agent as agent

    tools = [t for t in agent.TOOLS if t.get("name") == "run_workflow"]
    assert tools, "agent.TOOLS 里没有 run_workflow tool —— chat 根本无法触发任何 workflow"
    return tools[0]


def test_chat_tool_enum_contains_replenish():
    """chat 的 run_workflow tool enum 必须含 wf4_replenish_suggest，否则模型选不出来。"""
    tool = _run_workflow_tool()
    enum = tool["input_schema"]["properties"]["workflow"]["enum"]
    assert WORKFLOW_NAME in enum, (
        f"{WORKFLOW_NAME} 不在 run_workflow tool 的 workflow enum 里 → LLM 生成不出这个名字，"
        f"chat 永远触发不到补货 workflow。现有 enum: {enum}")


def test_chat_tool_description_mentions_replenish():
    """tool 说明点名补货 workflow，模型才知道用户问『该补货吗 / 算补货量』时选它。"""
    tool = _run_workflow_tool()
    desc = tool.get("description", "")
    assert WORKFLOW_NAME in desc, (
        f"run_workflow tool 说明里没点名 {WORKFLOW_NAME}，模型不知何时该选它（enum 里有名字但无引导）。")


def test_ui_sidebar_refresh_panel_contains_replenish():
    """UI 侧边栏刷新面板（sidebar.html refreshes 数组）必须有补货按钮，否则用户点不到。"""
    with open(SIDEBAR_HTML, encoding="utf-8") as f:
        html = f.read()

    # 定位 refreshPanel() 里的 refreshes: [...] 区块，避免误命中文件别处的同名字符串
    m = re.search(r"refreshes\s*:\s*\[(.*?)\]", html, re.S)
    assert m, "sidebar.html 里找不到 refreshes 数组 —— 侧边栏刷新面板结构变了，请同步本 smoke"
    block = m.group(1)
    assert f'workflow: "{WORKFLOW_NAME}"' in block or f"workflow: '{WORKFLOW_NAME}'" in block, (
        f"侧边栏 refreshes 面板没有 {WORKFLOW_NAME} 的刷新按钮 → 用户在 UI 上点不到补货刷新。"
        f"\nrefreshes 区块当前内容:\n{block.strip()[:600]}")


if __name__ == "__main__":
    import traceback

    tests = [
        test_chat_tool_enum_contains_replenish,
        test_chat_tool_description_mentions_replenish,
        test_ui_sidebar_refresh_panel_contains_replenish,
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

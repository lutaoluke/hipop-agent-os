"""Smoke：WS-168 —— prompt 文本外移到 _prompts 模块，agent.py 只保留 import 接线。

为什么存在
---------
WS-168（WS-164/S4）把 `agent.py` 内的 prompt 常量（`SYSTEM_PROMPT_LEGACY` /
`SYSTEM_PROMPT` / `_JUDGE_SYSTEM_PROMPT`）外移到 `hipop/server/_prompts.py`。
`agent.py`（CODEOWNERS 锁定的共享热点文件）只保留 `from ._prompts import ...` 的
再导出接线，不再承载 prompt 文本本体。本 smoke 把「外移成功且没引入三种死法」钉成 CI：

  1) 结构判据（fail-then-pass 的钉子）—— `agent.py` 不再在模块级定义任何 prompt 字符串常量；
     它们现在定义在 `_prompts.py`。迁移前常量在 agent.py，本断言 FAIL；外移后 PASS。
  2) 再导出契约（防接线缺失）—— `agent.SYSTEM_PROMPT is _prompts.SYSTEM_PROMPT`，
     且定义模块为 `_prompts`；既有测试按 `agent.SYSTEM_PROMPT` 取，外移后仍解析到同一对象。
  3) 文本 parity（防内容漂移）—— 迁移前后 SYSTEM_PROMPT / _JUDGE_SYSTEM_PROMPT 有意保留
     的关键词不回退（静态检查提取关键字）；SYSTEM_PROMPT_LEGACY 若删，先证明全仓零引用。
  4) 运行路径接通（防死代码短路）—— `chat()` 函数体内仍有 `SYSTEM_PROMPT` 的真实使用点
     （静态 AST 找到对应变量引用）。

fail-then-pass（对真实工件，开发期已跑过）：
  - 迁移前：agent.py 有 SYSTEM_PROMPT / SYSTEM_PROMPT_LEGACY / _JUDGE_SYSTEM_PROMPT 定义
    → test_prompts_live_in_new_module 红；迁移后：agent.py 0 个 prompt 常量定义 → 绿。

接线：文件名匹配 `tests/smoke_*.py`，被 Makefile 自动聚合进 `make test`（required PR check）。

跑法：
  python3 tests/smoke_ws168_prompt_migration.py
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
PROMPTS_PY = os.path.join(REPO, "hipop", "server", "_prompts.py")

PROMPT_CONST_NAMES = {"SYSTEM_PROMPT", "SYSTEM_PROMPT_LEGACY", "_JUDGE_SYSTEM_PROMPT"}


def _module_string_const_names(src: str) -> set:
    """静态解析：返回模块级字符串常量赋值的变量名集合（AST 不 import）。"""
    tree = ast.parse(src)
    names = set()
    for node in tree.body:
        val = None
        tgt = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    tgt = t.id
            val = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            tgt = node.target.id
            val = node.value
        if tgt and val is not None:
            if isinstance(val, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    names.add(tgt)
                elif isinstance(val, (ast.JoinedStr, ast.BinOp)):
                    names.add(tgt)
    return names


def test_prompts_live_in_new_module():
    """结构判据：agent.py 不再定义 prompt 常量；_prompts.py 承载它们。"""
    agent_src = open(AGENT_PY, encoding="utf-8").read()

    in_agent = _module_string_const_names(agent_src) & PROMPT_CONST_NAMES
    assert not in_agent, (
        f"agent.py 仍以模块级字符串常量形式定义 {sorted(in_agent)} —— "
        f"WS-168 要求全部外移到 _prompts.py（迁移前此断言应 FAIL，迁移后 PASS）。")

    assert os.path.exists(PROMPTS_PY), (
        f"_prompts.py 不存在（{PROMPTS_PY}）—— 外移目标模块尚未创建。")

    prompts_src = open(PROMPTS_PY, encoding="utf-8").read()
    in_prompts = _module_string_const_names(prompts_src) & PROMPT_CONST_NAMES
    # SYSTEM_PROMPT_LEGACY 允许删除（证明零引用）；其他两个必须在新模块
    required = {"SYSTEM_PROMPT", "_JUDGE_SYSTEM_PROMPT"}
    missing_required = required - in_prompts
    assert not missing_required, (
        f"_prompts.py 缺少必要 prompt 常量：{sorted(missing_required)} —— "
        f"外移漏搬。")

    # detector self-test：把 SYSTEM_PROMPT 定义喂回 agent src，必须被识别
    fake = agent_src + '\n\nSYSTEM_PROMPT = "fake"\n'
    assert "SYSTEM_PROMPT" in _module_string_const_names(fake), \
        "结构检测器自检失败：新增 prompt 常量未被识别"
    print(f"  ✓ 结构：agent.py 不再定义 prompt 常量；_prompts.py 承载 {sorted(in_prompts)}"
          f"（检测器自检通过）")


def test_reexport_contract():
    """再导出契约：agent.SYSTEM_PROMPT 与 agent._JUDGE_SYSTEM_PROMPT 解析到 _prompts 定义。"""
    from hipop.server import agent
    from hipop.server import _prompts

    for name in ("SYSTEM_PROMPT", "_JUDGE_SYSTEM_PROMPT"):
        a = getattr(agent, name, None)
        p = getattr(_prompts, name, None)
        assert p is not None, f"_prompts.{name} 不存在 —— 外移缺定义"
        assert a is p, (
            f"agent.{name} is not _prompts.{name} —— "
            f"agent.py 未再导出 / 取到别的对象（接线缺失）")
        # 确认定义模块
        if callable(a):
            mod = inspect.getmodule(a)
            assert mod is not None and mod.__name__.endswith("_prompts"), \
                f"agent.{name} 定义模块={mod.__name__ if mod else None}，没真外移"

    print(f"  ✓ 再导出：agent.SYSTEM_PROMPT / agent._JUDGE_SYSTEM_PROMPT "
          f"均 is _prompts 同名对象")


def test_system_prompt_key_content_intact():
    """文本 parity：SYSTEM_PROMPT 关键语义条目未在迁移中丢失。"""
    from hipop.server import _prompts

    sp = _prompts.SYSTEM_PROMPT
    # 这些是 prompt 中明确的工具路由条目，迁移不应删除
    expected_tokens = [
        "data_health_check",
        "query_sku_live",
        "run_workflow",
        "export_table",
        "total_stock_topn",
        "capture_feedback",
        "scope",          # {scope} 占位符
    ]
    missing = [tok for tok in expected_tokens if tok not in sp]
    assert not missing, (
        f"SYSTEM_PROMPT 关键条目在迁移中丢失：{missing} —— "
        f"只许搬承载位置，不许修改内容。")

    judge = _prompts._JUDGE_SYSTEM_PROMPT
    assert "confidence" in judge and "verdict" in judge, (
        f"_JUDGE_SYSTEM_PROMPT 关键字段（confidence/verdict）丢失 —— 内容漂移")

    print("  ✓ 文本 parity：SYSTEM_PROMPT 路由条目 + _JUDGE_SYSTEM_PROMPT 字段完整")


def test_chat_uses_system_prompt():
    """运行路径接通：chat() 函数体静态引用 SYSTEM_PROMPT（防死代码短路）。"""
    tree = ast.parse(open(AGENT_PY, encoding="utf-8").read())
    chat = next((n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "chat"), None)
    assert chat is not None, "agent.py 未找到 chat() 函数 —— 无法验证接线"

    refs = set()
    for node in ast.walk(chat):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)

    assert "SYSTEM_PROMPT" in refs, (
        "chat() 函数体内找不到 SYSTEM_PROMPT 引用 —— "
        "prompt 可能被外移但未在编排路径中使用（死代码短路）。")
    print("  ✓ chat() 接线：函数体内有 SYSTEM_PROMPT 引用（接线未断）")


def test_legacy_prompt_zero_refs_if_deleted():
    """SYSTEM_PROMPT_LEGACY 删除时证明全仓零引用（若保留则跳过）。"""
    from hipop.server import _prompts

    if hasattr(_prompts, "SYSTEM_PROMPT_LEGACY"):
        print("  ℹ SYSTEM_PROMPT_LEGACY 已保留在 _prompts（零引用验证跳过）")
        return

    # 如果删除了，扫全仓
    violations = []
    for dirpath, _dirs, names in os.walk(REPO):
        for n in names:
            if not n.endswith(".py"):
                continue
            path = os.path.join(dirpath, n)
            try:
                src = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if "SYSTEM_PROMPT_LEGACY" in src:
                violations.append(os.path.relpath(path, REPO))
    assert not violations, (
        f"SYSTEM_PROMPT_LEGACY 已从 _prompts 删除，但以下文件仍引用它：{violations}")
    print("  ✓ SYSTEM_PROMPT_LEGACY 删除且全仓零引用")


def run():
    tests = [
        test_prompts_live_in_new_module,
        test_reexport_contract,
        test_system_prompt_key_content_intact,
        test_chat_uses_system_prompt,
        test_legacy_prompt_zero_refs_if_deleted,
    ]
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
        print(f"\n✗ WS-168 prompt 外移 smoke：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ WS-168 prompt 外移 smoke 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""Smoke：`agent.py` 防回潮三层棘轮（WS-165 / WS-164-S1）。

为什么存在
---------
WS-162（PR #101）把 tool 注册元数据集中到 `tools_registry.yaml` 后，S2–S5 要继续
把**业务工具实现 / 路由 / formatter** 从 `hipop/server/agent.py` 外移。这条 smoke 是
机器闸（verifier，不是 prompt 规则），把「不要往 agent.py 堆回业务逻辑」钉成 CI：

  1) 行数棘轮 —— agent.py 行数只许降不许涨（基线 = WS-162 合并后主干真实行数）。
  2) 结构棘轮 —— 业务 tool 实现（def tool_ / def _tool_）+ 路由/formatter
     （def _deterministic_）的**函数数量**只许降不许涨；回潮即红。
  3) destructive funnel 棘轮 —— 破坏性 tool 实现只能经统一 funnel
     （`_exec_tool` → governance.propose_and_execute，均通过 TOOL_FUNCS 字典）被调到；
     任何生产模块**直接按名调** destructive 实现（绕过治理）即红。

只看行数挡不住「死代码短路」：行数不涨、却新增 `def tool_fake` / `def _deterministic_fake`
或外移模块直调 destructive 实现，仍是回潮。故 (2)(3) 与 (1) 同时把守。

每条棘轮都自带「检测器会咬人」的自检（detector self-test）——不仅断言当前主干干净，
还合成一个会触发回潮的样例、断言检测器**确实报红**。这防的是「门写了但其实是死的」
（harness 三死法之二：死代码短路）。

fail-then-pass（对真实工件，开发期已跑过、输出贴在 PR）：
  - 行数：临时往 agent.py 追加超预算空行 → test_line_budget 红 → 还原 → 绿。
  - 结构：临时加 `def tool_fake(): pass` → test_structure_budget 红 → 删 → 绿。
  - funnel：临时在 _provider_openai.py 加 `agent.tool_run_workflow(...)` 直调
    → test_destructive_funnel_no_bypass 红 → 删 → 绿。

接线：本文件名匹配 `tests/smoke_*.py`，被 Makefile 自动聚合进 `make test`
（见 smoke_makefile_autodiscover.py 钉死的自动发现），而 `make test` 是
`.github/workflows/gate.yml` 的 required PR check —— 故本棘轮真在 PR gate 上跑，
不是写了没人调。

跑法：
  python3 tests/smoke_agent_antiregress_ratchet.py
  python3 tests/smoke_agent_antiregress_ratchet.py --measure   # 打印当前真实计量值
  （也会被 `make test` 自动收进去）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

AGENT_PY = os.path.join(REPO, "hipop", "server", "agent.py")

# ── 基线（棘轮口径：只许降不许涨）─────────────────────────────
# 起点 = WS-162 / PR #101 合并后主干 agent.py 的真实计量（2026-06-10）。
# S2–S5 把实现外移、行数/函数数下降后，可手动把对应基线调小再收紧；绝不许调大。
LINE_BUDGET = 4463                 # agent.py 总行数上限
TOOL_DEF_BUDGET = 20               # `def tool_*` —— 业务 tool 实现
UNDERSCORE_TOOL_DEF_BUDGET = 3     # `def _tool_*` —— 下划线前缀 tool 实现
DETERMINISTIC_DEF_BUDGET = 15      # `def _deterministic_*` —— 确定性路由 / readonly formatter

# 结构棘轮覆盖的函数类别：(人类标签, 行首正则)
STRUCT_PATTERNS = [
    ("def tool_",          r'^def tool_\w+\s*\(',          TOOL_DEF_BUDGET),
    ("def _tool_",         r'^def _tool_\w+\s*\(',         UNDERSCORE_TOOL_DEF_BUDGET),
    ("def _deterministic_", r'^def _deterministic_\w+\s*\(', DETERMINISTIC_DEF_BUDGET),
]


# ── 纯计量函数（detector self-test 复用，保证测的就是门本身）──────────

def count_lines(src: str) -> int:
    return len(src.splitlines())


def count_defs(pattern: str, src: str) -> int:
    return len(re.findall(pattern, src, re.MULTILINE))


TOOLS_REGISTRY_YAML = os.path.join(REPO, "hipop", "server", "tools_registry.yaml")


def _destructive_tool_impls():
    """从 tools_registry.yaml 静态计算 destructive tool 集合 → 各自实现函数名。

    不硬编码 tool 名、也不 import agent.py（避免依赖 anthropic 等重运行时，
    本 smoke 在纯静态环境也能跑）。判定口径与 governance.is_destructive 一致：
    access == 'write' 或 risk_level ∈ {medium, high, critical}。impl 名取 yaml 的
    `impl:` 字段。新增 destructive tool 时自动纳入本棘轮覆盖。
    """
    import yaml

    with open(TOOLS_REGISTRY_YAML, encoding="utf-8") as f:
        reg = yaml.safe_load(f) or {}
    tools = reg.get("tools", {}) or {}
    impls = {}  # impl_func_name -> tool_name
    for tool_name, spec in tools.items():
        spec = spec or {}
        is_destr = spec.get("access") == "write" or spec.get("risk_level") in ("medium", "high", "critical")
        if is_destr:
            impl = spec.get("impl")
            assert impl, f"destructive tool {tool_name} 在 tools_registry.yaml 缺 impl 字段"
            impls[impl] = tool_name
    return impls


def find_bypass_calls(src: str, filename: str, impl_names):
    """在一份源码里找「直接按名调 destructive 实现」的越权点。

    允许（funnel 内的合法出现，仅限 agent.py）：
      - `def <impl>(`           —— 实现定义本身
      - `"<...>": <impl>,`/`}`  —— TOOL_FUNCS 字典登记（值就是函数对象，非调用）
    其余任何对 impl 名的文本引用都视为绕过 funnel（直调实现，跳过 governance）。

    返回 [(lineno, line, impl_name), ...]。
    """
    is_agent_py = os.path.basename(filename) == "agent.py"
    violations = []
    for impl in impl_names:
        # 词边界匹配函数名：前不接 word char（排除更长标识符的子串），
        # 但允许前接 `.`（属性访问 `agent.tool_run_workflow(...)` 正是要抓的绕过）。
        name_re = re.compile(r'(?<!\w)' + re.escape(impl) + r'(?!\w)')
        for i, line in enumerate(src.splitlines(), start=1):
            if not name_re.search(line):
                continue
            stripped = line.strip()
            if is_agent_py:
                # 定义行
                if re.match(r'^def\s+' + re.escape(impl) + r'\s*\(', stripped):
                    continue
                # TOOL_FUNCS 字典登记行：值是裸函数对象，行内不应有调用括号 `impl(`
                if re.search(r':\s*' + re.escape(impl) + r'\s*,?\s*$', stripped) \
                        and not re.search(re.escape(impl) + r'\s*\(', stripped):
                    continue
            violations.append((i, stripped, impl))
    return violations


def _production_py_files():
    """扫描范围 = hipop/ 下生产代码，排除测试件（test_* / smoke_* / */tests/*）。"""
    files = []
    root = os.path.join(REPO, "hipop")
    for dirpath, _dirs, names in os.walk(root):
        if os.sep + "tests" + os.sep in dirpath + os.sep:
            continue
        for n in names:
            if not n.endswith(".py"):
                continue
            if n.startswith("test_") or n.startswith("smoke_"):
                continue
            files.append(os.path.join(dirpath, n))
    return files


# ── 三条棘轮 ───────────────────────────────────────────────

def test_line_budget():
    """行数棘轮：agent.py 行数 ≤ 基线。"""
    src = open(AGENT_PY, encoding="utf-8").read()
    n = count_lines(src)
    assert n <= LINE_BUDGET, (
        f"agent.py 行数回潮：{n} > 基线 {LINE_BUDGET}。"
        f"业务逻辑应外移而非堆回 agent.py；若确为合理外移导致的净增，"
        f"请先把实现移出再说明，不要直接调大 LINE_BUDGET。")
    # detector self-test：超预算源码必须被判红
    fake = "x = 1\n" * (LINE_BUDGET + 5)
    assert count_lines(fake) > LINE_BUDGET, "行数检测器自检失败：超预算源码未被识别"
    print(f"  ✓ 行数棘轮：agent.py {n} 行 ≤ 基线 {LINE_BUDGET}（检测器自检通过）")


def test_structure_budget():
    """结构棘轮：tool 实现 + 路由/formatter 函数数量 ≤ 基线。"""
    src = open(AGENT_PY, encoding="utf-8").read()
    failures = []
    summary = []
    for label, pat, budget in STRUCT_PATTERNS:
        n = count_defs(pat, src)
        summary.append(f"{label}={n}/{budget}")
        if n > budget:
            failures.append(
                f"{label} 数量回潮：{n} > 基线 {budget}（业务实现/路由不应新增回 agent.py，应外移）")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ 结构棘轮失败 {len(failures)} 项")
        raise AssertionError("; ".join(failures))

    # detector self-test：合成「新增一个 def tool_fake」，计数必须 +1 越过基线
    inflated = src + "\n\ndef tool_fake_regress():\n    return {}\n"
    assert count_defs(STRUCT_PATTERNS[0][1], inflated) == count_defs(STRUCT_PATTERNS[0][1], src) + 1, (
        "结构检测器自检失败：新增 def tool_fake 未被计入")
    print(f"  ✓ 结构棘轮：{', '.join(summary)}（检测器自检通过：新增 def tool_ 会被计入）")


def test_destructive_funnel_no_bypass():
    """destructive funnel 棘轮：破坏性实现只能经 funnel 调到，禁止外部按名直调。"""
    impls = _destructive_tool_impls()
    assert impls, (
        "未发现任何 destructive tool —— governance.is_destructive 计算异常，"
        "棘轮失去保护对象（防『门指向空集』的假活）。")
    impl_names = list(impls)

    # 1) 对真实生产代码扫描：当前主干必须无越权直调
    all_violations = []
    for path in _production_py_files():
        try:
            src = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line, impl in find_bypass_calls(src, path, impl_names):
            rel = os.path.relpath(path, REPO)
            all_violations.append(f"{rel}:{lineno}  绕过 funnel 直调 {impl}（{impls[impl]}）→ {line}")
    if all_violations:
        for v in all_violations:
            print(f"  ✗ {v}")
        print(f"\n✗ destructive funnel 棘轮失败：{len(all_violations)} 处绕过")
        raise AssertionError("destructive 实现被绕过 funnel 直调")

    # 2) detector self-test：合成一个「provider 直调 destructive 实现」的源码，必须报红
    sample_impl = impl_names[0]
    bypass_src = (
        "from . import agent\n"
        "def handle(args):\n"
        f"    return agent.{sample_impl}(**args)   # 绕过 _exec_tool / governance\n"
    )
    flagged = find_bypass_calls(bypass_src, os.path.join(REPO, "hipop", "server", "_provider_fake.py"), impl_names)
    assert any(impl == sample_impl for _, _, impl in flagged), (
        f"funnel 检测器自检失败：直调 agent.{sample_impl}(...) 未被识别为绕过")

    # 3) 反向自检：funnel 内合法形态（def 行 + 字典登记行）不得误报
    legit_src = (
        f"def {sample_impl}(a):\n    return {{}}\n\n"
        f"TOOL_FUNCS = {{\n    \"{impls[sample_impl]}\": {sample_impl},\n}}\n"
    )
    false_pos = find_bypass_calls(legit_src, AGENT_PY, impl_names)
    assert not false_pos, f"funnel 检测器误报 funnel 内合法形态：{false_pos}"

    print(f"  ✓ funnel 棘轮：{len(impl_names)} 个 destructive 实现"
          f"（{', '.join(sorted(impl_names))}）无越权直调（检测器双向自检通过）")


def _measure():
    src = open(AGENT_PY, encoding="utf-8").read()
    print(f"agent.py 行数: {count_lines(src)}  (LINE_BUDGET={LINE_BUDGET})")
    for label, pat, budget in STRUCT_PATTERNS:
        print(f"{label}: {count_defs(pat, src)}  (budget={budget})")
    print("destructive impls:", _destructive_tool_impls())


def run():
    tests = [test_line_budget, test_structure_budget, test_destructive_funnel_no_bypass]
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
        print(f"\n✗ agent.py 防回潮棘轮：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ agent.py 防回潮三层棘轮全绿")
    return 0


if __name__ == "__main__":
    if "--measure" in sys.argv:
        _measure()
        sys.exit(0)
    sys.exit(run())

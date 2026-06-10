"""Smoke: WS-154 验收 #2「chat smoke 声明门」真有判别力、不是恒真,且已接进 CI 真执行(非 echo 桩)。

钉死三件事(fail-then-pass):
1. 注解契约逻辑(tests/ws154_chat_coverage_gate.check_annotation)对真实输入有判别力:
   - 缺 `chat smoke:` 行            → 必须红(ok=False)   ← 「该红就红」
   - 弱 N/A(`chat smoke: N/A` 无理由)→ 必须红
   - `chat smoke: N/A — 理由`        → 放行
   - `chat smoke: smoke_chat.py`    → 放行
2. 已接进独立 CI workflow:把 PR body 透进 WS154_PR_BODY,python 真实执行本门,失败码冒泡。
3. 反 echo-bypass(码长 Round-1 打回点 2):workflow 里对门脚本/未来 chat smoke 的调用必须是
   python/make 真实执行,不能被 `echo`/`if echo` 桩、`|| true`、`continue-on-error` 吞掉退出码。
   本 smoke 自带一个 echo-bypass 反例字符串,断言「结构校验」对它判红(证明不是搜字符串的空壳)。

确定性、无 DB/server/LLM,被 `make test` 自动聚合,验收 #4 不回退。
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW_DIR = os.path.join(REPO, ".github", "workflows")
GATE_SCRIPT = "ws154_chat_coverage_gate.py"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _strip_yaml_comments(text):
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"\s#", line)
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


def _runs_real(text, target):
    """结构校验:target 是否被 python/make 真实执行,且没被 echo 桩 / 吞退出码。
    回应 echo-bypass 红队:不是「文中出现 target 字符串」就算数。"""
    real = re.search(
        rf"(?m)^[^#]*\b(python3?|\$\{{?PYTHON\}}?|make)\b[^\n]*{re.escape(target)}", text
    )
    echo_stub = re.search(rf"(?m)^\s*(echo|if\s+echo|test\s)[^\n]*{re.escape(target)}", text)
    swallowed = any(
        target in ln and "|| true" in ln for ln in text.splitlines()
    ) or re.search(r"continue-on-error:\s*true", text)
    return bool(real) and not echo_stub and not swallowed


def run():
    failures = []
    sys.path.insert(0, REPO)
    try:
        from tests.ws154_chat_coverage_gate import check_annotation
    except Exception as e:
        print(f"  ✗ 无法 import check_annotation: {e}")
        print("\n✗ 声明门逻辑尚未落地(改动前预期 fail)")
        return 1

    # ── 1) 注解契约逻辑判别力 ─────────────────────────────────────────────
    cases = [
        ("缺注解行", "随便写点别的\n没有声明", False),
        ("弱 N/A 无理由", "改了点 CI\n\nchat smoke: N/A", False),
        ("N/A 带理由", "chat smoke: N/A — 只改 CI 覆盖门，未碰 chat 行为", True),
        ("指向具体 smoke", "修了拒绝渲染\n\nchat smoke: tests/smoke_chat.py 新增 WS-x case", True),
        ("中文冒号也认", "chat smoke：N/A — 文档改动", True),
    ]
    for label, body, want in cases:
        ok, _ = check_annotation(body)
        if ok != want:
            failures.append(f"注解判定错:[{label}] 期望 ok={want} 实得 ok={ok} —— 门恒真/恒假,无判别力。")
    if not failures:
        print("  ✓ 注解契约逻辑有判别力:缺行/弱 N/A 必红,N/A+理由 与 指向 smoke 放行")

    # ── 2) 已接进 CI workflow:透 PR body + python 真实执行 + 失败码冒泡 ─────
    wf_hit = None
    for p in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml")) +
                    glob.glob(os.path.join(WORKFLOW_DIR, "*.yaml"))):
        text = _strip_yaml_comments(_read(p))
        if GATE_SCRIPT in text:
            wf_hit = (p, text)
            break
    if not wf_hit:
        failures.append(f"没有 workflow 跑 {GATE_SCRIPT} —— 声明门没接进 CI,接线缺失。")
    else:
        p, text = wf_hit
        print(f"  · 声明门 CI workflow: {os.path.basename(p)}")
        if "pull_request" not in text or not re.search(r"branches:\s*\[?\s*['\"]?main", text):
            failures.append("声明门没在 pull_request→main 触发,合并前不跑。")
        if "WS154_PR_BODY" not in text or "pull_request.body" not in text:
            failures.append("声明门没把 PR 正文(github.event.pull_request.body)透进 WS154_PR_BODY,没法校验声明。")
        # 反 echo-bypass:门脚本必须被 python/make 真实执行
        if not _runs_real(text, GATE_SCRIPT):
            failures.append(f"{GATE_SCRIPT} 不是被 python/make 真实执行(疑似 echo 桩 / 吞退出码)。")

    # ── 3) echo-bypass 反例:结构校验必须对它判红(钉死红队绕过)───────────
    echo_bypass = (
        "jobs:\n  x:\n    steps:\n      - run: if echo " + GATE_SCRIPT + "; then echo ok; fi\n"
    )
    if _runs_real(echo_bypass, GATE_SCRIPT):
        failures.append("结构校验被 `if echo <script>` 蒙混过关 —— 反 echo-bypass 失败(码长打回点 2)。")
    else:
        print("  ✓ 反 echo-bypass:`if echo <script>` 桩被结构校验判红(不是搜字符串)")
    # 正例:真实 python 调用必须判绿
    if not _runs_real("      - run: python tests/" + GATE_SCRIPT + "\n", GATE_SCRIPT):
        failures.append("结构校验把真实 `python tests/<script>` 误判成假 —— 会拦正常接线。")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-154 验收 #2 声明门 smoke {len(failures)} 项不满足")
        return 1

    print("  ✓ 声明门已接 CI、透 PR body、python 真实执行、失败码冒泡,且抗 echo-bypass")
    print("✓ WS-154 验收 #2 chat smoke 声明门 smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

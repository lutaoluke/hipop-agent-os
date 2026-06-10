"""Smoke: WS-154 验收 #2 机械门(改 chat 行为必须配 chat smoke)真的能挡、不是恒真,且已接进 CI。

钉死两件事:
1. 覆盖逻辑(tests/ws154_chat_coverage_gate.check)对真实输入有判别力 —— fail-then-pass 的核心:
   - 「改了 chat 行为、没配 smoke」→ 必须判违例(ok=False)。  ← 这条是 fail-then-pass 里的「该红就红」
   - 「改了 chat 行为、配了 smoke」→ 必须放行。
   - 「没碰 chat 行为」→ 放行。
   三条都对,才证明它不是恒真/恒假的空壳(回应验门人「verifier 只搜字符串、挡不住空跑」的红队)。
2. 这道门已接进一个独立 CI workflow,在 PR→main 上跑 `--ci`,失败码冒泡(无 continue-on-error / `|| true`),
   且由 python/make 真实调用(不是 echo 糊弄)。

本 smoke 确定性、无 DB/server/LLM,被 `make test` 自动聚合,不拖慢确定性门(验收 #4 不回退)。
fail-then-pass:在 tests/ws154_chat_coverage_gate.py + 对应 workflow 落地前,本 smoke import 失败/找不到 workflow → 红;落地后 → 绿。
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW_DIR = os.path.join(REPO, ".github", "workflows")
GATE_INVOCATION = "ws154_chat_coverage_gate.py"


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


def run():
    failures = []

    # ── 1) 覆盖逻辑有判别力(fail-then-pass 核心)──────────────────────────
    sys.path.insert(0, REPO)
    try:
        from tests.ws154_chat_coverage_gate import check
    except Exception as e:
        print(f"  ✗ 无法 import tests/ws154_chat_coverage_gate.check: {e}")
        print("\n✗ 覆盖门逻辑尚未落地(改动前预期 fail)")
        return 1

    # a) 改了 chat 行为、没配 smoke → 必须判违例
    ok, reason = check(["hipop/server/agent.py", "hipop/server/api.py"])
    if ok:
        failures.append("违例场景(改 agent.py 没配 chat smoke)竟被放行 —— 门恒真,挡不住「改了行为没测」。")

    # b) 改了 chat 行为、配了 smoke → 必须放行
    ok2, _ = check(["hipop/server/agent.py", "tests/smoke_chat.py"])
    if not ok2:
        failures.append("合规场景(改 agent.py + 动 smoke_chat.py)被误挡 —— 门恒假,会拦正常 PR。")

    # b2) 新建一条全新 chat smoke 也算配了(glob tests/smoke_*chat*.py)
    ok2b, _ = check(["hipop/server/_safety.py", "tests/smoke_newfeature_chat.py"])
    if not ok2b:
        failures.append("新建 tests/smoke_*chat*.py 没被认成「对应 smoke」—— glob 口径漏了。")

    # c) 没碰 chat 行为 → 放行(避免对无关 PR 误报,比如本 PR 自己)
    ok3, _ = check([".github/workflows/gate-chat-coverage.yml", "tests/smoke_ws154_chat_coverage.py",
                    "tests/ws154_chat_coverage_gate.py", "README.md"])
    if not ok3:
        failures.append("无 chat 行为改动的 PR 被误挡 —— 误报会拖垮所有人(含本 PR 自己)。")

    if not failures:
        print("  ✓ 覆盖逻辑有判别力:违例必红、合规放行、新建 chat smoke 算配、无关改动不误挡")

    # ── 2) 已接进独立 CI workflow,PR→main 跑 --ci,失败码冒泡,且真实调用 ────
    wf_hit = None
    for p in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml")) +
                    glob.glob(os.path.join(WORKFLOW_DIR, "*.yaml"))):
        text = _strip_yaml_comments(_read(p))
        if GATE_INVOCATION in text:
            wf_hit = (p, text)
            break
    if not wf_hit:
        failures.append(f"没有 workflow 跑 {GATE_INVOCATION} —— 覆盖门没接进 CI,接线缺失。")
    else:
        p, text = wf_hit
        print(f"  · 覆盖门 CI workflow: {os.path.basename(p)}")
        if "pull_request" not in text:
            failures.append("覆盖门不在 pull_request 上触发,合并前不跑。")
        elif not re.search(r"branches:\s*\[?\s*['\"]?main", text):
            failures.append("覆盖门 pull_request 没限定 main 分支。")
        if re.search(r"continue-on-error:\s*true", text):
            failures.append("覆盖门 `continue-on-error: true`,失败被吞。")
        # 必须由 python/make 真实调用,不是 echo 糊弄(回应 echo-bypass 红队)
        real_call = re.search(
            rf"(?m)^[^#]*\b(python3?|\$\{{?PYTHON\}}?|make)\b[^\n]*{re.escape(GATE_INVOCATION)}",
            text,
        )
        echo_only = re.search(rf"(?m)^\s*(echo|if\s+echo)\b[^\n]*{re.escape(GATE_INVOCATION)}", text)
        if not real_call or echo_only:
            failures.append(f"{GATE_INVOCATION} 不是被 python/make 真实执行(疑似 echo 糊弄,挡不住空跑)。")
        for line in text.splitlines():
            if GATE_INVOCATION in line and "|| true" in line:
                failures.append(f"覆盖门命令用 `|| true` 洗白退出码:`{line.strip()}`")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-154 验收 #2 覆盖门 smoke {len(failures)} 项不满足")
        return 1

    print("  ✓ 覆盖门已接进独立 CI workflow、PR→main 触发、python 真实执行、失败码冒泡")
    print("✓ WS-154 验收 #2 chat 覆盖门 smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

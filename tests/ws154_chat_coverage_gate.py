"""WS-154 验收 #2 的机械门:改了 chat 行为的 PR,必须同时动一条 chat smoke,否则不许合并/close。

为什么是这个形态(而不是塞 prompt 规则)
----------------------------------------
验收 #2 原话:「新 WS 若涉及 chat 行为,必须有对应 smoke 才能 close。」
harness 明令禁止把这种规则塞进 SYSTEM_PROMPT(会变成没人测、改不动的死规则)。
确定性规则要落成 verifier。本模块就是那个 verifier 的纯逻辑核:

    给一组「本 PR 改动的文件」→ 判定「碰了 chat 行为代码 却没碰任何 chat smoke」是否成立。

它是确定性的、无 DB / server / LLM 依赖,既被 CI(gate-chat-coverage.yml,跑 `--ci` 模式,
对 PR 真实 diff 判定、违例则 job 红阻断合并)调用,也被 tests/smoke_ws154_chat_coverage.py
(喂合成文件列表做 fail-then-pass)钉死。

口径(v1,可被规划小队细化)
----------------------------
- 文件路径用「前缀 / fnmatch glob」判定,清单集中在下面两个常量,规划小队定稿后改这里即可,无需改逻辑。
- CHAT_BEHAVIOR_PATHS:动这些 = 动了 chat 行为(agent 协调器 + 各 chat 门 + provider + governance)。
- CHAT_TEST_PATHS:这些算「对应 chat smoke」。新加一条全新 chat smoke(tests/smoke_*chat*.py)即满足。
- 判定:碰了 behavior 但一条 test 都没碰 → 违例(返回不通过)。其余 → 通过。
  注:这是「耦合覆盖」式门,会对「纯非行为改动也碰了 behavior 文件」误报 —— 这是此类门的已知代价,
  作者补/动一条对应 smoke 即可解除;清单也可由规划小队收紧以降误报。宁可误报挡一下,也不放过「改了行为没测」。
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from typing import List, Tuple

# ── 口径常量(规划小队定稿后改这里)────────────────────────────────────
# 动这些文件 = 动了 chat 行为
CHAT_BEHAVIOR_PATHS = [
    "hipop/server/agent.py",                 # LLM 协调器 + tool 定义 + SYSTEM_PROMPT
    "hipop/server/_chat_boundary.py",        # chat 边界契约
    "hipop/server/_execution_intent_gate.py",# 执行意图门(决定 chat 能不能真触发动作)
    "hipop/server/_inventory_refresh_gate.py",
    "hipop/server/_safety.py",               # 回复安全/低置信处理
    "hipop/server/_factslot_contract.py",
    "hipop/server/_workflow_reply.py",
    "hipop/server/intent.py",
    "hipop/server/_provider.py",             # provider 分派
    "hipop/server/_provider_anthropic.py",
    "hipop/server/_provider_openai.py",
    "hipop/server/governance.py",            # 动作门 / 决策 pipeline(管 chat tool 执行)
    "hipop/server/governance_actions.yaml",
]

# 这些算「对应 chat smoke」(动其一即视为配了 smoke;新加 tests/smoke_*chat*.py 也命中)
CHAT_TEST_PATHS = [
    "tests/smoke_chat.py",
    "tests/smoke_chat_*.py",
    "tests/smoke_*chat*.py",                 # 任何含 chat 的 smoke(含新加的)
    "tests/smoke_t15_chat_unit.py",
    "tests/smoke_execution_intent_gate.py",
    "tests/smoke_fake_action_gate.py",
    "tests/smoke_ws133_no_fabrication_gate.py",
    "tests/smoke_safety.py",
]


def _matches(path: str, patterns: List[str]) -> bool:
    path = path.strip().lstrip("./")
    for pat in patterns:
        if path == pat or fnmatch.fnmatch(path, pat):
            return True
    return False


def check(changed_files: List[str]) -> Tuple[bool, str]:
    """返回 (ok, reason)。ok=False 表示违例:改了 chat 行为却没配 chat smoke。"""
    behavior = [f for f in changed_files if _matches(f, CHAT_BEHAVIOR_PATHS)]
    tests = [f for f in changed_files if _matches(f, CHAT_TEST_PATHS)]
    if behavior and not tests:
        return (
            False,
            "改了 chat 行为代码却没动任何 chat smoke —— 验收 #2 违例:\n"
            + "  碰到的 chat 行为文件:\n"
            + "".join(f"    - {f}\n" for f in behavior)
            + "  需在本 PR 同时新增/修改至少一条 chat smoke(如 tests/smoke_chat.py 加一个 case,"
            "或新建 tests/smoke_<行为>_chat*.py),用来钉死这次行为变更。",
        )
    if behavior and tests:
        return (True, f"chat 行为改动({len(behavior)} 文件)已配 chat smoke 改动({len(tests)} 文件)。")
    return (True, "本 PR 未触及 chat 行为代码,覆盖门 N/A。")


def _git_changed_files(base_ref: str) -> List[str]:
    """PR diff 的改动文件列表(base...HEAD 三点 diff = PR 真实引入的改动)。"""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [l for l in out.splitlines() if l.strip()]


def main_ci() -> int:
    # CI 模式:对 PR 真实 diff 判定。base ref 由 CI 传入(GitHub: pull_request.base.sha)。
    base = os.environ.get("WS154_BASE_REF") or os.environ.get("GITHUB_BASE_REF") or "origin/main"
    try:
        changed = _git_changed_files(base)
    except subprocess.CalledProcessError as e:
        print(f"::error::无法计算 PR diff(base={base}):{e.stderr or e}")
        return 2
    print(f"[ws154-coverage] base={base} 改动文件 {len(changed)} 个")
    ok, reason = check(changed)
    if ok:
        print(f"  ✓ {reason}")
        return 0
    print("  ✗ 验收 #2 覆盖门不通过：")
    print(reason)
    return 1


if __name__ == "__main__":
    sys.exit(main_ci())

"""Smoke: `make test` 必须自动聚合所有 tests/smoke_*.py（排除 chat）。

为什么存在
---------
WS-* 流水线反复栽在 Makefile 上：每个 PR 都往 `test:` 同一行手加自己的 smoke 目标，
多分支并行 → 每次 merge 都在这一行冲突（连环雷）。本改动把 `make test` 改成
自动 glob `tests/smoke_*.py`，新增 smoke 文件无需改 Makefile，从根上消除该冲突。

本 smoke 钉死"自动聚合"这条行为本身（fail-then-pass）：
  - 改动前（Makefile 硬编码 smoke 列表）：本文件 smoke_makefile_autodiscover.py
    不在硬编码列表里 → `make -n test` 不会引用它 → 断言 FAIL。
  - 改动后（glob 聚合）：`make -n test` 引用每一个 tests/smoke_*.py（除 chat）
    → 断言 PASS。

跑法：
  python3 tests/smoke_makefile_autodiscover.py
  （也会被 `make test` 自动收进去）
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def run():
    # 期望被 make test 覆盖的 smoke = 所有 tests/smoke_*.py，除 chat（需 server）
    expected = {
        os.path.basename(p)
        for p in glob.glob(os.path.join(REPO, "tests", "smoke_*.py"))
    }
    expected.discard("smoke_chat.py")

    # make -n test：dry-run 打印将执行的命令（make 会展开 $(SMOKE_FILES)），不真跑
    proc = subprocess.run(
        ["make", "-n", "test"],
        cwd=REPO, capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        print(f"✗ `make -n test` 退出码 {proc.returncode}\n{out[:400]}")
        return 1

    missing = sorted(s for s in expected if s not in out)
    failures = []
    if missing:
        failures.append(f"make test 未自动覆盖这些 smoke: {missing}")

    # 反向：chat 不应被 make test 自动跑（它需要 server）
    if "smoke_chat.py" in out:
        failures.append("make test 不应自动跑 smoke_chat.py（需 server，应只在 make test-chat）")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ {len(failures)} 项失败（改动前 Makefile 硬编码列表会缺新 smoke → 预期 fail）")
        return 1

    print(f"  ✓ make test 自动聚合了全部 {len(expected)} 个 smoke（chat 已正确排除）")
    print("✓ Makefile autodiscover smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

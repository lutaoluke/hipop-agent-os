"""Smoke: chat 端到端回归(smoke_chat.py)必须接进一道独立、会阻断合并的 CI gate。

WS-154 — 「chat smoke 进 CI:没对应 smoke 的 WS 不许 close」
=========================================================
背景(Luke 已拍板,2026-06-10)
  - 不依赖真人登录、确定性的 chat 行为门(smoke_t15_chat_unit / smoke_chat_boundary_contract /
    smoke_ws133_no_fabrication_gate ...)早已被 `make test` 自动聚合,由现有 gate.yml 跑 → 已是 required。
  - 本任务要补的是「全量 live chat e2e」(tests/smoke_chat.py 的 ~25 个真问真答 case,每跑一次真烧一次 LLM)。
    Luke 拍:这道也要进自动关卡、红了就拦。

本 smoke 钉死的是「接线」本身(fail-then-pass),覆盖三种死法里的两种:
  - 接线缺失: 本地有 smoke、CI 没跑     → 断言「存在一个独立 workflow 真的跑 smoke_chat / make test-chat」。
  - 死代码短路: CI 红但被旁路绕过      → 断言该 job/step 没有 `continue-on-error: true`、跑 chat 的命令没有 `|| true`,
                                          失败码必须冒泡成 job 红。
  - 占位假数据: CI 只跑空壳            → 断言 gate 真的喂了 live LLM 凭据(secrets/env),不是不调模型的空跑;
                                          并断言 smoke_chat.py 失败时 `sys.exit(1)` + 逐 case 打印(可定位到具体 case)。

它不依赖 DB / server / LLM,所以会被 `make test` 自动聚合、不拖慢确定性门(criterion #4 不回退)。

注意: 本 smoke 只钉「机制接没接对」。gate 真跑成绿还依赖两件只有 owner 能拍的事:
  1) 在 runner 上提供 live LLM 凭据(DEEPSEEK_API_KEY 等)+ chat 用的 DB;
  2) 在 branch protection 把这道 check 设成 required。
这两点写进 PR 正文交 Luke,不在本 smoke 范围。criterion #2(「改了 chat 行为的 WS 必须配 smoke 才能 close」)的
机械化口径仍待规划小队给定,亦不在本 smoke。

跑法:
  python3 tests/smoke_ws154_chat_ci_gate.py
  (也会被 `make test` 自动收进去)
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW_DIR = os.path.join(REPO, ".github", "workflows")

# 跑 chat e2e 的标志:命中其一即认为「这个 workflow 在跑 chat 全量回归」
CHAT_RUN_MARKERS = ("make test-chat", "test-chat", "smoke_chat.py", "run_smoke.sh")
# live LLM 凭据标志(防空壳):任一即认为真的喂了模型凭据
LLM_CRED_MARKERS = (
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
    "DOUBAO_API_KEY", "ARK_API_KEY", "HIPOP_ENV_FILE",
)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _strip_yaml_comments(text):
    """去掉 YAML 注释,避免「注释里提了一句」造成误判。
    YAML 行内注释需 ` #`(空格+井号)或行首 #;run 命令里的 `#` 极少见,这里按 YAML 规则切。"""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # 整行注释
        m = re.search(r"\s#", line)  # 行内注释:空白后跟 #
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


def _find_chat_gate_workflows():
    """返回所有「在 run 步骤里真跑 chat e2e」的 workflow 文件路径(注释里提到不算)。"""
    hits = []
    for p in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml")) +
                    glob.glob(os.path.join(WORKFLOW_DIR, "*.yaml"))):
        text = _strip_yaml_comments(_read(p))
        if any(m in text for m in CHAT_RUN_MARKERS):
            hits.append(p)
    return hits


def run():
    failures = []

    # ── 1) 必须存在一个独立 workflow 真的跑 chat e2e(接线缺失) ──────────
    chat_wfs = _find_chat_gate_workflows()
    if not chat_wfs:
        failures.append(
            "没有任何 .github/workflows/*.yml 跑 chat e2e(make test-chat / smoke_chat.py)"
            " —— 接线缺失:本地有 smoke,CI 没跑。"
        )
        # 没接线就没法继续校验后续契约,直接报告
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-154 chat CI gate 接线缺失(改动前预期 fail)")
        return 1

    gate_text = "\n".join(_strip_yaml_comments(_read(p)) for p in chat_wfs)
    names = ", ".join(os.path.basename(p) for p in chat_wfs)
    print(f"  · 跑 chat e2e 的 workflow: {names}")

    # ── 2) 必须在 PR → main 上触发(否则合并前根本不跑) ──────────────────
    if "pull_request" not in gate_text:
        failures.append("chat gate 不在 pull_request 上触发 —— 合并前不会跑,挡不住。")
    elif not re.search(r"branches:\s*\[?\s*['\"]?main", gate_text):
        failures.append("chat gate 的 pull_request 没限定到 main 分支(无法确认它守的是主干合并)。")

    # ── 3) 必须喂 live LLM 凭据(占位假数据/空壳) ──────────────────────────
    if not any(m in gate_text for m in LLM_CRED_MARKERS):
        failures.append(
            "chat gate 没引用任何 live LLM 凭据(DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / HIPOP_ENV_FILE ...)"
            " —— 像是不调模型的空壳跑,占位假数据死法。"
        )

    # ── 4) 失败码必须冒泡(死代码短路:CI 红但被绕过) ─────────────────────
    if re.search(r"continue-on-error:\s*true", gate_text):
        failures.append("chat gate 出现 `continue-on-error: true` —— 失败被吞,红了也不拦,死代码短路。")
    # 跑 chat 的命令行不许用 `|| true` 把退出码洗白
    for line in gate_text.splitlines():
        if any(m in line for m in CHAT_RUN_MARKERS) and "|| true" in line:
            failures.append(f"chat 运行命令用 `|| true` 洗白了退出码,失败不会冒泡:`{line.strip()}`")

    # ── 5) smoke_chat.py 本体:失败 exit≠0 + 逐 case 可定位(criterion #1/#3) ─
    chat_smoke = os.path.join(REPO, "tests", "smoke_chat.py")
    if not os.path.exists(chat_smoke):
        failures.append("tests/smoke_chat.py 不存在 —— gate 跑的是空气。")
    else:
        cs = _read(chat_smoke)
        if "sys.exit(1)" not in cs:
            failures.append("smoke_chat.py 失败路径没有 sys.exit(1) —— 失败不会让 gate 变红。")
        # 逐 case 打印形如 `[{i}/{len(cases)}] {c.name}` → CI 输出可定位到具体 case
        if not re.search(r"\[\{i\}/\{len\(cases\)\}\]", cs):
            failures.append("smoke_chat.py 未逐 case 打印进度(CI 输出无法定位到具体失败 case,criterion #3)。")

    # ── 6) 确定性 chat 子集仍由 `make test` 守(criterion #4 不回退) ─────────
    mk = _read(os.path.join(REPO, "Makefile"))
    if "smoke_*.py" not in mk or "filter-out tests/smoke_chat.py" not in mk:
        failures.append(
            "Makefile 不再自动聚合 smoke_*.py(且排除 smoke_chat.py)"
            " —— 确定性 chat 行为门可能脱离 `make test`,criterion #4 回退风险。"
        )

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-154 chat CI gate 契约 {len(failures)} 项不满足")
        return 1

    print("  ✓ chat e2e 独立 gate 已接线、PR→main 触发、喂 live LLM 凭据、失败码冒泡")
    print("  ✓ smoke_chat.py 失败 exit≠0 且逐 case 可定位;确定性子集仍由 make test 守")
    print("✓ WS-154 chat CI gate 接线 smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

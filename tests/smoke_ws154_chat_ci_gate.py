"""Smoke: WS-154 验收 #1/#3 — live 全量 chat e2e 阻断门(方案 ①)真接上、安全、抗 echo-bypass。

钉死(fail-then-pass：落地前红「接线缺失」,落地后绿):
1. 存在独立 workflow 在 PR→main 上跑 chat e2e,且**结构上真用 bash/python/make 执行**门逻辑/`smoke_chat.py`,
   未被 `echo`/`if echo` 桩、`|| true`、`continue-on-error` 吞掉退出码(回应验门人 echo-bypass 红队;带反例用例)。
2. 安全闸(方案 ①):job 带 `if: ...head.repo.full_name == github.repository`,fork PR 不跑 → 不触达 self-hosted/凭据。
3. self-hosted runner(label `hipop-live`):凭据从 runner 环境取,不从仓库/PR 取(断言用了 HIPOP_ENV_FILE,workflow 里无明文 key)。
4. 运行脚本 tests/ci_chat_e2e_gate.sh:有 preflight(缺凭据 exit≠0)、retry(attempts=2)、两次都红才 exit 1
   (确定性真红不被洗白)。这条**直接跑脚本**验证运行时行为,不只静态搜字符串。
5. smoke_chat.py 失败 sys.exit(1) 且逐 case 打印(验收 #3 可定位)。
6. 已上线的 chat 覆盖声明门 + Makefile 自动聚合保持(验收 #4 不回退)。
7. live gate 每次必须启动当前 checkout 的 server；不能因为 :8765 已 health 就复用旧进程。

确定性、无 DB/server/LLM(脚本自测走 WS154_SELFTEST 跳过 server,用假 smoke 命令),被 make test 自动聚合。
"""
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW_DIR = os.path.join(REPO, ".github", "workflows")
GATE_SCRIPT_REL = "tests/ci_chat_e2e_gate.sh"
GATE_SCRIPT = os.path.join(REPO, "tests", "ci_chat_e2e_gate.sh")
CHAT_TARGETS = ("ci_chat_e2e_gate.sh", "smoke_chat.py", "make test-chat", "run_smoke.sh")


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
    """结构校验:target 是否被 bash/python/make 真实执行,且没被 echo 桩 / 吞退出码。"""
    # 解释器/真实执行 token:bash/sh/python[3]/make 或 $PY、$PYTHON、${PY}、${PYTHON} 这类 python 别名变量
    real = re.search(
        rf"(?m)^[^#]*(\b(bash|sh|python3?|make)\b|\$\{{?PY(THON)?\}}?)[^\n]*{re.escape(target)}", text
    )
    echo_stub = re.search(rf"(?m)^\s*(echo|if\s+echo|test\s)[^\n]*{re.escape(target)}", text)
    swallowed = any(target in ln and "|| true" in ln for ln in text.splitlines()) \
        or bool(re.search(r"continue-on-error:\s*true", text))
    return bool(real) and not echo_stub and not swallowed


def _find_chat_gate():
    for p in sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.yml")) +
                    glob.glob(os.path.join(WORKFLOW_DIR, "*.yaml"))):
        text = _strip_yaml_comments(_read(p))
        if any(t in text for t in CHAT_TARGETS):
            return p, text
    return None, None


def _run_script(env_extra, expect):
    """跑 ci_chat_e2e_gate.sh,返回 (ok, rc)。expect 是期望退出码。"""
    env = dict(os.environ)
    env.update(env_extra)
    try:
        r = subprocess.run(["bash", GATE_SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        return (r.returncode == expect, r.returncode)
    except Exception as e:
        return (False, f"exc:{e}")


def run():
    failures = []

    # ── 1) 存在 chat e2e gate workflow,结构上真执行 ───────────────────────
    p, text = _find_chat_gate()
    if not p:
        print("  ✗ 没有 workflow 跑 live chat e2e(ci_chat_e2e_gate.sh / smoke_chat.py)—— 接线缺失。")
        print("\n✗ WS-154 live chat e2e gate 接线缺失(改动前预期 fail)")
        return 1
    print(f"  · live chat e2e gate workflow: {os.path.basename(p)}")

    if "pull_request" not in text or not re.search(r"branches:\s*\[?\s*['\"]?main", text):
        failures.append("gate 没在 pull_request→main 触发,合并前不跑。")
    if not _runs_real(text, "ci_chat_e2e_gate.sh"):
        failures.append("workflow 没用 bash 真实执行 ci_chat_e2e_gate.sh(疑似 echo 桩 / 吞退出码)。")

    # ── 2) 安全闸:非 fork 才跑 ────────────────────────────────────────────
    if "head.repo.full_name == github.repository" not in text.replace(" ", "").replace("'", "").replace('"', "") \
       and "head.repo.full_name==github.repository" not in text.replace(" ", "").replace("'", "").replace('"', ""):
        failures.append("gate 缺「非 fork 才跑」安全闸(head.repo.full_name == github.repository)—— 公开仓会重蹈 pwn-request。")

    # ── 3) self-hosted runner + 凭据从 runner 环境取,不从仓库/PR ──────────
    if "self-hosted" not in text:
        failures.append("gate 不在 self-hosted runner 上跑(方案 ① 需要 live 环境的自托管机器)。")
    if "HIPOP_ENV_FILE" not in text:
        failures.append("gate 没从 runner 环境(HIPOP_ENV_FILE)取凭据。")
    # 明文密钥泄漏检查:workflow 里不许出现真实 key(只许引用 env 文件 / vars)
    if re.search(r"(DEEPSEEK_API_KEY|ANTHROPIC_API_KEY|JWT_SECRET)\s*[:=]\s*[A-Za-z0-9/_-]{12,}", text):
        failures.append("workflow 里疑似写了明文凭据 —— 必须从 runner 环境取,不可入库。")

    # ── 4) 运行脚本的真实行为:preflight 红 / retry 不洗白真红 / flake 吸收 ──
    if not os.path.exists(GATE_SCRIPT):
        failures.append(f"{GATE_SCRIPT_REL} 不存在 —— gate 跑的是空气。")
    else:
        st = _read(GATE_SCRIPT)
        if not _runs_real(st, "smoke_chat.py"):
            failures.append("ci_chat_e2e_gate.sh 没用 python 真实执行 smoke_chat.py。")
        if "attempts=2" not in st.replace(" ", ""):
            failures.append("脚本没有 attempts=2 的重试。")
        start_idx = st.find('-m uvicorn hipop.server.main:app')
        health_gate_idx = st.find('if ! curl -sS -m 3 "$URL/health"')
        if 0 <= health_gate_idx < start_idx:
            failures.append(
                "ci_chat_e2e_gate.sh 只在 :8765 health 失败时才启动 server，"
                "会复用旧进程测试旧代码。live gate 必须先清理监听端口并启动当前 checkout。"
            )
        if "lsof -tiTCP" not in st or "STARTED_SERVER" not in st:
            failures.append("ci_chat_e2e_gate.sh 缺少端口占用清理 + 自己启动的 server 句柄，无法保证测的是当前 checkout。")
        if 'kill -0 "$STARTED_SERVER"' not in st:
            failures.append("ci_chat_e2e_gate.sh 启动后未校验 STARTED_SERVER 仍存活，旧 server 可能抢回端口并用 health 蒙混。")
        if "pick_free_port" not in st or "HIPOP_PUBLIC_BASE_URL" not in st:
            failures.append("ci_chat_e2e_gate.sh 缺少 8765 无法释放时的专属端口 fallback，persistent 旧 server 会卡死 live gate。")
        # 运行时验证(比静态搜字符串强):
        fake_env = "/tmp/ws154_smoke_fake_env"
        with open(fake_env, "w") as f:
            f.write("DEEPSEEK_API_KEY=fake\nDB_URL=postgresql://x\nJWT_SECRET=fake\n")
        # a) preflight 缺凭据 → 非 0
        ok, rc = _run_script({"HIPOP_ENV_FILE": "/nonexistent_ws154"}, expect=3)
        if not ok:
            failures.append(f"preflight 缺凭据没红(期望 exit 3,实得 {rc})—— 可能空壳绿。")
        # b) 确定性真红(smoke 每次都 fail)→ 两次都红 → exit 1(不被洗白)
        ok, rc = _run_script(
            {"WS154_SELFTEST": "1", "WS154_SMOKE_CMD": "false", "HIPOP_ENV_FILE": fake_env}, expect=1)
        if not ok:
            failures.append(f"retry 把确定性真红洗白了(期望 exit 1,实得 {rc})—— 重试不许掩盖真 bug。")
        # c) 一次过 → exit 0
        ok, rc = _run_script(
            {"WS154_SELFTEST": "1", "WS154_SMOKE_CMD": "true", "HIPOP_ENV_FILE": fake_env}, expect=0)
        if not ok:
            failures.append(f"smoke 通过时 gate 没绿(期望 exit 0,实得 {rc})。")
        if not failures:
            print("  ✓ 运行时验证:preflight 缺凭据→exit3、确定性真红→exit1(不洗白)、通过→exit0")

    # ── 5) echo-bypass 反例:结构校验必须对它判红 ─────────────────────────
    echo_bypass = "    steps:\n      - run: if echo ci_chat_e2e_gate.sh; then echo ok; fi\n"
    if _runs_real(echo_bypass, "ci_chat_e2e_gate.sh"):
        failures.append("结构校验被 `if echo <script>` 蒙混 —— 反 echo-bypass 失败。")
    else:
        print("  ✓ 反 echo-bypass:`if echo <script>` 桩被结构校验判红")

    # ── 6) smoke_chat.py 失败 exit≠0 + 逐 case 可定位 ─────────────────────
    cs_path = os.path.join(REPO, "tests", "smoke_chat.py")
    if not os.path.exists(cs_path):
        failures.append("tests/smoke_chat.py 不存在。")
    else:
        cs = _read(cs_path)
        if "sys.exit(1)" not in cs:
            failures.append("smoke_chat.py 失败路径无 sys.exit(1)。")
        if not re.search(r"\[\{i\}/\{len\(cases\)\}\]", cs):
            failures.append("smoke_chat.py 未逐 case 打印(验收 #3 无法定位)。")

    # ── 7) 不回退:覆盖声明门 + Makefile 自动聚合仍在 ─────────────────────
    mk = _read(os.path.join(REPO, "Makefile"))
    if "smoke_*.py" not in mk or "filter-out tests/smoke_chat.py" not in mk:
        failures.append("Makefile 自动聚合/排除 chat 被破坏(验收 #4 回退风险)。")
    if not os.path.exists(os.path.join(REPO, "tests", "ws154_chat_coverage_gate.py")):
        failures.append("已上线的 chat 覆盖声明门(ws154_chat_coverage_gate.py)不见了 —— 不许回退。")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-154 live chat e2e gate smoke {len(failures)} 项不满足")
        return 1

    print("  ✓ live chat e2e gate 已接线、非 fork 安全闸、self-hosted+runner 取凭据、retry 不洗白真红、抗 echo-bypass")
    print("✓ WS-154 live chat e2e gate 接线 smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

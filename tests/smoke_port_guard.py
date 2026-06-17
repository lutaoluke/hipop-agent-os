"""smoke_port_guard.py — WS-194 fail-then-pass smoke

验收（WS-194）：hipop/server/main.py 启动器加端口护栏——
非正式服务（XPC_SERVICE_NAME != com.hipop.workbench）绑定端口 8765 时
直接拒启并打印可操作提示；正式服务、非生产端口、逃生门均不受影响。

FAIL 条件（修前）：
  - _check_prod_port_guard 不存在，或存在但不调用 os._exit(1)
  - 场景 1（无 XPC）无拒启
  - 场景 2（XPC=pr119）无拒启
  - 正路（正式服务 / 8766 端口 / 逃生门）被误拒

PASS 条件（修后）：
  - 场景 1/2 中 os._exit(1) 被调用，stderr 含 '8765 生产端口' 提示
  - 正路均无 os._exit
"""

import ast
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def _load_guard_fn():
    """从 main.py 用 AST 提取 _check_prod_port_guard，不触发模块级代码（FastAPI 等）。"""
    src = (REPO / "hipop/server/main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_check_prod_port_guard":
            mod = ast.Module(body=[node], type_ignores=[])
            code = compile(mod, str(REPO / "hipop/server/main.py"), "exec")
            ns = {"os": os, "sys": sys}
            exec(code, ns)
            return ns["_check_prod_port_guard"]
    raise RuntimeError("_check_prod_port_guard 未在 hipop/server/main.py 中找到")


_guard = _load_guard_fn()


def _call_guard(argv, env):
    """用指定 argv 和环境变量调用 guard，返回 (exit_called, exit_code, stderr_lines)。"""
    stderr_out = []
    exit_calls = []

    import io

    def _fake_exit(code):
        exit_calls.append(code)

    fake_stderr = io.StringIO()

    with patch.object(sys, "argv", argv):
        with patch.dict(os.environ, env, clear=True):
            with patch("os._exit", side_effect=_fake_exit):
                with patch("sys.stderr", fake_stderr):
                    _guard()

    stderr_text = fake_stderr.getvalue()
    return exit_calls, stderr_text


# ── Test 1: 端口=8765 且 XPC 缺失 → 被拒 ──────────────────────────────────────

def test_port_8765_no_xpc_rejected():
    """端口=8765 且 XPC_SERVICE_NAME 未设置 → os._exit(1) 被调用。

    FAIL（修前）：guard 不存在或不拒启。
    PASS（修后）：os._exit(1) 调用，stderr 含 '8765 生产端口'。
    """
    exits, stderr = _call_guard(
        argv=["uvicorn", "hipop.server.main:app", "--port", "8765"],
        env={},  # 无 XPC_SERVICE_NAME
    )
    assert exits == [1], (
        f"端口=8765 且无 XPC_SERVICE_NAME 时 os._exit(1) 应被调用，实际 exit_calls={exits}\n"
        "修前 FAIL 预期：guard 未加或无拒启逻辑。"
    )
    assert "8765" in stderr and ("生产端口" in stderr or "8765 是 HIPOP" in stderr or "FATAL" in stderr), (
        f"stderr 应含 '8765 生产端口' 提示，实际: {stderr!r}"
    )
    print(f"    exit(1) ✓  stderr={stderr.strip()[:80]!r}")


# ── Test 2: 端口=8765 且 XPC=PR 服务 → 被拒 ───────────────────────────────────

def test_port_8765_pr_xpc_rejected():
    """端口=8765 且 XPC_SERVICE_NAME=com.hipop.workbench.pr119（临时 PR 服务）→ 被拒。

    FAIL（修前）：guard 未加。
    PASS（修后）：os._exit(1) 调用。
    """
    exits, stderr = _call_guard(
        argv=["uvicorn", "hipop.server.main:app", "--port", "8765"],
        env={"XPC_SERVICE_NAME": "com.hipop.workbench.pr119"},
    )
    assert exits == [1], (
        f"PR 临时服务绑 8765 时应被拒，实际 exit_calls={exits}\n"
        "修前 FAIL 预期：guard 未加，PR 服务可任意占生产端口。"
    )
    print(f"    exit(1) ✓  xpc=pr119 被拒")


# ── Test 3: 端口=8765 且 XPC=官方服务 → 放行 ───────────────────────────────────

def test_port_8765_official_xpc_allowed():
    """端口=8765 且 XPC_SERVICE_NAME=com.hipop.workbench（正式 launchd 服务）→ 放行。

    修前修后均应 PASS（若 guard 存在则为正路测试）。
    """
    exits, stderr = _call_guard(
        argv=["uvicorn", "hipop.server.main:app", "--port", "8765"],
        env={"XPC_SERVICE_NAME": "com.hipop.workbench"},
    )
    assert exits == [], (
        f"正式服务绑 8765 不应被拒，实际 exit_calls={exits}\n  stderr={stderr!r}"
    )
    print("    正式服务放行 ✓")


# ── Test 4: 端口=8766（临时口）→ 不受影响 ─────────────────────────────────────

def test_non_prod_port_unaffected():
    """端口=8766（临时/开发端口）→ guard 不介入，不论 XPC 是否设置。

    修前修后均应 PASS（guard 应只看 8765）。
    """
    for env in ({}, {"XPC_SERVICE_NAME": "com.hipop.workbench.pr119"}):
        exits, stderr = _call_guard(
            argv=["uvicorn", "hipop.server.main:app", "--port", "8766"],
            env=env,
        )
        assert exits == [], (
            f"非生产端口 8766 不应被拒，实际 exit_calls={exits}  env={env}"
        )
    print("    8766 端口不受影响 ✓")


# ── Test 5: HIPOP_ALLOW_PROD_PORT=1 逃生门 → 放行 ─────────────────────────────

def test_escape_hatch_bypasses_guard():
    """HIPOP_ALLOW_PROD_PORT=1 时即使 XPC 不对也放行（运维应急逃生门）。

    FAIL（修前）：guard 未加（逃生门无从测试）。
    PASS（修后）：HIPOP_ALLOW_PROD_PORT=1 跳过所有拒启逻辑。
    """
    exits, stderr = _call_guard(
        argv=["uvicorn", "hipop.server.main:app", "--port", "8765"],
        env={"HIPOP_ALLOW_PROD_PORT": "1"},  # 无 XPC_SERVICE_NAME
    )
    assert exits == [], (
        f"HIPOP_ALLOW_PROD_PORT=1 时不应拒启，实际 exit_calls={exits}\n  stderr={stderr!r}"
    )
    print("    逃生门 HIPOP_ALLOW_PROD_PORT=1 生效 ✓")


# ── Test 6: --port=8765 (= 号形式) → 被拒 ────────────────────────────────────

def test_port_equals_form_rejected():
    """--port=8765（等号形式 argv）也能被识别并拒启。"""
    exits, stderr = _call_guard(
        argv=["uvicorn", "hipop.server.main:app", "--port=8765"],
        env={},
    )
    assert exits == [1], (
        f"--port=8765 形式应被识别并拒启，实际 exit_calls={exits}"
    )
    print("    --port=8765 等号形式识别 ✓")


# ── Test 7: 无 --port 参数（正常 dev 启动）→ 不受影响 ─────────────────────────

def test_no_port_arg_unaffected():
    """sys.argv 无 --port 参数（如直接 python main.py）→ guard 不介入。"""
    exits, stderr = _call_guard(
        argv=["python", "hipop/server/main.py"],
        env={},
    )
    assert exits == [], (
        f"无 --port 参数时不应拒启，实际 exit_calls={exits}"
    )
    print("    无 --port 参数不受影响 ✓")


# ── main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_port_guard — WS-194 端口护栏（非 official 服务拒绑 8765）")

    tests = [
        ("test_port_8765_no_xpc_rejected", test_port_8765_no_xpc_rejected),
        ("test_port_8765_pr_xpc_rejected", test_port_8765_pr_xpc_rejected),
        ("test_port_8765_official_xpc_allowed", test_port_8765_official_xpc_allowed),
        ("test_non_prod_port_unaffected", test_non_prod_port_unaffected),
        ("test_escape_hatch_bypasses_guard", test_escape_hatch_bypasses_guard),
        ("test_port_equals_form_rejected", test_port_equals_form_rejected),
        ("test_no_port_arg_unaffected", test_no_port_arg_unaffected),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            first_line = str(e).split("\n")[0]
            print(f"  ✗ {name}: {first_line}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_port_guard all {len(tests)} passed")

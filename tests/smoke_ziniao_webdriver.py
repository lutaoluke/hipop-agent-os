"""Smoke: 紫鸟 web_driver 运维入口 healthcheck/start 真实性（WS-33.4 / WS-42）
— fail-then-pass 承重墙。

钉死 `hipop/scripts/ziniao_webdriver` 的健康检查/启动语义（无需真实紫鸟：只替身两类
外部边界 —— `_platform_browser._webdriver_listening`（TCP 探 18080）、`_cdp_version`
（GET /json/version）；状态文件用临时目录真落盘真读）：

  ① 端口在听 + live session CDP 可达 → 退出 0，且真报出 CDP 活着。
  ② 端口在听 + session CDP 不可达（端口已死/过期）→ 退出 0（webdriver 本身在），
     但**如实**报"不可达"，不谎报 CDP ok。
  ③ 端口未监听 → 退出 3（blocked），且打印拉起命令；**绝不**退出 0。
  ④ start 时缺紫鸟 app → 退出 2（不静默假成功，不乱跑 open）。
  ⑤ 旧 probe_ziniao_webdriver 已 deprecated：不再暴露手传 debuggPort 的 start 路径。

fail-then-pass 证明（env 开关复刻"占位假数据"死法）：
  - 默认跑 → 全过。
  - SMOKE_ZINIAO_FAKE_HEALTH=1 → 让 healthcheck 的端口探测**永远报在听**（即"只 echo
    文案、不真检 18080"的死法）→ ③ 的「端口 down → blocked」断言 FAIL。
  改动前（ziniao_webdriver 还不存在）整个文件 ImportError 即全 fail。

跑法：
  python3 tests/smoke_ziniao_webdriver.py
  SMOKE_ZINIAO_FAKE_HEALTH=1 python3 tests/smoke_ziniao_webdriver.py   # 看回归 fail
  （也被 make test 自动聚合）
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 状态文件落临时目录，绝不污染真实 ~/hipop。
_TMP_STATE = tempfile.mkdtemp(prefix="smoke_ziniao_wd_")
os.environ["HIPOP_STATE_DIR"] = _TMP_STATE
os.environ.setdefault("ZINIAO_USERNAME", "smoke-wd-user")
os.environ.setdefault("ZINIAO_PASSWORD", "smoke-wd-pw")

from hipop.server import _platform_browser as pb  # noqa: E402
from hipop.scripts import ziniao_webdriver as zw  # noqa: E402

PORT = pb._webdriver_port()
FAKE_HEALTH = os.environ.get("SMOKE_ZINIAO_FAKE_HEALTH") == "1"


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")


def _set_listening(value: bool):
    """替身 18080 端口探测。death 开关下永远报在听（= 不真检端口的占位死法）。"""
    pb._PERSIST_DIR = _TMP_STATE  # _platform_browser 在 import 时已读 env，这里再钉死。
    if FAKE_HEALTH:
        pb._webdriver_listening = lambda port=None, timeout=1.5: True
    else:
        pb._webdriver_listening = lambda port=None, timeout=1.5: value


def _set_cdp(reachable: bool):
    pb._cdp_version = lambda cdp_url, timeout=3.0: (
        {"Browser": "Chrome/fake"} if reachable else None)


def _write_state(name="ziniao_state_smoke.json", *, cdp="http://127.0.0.1:40000",
                 expires_at=4_000_000_000.0):
    for f in os.listdir(_TMP_STATE):
        os.remove(os.path.join(_TMP_STATE, f))
    with open(os.path.join(_TMP_STATE, name), "w") as f:
        json.dump({"store_id": "smoke", "store_name": "smoke-noon-sa",
                   "cdp_url": cdp, "debugging_port": 40000,
                   "expires_at": expires_at}, f)


def _run_health():
    lines = []
    rc = zw.healthcheck(emit=lines.append)
    return rc, "\n".join(lines)


def run():
    check = _Checker()
    pb._PERSIST_DIR = _TMP_STATE

    # ── ① 端口在听 + CDP 可达 ──
    print("== ① 端口在听 + live session CDP 可达 → 0 且真报 CDP 活 ==")
    _set_listening(True)
    _set_cdp(True)
    _write_state()
    rc, out = _run_health()
    check("退出 0", rc == 0, f"got {rc}")
    check("报出 webdriver 端口在听", f"127.0.0.1:{PORT}" in out and "在听" in out)
    check("真报出对应 CDP /json/version 可达", "可达" in out and "Chrome/fake" in out, out)

    # ── ② 端口在听 + CDP 不可达 → 仍 0，但如实报不可达 ──
    print("== ② 端口在听 + session CDP 不可达 → 0，但不谎报 CDP ok ==")
    _set_listening(True)
    _set_cdp(False)
    _write_state()
    rc, out = _run_health()
    check("退出 0（webdriver 本身在）", rc == 0, f"got {rc}")
    check("如实报 CDP 不可达（不谎报可达）", "不可达" in out, out)

    # ── ③ 端口未监听 → blocked(3) + 打印拉起命令 ──
    print("== ③ 端口未监听 → 退出 3 blocked + 拉起命令 ==")
    _set_listening(False)
    _set_cdp(False)
    rc, out = _run_health()
    check("端口 down → 退出 3（blocked）", rc == 3, f"got {rc}")
    check("打印 run_type=web_driver 拉起命令",
          "--run_type=web_driver" in out, out)
    check("指向 ziniao_webdriver start 入口",
          "ziniao_webdriver start" in out, out)

    # ── ④ start 缺 app → 退出 2，不静默假成功 ──
    print("== ④ start 缺紫鸟 app → 退出 2（不假成功）==")
    _set_listening(False)
    _orig_app = zw.ZINIAO_APP
    try:
        zw.ZINIAO_APP = os.path.join(_TMP_STATE, "no-such-ziniao.app")
        lines = []
        rc = zw.start(emit=lines.append)
        check("缺 app → 退出 2", rc == 2, f"got {rc}")
        check("提示找不到紫鸟客户端", "找不到紫鸟" in "\n".join(lines))
    finally:
        zw.ZINIAO_APP = _orig_app

    # ── ⑤ 旧 probe deprecated：不再暴露手传 debuggPort 的 start 路径 ──
    print("== ⑤ 旧 probe_ziniao_webdriver 已 deprecated ==")
    from hipop.scripts import probe_ziniao_webdriver as probe
    check("probe 文档标 DEPRECATED", "DEPRECATED" in (probe.__doc__ or ""))
    check("probe 不再有手传 debuggPort 的 start_one 入口",
          not hasattr(probe, "start_one"))
    check("probe 指向 _platform_browser.get_platform_session",
          "get_platform_session" in (probe.__doc__ or ""))

    print()
    if FAKE_HEALTH and check.failures:
        print("  （SMOKE_ZINIAO_FAKE_HEALTH=1：healthcheck 端口探测被退化成『永远在听』"
              "→ 端口 down 也谎报 0，这是预期的『改动前/占位假数据 fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print("✓ 紫鸟 web_driver 运维入口 healthcheck/start/deprecation smoke 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""紫鸟超级浏览器 web_driver 模式的**本机运维入口**（WS-33.4 / WS-42）。

定位
----
`hipop/server/_platform_browser.py` 的 `get_platform_session` 依赖紫鸟 web_driver 模式常驻
在 `127.0.0.1:18080`。这条以前是口头前置（"先手动 open -na ziniao …"），属于隐性人工动作 ——
紫鸟客户端退出 / 端口断 / Mac 重启后就静默断链。本模块把它变成**可复跑、可健康检查、可守护**
的运维入口，配套 launchd `com.hipop.ziniao.plist` 做开机自启 + keepalive。

进程层只负责把紫鸟 web_driver 拉起 / 守活；紫鸟**账号认证**仍由每次 `_platform_browser`
调用带 company/username/password 完成（见 `resolve_credentials`），这里**不引入任何"紫鸟
token"状态**。

子命令
------
  start        退出旧 ziniao → `open -na … --run_type=web_driver --port=18080` → 等端口起。
  stop         `pkill -TERM -i ziniao`。
  restart      stop + start。
  healthcheck  **真实**检测：①TCP 连 `127.0.0.1:18080`（webdriver 控制端口在听）；
               ②对 `~/hipop/ziniao_state_*.json` 里每个 live session，GET 其
               `cdp_url/json/version` 验证 chromium debug 端口真实可达。
               绝不只 echo 文案。端口 down → 退出码 3（blocked）+ 明确启动命令。
  daemon       launchd 入口：循环确保端口在听，断了就 restart。自身常驻，配 KeepAlive。

健康检查 / 端口 / CDP 探测全部**复用** `_platform_browser` 的真函数（`_webdriver_listening`
/ `_cdp_version` / `_PERSIST_DIR`），不另起一套绕过协议层的逻辑。

退出码
------
  0  健康（webdriver 端口在听 / 命令成功）
  2  用法错误 / 缺 ziniao.app
  3  blocked —— webdriver 端口未监听（需人工或 daemon 拉起）

跑法
----
  python3 -m hipop.scripts.ziniao_webdriver healthcheck
  python3 -m hipop.scripts.ziniao_webdriver start
  python3 -m hipop.scripts.ziniao_webdriver daemon          # launchd 用
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

# 复用 _platform_browser 的真探测函数 —— 不另写一套绕过协议层的端口/CDP 逻辑。
from hipop.server import _platform_browser as pb

ZINIAO_APP = os.environ.get("ZINIAO_APP_PATH", "/Applications/ziniao.app")
_DAEMON_INTERVAL = int(os.environ.get("ZINIAO_DAEMON_INTERVAL", "30"))
_START_TIMEOUT = int(os.environ.get("ZINIAO_START_TIMEOUT", "40"))


def _port() -> int:
    return pb._webdriver_port()


def _start_cmd(port: int) -> str:
    return (f"open -na {ZINIAO_APP} --args --run_type=web_driver --port={port}")


# ── 进程动作 ────────────────────────────────────────────────────────────
def stop(emit=print) -> int:
    """退出紫鸟客户端（容忍"没在跑"）。"""
    emit("→ pkill -TERM -i ziniao")
    subprocess.run(["pkill", "-TERM", "-i", "ziniao"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def start(emit=print, *, wait: bool = True) -> int:
    """拉起紫鸟 web_driver 模式，等端口起。缺 app → 退出 2；超时未起 → 退出 3。"""
    port = _port()
    if not os.path.exists(ZINIAO_APP):
        emit(f"✗ 找不到紫鸟客户端：{ZINIAO_APP}（设 ZINIAO_APP_PATH 覆写）")
        return 2
    if pb._webdriver_listening(port):
        emit(f"✓ 端口 127.0.0.1:{port} 已在听，无需重启")
        return 0
    # 先确保没有残留的非 web_driver 实例占着。
    stop(emit)
    time.sleep(2)
    emit(f"→ {_start_cmd(port)}")
    rc = subprocess.run(
        ["open", "-na", ZINIAO_APP, "--args",
         "--run_type=web_driver", f"--port={port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc.returncode != 0:
        emit(f"✗ open 启动失败：{rc.stderr.decode('utf-8', 'replace').strip()}")
        return 2
    if not wait:
        return 0
    deadline = time.time() + _START_TIMEOUT
    while time.time() < deadline:
        if pb._webdriver_listening(port):
            emit(f"✓ 端口 127.0.0.1:{port} 已起（等了 "
                 f"{int(_START_TIMEOUT - (deadline - time.time()))}s）")
            return 0
        time.sleep(2)
    emit(f"✗ 等了 {_START_TIMEOUT}s 端口 {port} 仍未监听 —— blocked，"
         f"请手动确认紫鸟已登录并允许 web_driver 模式")
    return 3


def restart(emit=print) -> int:
    stop(emit)
    time.sleep(2)
    return start(emit)


# ── 真实健康检查 ────────────────────────────────────────────────────────
def _live_sessions():
    """读 `~/hipop/ziniao_state_*.json`，对每个 cdp_url 真实探测 /json/version。
    返回 [(path, store_name, cdp_url, version_or_None, expired_bool)]。"""
    out = []
    for path in sorted(glob.glob(os.path.join(pb._PERSIST_DIR, "ziniao_state_*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        cdp = d.get("cdp_url")
        exp = d.get("expires_at")
        expired = bool(exp) and time.time() >= float(exp)
        ver = pb._cdp_version(cdp) if cdp else None
        out.append((os.path.basename(path), d.get("store_name") or d.get("store_id"),
                    cdp, ver, expired))
    return out


def healthcheck(emit=print) -> int:
    """真实健康检查：18080 监听 + 每个 live session 的 CDP /json/version 可达性。
    端口 down → 退出 3（blocked）。绝不只打印操作手册。"""
    port = _port()
    listening = pb._webdriver_listening(port)
    emit(f"=== 紫鸟 web_driver healthcheck @127.0.0.1:{port} ===")
    if not listening:
        emit(f"✗ webdriver 控制端口 127.0.0.1:{port} 未监听 —— blocked")
        emit(f"  拉起：{_start_cmd(port)}")
        emit(f"  或：python3 -m hipop.scripts.ziniao_webdriver start")
        emit(f"  常驻：bash hipop/launchd/install.sh install（含 com.hipop.ziniao keepalive）")
        return 3
    emit(f"✓ webdriver 控制端口 127.0.0.1:{port} 在听")

    sessions = _live_sessions()
    if not sessions:
        emit("· 暂无 live session state（~/hipop/ziniao_state_*.json）—— "
             "首次取数时 _platform_browser 会 startBrowser 并落盘")
        return 0
    for fname, name, cdp, ver, expired in sessions:
        tag = "（已过 TTL，下次取数会重拉）" if expired else ""
        if ver:
            emit(f"✓ {name}: CDP {cdp} /json/version 可达 → "
                 f"{ver.get('Browser', '?')} {tag}")
        else:
            emit(f"· {name}: CDP {cdp} 不可达{tag} —— 该 store 端口已死，"
                 f"下次取数 _platform_browser 自动回落重 start")
    return 0


# ── launchd 守护 ────────────────────────────────────────────────────────
def daemon(emit=print) -> int:
    """launchd 入口：循环确保 webdriver 端口在听，断了就 restart。自身常驻
    （配 plist 的 KeepAlive：本进程若崩，launchd 重新拉起 daemon）。"""
    port = _port()
    emit(f"[ziniao-daemon] 启动，看护 127.0.0.1:{port}，每 {_DAEMON_INTERVAL}s 巡检")
    # 启动即确保一次（开机自启场景：紫鸟还没起）。
    if not pb._webdriver_listening(port):
        emit("[ziniao-daemon] 端口未监听，首次拉起")
        start(emit)
    while True:
        time.sleep(_DAEMON_INTERVAL)
        if not pb._webdriver_listening(port):
            emit(f"[ziniao-daemon] {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"端口 {port} 断了，restart")
            start(emit)


_CMDS = {
    "start": lambda: start(),
    "stop": stop,
    "restart": restart,
    "healthcheck": healthcheck,
    "health": healthcheck,
    "status": healthcheck,
    "daemon": daemon,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="紫鸟 web_driver 本机运维入口")
    p.add_argument("cmd", choices=sorted(_CMDS), help="子命令")
    args = p.parse_args(argv)
    return _CMDS[args.cmd]() or 0


if __name__ == "__main__":
    sys.exit(main())

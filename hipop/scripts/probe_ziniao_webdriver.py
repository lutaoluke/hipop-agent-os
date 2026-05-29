"""
紫鸟客户端 web_driver 模式 probe。

前置：
1. 退出 ziniao GUI（osascript -e 'tell app "ziniao" to quit'）
2. open -a ziniao --args --run_type=web_driver --port=18080
3. 等 ~5s 让本机服务起来

本脚本目标：
  applyAuth → getBrowserList → 找 noon 店铺 → startBrowser 拿 chromium debug port
  返回 debugPort 给后续 playwright 接管脚本用
"""
import json
import os
import socket
import sys
import time
import uuid
from urllib import request as urlreq
from urllib.error import URLError, HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config", "hipop.json")


def load_cfg():
    with open(CFG_PATH) as f:
        return json.load(f)


def is_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def http_post(port: int, path: str, body: dict, timeout: int = 15):
    """先尝试 HTTP POST。"""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urlreq.Request(url, data=data, method="POST",
                         headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
        try:
            return r.status, json.loads(raw)
        except json.JSONDecodeError:
            return r.status, raw


def tcp_send(port: int, body: dict, timeout: int = 15):
    """fallback: 部分版本走 TCP+JSONLine。"""
    payload = (json.dumps(body) + "\r\n").encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(payload)
        s.settimeout(timeout)
        chunks = []
        while True:
            try:
                buf = s.recv(65536)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
            if buf.endswith(b"\n") or buf.endswith(b"\r\n"):
                break
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


def call(port: int, body: dict):
    """先 HTTP, 失败再 TCP。"""
    body.setdefault("requestId", uuid.uuid4().hex[:16])
    print(f"\n→ POST {body.get('action')} requestId={body['requestId']}")
    try:
        status, resp = http_post(port, "/", body)
        print(f"  HTTP {status}")
        return resp
    except (HTTPError, URLError, ConnectionResetError, OSError) as e:
        print(f"  HTTP failed: {e}; trying TCP")
    try:
        resp = tcp_send(port, body)
        print(f"  TCP returned: {type(resp).__name__}")
        return resp
    except Exception as e:
        print(f"  TCP failed: {e}")
        return None


def main():
    cfg = load_cfg()
    zc = cfg["ziniao_client"]
    port = zc["web_driver_port"]
    debug_port_pref = zc["debug_port_default"]

    print(f"=== probe 紫鸟 web_driver mode @127.0.0.1:{port} ===")
    if not is_listening(port):
        print(f"\n端口 {port} 未监听。先跑：")
        print(f"  osascript -e 'tell app \"ziniao\" to quit'")
        print(f"  sleep 2")
        print(f"  open -a ziniao --args --run_type=web_driver --port={port}")
        print(f"  sleep 5")
        sys.exit(1)
    print(f"端口 {port} 已监听 ✓")

    # 1. applyAuth
    auth_body = {
        "action": "applyAuth",
        "company": zc["company"],
        "username": zc["username"],
        "password": zc["password"],
    }
    auth_resp = call(port, auth_body)
    print(f"applyAuth resp: {json.dumps(auth_resp, ensure_ascii=False)[:600]}"
          if isinstance(auth_resp, dict) else f"applyAuth resp(raw): {str(auth_resp)[:600]}")
    if not auth_resp:
        print("FAIL: applyAuth 没返回，可能协议不是 JSON-over-HTTP/TCP")
        sys.exit(2)

    # 2. getBrowserList
    list_body = {"action": "getBrowserList"}
    list_resp = call(port, list_body)
    if isinstance(list_resp, dict):
        # 尝试常见字段位置
        browsers = (list_resp.get("data")
                    or list_resp.get("browsers")
                    or list_resp.get("list")
                    or list_resp.get("result"))
        if isinstance(browsers, dict):
            browsers = (browsers.get("list")
                        or browsers.get("data")
                        or browsers.get("browsers"))
        n = len(browsers) if isinstance(browsers, list) else 0
        print(f"\ngetBrowserList: {n} 个浏览器实例")
        if isinstance(browsers, list):
            for b in browsers[:20]:
                if isinstance(b, dict):
                    keys = ("browserOauth", "browserId", "id", "uuid",
                            "browserName", "name", "label", "title",
                            "platformAccount", "platform", "containerId")
                    digest = {k: b.get(k) for k in keys if k in b}
                    print(f"  - {digest}")
    else:
        print(f"\ngetBrowserList raw: {str(list_resp)[:600]}")

    print("\n=== probe 阶段 1 完成。下一步看终端输出选 noon 店铺，传给 startBrowser ===")
    print("用法：python3 probe_ziniao_webdriver.py start <browserOauth>")


def start_one(browser_oauth: str):
    cfg = load_cfg()
    zc = cfg["ziniao_client"]
    port = zc["web_driver_port"]
    debug_port = zc["debug_port_default"]

    if not is_listening(port):
        print(f"web_driver port {port} 没起，先跑 main 阶段")
        sys.exit(1)

    body = {
        "action": "startBrowser",
        "browserOauth": browser_oauth,
        "runMode": "2",  # 2 = 有 UI
        "webDriverConfig": {
            "debuggPort": debug_port,
            "notPromptForDownload": 1,
        },
    }
    resp = call(port, body)
    print(f"\nstartBrowser resp: {json.dumps(resp, ensure_ascii=False)[:1500]}"
          if isinstance(resp, dict) else f"startBrowser raw: {str(resp)[:1500]}")

    # 抠 debug port
    if isinstance(resp, dict):
        for k in ("debuggingPort", "debugPort", "debuggPort", "remoteDebuggingPort"):
            if k in resp:
                print(f"\n✓ chromium debug port = {resp[k]}")
                print(f"  连接：playwright.chromium.connect_over_cdp(\"http://127.0.0.1:{resp[k]}\")")
                return
        # 嵌套
        data = resp.get("data") if isinstance(resp.get("data"), dict) else None
        if data:
            for k in ("debuggingPort", "debugPort", "debuggPort", "remoteDebuggingPort"):
                if k in data:
                    print(f"\n✓ chromium debug port (data.{k}) = {data[k]}")
                    return
        print(f"\n⚠ 没找到 debug port 字段，原始 response 看上面")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        if len(sys.argv) < 3:
            print("用法：python3 probe_ziniao_webdriver.py start <browserOauth>")
            sys.exit(1)
        start_one(sys.argv[2])
    else:
        main()

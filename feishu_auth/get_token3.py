"""
飞书 user_access_token 获取脚本（本地服务器自动接收回调版）
"""
import json
import threading
import webbrowser
import urllib.parse
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

APP_ID      = "cli_a96a395aaafa5cb5"
APP_SECRET  = "__REDACTED_FEISHU_APP_SECRET__"
REDIRECT_URI = "http://localhost:9898/callback"
PORT        = 9898

# ── 1. 获取 app_access_token ─────────────────────────────
def get_app_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        headers={"Content-Type": "application/json"}
    )
    data = resp.json()
    token = data.get("app_access_token")
    if not token:
        raise RuntimeError(f"获取 app_access_token 失败: {data}")
    print(f"✓ app_access_token 获取成功")
    return token

# ── 2. 本地服务器接收 OAuth 回调 ─────────────────────────
code_holder = {}

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code   = params.get("code", [None])[0]

        if code:
            code_holder["code"] = code
            body = b"<h2>Authorization successful! You can close this tab.</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received")

    def log_message(self, *args):
        pass  # 静默日志

def wait_for_code():
    server = HTTPServer(("localhost", PORT), CallbackHandler)
    server.timeout = 120
    print(f"  本地回调服务已启动，等待授权（最多等待 120 秒）...")
    while not code_holder.get("code"):
        server.handle_request()
    server.server_close()

# ── 3. 换取 user_access_token ────────────────────────────
def exchange_code(code, app_token):
    # 优先试 OIDC 接口
    for url in [
        "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
        "https://open.feishu.cn/open-apis/authen/v1/access_token",
    ]:
        resp = requests.post(
            url,
            json={"grant_type": "authorization_code", "code": code},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {app_token}",
            }
        )
        result = resp.json()
        data   = result.get("data") or result
        token  = data.get("access_token")
        if token:
            return token, data
    return None, result

# ── 主流程 ────────────────────────────────────────────────
if __name__ == "__main__":
    app_token = get_app_token()

    auth_url = (
        "https://open.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={APP_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope=bitable:app"
        f"&state=hipop"
    )

    print("\n步骤1：正在打开浏览器，请在飞书中完成授权...")
    print(f"  授权链接：{auth_url}\n")

    # 先启动服务器线程，再打开浏览器
    t = threading.Thread(target=wait_for_code, daemon=True)
    t.start()
    webbrowser.open(auth_url)
    t.join(timeout=120)

    code = code_holder.get("code")
    if not code:
        print("✗ 超时未收到授权码，请重试")
        exit(1)

    print(f"✓ 收到授权码: {code[:10]}...")
    print("  正在换取 user_access_token...")

    token, token_data = exchange_code(code, app_token)

    if token:
        print(f"\n✓ user_access_token 获取成功！")
        print(f"  token: {token[:20]}...")
        # 保存
        out = {"user_access_token": token, **token_data}
        with open("token.json", "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("  已保存到 token.json")

        # 测试：读取多维表格列表
        BASE_ID = "BE2Ab41lvaJdzbs0c7QcgaWbnid"
        test = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_ID}",
            headers={"Authorization": f"Bearer {token}"}
        ).json()
        if test.get("code") == 0:
            print(f"  ✓ 多维表格访问成功：{test['data']['app']['name']}")
        else:
            print(f"  多维表格访问结果: code={test.get('code')} msg={test.get('msg')}")
    else:
        print(f"\n✗ 换取 token 失败:")
        print(json.dumps(token_data, ensure_ascii=False, indent=2))

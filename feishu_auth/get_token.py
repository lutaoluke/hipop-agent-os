"""
飞书 user_access_token 获取脚本
运行后会自动打开浏览器，授权完成后自动捕获 token
"""
import http.server
import threading
import webbrowser
import urllib.parse
import json
import requests
import os

APP_ID = "cli_a96a395aaafa5cb5"
APP_SECRET = "__REDACTED_FEISHU_APP_SECRET__"
REDIRECT_URI = "http://localhost:9898/callback"

auth_code = None

class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>授权成功！回到终端查看 token</h2>".encode())
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默日志

def get_app_access_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        headers={"Content-Type": "application/json"}
    )
    data = resp.json()
    return data.get("app_access_token")

def get_user_access_token(code):
    app_token = get_app_access_token()
    print(f"  app_token: {app_token[:20]}...")

    # 先试 OIDC 接口
    resp = requests.post(
        "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
        json={"grant_type": "authorization_code", "code": code},
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"}
    )
    result = resp.json()
    if result.get("code") != 0:
        print(f"  OIDC 接口失败({result.get('code')}): {result.get('msg')}")
        # 回退到旧接口
        resp2 = requests.post(
            "https://open.feishu.cn/open-apis/authen/v1/access_token",
            json={"grant_type": "authorization_code", "code": code},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"}
        )
        result = resp2.json()
        print(f"  旧接口结果: code={result.get('code')}, msg={result.get('msg')}")
    return result

if __name__ == "__main__":
    # 先在飞书开放平台的应用设置里添加重定向 URL：http://localhost:9898/callback
    # 路径：应用设置 → 安全设置 → 重定向 URL

    auth_url = (
        f"https://open.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={APP_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope=bitable:app"
        f"&state=hipop"
    )

    # 启动本地服务器
    server = http.server.HTTPServer(("localhost", 9898), CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"\n正在打开浏览器进行授权...")
    print(f"如果浏览器未自动打开，请手动访问：\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=120)

    if auth_code:
        print(f"获取到授权码，正在换取 token...")
        result = get_user_access_token(auth_code)

        # 兼容两种接口返回格式
        token_data = result.get("data") or result
        token = token_data.get("access_token")

        if token:
            refresh = token_data.get("refresh_token", "")
            print(f"\n✓ user_access_token 获取成功！")
            print(f"\naccess_token:\n{token}")
            if refresh:
                print(f"\nrefresh_token:\n{refresh}")

            with open("token.json", "w") as f:
                json.dump(token_data, f, indent=2, ensure_ascii=False)
            print(f"\n已保存到 token.json")
        else:
            print(f"获取失败:\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    else:
        print("超时未收到授权回调")

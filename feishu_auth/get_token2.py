"""
飞书 user_access_token 获取脚本（手动粘贴版）
"""
import urllib.parse
import json
import requests
import webbrowser

APP_ID = "cli_a96a395aaafa5cb5"
APP_SECRET = "__REDACTED_FEISHU_APP_SECRET__"
REDIRECT_URI = "http://localhost:9898/callback"

def get_app_access_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        headers={"Content-Type": "application/json"}
    )
    return resp.json().get("app_access_token")

auth_url = (
    f"https://open.feishu.cn/open-apis/authen/v1/authorize"
    f"?app_id={APP_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&scope=bitable:app"
    f"&state=hipop"
)

print("\n步骤1：打开以下链接完成授权（浏览器可能自动打开）")
print(f"\n{auth_url}\n")
webbrowser.open(auth_url)

print("步骤2：授权后浏览器会跳转到 localhost（会显示无法连接，这是正常的）")
print("       复制浏览器地址栏里完整的 URL，粘贴到下面：\n")

callback_url = input("粘贴 URL：").strip()

# 提取 code
parsed = urllib.parse.urlparse(callback_url)
params = urllib.parse.parse_qs(parsed.query)
code = params.get("code", [None])[0]

if not code:
    print("未找到 code，请检查粘贴的 URL")
    exit(1)

print(f"\n获取到 code: {code[:10]}...")
print("正在换取 token...")

app_token = get_app_access_token()

# 尝试 OIDC 接口
resp = requests.post(
    "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
    json={"grant_type": "authorization_code", "code": code},
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"}
)
result = resp.json()

if result.get("code") != 0:
    # 回退旧接口
    resp = requests.post(
        "https://open.feishu.cn/open-apis/authen/v1/access_token",
        json={"grant_type": "authorization_code", "code": code},
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"}
    )
    result = resp.json()

token_data = result.get("data") or result
token = token_data.get("access_token")

if token:
    print(f"\n✓ 成功！")
    print(f"\naccess_token: {token}")
    with open("token.json", "w") as f:
        json.dump(token_data, f, indent=2, ensure_ascii=False)
    print("\n已保存到 token.json")

    # 验证能否访问多维表格
    BASE_ID = "BE2Ab41lvaJdzbs0c7QcgaWbnid"
    test = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_ID}",
        headers={"Authorization": f"Bearer {token}"}
    ).json()
    if test.get("code") == 0:
        print(f"\n✓ 多维表格访问成功！表格名: {test['data']['app']['name']}")
    else:
        print(f"\n多维表格访问结果: {test.get('msg')}")
else:
    print(f"\n失败: {json.dumps(result, ensure_ascii=False, indent=2)}")

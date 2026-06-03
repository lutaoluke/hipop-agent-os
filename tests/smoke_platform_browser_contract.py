"""Smoke: 平台浏览器协议客户端 contract（WS-33.1）— fail-then-pass 承重墙。

钉死 `hipop/server/_platform_browser.py` 的协议/契约层（无需真实紫鸟在线，用 fake
webdriver server 当 verifier）：

  ① getBrowserList 鉴权 body —— 必含 company/username/password/action/requestId，
     缺任一 fake server 直接拒。
  ② 多店枚举 —— fake server 返回 2 个 store，`list_stores()` 必须输出**全部**，每个
     带 browser_id / store_username / name（后续 tenant/entity 映射所需）。只取第一店
     或写死 browserId 26865530773075 必须 fail。
  ③ store_key 选店 —— 平台无关 `select_store` 按 NOON-SA / NOON-AE 选中对应 store，
     取**当次**响应的最新 browserOauth（两次 getBrowserList 的 oauth 不同，证明不缓存
     旧值）。
  ④ startBrowser 精确 schema —— 请求体**绝不含** debuggPort/debugPort/debuggingPort；
     fake server 一旦发现 debug 端口入参就返回 -10000（复刻真实紫鸟行为）。端口由响应
     的 debuggingPort 解析（9876），不等于 config 的 debug_port_default(9223)，证明
     不写死。
  ⑤ 错误路径 —— 缺 store / 缺 browserOauth / startBrowser 返回 -10000 / 缺 debuggingPort
     都抛 PlatformBrowserError(blocked)，不 stub。

fail-then-pass 证明（两个 env 开关复刻两种「改动前 / 回归」死法）：
  - 默认跑 → 全过。
  - SMOKE_LEGACY_DEBUGPORT=1 → 把 startBrowser body 退回旧 probe（webDriverConfig 里塞
    debuggPort）→ fake server 返回 -10000 → 「startBrowser 解析端口」断言 FAIL。
    （复刻死法②死代码短路：旧 probe 手工 debug 端口绕过自动分配。）
  - SMOKE_FIRST_STORE_ONLY=1 → 把 list_stores 截成只取第一店 → 「枚举全部 store」断言
    FAIL。（复刻死法③占位假数据：只看第一店/硬编 browserId 冒充多店。）
  改动前（_platform_browser.py 还不存在）整个文件 ImportError 即全 fail。

跑法：
  python3 tests/smoke_platform_browser_contract.py
  SMOKE_LEGACY_DEBUGPORT=1 python3 tests/smoke_platform_browser_contract.py   # 看回归 fail
  SMOKE_FIRST_STORE_ONLY=1 python3 tests/smoke_platform_browser_contract.py   # 看回归 fail
  （也被 make test 自动聚合）
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 必须在 import 模块（触发 load_config 展开）之前设好 ziniao_client 凭据 env。
os.environ["ZINIAO_COMPANY"] = "点购优品跨境"
os.environ["ZINIAO_USERNAME"] = "smoke-ziniao-user"
os.environ["ZINIAO_PASSWORD"] = "smoke-ziniao-pw"

from hipop.server import _platform_browser as pb  # noqa: E402

# config 里 ziniao_client.debug_port_default —— startBrowser 解析出的端口必须 != 它，
# 证明端口来自响应而非写死 default。
CFG_DEBUG_DEFAULT = 9223
# fake server 给 startBrowser 分配的端口（刻意 != CFG_DEBUG_DEFAULT）。
ASSIGNED_PORT = 9876

# 多店 fixture：2 个 store（NOON-SA / NOON-AE），browserId 各异。
_STORE_FIXTURE = [
    {"browserId": "26865530773075", "browserName": "44158-HIPOP-NOON-SA",
     "platformAccount": "hipop-noon-sa"},
    {"browserId": "99887766554433", "browserName": "44158-HIPOP-NOON-AE",
     "platformAccount": "hipop-noon-ae"},
]


class _State:
    """fake server 收到的请求 + 调用计数（供断言/旋转 oauth）。"""
    def __init__(self):
        self.browser_list_calls = 0
        self.last_browser_list_body = None
        self.last_start_body = None


STATE = _State()
REQUIRED_AUTH_KEYS = ("company", "username", "password", "action", "requestId")


class _FakeZiniao(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except json.JSONDecodeError:
            return self._send({"code": -1, "msg": "bad json"})
        action = body.get("action")

        if action == "getBrowserList":
            STATE.browser_list_calls += 1
            STATE.last_browser_list_body = body
            missing = [k for k in REQUIRED_AUTH_KEYS if not body.get(k)]
            if missing:
                return self._send({"statusCode": -1, "err": f"missing {missing}"})
            # 每次旋转 browserOauth（模拟真实「每次取最新」），编号进 oauth 供断言。
            seq = STATE.browser_list_calls
            stores = []
            for s in _STORE_FIXTURE:
                tag = "SA" if s["browserId"] == "26865530773075" else "AE"
                stores.append({**s, "browserOauth": f"OAUTH-{tag}-{seq}"})
            # 真实紫鸟 web_driver 形态：statusCode/err/browserList（顶层），非 code/data。
            # WS-33.3 live 实测踩过：只认 code/data 会把真实 browserList 解析成 0 店。
            return self._send({"statusCode": 0, "err": "", "browserList": stores})

        if action == "startBrowser":
            STATE.last_start_body = body
            # 死法②钉死：请求体任何层级出现 debug 端口入参 → -10000（复刻真实紫鸟）。
            # 端口入参非法优先于鉴权判定（旧 probe 带 debuggPort 必拒，与有无鉴权无关）。
            if _has_forbidden_port(body):
                return self._send({"statusCode": -10000, "err": "debugg port not allowed"})
            # 真实紫鸟 startBrowser **必须**带鉴权三件套，缺则 -10003「参数不能为空
            # （登录状态错误）」。WS-33.3 live 实测踩过：不带 auth 的 startBrowser 直接 -10003。
            auth_missing = [k for k in ("company", "username", "password") if not body.get(k)]
            if auth_missing:
                return self._send({"statusCode": -10003,
                                   "err": f"参数不能为空（登录状态错误）missing {auth_missing}"})
            oauth = body.get("browserOauth") or ""
            # 故意触发「缺 debuggingPort」错误路径的探针。
            if "NOPORT" in oauth:
                return self._send({"statusCode": 0, "err": "", "data": {}})
            # 真实形态：debuggingPort 在顶层。
            return self._send({"statusCode": 0, "err": "", "debuggingPort": ASSIGNED_PORT})

        return self._send({"statusCode": -1, "err": f"unknown action {action}"})


def _has_forbidden_port(obj) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in pb._FORBIDDEN_DEBUG_PORT_KEYS:
                return True
            if _has_forbidden_port(v):
                return True
    elif isinstance(obj, list):
        return any(_has_forbidden_port(x) for x in obj)
    return False


# ── 旧 probe（死法②回归）：startBrowser 退回手工 debuggPort ──────────────
def _legacy_start_body(browser_oauth, run_mode, request_id):
    return {
        "action": "startBrowser",
        "browserOauth": browser_oauth,
        "runMode": run_mode,
        "webDriverConfig": {"debuggPort": CFG_DEBUG_DEFAULT, "notPromptForDownload": 1},
        "requestId": request_id,
    }


class _Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            self.failures.append(name)
            print(f"  ✗ {name} {detail}")

    def raises_blocked(self, name, fn):
        try:
            fn()
            self.failures.append(name)
            print(f"  ✗ {name} （未抛错）")
        except pb.PlatformBrowserError as e:
            ok = getattr(e, "blocked", False)
            if ok:
                print(f"  ✓ {name} → blocked: {str(e)[:60]}")
            else:
                self.failures.append(name)
                print(f"  ✗ {name} （抛了但 blocked!=True）")
        except Exception as e:  # noqa: BLE001
            self.failures.append(name)
            print(f"  ✗ {name} （抛了非 PlatformBrowserError: {type(e).__name__}: {e}）")


def run():
    legacy = os.environ.get("SMOKE_LEGACY_DEBUGPORT") == "1"
    first_only = os.environ.get("SMOKE_FIRST_STORE_ONLY") == "1"
    if legacy:
        pb._build_start_browser_body = _legacy_start_body
    if first_only:
        _orig_list = pb.list_stores
        pb.list_stores = lambda account=None, *, port=None: _orig_list(account, port=port)[:1]

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeZiniao)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    check = _Checker()
    try:
        print("== ① getBrowserList 鉴权 body ==")
        stores = pb.list_stores(port=port)
        body = STATE.last_browser_list_body or {}
        for k in REQUIRED_AUTH_KEYS:
            check(f"body 含 {k}", bool(body.get(k)), f"got {body.get(k)!r}")
        check("company==点购优品跨境", body.get("company") == "点购优品跨境",
              f"got {body.get('company')!r}")
        check("username==smoke-ziniao-user", body.get("username") == "smoke-ziniao-user",
              f"got {body.get('username')!r}")
        check("password==smoke-ziniao-pw", body.get("password") == "smoke-ziniao-pw")
        check("action==getBrowserList", body.get("action") == "getBrowserList")

        print("== ② 多店枚举（全部输出，不只第一店）==")
        check("list_stores 返回 2 店", len(stores) == 2, f"got {len(stores)}")
        ids = {s.browser_id for s in stores}
        check("含 SA store 26865530773075", "26865530773075" in ids, f"got {ids}")
        check("含 AE store 99887766554433", "99887766554433" in ids,
              f"got {ids}（只取第一店/硬编 browserId 会缺）")
        for s in stores:
            check(f"store {s.name} 带 browser_id", bool(s.browser_id))
            check(f"store {s.name} 带 store_username", bool(s.store_username),
                  f"got {s.store_username!r}")
            check(f"store {s.name} 带 name", bool(s.name))

        print("== ③ store_key 选店 + 最新 browserOauth ==")
        # 再拉一次，oauth 旋转 → 证明每次取最新、不缓存旧值。
        stores2 = pb.list_stores(port=port)
        if stores and stores2:
            check("两次 getBrowserList 的 oauth 不同（不缓存旧值）",
                  stores[0].browser_oauth != stores2[0].browser_oauth,
                  f"{stores[0].browser_oauth} vs {stores2[0].browser_oauth}")
        try:
            ae = pb.select_store(stores2, "NOON-AE")
            check("NOON-AE 选中 AE store", ae.browser_id == "99887766554433",
                  f"got {ae.browser_id}")
            check("NOON-AE 取当次 oauth", ae.browser_oauth == f"OAUTH-AE-{STATE.browser_list_calls}",
                  f"got {ae.browser_oauth}")
            sa = pb.select_store(stores2, "26865530773075")  # 按 browser_id 精确选
            check("按 browser_id 选中 SA store", sa.name == "44158-HIPOP-NOON-SA",
                  f"got {sa.name}")
            by_user = pb.select_store(stores2, "hipop-noon-ae")  # 按 store_username 选
            check("按 store_username 选中 AE store", by_user.browser_id == "99887766554433")
        except pb.PlatformBrowserError as e:
            check("select_store 选店", False, f"抛错 {e}")

        print("== ④ startBrowser 精确 schema + 端口从响应解析 ==")
        if stores2:
            target = pb.select_store(stores2, "NOON-SA")
            try:
                dbg = pb.start_browser(target.browser_oauth, port=port)
                check("startBrowser 返回 debuggingPort 9876（从响应解析）",
                      dbg == ASSIGNED_PORT, f"got {dbg}")
                check("解析端口 != config debug_port_default 9223（非写死）",
                      dbg != CFG_DEBUG_DEFAULT, f"got {dbg}")
            except pb.PlatformBrowserError as e:
                # SMOKE_LEGACY_DEBUGPORT=1 时这里预期 fail（-10000）。
                check("startBrowser 返回 debuggingPort 9876（从响应解析）", False,
                      f"抛错 {str(e)[:80]}")
            sent = STATE.last_start_body or {}
            check("startBrowser body 不含任何 debug 端口入参",
                  not _has_forbidden_port(sent),
                  f"body={json.dumps(sent, ensure_ascii=False)}")
            check("startBrowser action 正确", sent.get("action") == "startBrowser")
            check("startBrowser 带 browserOauth", bool(sent.get("browserOauth")))
            # WS-33.3 live 实测：真实紫鸟 startBrowser 缺 company/username/password →
            # -10003。钉死 body 必带鉴权三件套（不带则上面「返回端口」断言已 fail）。
            check("startBrowser body 带鉴权三件套 company/username/password",
                  all(sent.get(k) for k in ("company", "username", "password")),
                  f"body={json.dumps(sent, ensure_ascii=False)}")

        print("== ⑤ open_cdp_endpoint 复用入口端到端（list→select→start 串通）==")
        try:
            ep = pb.open_cdp_endpoint("NOON-SA", port=port)
            check("open_cdp_endpoint 返回 debugging_port 9876",
                  ep.debugging_port == ASSIGNED_PORT, f"got {ep.debugging_port}")
            check("open_cdp_endpoint cdp_url 用解析端口",
                  ep.cdp_url == f"http://127.0.0.1:{ASSIGNED_PORT}", f"got {ep.cdp_url}")
            check("open_cdp_endpoint 选中 SA store",
                  ep.store.browser_id == "26865530773075", f"got {ep.store.browser_id}")
        except pb.PlatformBrowserError as e:
            # SMOKE_LEGACY_DEBUGPORT=1 时这里也预期 fail（startBrowser -10000）。
            check("open_cdp_endpoint 返回 debugging_port 9876", False, f"抛错 {str(e)[:80]}")

        print("== ⑥ 错误路径都抛 blocked（不 stub）==")
        check.raises_blocked("未知 store_key → blocked",
                             lambda: pb.select_store(stores2, "NOON-NONEXIST"))
        no_oauth = [pb.Store(browser_id="x", browser_oauth=None, name="n",
                             store_username="u", account="a")]
        check.raises_blocked("store 缺 browserOauth → blocked",
                             lambda: pb.select_store(no_oauth, "x"))
        check.raises_blocked("startBrowser 缺 browserOauth → blocked",
                             lambda: pb.start_browser("", port=port))
        check.raises_blocked("startBrowser 缺 debuggingPort → blocked",
                             lambda: pb.start_browser("OAUTH-NOPORT", port=port))
        # 直发 debug 端口入参 → 真实紫鸟 -10000 → blocked（旧 probe 行为被钉死为非法）
        legacy_body = _legacy_start_body("OAUTH-SA-1", "2", "rid")
        resp = pb._post(port, legacy_body)
        check("旧 probe（带 debuggPort）被 fake server 拒 -10000",
              isinstance(resp, dict) and str(pb._resp_code(resp)) == "-10000",
              f"got {resp}")

    finally:
        srv.shutdown()

    print()
    if legacy and check.failures:
        print("  （SMOKE_LEGACY_DEBUGPORT=1：startBrowser 退回旧 debuggPort → -10000，"
              "这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if first_only and check.failures:
        print("  （SMOKE_FIRST_STORE_ONLY=1：只取第一店 → 缺 AE store，"
              "这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print(f"✓ 平台浏览器协议客户端 contract smoke 全过"
          f"（{STATE.browser_list_calls} 次 getBrowserList）")
    return 0


if __name__ == "__main__":
    sys.exit(run())

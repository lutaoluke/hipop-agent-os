"""Smoke: get_platform_session 实连 CDP + 三级回落 + 登录态 blocked（WS-33.3）
— fail-then-pass 承重墙。

钉死 `hipop/server/_platform_browser.get_platform_session` 的编排（无需真实紫鸟/真实
playwright：只替身两类外部边界 —— webdriver 调用 list_stores/start_browser、CDP 网络
_cdp_version、playwright 接管 _connect_cdp/_acquire_page；其余编排/三级回落/登录检测/
warmup-retry/持久化全用真函数，死法就活在这些真函数里）：

  ② 三级回落可观察 —— 首拉走 webdriver startBrowser 并落盘；再拉命中内存 cache（不再
     start）；内存过期但磁盘新鲜 → 走磁盘 reconnect（不 start）；磁盘也过期 → 重新
     startBrowser（acceptance #2「过期状态必须重新 start」）。
  ③ 冷 goto 被 abort → 先平台根域 warmup 再重试，`_NAV_STATS` 证明 retry 路径被走过。
  ④ 落登录页 / 缺会话 cookie → blocked + 人工登录提示，绝不返回 page。
  ⑤ 缺紫鸟 webdriver / CDP 连接失败 / 接管后 CDP 不可达 → blocked，不返回假 page，
     不落 state。
  ⑥ 内存 cache 命中但 chromium 已没（CDP 不可达）→ 必须重拉，绝不返回旧 page。

fail-then-pass 证明（两个 env 开关复刻两种死法）：
  - 默认跑 → 全过。
  - SMOKE_SESSION_RETURN_STALE=1 → 把 `_session_alive` 退回「只看 TTL、不验 CDP」
    （死法·死代码短路：chromium 没了还返回旧 page）→ ⑥ 的「CDP 不可达必须重拉」断言 FAIL。
  - SMOKE_SESSION_STUB_LOGIN=1 → 把 `_detect_login` 退回「永远 ok」（死法·占位假数据：
    stub 登录态）→ ④ 的「登录页 → blocked」断言 FAIL。
  改动前（get_platform_session 还不存在）整个文件 AttributeError 即全 fail。

跑法：
  python3 tests/smoke_platform_session.py
  SMOKE_SESSION_RETURN_STALE=1 python3 tests/smoke_platform_session.py   # 看回归 fail
  SMOKE_SESSION_STUB_LOGIN=1 python3 tests/smoke_platform_session.py     # 看回归 fail
  （也被 make test 自动聚合）
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# 把会话 state 落到临时目录，绝不污染真实 ~/hipop。必须在 import pb 之前设。
_TMP_STATE = tempfile.mkdtemp(prefix="smoke_ziniao_state_")
os.environ["HIPOP_STATE_DIR"] = _TMP_STATE
# ziniao_client 凭据 env（fake）——本 smoke 不碰真实账号。
os.environ.setdefault("ZINIAO_COMPANY", "点购优品跨境")
os.environ.setdefault("ZINIAO_USERNAME", "smoke-session-user")
os.environ.setdefault("ZINIAO_PASSWORD", "smoke-session-pw")

from hipop.server import _platform_browser as pb  # noqa: E402

NOON_OK_URL = "https://noon-catalog.noon.partners/en/catalog?project=PRJ44158&tab=noon"
NOON_LOGIN_URL = "https://login.noon.partners/signin?return=catalog"
ROOT_URL = "https://noon-catalog.noon.partners/"
STORE_ID = "26865530773075"


# ── 替身 ────────────────────────────────────────────────────────────────
class FakeError(Exception):
    pass


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


class FakePage:
    """可控 goto：abort_left 次先抛 net::ERR_ABORTED，之后落到 final_url。"""
    def __init__(self, final_url, cookies, abort_left=0):
        self._url = "about:blank"
        self._final_url = final_url
        self.abort_left = abort_left
        self.goto_calls = []
        self.context = FakeContext(cookies)

    @property
    def url(self):
        return self._url

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        # 复刻「冷 goto 深链被紫鸟扩展 abort，平台根域 warmup 放行」：只 abort 深链。
        if url != ROOT_URL and self.abort_left > 0:
            self.abort_left -= 1
            raise FakeError("net::ERR_ABORTED at " + url)
        # warmup（goto root）只过渡；最终 check_url 落到 final_url。
        self._url = url if url == ROOT_URL else self._final_url
        return None


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    @property
    def contexts(self):
        return [_FakeBCtx(self._page)]

    def close(self):
        pass


class _FakeBCtx:
    def __init__(self, page):
        self._page = page

    @property
    def pages(self):
        return [self._page]


class _Harness:
    """统一替身：计 webdriver/connect 次数，控 CDP 可达与登录页/cookie。"""
    def __init__(self):
        self.start_calls = 0
        self.connect_calls = 0
        self.assigned_port = 9555
        self.cdp_reachable = True
        self.page = FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x"}])

    def install(self):
        store = pb.Store(browser_id=STORE_ID, browser_oauth="OAUTH-LIVE",
                         name="44158-HIPOP-NOON-SA", store_username="hipop-noon-sa",
                         account="smoke-session-user")
        pb._assert_webdriver_up = lambda port=None: port or 18080
        pb.list_stores = lambda account=None, *, port=None, store_key=None: [store]

        def _start(oauth, *, run_mode="2", port=None, creds=None,
                   store_key=None, account=None):
            self.start_calls += 1
            return self.assigned_port
        pb.start_browser = _start
        pb._cdp_version = lambda cdp_url, timeout=3.0: (
            {"Browser": "Chrome/fake"} if self.cdp_reachable else None)

        def _connect(cdp_url):
            self.connect_calls += 1
            return ("PW", FakeBrowser(self.page))
        pb._connect_cdp = _connect
        pb._safe_close = lambda pw, browser: None


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
            print(f"  ✗ {name} （未抛错，可能返回了假 page）")
        except pb.PlatformBrowserError as e:
            if getattr(e, "blocked", False):
                print(f"  ✓ {name} → blocked: {str(e)[:60]}")
            else:
                self.failures.append(name)
                print(f"  ✗ {name} （抛了但 blocked!=True）")
        except Exception as e:  # noqa: BLE001
            self.failures.append(name)
            print(f"  ✗ {name} （非 PlatformBrowserError: {type(e).__name__}: {e}）")


# ── 原函数备份（每个场景前还原，隔离替身污染）──────────────────────────
_ORIG = {n: getattr(pb, n) for n in (
    "_assert_webdriver_up", "list_stores", "start_browser", "_cdp_version",
    "_connect_cdp", "_safe_close", "_session_alive", "_detect_login")}


def _restore():
    for n, f in _ORIG.items():
        setattr(pb, n, f)
    pb._session_cache.clear()
    pb._NAV_STATS.update(attempts=0, warmups=0, aborts=0)
    for f in os.listdir(_TMP_STATE):
        os.remove(os.path.join(_TMP_STATE, f))


def run():
    stale = os.environ.get("SMOKE_SESSION_RETURN_STALE") == "1"
    stub_login = os.environ.get("SMOKE_SESSION_STUB_LOGIN") == "1"
    check = _Checker()

    # ── ② 三级回落 ──
    print("== ② 三级回落：webdriver → 内存 cache → 磁盘 → 过期重启 ==")
    _restore()
    h = _Harness()
    h.install()
    if stale:
        pb._session_alive = lambda s: time.time() < s.exp  # 死法：不验 CDP
    if stub_login:
        pb._detect_login = lambda page, pcfg: pb.LoginState("ok", "stub")

    page1 = pb.get_platform_session(7, "noon")
    check("首拉返回真实 page（非 None）", page1 is h.page, f"got {page1!r}")
    check("首拉走 webdriver startBrowser 1 次", h.start_calls == 1, f"got {h.start_calls}")
    check("首拉落盘 ziniao_state_<store>.json",
          os.path.exists(pb._state_path(STORE_ID)))
    sess = pb._session_cache[(7, "noon")]
    check("首拉 source==webdriver", sess.source == "webdriver", f"got {sess.source}")

    page2 = pb.get_platform_session(7, "noon")
    check("再拉命中内存 cache（不再 startBrowser）", h.start_calls == 1, f"got {h.start_calls}")
    check("内存 cache 返回同一 page", page2 is page1)

    # 内存过期、磁盘仍新鲜 → 走磁盘 reconnect，不 startBrowser。
    pb._session_cache[(7, "noon")].exp = time.time() - 1
    start_before = h.start_calls
    page3 = pb.get_platform_session(7, "noon")
    check("内存过期+磁盘新鲜 → 走磁盘（不 startBrowser）",
          h.start_calls == start_before, f"start {start_before}->{h.start_calls}")
    check("磁盘 tier source==disk",
          pb._session_cache[(7, "noon")].source == "disk",
          f"got {pb._session_cache[(7, 'noon')].source}")
    check("磁盘 tier 仍返回 page", page3 is h.page)

    # 磁盘也过期 → 必须重新 startBrowser（acceptance #2）。
    import json as _json
    sp = pb._state_path(STORE_ID)
    d = _json.load(open(sp))
    d["expires_at"] = time.time() - 1
    _json.dump(d, open(sp, "w"))
    pb._session_cache[(7, "noon")].exp = time.time() - 1
    start_before = h.start_calls
    pb.get_platform_session(7, "noon")
    check("内存+磁盘都过期 → 重新 startBrowser",
          h.start_calls == start_before + 1, f"start {start_before}->{h.start_calls}")

    # ── ⑥ 内存命中但 CDP 已死 → 必须重拉（不返回旧 page）──
    print("== ⑥ 内存命中但 chromium 已没（CDP 不可达）→ 必须重拉 ==")
    _restore()
    h = _Harness()
    h.install()
    if stale:
        pb._session_alive = lambda s: time.time() < s.exp
    if stub_login:
        pb._detect_login = lambda page, pcfg: pb.LoginState("ok", "stub")
    pb.get_platform_session(7, "noon")            # 建 cache
    start_before = h.start_calls
    h.cdp_reachable = False                        # chromium 没了
    # 磁盘 state 的 cdp 也不可达 → tier2 失效，应回落 tier3 重新 start。
    if stale:
        # 死法下 _session_alive 不验 CDP → 直接返回旧 page，不重拉。
        page = pb.get_platform_session(7, "noon")
        check("CDP 不可达时必须重拉（不复用旧 page）",
              h.start_calls > start_before,
              f"start 没增长（{start_before}->{h.start_calls}）：返回了 stale page")
    else:
        # 正常：CDP 死 → 接管前 _cdp_version 也 None → blocked（不返回假 page）。
        check.raises_blocked("CDP 不可达 → 重拉且 blocked（不返回旧/假 page）",
                             lambda: pb.get_platform_session(7, "noon"))
        check("CDP 不可达确实触发了重拉（startBrowser 被再调）",
              h.start_calls > start_before, f"start {start_before}->{h.start_calls}")

    # ── ③ 冷 goto abort → warmup + retry ──
    print("== ③ 冷 goto 被 abort → 平台根域 warmup 再重试 ==")
    _restore()
    h = _Harness()
    h.page = FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x"}], abort_left=2)
    h.install()
    if stub_login:
        pb._detect_login = lambda page, pcfg: pb.LoginState("ok", "stub")
    page = pb.get_platform_session(7, "noon")
    check("abort 2 次后最终拿到 page", page is h.page)
    check("warmup 路径被走过（_NAV_STATS.warmups>0）", pb._NAV_STATS["warmups"] >= 2,
          f"got {pb._NAV_STATS}")
    check("retry 后落到 OK url（非登录页）", h.page.url == NOON_OK_URL, f"got {h.page.url}")
    check("goto 含平台根域 warmup", ROOT_URL in h.page.goto_calls,
          f"calls={h.page.goto_calls}")

    # ── ④ 落登录页 / 缺 cookie → blocked ──
    print("== ④ 落登录页 / 缺会话 cookie → blocked（不 stub 登录态）==")
    _restore()
    h = _Harness()
    h.page = FakePage(NOON_LOGIN_URL, [{"name": "_npsid", "value": "x"}])
    h.install()
    if stub_login:
        pb._detect_login = lambda page, pcfg: pb.LoginState("ok", "stub")
    check.raises_blocked("落 login.noon.partners → blocked",
                         lambda: pb.get_platform_session(7, "noon"))

    _restore()
    h = _Harness()
    h.page = FakePage(NOON_OK_URL, [{"name": "visitor_id", "value": "x"}])  # 缺 _npsid
    h.install()
    if stub_login:
        pb._detect_login = lambda page, pcfg: pb.LoginState("ok", "stub")
    check.raises_blocked("缺会话 cookie _npsid → blocked",
                         lambda: pb.get_platform_session(7, "noon"))

    # ── ⑤ 缺紫鸟 / CDP 连接失败 / 接管后 CDP 不可达 → blocked ──
    print("== ⑤ 缺紫鸟 / CDP 连接失败 → blocked，不返回假 page ==")
    _restore()
    h = _Harness()
    h.install()
    def _down(port=None):
        raise pb.PlatformBrowserError("紫鸟 web_driver 端口未监听", blocked=True)
    pb._assert_webdriver_up = _down
    check.raises_blocked("缺紫鸟 webdriver → blocked",
                         lambda: pb.get_platform_session(7, "noon"))

    _restore()
    h = _Harness()
    h.install()
    def _connect_fail(cdp_url):
        raise pb.PlatformBrowserError(f"connect_over_cdp({cdp_url}) 失败", blocked=True)
    pb._connect_cdp = _connect_fail
    check.raises_blocked("CDP 连接失败 → blocked",
                         lambda: pb.get_platform_session(7, "noon"))

    _restore()
    h = _Harness()
    h.cdp_reachable = False
    h.install()
    check.raises_blocked("接管前 CDP /json/version 不可达 → blocked",
                         lambda: pb.get_platform_session(7, "noon"))
    check("blocked 路径不落 state 文件", not os.path.exists(pb._state_path(STORE_ID)))

    _restore()
    print()
    if stale and check.failures:
        print("  （SMOKE_SESSION_RETURN_STALE=1：_session_alive 退回只看 TTL → chromium "
              "没了仍返回旧 page，这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if stub_login and check.failures:
        print("  （SMOKE_SESSION_STUB_LOGIN=1：_detect_login 永远 ok → stub 登录态，"
              "这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print("✓ get_platform_session 三级回落 + 登录态 blocked smoke 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""Smoke: 每店会话有效期记录 + 紫鸟续登触发（WS-47）— fail-then-pass 承重墙。

钉死 `hipop/server/_platform_browser` 在 WS-33.3 三级回落之上新增的「会话健康层」：每店
`~/hipop/ziniao_state_<store>.json` 记录**实测** session cookie 到期 / 最后实测时间 /
建议续登时间；`get_platform_session` 读它、更新它、并在临近到期时 report blocked + 触发
紫鸟续登提示（形态镜像 refresh-dbuyerp-token）。

覆盖三个死法（WS-47）：
  ① 接线缺失 —— 状态文件写了但没人记录/读取 cookie 有效期、不触发续登。
     → 场景 1 钉死「get_platform_session 成功路径必把 cookie 有效期/建议续登时间落每店
       state」；场景 4 钉死「check_session_health 真读这些字段」。
  ② 死代码短路 —— 只查紫鸟账号认证、不查每店 Noon login/cookie 有效期。
     → 场景 3 钉死「登录态 OK 但 cookie 临近到期 / 无有效期 → 仍 blocked + 续登」。
  ③ 占位/过时假数据 —— 写死 29 天 / 2026-07-02 当默认值；或 health 只信落盘那个会过时的
     needs_renewal 布尔、不按当前时间重算。
     → 场景 2 用 44158 基线（_npsid 到 2026-07-02）+ 另一到期不同的店，证明「建议续登时间
       = 各店实测到期 − 提前量」按店各算、互不相同，绝非写死；
     → 场景 5（验门红队洞）证明「落盘时健康的 state 跨过 suggested_relogin_at 后，
       check_session_health 按当前时间确定性变红」，不无条件信落盘布尔。

fail-then-pass 证明（三个 env 开关复刻三种死法）：
  - 默认跑 → 全过。
  - SMOKE_HEALTH_NO_RECORD=1 → 把 `_save_state` 退回「不记录 cookie 有效期/建议续登」
    （死法①：状态写了但有效期没接线）→ 场景 1 的「state 含 cookie_expires_at /
    suggested_relogin_at」断言 FAIL。
  - SMOKE_HEALTH_STUB_RENEWAL=1 → 把 `_check_renewal` 退回「永远 ok」（死法②：只查账号、
    不查 cookie 有效期）→ 场景 3 的「临近到期 → blocked」断言 FAIL。
  - SMOKE_HEALTH_TRUST_BOOL=1 → 把 `_state_needs_renewal` 退回「只信落盘布尔」（死法③：
    过时假数据）→ 场景 5 的「跨过续登窗口必须变红」断言 FAIL。
  改动前（_check_renewal / cookie 有效期记录还不存在）整个文件 AttributeError 即全 fail。

跑法：
  python3 tests/smoke_session_health.py
  SMOKE_HEALTH_NO_RECORD=1 python3 tests/smoke_session_health.py     # 看回归 fail
  SMOKE_HEALTH_STUB_RENEWAL=1 python3 tests/smoke_session_health.py  # 看回归 fail
  SMOKE_HEALTH_TRUST_BOOL=1 python3 tests/smoke_session_health.py    # 看回归 fail
  （也被 make test 自动聚合）
"""
import datetime
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# state 落临时目录，绝不污染真实 ~/hipop。必须在 import pb 之前设。
_TMP_STATE = tempfile.mkdtemp(prefix="smoke_session_health_")
os.environ["HIPOP_STATE_DIR"] = _TMP_STATE
os.environ.setdefault("ZINIAO_COMPANY", "点购优品跨境")
os.environ.setdefault("ZINIAO_USERNAME", "smoke-health-user")
os.environ.setdefault("ZINIAO_PASSWORD", "smoke-health-pw")

from hipop.server import _platform_browser as pb  # noqa: E402

NOON_OK_URL = "https://noon-catalog.noon.partners/en/catalog?project=PRJ44158&tab=noon"
ROOT_URL = "https://noon-catalog.noon.partners/"
STORE_ID = "26865530773075"
RENEW_BEFORE = 5 * 86400  # 提前 5 天续登窗口（本 smoke 固定，独立于 config）

# 44158 基线：_npsid 首个到期日 2026-07-02（UTC）。这是 fixture，不是代码默认值。
T_0702 = datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc).timestamp()

PCFG = pb.PlatformCfg(
    name="noon", root_url=ROOT_URL, check_url=NOON_OK_URL,
    login_markers=["/login", "/signin", "login.noon"], session_cookie="_npsid")


# ── 替身 ────────────────────────────────────────────────────────────────
class FakeError(Exception):
    pass


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


class FakePage:
    def __init__(self, final_url, cookies):
        self._url = "about:blank"
        self._final_url = final_url
        self.context = FakeContext(cookies)

    @property
    def url(self):
        return self._url

    def goto(self, url, **kw):
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
    """替身 webdriver/CDP/playwright 边界；真函数跑健康层逻辑。"""
    def __init__(self, page, browser_id=STORE_ID, name="44158-HIPOP-NOON-SA"):
        self.page = page
        self.browser_id = browser_id
        self.name = name

    def install(self):
        store = pb.Store(browser_id=self.browser_id, browser_oauth="OAUTH-LIVE",
                         name=self.name, store_username="hipop-noon-sa",
                         account="smoke-health-user")
        pb._assert_webdriver_up = lambda port=None: port or 18080
        pb.list_stores = lambda account=None, *, port=None, store_key=None: [store]
        pb.start_browser = (lambda oauth, *, run_mode="2", port=None, creds=None,
                            store_key=None, account=None: 9777)
        pb._cdp_version = lambda cdp_url, timeout=3.0: {"Browser": "Chrome/fake"}
        pb._connect_cdp = lambda cdp_url: ("PW", FakeBrowser(self.page))
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

    def raises_blocked(self, name, fn, must_contain=None):
        try:
            fn()
            self.failures.append(name)
            print(f"  ✗ {name} （未抛错，可能返回了假 page / 临近到期还放行）")
        except pb.PlatformBrowserError as e:
            ok = getattr(e, "blocked", False)
            if must_contain and must_contain not in str(e):
                ok = False
            if ok:
                print(f"  ✓ {name} → blocked: {str(e)[:60]}")
            else:
                self.failures.append(name)
                print(f"  ✗ {name} （blocked!=True 或缺关键词 {must_contain!r}）")
        except Exception as e:  # noqa: BLE001
            self.failures.append(name)
            print(f"  ✗ {name} （非 PlatformBrowserError: {type(e).__name__}: {e}）")


_ORIG = {n: getattr(pb, n) for n in (
    "_assert_webdriver_up", "list_stores", "start_browser", "_cdp_version",
    "_connect_cdp", "_safe_close", "_check_renewal", "_save_state",
    "_renew_before_seconds", "_state_needs_renewal")}


def _restore():
    for n, f in _ORIG.items():
        setattr(pb, n, f)
    pb._session_cache.clear()
    pb._NAV_STATS.update(attempts=0, warmups=0, aborts=0)
    for f in os.listdir(_TMP_STATE):
        os.remove(os.path.join(_TMP_STATE, f))
    # 续登窗口固定 5 天，独立于 config（场景断言可预测）。
    pb._renew_before_seconds = lambda: RENEW_BEFORE


def _save_state_no_health(store_id, store, cdp_url, port, exp, *,
                          renewal=None, session_check="ok"):
    """死法①复刻：状态文件照写，但 cookie 有效期 / 建议续登时间没接线（不记录）。"""
    os.makedirs(pb._PERSIST_DIR, exist_ok=True)
    path = pb._state_path(store_id)
    json.dump({"store_id": store_id, "expires_at": float(exp),
               "saved_at": time.time()}, open(path, "w"))


def run():
    no_record = os.environ.get("SMOKE_HEALTH_NO_RECORD") == "1"
    stub_renewal = os.environ.get("SMOKE_HEALTH_STUB_RENEWAL") == "1"
    trust_bool = os.environ.get("SMOKE_HEALTH_TRUST_BOOL") == "1"
    check = _Checker()

    def _apply_deaths():
        if no_record:
            pb._save_state = _save_state_no_health
        if stub_renewal:
            pb._check_renewal = (lambda page, pcfg, now=None: pb.RenewalState(
                kind="ok", cookie_name="_npsid", cookie_expires_at=None,
                days_left=None, suggested_relogin_at=None, detail="stub"))
        if trust_bool:
            # 死法③复刻：health 无条件信落盘 needs_renewal 布尔，不按当前时间重算。
            pb._state_needs_renewal = lambda d, now: bool(d.get("needs_renewal"))

    # ── ① 接线：成功路径把实测 cookie 有效期 + 建议续登时间落每店 state ──
    print("== ① 每店 state 记录实测会话有效期 + 建议续登时间 ==")
    _restore()
    far_exp = time.time() + 60 * 86400  # 远期，未进续登窗口
    h = _Harness(FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x",
                                         "expires": far_exp}]))
    h.install()
    _apply_deaths()
    page = pb.get_platform_session(7, "noon")
    check("会话有效 → 返回真实 page", page is h.page)
    sp = pb._state_path(STORE_ID)
    check("落每店 state（按 store_id 命名，非按 account）",
          os.path.exists(sp) and STORE_ID in os.path.basename(sp))
    d = json.load(open(sp)) if os.path.exists(sp) else {}
    check("state 记录实测 cookie 到期 cookie_expires_at",
          abs((d.get("cookie_expires_at") or 0) - far_exp) < 1,
          f"got {d.get('cookie_expires_at')}")
    check("state 记录建议续登时间 = 到期 − 提前量",
          d.get("suggested_relogin_at") is not None
          and abs(d["suggested_relogin_at"] - (far_exp - RENEW_BEFORE)) < 1,
          f"got {d.get('suggested_relogin_at')}")
    check("state 记录最后实测时间 checked_at", bool(d.get("checked_at")))
    check("state 记录 cookie 名 + 本次未需续登",
          d.get("cookie_name") == "_npsid" and d.get("needs_renewal") is False,
          f"got cookie_name={d.get('cookie_name')} needs_renewal={d.get('needs_renewal')}")

    # ── ② 基线非写死：建议续登时间按各店实测到期各算 ──
    # 本场景验**纯计算**（建议续登时间 = 实测到期 − 提前量），与 death-② 的「生产路径
    # 短路续登决策」是两件事——故始终用真函数 `_check_renewal`，不受 stub 影响。
    print("== ② 44158 基线（_npsid→2026-07-02）窗口可算，且各店各异（非写死）==")
    _restore()
    pb._renew_before_seconds = lambda: RENEW_BEFORE
    _check_renewal = _ORIG["_check_renewal"]
    p_base = FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x", "expires": T_0702}])
    other_exp = T_0702 + 40 * 86400
    p_other = FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x", "expires": other_exp}])

    r_base = _check_renewal(p_base, PCFG, now=T_0702 - 29 * 86400)  # 模拟今天 ~6/03
    check("基线到期前 29 天 → ok（未进 5 天窗口）", r_base.kind == "ok",
          f"got {r_base.kind}")
    check("基线建议续登时间 = 2026-07-02 − 5 天",
          abs(r_base.suggested_relogin_at - (T_0702 - RENEW_BEFORE)) < 1,
          f"got {r_base.suggested_relogin_at}")

    r_base_late = _check_renewal(p_base, PCFG, now=T_0702 - 2 * 86400)  # 进窗口
    check("基线到期前 2 天 → needs_renewal", r_base_late.kind == "needs_renewal",
          f"got {r_base_late.kind}")

    r_other = _check_renewal(p_other, PCFG, now=T_0702 - 29 * 86400)
    check("另一店到期不同 → 建议续登时间不同（证明非写死 2026-07-02）",
          r_other.suggested_relogin_at is not None
          and abs(r_other.suggested_relogin_at - (other_exp - RENEW_BEFORE)) < 1
          and r_other.suggested_relogin_at != r_base.suggested_relogin_at,
          f"base={r_base.suggested_relogin_at} other={r_other.suggested_relogin_at}")

    # ── ③ 登录 OK 但 cookie 临近到期 / 无有效期 → blocked + 续登（不只查账号）──
    print("== ③ 临近到期 / 无 cookie 有效期 → blocked + 紫鸟续登（非紫鸟 token 假状态）==")
    _restore()
    near_exp = time.time() + 2 * 86400  # 2 天后到期，已进 5 天窗口
    h = _Harness(FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x",
                                         "expires": near_exp}]))
    h.install()
    _apply_deaths()
    check.raises_blocked("登录 OK 但临近到期 → blocked + 提示续登",
                         lambda: pb.get_platform_session(7, "noon"),
                         must_contain="续登")
    # blocked 但仍把健康状态落盘（可观测）。
    d = json.load(open(pb._state_path(STORE_ID))) if os.path.exists(
        pb._state_path(STORE_ID)) else {}
    check("临近到期路径仍落 state 且 needs_renewal=True（除非死法关掉记录）",
          no_record or d.get("needs_renewal") is True, f"got {d.get('needs_renewal')}")
    check("续登提示不报『紫鸟 token 过期』假状态",
          "紫鸟 token" not in d.get("session_check", "") if d else True)

    _restore()
    # session cookie 无 expires（真 session cookie）→ 无法担保有效期 → needs_renewal。
    h = _Harness(FakePage(NOON_OK_URL, [{"name": "_npsid", "value": "x"}]))
    h.install()
    _apply_deaths()
    check.raises_blocked("会话 cookie 无可读到期 → blocked + 续登",
                         lambda: pb.get_platform_session(7, "noon"),
                         must_contain="续登")

    # ── ④ check_session_health 真读这些字段并汇总 needs_renewal ──
    print("== ④ check_session_health 扫每店 state 汇总有效期 / needs_renewal ==")
    _restore()
    # 直接落两份 state（一健康、一需续登），验扫描函数真读 WS-47 字段。
    s_ok = pb.Store(browser_id="STORE-OK", browser_oauth="o", name="44158-NOON-SA",
                    store_username="ok", account="a")
    s_due = pb.Store(browser_id="STORE-DUE", browser_oauth="o", name="50000-NOON-AE",
                     store_username="due", account="a")
    pb._save_state("STORE-OK", s_ok, "http://127.0.0.1:1", 1, time.time() + 600,
                   renewal=pb.RenewalState("ok", "_npsid", time.time() + 60 * 86400,
                                           60.0, time.time() + 55 * 86400, "ok"))
    pb._save_state("STORE-DUE", s_due, "http://127.0.0.1:2", 2, time.time() + 600,
                   renewal=pb.RenewalState("needs_renewal", "_npsid",
                                           time.time() + 2 * 86400, 2.0,
                                           time.time() - 3 * 86400, "due"))
    health = pb.check_session_health()
    ids = {s["store_id"] for s in health["stores"]}
    check("扫到两店 state", {"STORE-OK", "STORE-DUE"} <= ids, f"got {ids}")
    check("汇总 needs_renewal=True（有店需续登）", health["needs_renewal"] is True)
    due = next((s for s in health["stores"] if s["store_id"] == "STORE-DUE"), {})
    check("needs_renewal 店带实测剩余天数（~2 天）",
          due.get("cookie_days_left") is not None and 1 <= due["cookie_days_left"] <= 3,
          f"got {due.get('cookie_days_left')}")
    check("needs_renewal 店标记 needs_renewal=True", due.get("needs_renewal") is True)

    # ── ⑤ 验门红队洞：落盘时健康的 state 跨过建议续登时间后，health 必须按当前时间变红 ──
    # （不能无条件信落盘 needs_renewal 布尔——否则 startup /health 错过主动续登窗口）
    print("== ⑤ 健康 state 跨过 suggested_relogin_at → health 确定性变红（非信落盘布尔）==")
    _restore()
    _apply_deaths()
    base = time.time()
    s_stale = pb.Store(browser_id="STORE-STALE", browser_oauth="o",
                       name="44158-NOON-SA", store_username="stale", account="a")
    # 落盘当下：cookie 还剩 29 天、建议续登时间在未来 → 落盘 needs_renewal=False（健康）。
    pb._save_state("STORE-STALE", s_stale, "http://127.0.0.1:9", 9, base + 600,
                   renewal=pb.RenewalState("ok", "_npsid", base + 29 * 86400,
                                           29.0, base + 24 * 86400, "ok"))
    saved = json.load(open(pb._state_path("STORE-STALE")))
    check("前提：落盘时 needs_renewal=False（当时健康）",
          saved.get("needs_renewal") is False, f"got {saved.get('needs_renewal')}")

    # 此刻按当前时间仍健康（未到 suggested）。
    h_before = pb.check_session_health(now=base)
    stale_b = next((s for s in h_before["stores"] if s["store_id"] == "STORE-STALE"), {})
    check("到期前 29 天（未到建议续登）→ health 仍健康", stale_b.get("needs_renewal") is False,
          f"got {stale_b.get('needs_renewal')}")

    # 时间推进到「建议续登时间之后、实测到期之前」（如 base+25 天，cookie 还剩 4 天）。
    future = base + 25 * 86400
    h_after = pb.check_session_health(now=future)
    stale_a = next((s for s in h_after["stores"] if s["store_id"] == "STORE-STALE"), {})
    check("跨过 suggested_relogin_at（cookie 仍未到期）→ health 按当前时间变红",
          stale_a.get("needs_renewal") is True, f"got {stale_a.get('needs_renewal')}")
    check("跨过续登窗口后全局 needs_renewal=True", h_after["needs_renewal"] is True)
    check("变红与落盘布尔无关：needs_renewal_at_check 仍是落盘时的 False",
          stale_a.get("needs_renewal_at_check") is False,
          f"got {stale_a.get('needs_renewal_at_check')}")

    _restore()
    print()
    if trust_bool and check.failures:
        print("  （SMOKE_HEALTH_TRUST_BOOL=1：_state_needs_renewal 退回只信落盘布尔 → "
              "健康 state 跨过建议续登时间也不变红，这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if no_record and check.failures:
        print("  （SMOKE_HEALTH_NO_RECORD=1：_save_state 退回不记录 cookie 有效期 → "
              "状态写了但有效期没接线，这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if stub_renewal and check.failures:
        print("  （SMOKE_HEALTH_STUB_RENEWAL=1：_check_renewal 永远 ok → 只查账号不查 "
              "cookie 有效期，这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print("✓ 每店会话有效期记录 + 紫鸟续登触发 smoke 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

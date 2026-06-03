"""Smoke: 平台浏览器凭据来源 + tenant context/RLS 契约（WS-33.2）— fail-then-pass 承重墙。

钉死 `hipop/server/_platform_browser.resolve_credentials` 的凭据边界（issue 风险栏点名的
三种死法），无需真实紫鸟在线：

  ① config 回落（acceptance #1）—— 无 DB 凭据但 ziniao_client env 有值时，resolve 走
     env 展开拿到明文；明文**只来自 env**（本 smoke 把 ZINIAO_* 设成 fake 值，断言 resolve
     回的就是这些 fake 值），仓库/fixture 里没有任何真实 username/password 硬编码。

  ② tenant 加密凭据 + 不串租户（acceptance #2）—— 给 tenant 1 / tenant 2 各写一行**加密**
     凭据（库里存的是 fer: 密文，不是明文）。resolve 经 `_crypto.decrypt` + `_data.conn()`
     按**当前 context** 解出本租户明文；context=1 永远拿不到 tenant 2 的账号，反之亦然。
     包含「只有 tenant 2 配了某 store_key」时 context=1 不串读的红队断言。

  ③ 绝不内部重设 tenant（acceptance #3）—— resolve 全程**不调用**
     `_data.set_current_tenant`（继承上游 context）。本 smoke 把 set_current_tenant 包成
     记账探针，断言 resolve 期间调用次数为 0。

  ④ 缺失/解密失败 → blocked，不回落假账号（acceptance #4）—— tenant 配了行但密文损坏 →
     PlatformBrowserError(blocked)，绝不回落 config 默认账号；无 tenant 行且 config 也缺
     username/password → blocked。

fail-then-pass 证明（两个 env 开关复刻两种死法，跑出预期 fail）：
  - 默认跑 → 全过。
  - SMOKE_RESET_TENANT=1 → 把 resolve 退回「内部 set_current_tenant(1)」（死法·死代码短路：
    覆盖上游 context 串租户）→ ③ 的「0 次重设」与 ② 的「context=2 拿 tenant2」断言 FAIL。
  - SMOKE_FALLBACK_FAKE_ACCOUNT=1 → 把 resolve 退回「解密失败时回落 config 假账号」（死法·
    占位假数据/默认账号）→ ④ 的「解密失败必须 blocked」断言 FAIL。
  改动前（resolve_credentials 还不存在）整个文件 AttributeError 即全 fail。

跑法（与 smoke_feedback 同套路，PG）：
  python3 tests/smoke_platform_browser_credentials.py
  SMOKE_RESET_TENANT=1 python3 tests/smoke_platform_browser_credentials.py          # 看回归 fail
  SMOKE_FALLBACK_FAKE_ACCOUNT=1 python3 tests/smoke_platform_browser_credentials.py # 看回归 fail
  （也被 make test 自动聚合）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# 必须在 import pb（触发 load_config 展开 + _crypto key 派生）之前设好 env。
os.environ.setdefault("DB_URL", "postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
os.environ.setdefault("JWT_SECRET", "hipop_alpha_stable_secret_keep_this")
# ziniao_client.username/password 仅来自这两个 env（fake 值）——证明明文不在仓库硬编码。
os.environ["ZINIAO_COMPANY"] = "点购优品跨境"
os.environ["ZINIAO_USERNAME"] = "smoke-config-user"
os.environ["ZINIAO_PASSWORD"] = "smoke-config-pw"

from hipop.server import _platform_browser as pb  # noqa: E402
from hipop.server import data as _data            # noqa: E402
from hipop.server import _crypto                  # noqa: E402

# 两个租户的 fake 紫鸟账号（明文只在测试内存，库里存密文）。
T1, T2 = 1, 2
CRED1 = ("点购优品跨境", "tenant1-ziniao-user", "tenant1-ziniao-pw")
CRED2 = ("点购优品跨境", "tenant2-ziniao-user", "tenant2-ziniao-pw")
# tenant 2 额外给某 store 单配账号，用于「context=1 不串读 tenant2 store」红队。
CRED2_STORE = ("点购优品跨境", "tenant2-noon-ae-user", "tenant2-noon-ae-pw")

_ORIG_SET_TENANT = _data.set_current_tenant
_ORIG_RESOLVE = pb.resolve_credentials


# ── fail-then-pass：复刻两种死法 ───────────────────────────────────────
def _legacy_resolve_resets_tenant(*, store_key=None, account=None):
    """死法·死代码短路：凭据函数内部重设 tenant，覆盖上游 context（串租户）。"""
    _ORIG_SET_TENANT(1)
    return _ORIG_RESOLVE(store_key=store_key, account=account)


def _legacy_resolve_fake_on_fail(*, store_key=None, account=None):
    """死法·占位假数据：解密失败不报 blocked，悄悄回落 config 默认账号。"""
    try:
        return _ORIG_RESOLVE(store_key=store_key, account=account)
    except pb.PlatformBrowserError:
        cfg = pb._client_cfg()
        return pb.Credentials(
            company=cfg.get("company"),
            username=cfg.get("username") or "default-fake-user",
            password=cfg.get("password") or "default-fake-pw",
            source="config")


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
            print(f"  ✗ {name} （未抛错，可能回落了假账号）")
        except pb.PlatformBrowserError as e:
            if getattr(e, "blocked", False):
                print(f"  ✓ {name} → blocked: {str(e)[:50]}")
            else:
                self.failures.append(name)
                print(f"  ✗ {name} （抛了但 blocked!=True）")
        except Exception as e:  # noqa: BLE001
            self.failures.append(name)
            print(f"  ✗ {name} （非 PlatformBrowserError: {type(e).__name__}: {e}）")


def _seed_cred(tid, store_key, company, user, pw):
    """写一行**加密**凭据（库里只存密文）。"""
    _ORIG_SET_TENANT(tid)
    _data.ensure_platform_browser_cred_table()
    with _data.conn() as c:
        c.execute("DELETE FROM tenant_platform_browser_credentials "
                  "WHERE tenant_id=? AND store_key=?", (tid, store_key))
        c.execute(
            "INSERT INTO tenant_platform_browser_credentials "
            "(tenant_id, store_key, provider, company_enc, username_enc, password_enc) "
            "VALUES (?,?,?,?,?,?)",
            (tid, store_key, "ziniao",
             _crypto.encrypt(company), _crypto.encrypt(user), _crypto.encrypt(pw)))
        c.commit()


def _seed_corrupt_cred(tid, store_key):
    """写一行密文损坏的凭据（解密会失败 → 必须 blocked）。"""
    _ORIG_SET_TENANT(tid)
    _data.ensure_platform_browser_cred_table()
    with _data.conn() as c:
        c.execute("DELETE FROM tenant_platform_browser_credentials "
                  "WHERE tenant_id=? AND store_key=?", (tid, store_key))
        c.execute(
            "INSERT INTO tenant_platform_browser_credentials "
            "(tenant_id, store_key, provider, company_enc, username_enc, password_enc) "
            "VALUES (?,?,?,?,?,?)",
            (tid, store_key, "ziniao", "fer:not-a-valid-token",
             "fer:garbage-cipher", "fer:garbage-cipher"))
        c.commit()


def _cleanup():
    for tid in (T1, T2):
        _ORIG_SET_TENANT(tid)
        try:
            with _data.conn() as c:
                c.execute("DELETE FROM tenant_platform_browser_credentials WHERE tenant_id=?",
                          (tid,))
                c.commit()
        except Exception:
            pass


def run():
    reset_tenant = os.environ.get("SMOKE_RESET_TENANT") == "1"
    fake_on_fail = os.environ.get("SMOKE_FALLBACK_FAKE_ACCOUNT") == "1"
    if reset_tenant:
        pb.resolve_credentials = _legacy_resolve_resets_tenant
    if fake_on_fail:
        pb.resolve_credentials = _legacy_resolve_fake_on_fail

    check = _Checker()
    try:
        # ── seed 两租户加密凭据 ──
        _seed_cred(T1, "*", *CRED1)
        _seed_cred(T2, "*", *CRED2)
        _seed_cred(T2, "NOON-AE", *CRED2_STORE)

        print("== ① config 回落：无 DB 凭据 → env 展开（明文只来自 env）==")
        _ORIG_SET_TENANT(None)        # 无 tenant context → 不查库
        c0 = pb.resolve_credentials()
        check("source==config", c0.source == "config", f"got {c0.source}")
        check("username 来自 env(ZINIAO_USERNAME)", c0.username == "smoke-config-user",
              f"got {c0.username!r}")
        check("password 来自 env(ZINIAO_PASSWORD)", c0.password == "smoke-config-pw")
        check("company 来自 env(ZINIAO_COMPANY)", c0.company == "点购优品跨境")
        # 明文不是写死在源码里：改 env 再 reload，resolve 必须跟着变。
        os.environ["ZINIAO_USERNAME"] = "smoke-config-user-ROTATED"
        from hipop.scripts._config import reload_config
        reload_config()
        c0b = pb.resolve_credentials()
        check("env 改了 resolve 跟着变（非硬编码）",
              c0b.username == "smoke-config-user-ROTATED", f"got {c0b.username!r}")
        os.environ["ZINIAO_USERNAME"] = "smoke-config-user"
        reload_config()

        print("== ② tenant 加密凭据：经 _crypto.decrypt + _data.conn 按 context 解 ==")
        # 库里确实是密文，不是明文。
        _ORIG_SET_TENANT(T1)
        raw = _data._fetch(
            "SELECT username_enc FROM tenant_platform_browser_credentials "
            "WHERE tenant_id=? AND store_key='*'", (T1,))
        check("库里存的是密文(fer:)而非明文",
              bool(raw) and str(raw[0]["username_enc"]).startswith("fer:")
              and CRED1[1] not in str(raw[0]["username_enc"]),
              f"got {raw[0]['username_enc'][:24] if raw else None!r}")

        c1 = pb.resolve_credentials()
        check("tenant1 source==tenant_db", c1.source == "tenant_db", f"got {c1.source}")
        check("tenant1 解出自己的 username", c1.username == CRED1[1], f"got {c1.username!r}")
        check("tenant1 解出自己的 password", c1.password == CRED1[2])

        _ORIG_SET_TENANT(T2)
        c2 = pb.resolve_credentials()
        check("tenant2 source==tenant_db", c2.source == "tenant_db", f"got {c2.source}")
        check("tenant2 解出自己的 username", c2.username == CRED2[1], f"got {c2.username!r}")

        print("== ②b 不串租户：context=1 永远拿不到 tenant2 账号 ==")
        _ORIG_SET_TENANT(T1)
        c1b = pb.resolve_credentials()
        check("context=1 不串到 tenant2 的 username",
              c1b.username == CRED1[1] and c1b.username != CRED2[1],
              f"got {c1b.username!r}")
        # tenant2 单配了 NOON-AE，context=1 请求同 store_key 也只能落到自己的 '*'。
        c1_ae = pb.resolve_credentials(store_key="NOON-AE")
        check("context=1 请 NOON-AE 落自己 '*'，不串 tenant2 的 store 账号",
              c1_ae.username == CRED1[1] and c1_ae.username != CRED2_STORE[1],
              f"got {c1_ae.username!r}")
        # 直查底层 row 也证明隔离。
        row_leak = _data.get_platform_browser_cred_row("NOON-AE")
        check("get_platform_browser_cred_row(context=1) 不返回 tenant2 的行",
              row_leak is not None and row_leak.get("tenant_id") in (T1, str(T1)),
              f"got tenant_id={row_leak.get('tenant_id') if row_leak else None}")
        # context=2 才拿得到自己 NOON-AE 专属账号（隔离没把数据弄丢）。
        _ORIG_SET_TENANT(T2)
        c2_ae = pb.resolve_credentials(store_key="NOON-AE")
        check("context=2 精确 store_key 命中自己 NOON-AE 专属账号",
              c2_ae.username == CRED2_STORE[1], f"got {c2_ae.username!r}")

        print("== ③ resolve 全程不内部重设 tenant（继承上游 context）==")
        calls = []
        _data.set_current_tenant = lambda tid: calls.append(tid)
        try:
            _ORIG_SET_TENANT(T2)      # 用真函数设好 context
            pb.resolve_credentials()                  # tenant_db 路径
            pb.resolve_credentials(store_key="NOON-AE")
            _ORIG_SET_TENANT(None)
            pb.resolve_credentials()                  # config 路径
        finally:
            _data.set_current_tenant = _ORIG_SET_TENANT
        check("resolve 期间 set_current_tenant 调用 0 次", len(calls) == 0,
              f"被调用 {len(calls)} 次: {calls}")

        print("== ④ 缺失/解密失败 → blocked，不回落假账号 ==")
        # 4a) tenant 配了行但密文损坏 → blocked（绝不回落 config 假账号）。
        _seed_corrupt_cred(T1, "*")
        _ORIG_SET_TENANT(T1)
        check.raises_blocked("密文损坏 → blocked（不回落 config）",
                             lambda: pb.resolve_credentials())
        _seed_cred(T1, "*", *CRED1)   # 复原，避免污染后续/其它 smoke

        # 4b) 无 tenant 行 + config 也缺 username → blocked。
        _ORIG_SET_TENANT(None)
        _saved_user = os.environ.pop("ZINIAO_USERNAME", None)
        reload_config()
        try:
            check.raises_blocked("无 tenant 行且 config 缺 username → blocked",
                                 lambda: pb.resolve_credentials())
        finally:
            if _saved_user is not None:
                os.environ["ZINIAO_USERNAME"] = _saved_user
            reload_config()

    finally:
        pb.resolve_credentials = _ORIG_RESOLVE
        _data.set_current_tenant = _ORIG_SET_TENANT
        _cleanup()

    print()
    if reset_tenant and check.failures:
        print("  （SMOKE_RESET_TENANT=1：resolve 退回内部 set_current_tenant → 串租户，"
              "这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if fake_on_fail and check.failures:
        print("  （SMOKE_FALLBACK_FAKE_ACCOUNT=1：解密失败回落 config 假账号，"
              "这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print("✓ 平台浏览器凭据来源 + tenant context/RLS 契约 smoke 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

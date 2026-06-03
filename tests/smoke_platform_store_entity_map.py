"""Smoke: store → tenant/entity 映射契约（WS-46 / WS-33.0）— fail-then-pass 承重墙。

钉死 `hipop/server/_platform_browser.{resolve_store_entity,map_stores}` 的映射边界（issue
风险栏点名的三种死法），无需真实紫鸟在线（fake getBrowserList server + 注入 fake
sales_entities，不碰 DB）：

  ① 多店全枚举 + 逐店映射（acceptance #1/#2）—— fake server 返回 3 个 store（SA / AE /
     EG），`map_stores()` 必须遍历**全部**、每店带 browser_id/store_username/name，并把
     SA→hipop_ksa、AE→hipop_uae 解析到 sales_entities 行里的 tenant_id（取自匹配行，不是
     默认值）。只取第一店 / 漏掉某店必须 fail（接线缺失死法）。

  ② 缺映射 → 红灯 blocked，绝不默认塞（acceptance #2）—— EG store 在 sales_entities 里没
     对应 entity，`resolve_store_entity` 必须抛 PlatformBrowserError(blocked)，`map_stores`
     把它收进 blocked 列表，**绝不**悄悄塞 tenant=1 / 当前 entity（占位假数据死法）。

  ③ 不串租户 / 不偷默认 tenant（红队）—— 当两个 tenant 各有一个 SA·Noon entity 时，SA
     store 命中 >1 行 → 映射不唯一 → blocked。绝不为了「有结果」抢 tenant=1。

  ④ 真相源接线（接线缺失死法）—— `map_stores()` 不传 entities 时必须真去调
     `data.sales_entities_for_mapping()`（探针计数 > 0），证明映射读的是 sales_entities
     真相源，不是写死表。

fail-then-pass 证明（两个 env 开关复刻两种死法，跑出预期 fail）：
  - 默认跑 → 全过。
  - SMOKE_DEFAULT_TENANT=1 → 把 resolve 退回「缺映射时默认塞 tenant=1/空 entity」（死法·
    占位假数据/默认塞）→ ②③ 的「缺映射/不唯一必须 blocked」断言 FAIL。
  - SMOKE_FIRST_STORE_ONLY=1 → 把 map_stores 退回「只处理第一店」（死法·接线缺失：后续只按
    单店取数，多店枚举没人调）→ ① 的「全部 store 都被逐店映射」断言 FAIL。
  改动前（resolve_store_entity/map_stores 还不存在）整个文件 AttributeError 即全 fail。

跑法：
  python3 tests/smoke_platform_store_entity_map.py
  SMOKE_DEFAULT_TENANT=1 python3 tests/smoke_platform_store_entity_map.py     # 看回归 fail
  SMOKE_FIRST_STORE_ONLY=1 python3 tests/smoke_platform_store_entity_map.py   # 看回归 fail
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

# 3 个 store fixture：SA / AE 各能映射到一个 entity，EG 故意缺映射（红灯）。
_STORE_FIXTURE = [
    {"browserId": "26865530773075", "browserName": "44158-HIPOP-NOON-SA",
     "platformAccount": "hipop-noon-sa", "browserOauth": "OAUTH-SA"},
    {"browserId": "99887766554433", "browserName": "44158-HIPOP-NOON-AE",
     "platformAccount": "hipop-noon-ae", "browserOauth": "OAUTH-AE"},
    {"browserId": "11112222333344", "browserName": "44158-HIPOP-NOON-EG",
     "platformAccount": "hipop-noon-eg", "browserOauth": "OAUTH-EG"},
]

# 注入的 sales_entities 真相源（镜像 hipop.json 的 KSA/UAE，country 用规范码 SA/AE）。
_ENTITIES = [
    {"tenant_id": 1, "alias": "hipop_ksa", "country": "SA", "platform": "Noon",
     "store_name": "HIPOP-NOON-KSA"},
    {"tenant_id": 1, "alias": "hipop_uae", "country": "AE", "platform": "Noon",
     "store_name": "HIPOP-NOON-UAE"},
]
# 红队：两个 tenant 各有一个 SA·Noon entity → SA store 映射不唯一。
_ENTITIES_AMBIGUOUS = _ENTITIES + [
    {"tenant_id": 2, "alias": "other_ksa", "country": "SA", "platform": "Noon",
     "store_name": "OTHER-NOON-KSA"},
]

REQUIRED_AUTH_KEYS = ("company", "username", "password", "action", "requestId")


class _State:
    def __init__(self):
        self.browser_list_calls = 0
        self.entity_source_calls = 0


STATE = _State()


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
            return self._send({"statusCode": -1, "err": "bad json"})
        if body.get("action") == "getBrowserList":
            STATE.browser_list_calls += 1
            missing = [k for k in REQUIRED_AUTH_KEYS if not body.get(k)]
            if missing:
                return self._send({"statusCode": -1, "err": f"missing {missing}"})
            return self._send({"statusCode": 0, "err": "",
                               "browserList": list(_STORE_FIXTURE)})
        return self._send({"statusCode": -1, "err": "unknown action"})


# ── fail-then-pass：复刻两种死法 ───────────────────────────────────────
def _legacy_resolve_default_tenant(store, *, entities=None):
    """死法·占位假数据/默认塞：缺映射不报 blocked，悄悄回落 tenant=1 / 空 entity。"""
    try:
        return _ORIG_RESOLVE(store, entities=entities)
    except pb.PlatformBrowserError:
        return pb.StoreEntity(store=store, tenant_id=1, entity_alias="",
                              country="", matched_on="DEFAULT-FALLBACK")


def _legacy_map_first_only(account=None, *, port=None, store_key=None, entities=None):
    """死法·接线缺失：只处理第一店，后续多店枚举没人逐店映射。"""
    stores = pb.list_stores(account=account, port=port, store_key=store_key)[:1]
    rows = entities if entities is not None else pb._data.sales_entities_for_mapping()
    resolved, blocked = [], []
    for s in stores:
        try:
            resolved.append(pb.resolve_store_entity(s, entities=rows))
        except pb.PlatformBrowserError as e:
            blocked.append((s, str(e)))
    return resolved, blocked


_ORIG_RESOLVE = pb.resolve_store_entity


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
            print(f"  ✗ {name} （未抛错——可能默认塞了 tenant/entity）")
        except pb.PlatformBrowserError as e:
            if getattr(e, "blocked", False):
                print(f"  ✓ {name} → blocked: {str(e)[:70]}")
            else:
                self.failures.append(name)
                print(f"  ✗ {name} （抛了但 blocked!=True）")
        except Exception as e:  # noqa: BLE001
            self.failures.append(name)
            print(f"  ✗ {name} （非 PlatformBrowserError: {type(e).__name__}: {e}）")


def run():
    default_tenant = os.environ.get("SMOKE_DEFAULT_TENANT") == "1"
    first_only = os.environ.get("SMOKE_FIRST_STORE_ONLY") == "1"
    if default_tenant:
        pb.resolve_store_entity = _legacy_resolve_default_tenant
    if first_only:
        pb.map_stores = _legacy_map_first_only

    # 真相源探针：证明 map_stores() 不传 entities 时真去读 sales_entities（接线）。
    def _fake_source(active_only=True):
        STATE.entity_source_calls += 1
        return list(_ENTITIES)
    pb._data.sales_entities_for_mapping = _fake_source

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeZiniao)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    check = _Checker()
    try:
        print("== ① 多店全枚举 + 逐店映射到 sales_entities 行的 tenant_id ==")
        resolved, blocked = pb.map_stores(port=port, entities=_ENTITIES)
        by_alias = {se.entity_alias: se for se in resolved}
        check("SA store 映射到 hipop_ksa", "hipop_ksa" in by_alias,
              f"got {sorted(by_alias)}")
        check("AE store 映射到 hipop_uae", "hipop_uae" in by_alias,
              f"got {sorted(by_alias)}（只取第一店/漏店会缺）")
        if "hipop_ksa" in by_alias:
            se = by_alias["hipop_ksa"]
            check("hipop_ksa tenant_id 取自匹配行 (==1)", se.tenant_id == 1,
                  f"got {se.tenant_id}")
            check("hipop_ksa 带原 store 的 browser_id", bool(se.store.browser_id))
            check("hipop_ksa 国别规范化为 SA", se.country == "SA", f"got {se.country}")
        check("2 个店成功映射（全枚举，非只第一店）", len(resolved) == 2,
              f"got {len(resolved)}")

        print("== ② 缺映射 store → blocked，不默认塞 ==")
        blocked_ids = {s.browser_id for s, _ in blocked}
        check("EG store 进 blocked 列表", "11112222333344" in blocked_ids,
              f"blocked={blocked_ids}")
        check("EG store 没被悄悄塞进某 entity",
              "11112222333344" not in {se.store.browser_id for se in resolved},
              "EG 不应出现在 resolved")
        eg_store = pb.Store(browser_id="11112222333344", browser_oauth="x",
                            name="44158-HIPOP-NOON-EG", store_username="hipop-noon-eg",
                            account="a")
        check.raises_blocked("resolve_store_entity(EG) → blocked",
                             lambda: pb.resolve_store_entity(eg_store, entities=_ENTITIES))

        print("== ③ 红队：两租户同 SA·Noon → 映射不唯一 → blocked（不偷 tenant=1）==")
        sa_store = pb.Store(browser_id="26865530773075", browser_oauth="x",
                            name="44158-HIPOP-NOON-SA", store_username="hipop-noon-sa",
                            account="a")
        check.raises_blocked(
            "SA store 命中两租户 entity → blocked",
            lambda: pb.resolve_store_entity(sa_store, entities=_ENTITIES_AMBIGUOUS))

        print("== ④ 真相源接线：map_stores() 不传 entities 真读 sales_entities ==")
        STATE.entity_source_calls = 0
        resolved2, _ = pb.map_stores(port=port)  # 不传 entities → 走 data 层探针
        check("map_stores 调用了 data.sales_entities_for_mapping",
              STATE.entity_source_calls > 0, f"calls={STATE.entity_source_calls}")
        check("默认真相源路径同样映射出 2 店", len(resolved2) == 2,
              f"got {len(resolved2)}")
    finally:
        srv.shutdown()
        pb.resolve_store_entity = _ORIG_RESOLVE

    print()
    if default_tenant and check.failures:
        print("  （SMOKE_DEFAULT_TENANT=1：缺映射退回默认 tenant=1/空 entity → 缺映射/不唯一"
              "不再 blocked，这是预期的『改动前 fail』。去掉变量再跑应全过。）")
    if first_only and check.failures:
        print("  （SMOKE_FIRST_STORE_ONLY=1：只处理第一店 → 漏掉 AE/EG，这是预期的『改动前"
              " fail』。去掉变量再跑应全过。）")
    if check.failures:
        print(f"✗ {len(check.failures)} 项断言失败: {check.failures}")
        return 1
    print(f"✓ store→tenant/entity 映射契约 smoke 全过"
          f"（{STATE.browser_list_calls} 次 getBrowserList）")
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""平台浏览器协议客户端（系统层，与 _erp_auth.py 并列）——紫鸟超级浏览器 web_driver 模式。

定位
----
noon 等平台官方仓数据无程序化 API，须经紫鸟超级浏览器接管已登录会话取数。本模块把这条
能力做成**系统级、平台无关**的协议客户端：任何工作流按平台无关 `store_key` 拿"已登录的
平台浏览器会话"，noon 只是第一个 adapter。镜像 `_erp_auth.py` 的工程形态（读配置 → 调本机
服务 → 解析 → 三级回落入口），凭据走 env/加密表，绝不进 prompt/skill/仓库。

本模块（WS-33.1）只做**协议客户端骨架 + 契约层**：
  - `get_browser_list()` / `list_stores(account)` —— 调本机 webdriver `getBrowserList`，
    枚举该 account 下**全部** store（不硬编 browserId），归一化出后续 tenant/entity 映射
    需要的字段（browser_id / store_username / name / account）。
  - `select_store(stores, store_key)` —— 平台无关 store_key 选中对应 store，取**当次**响应
    的最新 `browserOauth`（不缓存、不取旧值）。
  - `start_browser(browser_oauth)` —— 用精确 schema 调 `startBrowser`，**绝不传**
    debuggPort/debugPort/debuggingPort（传了真实紫鸟返回 -10000），端口由紫鸟自动分配，
    从响应 `debuggingPort` 解析（不写死 config 的 debug_port_default）。
  - `open_cdp_endpoint(store_key, account)` —— 串起上面三步，返回 CDP 端点（cdp_url +
    debugging_port + store），即后续 `get_platform_session(tenant_id, store_key)` 可复用入口
    的**底层能力**。

下一条（WS-33.2/33.3）在 `open_cdp_endpoint` 之上加：
  `playwright.chromium.connect_over_cdp` 接管 page、三级回落（内存 cache → 磁盘
  ziniao_state_<account>.json → 重启 webdriver+startBrowser）、登录态检测（落 login 则
  report blocked，绝不 stub）。本条不实现这些，只钉死协议/契约。

多租户契约（与 _erp_auth 一致）：调用方先 `data.set_current_tenant(tenant_id)`，本服务
继承 contextvar，**不重设**（`resolve_credentials` 绝不调用 `set_current_tenant`）。凭据来源
（WS-33.2）：优先 `tenant_platform_browser_credentials` 加密表（`_crypto.decrypt` + 按当前
context/RLS 查），无 tenant 行时回落 `ziniao_client` config（env 展开）；两处都没有 → blocked，
绝不退回默认/假账号。

webdriver 前置（需常驻，别成隐性人工）—— 用运维入口，别手敲：
  python3 -m hipop.scripts.ziniao_webdriver healthcheck   # 真实检 18080 + 各 live CDP
  python3 -m hipop.scripts.ziniao_webdriver start         # pkill + open -na 拉起
  bash hipop/launchd/install.sh install                   # 开机自启 + keepalive 常驻
底层动作仍是 `pkill -TERM -i ziniao` + `open -na /Applications/ziniao.app --args
--run_type=web_driver --port=18080`，封装在 `hipop/scripts/ziniao_webdriver.py`（WS-33.4）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

from hipop.scripts._config import load_config
from . import _crypto
from . import data as _data

# 本机 webdriver 在 127.0.0.1，必须绕过任何系统/环境代理（否则 localhost 请求被代理吞掉）。
_NO_PROXY_OPENER = _urlreq.build_opener(_urlreq.ProxyHandler({}))


# startBrowser 绝不允许出现的 debug 端口入参（传了真实紫鸟返回 -10000）。
# 端口由紫鸟自动分配，只能从 startBrowser 响应里读 debuggingPort。
_FORBIDDEN_DEBUG_PORT_KEYS = ("debuggPort", "debugPort", "debuggingPort", "remoteDebuggingPort")


class PlatformBrowserError(RuntimeError):
    """协议/契约层错误。blocked=True 表示需人工介入（缺 store / 缺会话 / 紫鸟拒绝），
    调用方应 report blocked、绝不 stub 假数据。"""

    def __init__(self, message: str, *, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


@dataclass
class Store:
    """归一化后的 store，携带后续 tenant/entity 映射所需字段。"""
    browser_id: str
    browser_oauth: Optional[str]
    name: str
    store_username: str
    account: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class CdpEndpoint:
    """startBrowser 后拿到的 CDP 端点；后续 connect_over_cdp 的输入。"""
    store: Store
    debugging_port: int
    cdp_url: str


# ── 配置 ────────────────────────────────────────────────────────────
def _client_cfg() -> dict:
    """ziniao_client 配置（company/username/password/web_driver_port，env 已展开）。"""
    cfg = load_config().get("ziniao_client") or {}
    return cfg


def _webdriver_port() -> int:
    return int(_client_cfg().get("web_driver_port") or 18080)


def _req_id() -> str:
    return uuid.uuid4().hex[:16]


# ── 凭据来源（WS-33.2）──────────────────────────────────────────────
@dataclass
class Credentials:
    """解析后的紫鸟登录凭据（明文仅在内存，绝不落 prompt/skill/仓库）。
    source 标识来源：'tenant_db'（加密表解密）或 'config'（ziniao_client env 展开）。"""
    company: Optional[str]
    username: str
    password: str
    source: str


def resolve_credentials(*, store_key: Optional[str] = None,
                        account: Optional[str] = None) -> Credentials:
    """解析当前调用应使用的紫鸟凭据。**继承调用方已设的 tenant context，绝不在此
    重设 tenant**（不调用 `_data.set_current_tenant`）——多租户契约与 _erp_auth 一致。

    优先级：
      1) tenant 加密凭据表 `tenant_platform_browser_credentials`：按**当前 context/RLS**
         查 (tenant, store_key)→(tenant, '*')，密文经 `_crypto.decrypt` 解出明文。tenant
         **显式配了行**就必须解密成功，解密失败/缺字段 → blocked，**绝不回落 config 假
         账号**（acceptance #4）。
      2) 无 tenant 行时回落 `ziniao_client` 配置（`_config` 已展开 env 占位符）。
      3) 两处都拿不到 username/password → blocked（不退回默认/假账号）。
    """
    cfg = _client_cfg()
    row = _data.get_platform_browser_cred_row(store_key)
    if row is not None:
        # tenant 显式配了凭据 → 必须解密成功，绝不回落 config。
        user = _crypto.decrypt(row.get("username_enc"))
        pw = _crypto.decrypt(row.get("password_enc"))
        company = _crypto.decrypt(row.get("company_enc")) or cfg.get("company")
        if not user or not pw:
            raise PlatformBrowserError(
                f"tenant={_data.get_current_tenant()} store_key={store_key!r} 平台浏览器"
                f"凭据解密失败或缺字段（密文损坏 / JWT_SECRET 变更）——不回落默认账号",
                blocked=True)
        return Credentials(company=company, username=account or user,
                           password=pw, source="tenant_db")
    # 回落 ziniao_client config（env 展开）。
    user = account or cfg.get("username")
    pw = cfg.get("password")
    if not user or not pw:
        raise PlatformBrowserError(
            "缺平台浏览器凭据：tenant 加密表无对应行，且 ziniao_client env/config 未配 "
            "username/password —— 绝不退回默认/假账号，请配置凭据或 set_current_tenant 后建表",
            blocked=True)
    return Credentials(company=cfg.get("company"), username=user,
                       password=pw, source="config")


# ── 本机 webdriver 调用 ──────────────────────────────────────────────
def _post(port: int, body: dict, timeout: int = 20):
    """POST JSON 到本机紫鸟 webdriver，HTTP 优先，失败回落 TCP+JSONLine（部分版本）。"""
    payload = json.dumps(body).encode("utf-8")
    url = f"http://127.0.0.1:{port}/"
    req = _urlreq.Request(url, data=payload, method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return _loads(raw)
    except (HTTPError, URLError, ConnectionResetError, OSError):
        return _tcp(port, body, timeout)


def _tcp(port: int, body: dict, timeout: int):
    line = (json.dumps(body) + "\r\n").encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(line)
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
            if buf.endswith(b"\n"):
                break
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    return _loads(raw) if raw else None


def _loads(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ── getBrowserList → stores ─────────────────────────────────────────
def get_browser_list(account: Optional[str] = None, *, port: Optional[int] = None,
                     store_key: Optional[str] = None) -> list:
    """调本机 `getBrowserList`，返回原始 store 条目列表。

    请求 body 必含 company/username/password/action/requestId（紫鸟鉴权所需）；凭据经
    `resolve_credentials`（tenant 加密表 → ziniao_client config 回落，绝不假账号）。
    `account` 覆盖默认登录用户名（用于枚举指定 account 下的 store）。
    """
    creds = resolve_credentials(store_key=store_key, account=account)
    return _get_browser_list(creds, port)


def _get_browser_list(creds: "Credentials", port: Optional[int] = None) -> list:
    body = {
        "action": "getBrowserList",
        "company": creds.company,
        "username": creds.username,
        "password": creds.password,
        "requestId": _req_id(),
    }
    resp = _post(port or _webdriver_port(), body)
    return _extract_entries(resp)


def _resp_code(resp: dict):
    """统一取响应状态码：真实紫鸟用 `statusCode`，fake/旧形态用 `code`。"""
    for k in ("statusCode", "code", "status"):
        if resp.get(k) is not None:
            return resp.get(k)
    return None


def _resp_msg(resp: dict):
    """统一取错误信息：真实紫鸟用 `err`，fake/旧形态用 `msg`。"""
    return resp.get("err") or resp.get("msg") or resp


def _extract_entries(resp) -> list:
    """从多种可能的响应形态里抠出 store 列表（镜像 probe 的容错位置）。

    真实紫鸟 web_driver 返回 `{"statusCode":0,"err":"","browserList":[...]}`；fake/旧形态
    用 `{"code":0,"data":[...]}`。两套 key 都兼容；statusCode/code 非 0 → blocked（暴露
    鉴权失败等，不静默当「没有店」）。
    """
    if not isinstance(resp, dict):
        raise PlatformBrowserError(
            f"getBrowserList 返回非 JSON dict: {str(resp)[:200]}", blocked=True)
    code = _resp_code(resp)
    if str(code) == "-10000":
        raise PlatformBrowserError(
            f"getBrowserList 返回 -10000: {_resp_msg(resp)}", blocked=True)
    if code is not None and str(code) not in ("0", "None"):
        raise PlatformBrowserError(
            f"getBrowserList 失败 statusCode={code}: {_resp_msg(resp)} "
            f"（紫鸟鉴权失败 / company-username-password 不匹配？）", blocked=True)
    data = (resp.get("browserList") or resp.get("data") or resp.get("browsers")
            or resp.get("list") or resp.get("result"))
    if isinstance(data, dict):
        data = (data.get("browserList") or data.get("list")
                or data.get("data") or data.get("browsers"))
    return data if isinstance(data, list) else []


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _norm_store(entry: dict, account: str) -> Store:
    return Store(
        browser_id=str(_first(entry, "browserId", "id", "uuid") or ""),
        browser_oauth=_first(entry, "browserOauth", "oauth"),
        name=str(_first(entry, "browserName", "name", "label", "title") or ""),
        store_username=str(_first(entry, "platformAccount", "storeUsername",
                                  "store_username", "username", "account") or ""),
        account=account or "",
        raw=entry,
    )


def list_stores(account: Optional[str] = None, *, port: Optional[int] = None,
                store_key: Optional[str] = None) -> list:
    """枚举 account 下**全部** store（不硬编 browserId）。

    凭据经 `resolve_credentials`（继承 tenant context，不重设）。每个 Store 携带后续
    tenant/entity 映射所需字段：browser_id、store_username、name。返回 [] 表示该 account
    没有任何 store（调用方/select_store 据此 report blocked）。
    """
    creds = resolve_credentials(store_key=store_key, account=account)
    entries = _get_browser_list(creds, port)
    return [_norm_store(e, creds.username) for e in entries if isinstance(e, dict)]


def select_store(stores: list, store_key) -> Store:
    """平台无关 `store_key` 选中 store，返回带**当次最新** browserOauth 的那条。

    匹配优先级：browser_id / store_username / name 精确 → name/store_username 大小写不敏感
    子串（如 "NOON-SA" 命中 "44158-HIPOP-NOON-SA"）。命中 0 或多于 1 个 → blocked。
    选中的 store 缺 browser_oauth → blocked（不 stub）。
    """
    key = str(store_key).strip()
    if not key:
        raise PlatformBrowserError("store_key 为空", blocked=True)

    for s in stores:
        if key in (s.browser_id, s.store_username, s.name):
            return _require_oauth(s, store_key)

    kl = key.lower()
    matches = [s for s in stores
               if kl in s.name.lower() or kl in (s.store_username or "").lower()]
    if len(matches) == 1:
        return _require_oauth(matches[0], store_key)
    if len(matches) > 1:
        raise PlatformBrowserError(
            f"store_key {store_key!r} 命中多店 {[s.name for s in matches]}，需更精确",
            blocked=True)
    raise PlatformBrowserError(
        f"store_key {store_key!r} 在 {len(stores)} 个 store 中未找到: "
        f"{[s.name or s.browser_id for s in stores]}",
        blocked=True)


def _require_oauth(s: Store, store_key) -> Store:
    if not s.browser_oauth:
        raise PlatformBrowserError(
            f"store {store_key!r}（browser_id={s.browser_id}）缺 browserOauth，无法 startBrowser",
            blocked=True)
    return s


# ════════════════════════════════════════════════════════════════════════
# WS-46 (WS-33.0) · store → tenant/entity 映射契约
# ════════════════════════════════════════════════════════════════════════
# getBrowserList 枚举出的每个 store 必须能解析到**唯一**的 (tenant_id, entity_alias)，
# 否则确定性红灯 blocked —— 绝不默认塞 tenant=1 或当前 entity（acceptance #2 的死法）。
# 真相源是 `sales_entities`（data.sales_entities_for_mapping）；tenant_id 取自匹配到的行，
# 不是默认值。映射规则全写进代码（确定性 verifier），不进 prompt/skill。
#
# 紫鸟 store 名常用国别俗称（KSA/UAE），而 sales_entities.country 用规范码（SA/AE）。
# 这张别名表是唯一需要的人工映射事实，放在代码里钉死、可被 smoke 回归。
_COUNTRY_ALIASES = {
    "SA": "SA", "KSA": "SA", "SAU": "SA",
    "AE": "AE", "UAE": "AE", "ARE": "AE",
}


@dataclass
class StoreEntity:
    """一个 store 解析到的归属。tenant_id 来自 sales_entities 匹配行，不是默认值。"""
    store: Store
    tenant_id: int
    entity_alias: str
    country: str
    matched_on: str


def _entity_rows(entities: Optional[list]) -> list:
    """映射真相源：显式传入（smoke 注入）或从 data 层 sales_entities 读。"""
    if entities is not None:
        return entities
    return _data.sales_entities_for_mapping() or []


def _store_country(store: Store) -> set:
    """从 store 标识（name / store_username / browser_id）抽出命中的规范国别码集合。
    按词边界匹配（split 非字母），'USA' 不会误命中 'SA'。"""
    text = f"{store.name} {store.store_username} {store.browser_id}"
    toks = set(re.sub(r"[^A-Za-z]+", " ", text).upper().split())
    return {canon for raw, canon in _COUNTRY_ALIASES.items() if raw in toks}


def resolve_store_entity(store: Store, *, entities: Optional[list] = None) -> StoreEntity:
    """把一个 store 解析到唯一 (tenant_id, entity_alias)，否则 blocked。

    确定性规则：
      1) 从 store 标识唯一确定规范国别码（命中 0 或 >1 个 → blocked，不猜）。
      2) 在 sales_entities 里取该国别的行；若其中有平台名（entity.platform）出现在 store
         标识里的行，则收窄到这些行（多平台同国别时按平台区分）。
      3) 收窄后**恰好 1 行** → 映射成功，tenant_id/entity_alias 取自该行；0 或 >1 → blocked。
    任何 blocked 分支都绝不回落 tenant=1 / 当前 entity（acceptance #2）。
    """
    rows = _entity_rows(entities)
    countries = _store_country(store)
    if len(countries) != 1:
        raise PlatformBrowserError(
            f"store name={store.name!r} browser_id={store.browser_id!r} 无法从标识唯一确定"
            f"国别（命中 {sorted(countries)}）—— 缺映射，blocked，绝不默认塞 tenant/entity",
            blocked=True)
    country = next(iter(countries))

    by_country = [r for r in rows if str(r.get("country") or "").upper() == country]
    text_l = f"{store.name} {store.store_username} {store.browser_id}".lower()
    by_platform = [r for r in by_country
                   if str(r.get("platform") or "").lower() in text_l]
    chosen = by_platform or by_country

    if len(chosen) == 1:
        r = chosen[0]
        return StoreEntity(
            store=store,
            tenant_id=int(r["tenant_id"]),
            entity_alias=r["alias"],
            country=country,
            matched_on=(f"country={country}"
                        + (f",platform={r.get('platform')}" if by_platform else "")),
        )
    if not chosen:
        raise PlatformBrowserError(
            f"store name={store.name!r}（国别={country}）在 sales_entities 中无匹配 entity "
            f"—— 缺映射，blocked，绝不默认塞 tenant=1/当前 entity", blocked=True)
    raise PlatformBrowserError(
        f"store name={store.name!r}（国别={country}）命中多个 entity "
        f"{[r.get('alias') for r in chosen]} —— 映射不唯一，blocked，需更精确的平台/国别区分",
        blocked=True)


def map_stores(account: Optional[str] = None, *, port: Optional[int] = None,
               store_key: Optional[str] = None,
               entities: Optional[list] = None) -> tuple:
    """枚举 account 下**全部** store 并逐店解析 tenant/entity。

    返回 `(resolved, blocked)`：
      - resolved: list[StoreEntity]，已唯一映射的店；
      - blocked:  list[(Store, reason)]，缺映射/不唯一的店——调用方据此红灯，**绝不**
        把它们悄悄塞进默认 tenant/entity。
    本函数不抛错（除非连 store 都枚举不出来）——把成功与缺映射分开返回，让调用方既能
    继续处理已映射的店，又能如实暴露缺映射的店。
    """
    stores = list_stores(account=account, port=port, store_key=store_key)
    rows = _entity_rows(entities)
    resolved, blocked = [], []
    for s in stores:
        try:
            resolved.append(resolve_store_entity(s, entities=rows))
        except PlatformBrowserError as e:
            blocked.append((s, str(e)))
    return resolved, blocked


# ── startBrowser → debuggingPort ────────────────────────────────────
def _build_start_browser_body(browser_oauth: str, run_mode: str, request_id: str) -> dict:
    """startBrowser 精确 schema。

    刻意**不含**任何 debug 端口入参——紫鸟会自动分配端口并在响应里返回 debuggingPort；
    手工塞 debuggPort/debugPort 等会被拒（-10000）。这条规则由 contract smoke 的 fake
    server 钉死，不是 prompt 约定。

    注意：真实紫鸟 web_driver 的 startBrowser **要求 body 携带 company/username/password**
    （缺则 -10003「参数不能为空（登录状态错误）」）。鉴权三件套由 `start_browser` 在调用前
    经 `_with_auth` 合并进来（实测 applyAuth 不是必需，body 带鉴权即可 statusCode 0），这里
    只保留与端口无关的精确 schema，签名维持三参不变（legacy contract smoke 替身依赖此签名）。
    """
    return {
        "action": "startBrowser",
        "browserOauth": browser_oauth,
        "runMode": run_mode,
        "webDriverConfig": {
            "notPromptForDownload": 1,
        },
        "requestId": request_id,
    }


def _with_auth(body: dict, creds: "Credentials") -> dict:
    """把紫鸟鉴权三件套合并进请求 body（startBrowser 真实必需；不覆盖已存在的键）。"""
    body.setdefault("company", creds.company)
    body.setdefault("username", creds.username)
    body.setdefault("password", creds.password)
    return body


def _extract_debug_port(resp: dict):
    for k in ("debuggingPort", "debugPort", "remoteDebuggingPort"):
        if resp.get(k) is not None:
            return resp[k]
    data = resp.get("data")
    if isinstance(data, dict):
        for k in ("debuggingPort", "debugPort", "remoteDebuggingPort"):
            if data.get(k) is not None:
                return data[k]
    return None


def start_browser(browser_oauth: str, *, run_mode: str = "2",
                  port: Optional[int] = None, creds: "Optional[Credentials]" = None,
                  store_key: Optional[str] = None,
                  account: Optional[str] = None) -> int:
    """调 `startBrowser` 接管该 store 的 chromium，返回紫鸟分配的 debuggingPort。

    端口从**响应**解析，不写死 config 的 debug_port_default。返回 -10000 / 缺 debuggingPort
    都按 blocked 抛错，不 stub。请求 body 经 `_with_auth` 带紫鸟鉴权三件套（真实 startBrowser
    必需；缺则 -10003 登录状态错误）；`creds` 未传则按 `resolve_credentials(store_key, account)`
    解析（继承 tenant context，不重设）。
    """
    if not browser_oauth:
        raise PlatformBrowserError("startBrowser 缺 browserOauth", blocked=True)
    port = port or _webdriver_port()
    if creds is None:
        creds = resolve_credentials(store_key=store_key, account=account)
    body = _build_start_browser_body(browser_oauth, run_mode, _req_id())
    body = _with_auth(body, creds)
    resp = _post(port, body)
    if not isinstance(resp, dict):
        raise PlatformBrowserError(
            f"startBrowser 返回非 JSON dict: {str(resp)[:200]}", blocked=True)
    code = _resp_code(resp)
    if str(code) == "-10000":
        raise PlatformBrowserError(
            f"startBrowser 返回 -10000（通常是传了非法入参，如 debug port）: "
            f"{_resp_msg(resp)}", blocked=True)
    dbg = _extract_debug_port(resp)
    if dbg is None:
        # statusCode/code 非 0 且没拿到端口 → 暴露真实失败原因（不静默）。
        if code is not None and str(code) not in ("0", "None"):
            raise PlatformBrowserError(
                f"startBrowser 失败 statusCode={code}: {_resp_msg(resp)}",
                blocked=True)
        raise PlatformBrowserError(
            f"startBrowser 响应缺 debuggingPort: "
            f"{json.dumps(resp, ensure_ascii=False)[:300]}", blocked=True)
    return int(dbg)


# ── 复用入口底层能力（后续 get_platform_session 之下） ──────────────────
def open_cdp_endpoint(store_key, account: Optional[str] = None, *,
                      port: Optional[int] = None) -> CdpEndpoint:
    """list_stores → select_store → start_browser，串成可被 playwright 接管的 CDP 端点。

    这是后续 `get_platform_session(tenant_id, store_key)` 可复用入口的底层能力；本条不做
    connect_over_cdp / 登录态检测 / 三级回落（WS-33.3）。凭据由 `resolve_credentials` 按
    当前 tenant context 解析（继承不重设）；同一 `store_key` 既用于选店也用于选凭据。
    """
    stores = list_stores(account=account, port=port, store_key=store_key)
    if not stores:
        raise PlatformBrowserError(
            f"account {account or _client_cfg().get('username')!r} 下没有任何 store",
            blocked=True)
    store = select_store(stores, store_key)
    dbg = start_browser(store.browser_oauth, port=port)
    return CdpEndpoint(store=store, debugging_port=dbg,
                       cdp_url=f"http://127.0.0.1:{dbg}")


# ════════════════════════════════════════════════════════════════════════
# WS-33.3 · get_platform_session：实连 CDP + 三级回落 + 登录态 blocked
# ════════════════════════════════════════════════════════════════════════
# 系统入口 `get_platform_session(tenant_id, store_key) -> page`：调用方先
# `data.set_current_tenant(tenant_id)`，本服务在 open_cdp_endpoint 之上加：
#   ① `playwright.chromium.connect_over_cdp(cdp_url)` 接管真实已登录 page；
#   ② 三级回落——内存 cache(TTL) → 磁盘 `~/hipop/ziniao_state_<store>.json`
#      （含 debug/cdp/有效期/session 信息）→ webdriver `startBrowser`，过期必重启；
#   ③ 冷 `goto` 被紫鸟扩展 abort → 先平台根域 warmup 再重试 warmup_retries 次；
#   ④ 落平台登录页 / 缺会话 cookie → report blocked（refresh-dbuyerp-token 模式
#      提示人工用紫鸟登一次），绝不 stub 登录态；
#   ⑤ 缺紫鸟 webdriver / 缺会话 / CDP 连接失败 → blocked，不返回假 page。
# 平台无关：noon 只是第一个 store_key / 平台根域配置，签名不含任何平台名。
#
# WS-47 在此之上加「会话健康层」：登录态 OK 后再从**实测** session cookie（如 _npsid）
# 算到期/建议续登时间，落进每店 state（cookie_expires_at/suggested_relogin_at/checked_at/
# needs_renewal）；临近到期 / 无可读有效期 → blocked + 续登提示（镜像 refresh-dbuyerp-
# token），绝不在临近到期时继续取数。`check_session_health()` 扫每店 state 供 /health
# 与 startup 观测。续登窗口 renew_before_days 配置化，到期日按各店实测算，绝不写死。

# 会话 state 持久化目录（每店一份）。可经 HIPOP_STATE_DIR 覆写（smoke 用临时目录）。
_PERSIST_DIR = os.environ.get("HIPOP_STATE_DIR") or os.path.expanduser("~/hipop")

_session_lock = threading.RLock()
# (tenant_id, store_key) -> PlatformSession
_session_cache: dict = {}
# 可观测：navigate warmup/retry 计数（live log / smoke 据此证明 retry 路径被调用）。
_NAV_STATS = {"attempts": 0, "warmups": 0, "aborts": 0}


@dataclass
class PlatformCfg:
    """平台登录态检测规则（来自 config `platform_browser.platforms.<name>`）。"""
    name: str
    root_url: str
    check_url: str
    login_markers: list
    session_cookie: Optional[str]


@dataclass
class LoginState:
    """登录态判定结果。kind: 'ok' | 'login'（未登录/落登录页/缺会话）。"""
    kind: str
    detail: str


@dataclass
class RenewalState:
    """会话有效期/续登判定（WS-47）。kind: 'ok' | 'needs_renewal'。

    所有时间字段从**实测** session cookie 算（不写死日期/天数），落进每店 state 文件，
    供 `check_session_health` 观测、供运营按「建议续登时间」主动续登。
    """
    kind: str
    cookie_name: Optional[str]
    cookie_expires_at: Optional[float]   # 实测会话 cookie 到期（unix 秒）；None=无有效期/未知
    days_left: Optional[float]
    suggested_relogin_at: Optional[float]
    detail: str


@dataclass
class PlatformSession:
    """已接管的平台浏览器会话；`get_platform_session` 返回其 `.page`。
    保留 browser/playwright 句柄避免被 GC，淘汰时 best-effort 断开。"""
    page: object
    store: Store
    cdp_url: str
    debugging_port: int
    source: str               # 'memory' | 'disk' | 'webdriver'
    exp: float
    browser: object = field(default=None, repr=False)
    pw: object = field(default=None, repr=False)


# ── 平台配置 ────────────────────────────────────────────────────────────
def _platform_browser_cfg() -> dict:
    return load_config().get("platform_browser") or {}


def _session_ttl() -> int:
    return int(_platform_browser_cfg().get("session_ttl_seconds") or 600)


def _warmup_retries() -> int:
    return int(_platform_browser_cfg().get("warmup_retries") or 3)


def _renew_before_seconds() -> float:
    """提前续登量（秒）：会话 cookie 还剩这么久到期时就进入「主动续登窗口」。
    来自 config `platform_browser.renew_before_days`（默认 5 天）。这是**窗口大小**，
    不是到期日——到期日按各店实测 cookie 算（WS-47 死法③：绝不写死 2026-07-02/29 天）。"""
    days = _platform_browser_cfg().get("renew_before_days")
    if days is None:
        days = 5
    return float(days) * 86400.0


def _platform_cfg_for(store_key) -> PlatformCfg:
    """store_key → 平台根域/登录检测配置。平台无关：按平台名子串命中（noon /
    NOON-SA 都落 noon）；单平台部署时数字 store_key 也回落到唯一平台；都不命中
    → blocked（不瞎猜根域）。"""
    platforms = _platform_browser_cfg().get("platforms") or {}
    if not platforms:
        raise PlatformBrowserError(
            "缺 config platform_browser.platforms —— 无法确定平台根域/登录检测规则",
            blocked=True)
    k = str(store_key).lower()
    chosen = None
    for name, pc in platforms.items():
        if name.lower() in k or k in name.lower():
            chosen = (name, pc)
            break
    if chosen is None and len(platforms) == 1:
        chosen = next(iter(platforms.items()))
    if chosen is None:
        raise PlatformBrowserError(
            f"store_key {store_key!r} 无法匹配任何平台根域配置 "
            f"{list(platforms)} —— 绝不瞎猜", blocked=True)
    name, pc = chosen
    root = pc.get("root_url") or ""
    if not root:
        raise PlatformBrowserError(
            f"平台 {name} 缺 root_url 配置", blocked=True)
    return PlatformCfg(
        name=name,
        root_url=root,
        check_url=pc.get("check_url") or root,
        login_markers=list(pc.get("login_url_markers") or []),
        session_cookie=pc.get("session_cookie"),
    )


# ── 进程层：webdriver 端口 / CDP /json/version 可达性 ──────────────────
def _webdriver_listening(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _assert_webdriver_up(port: Optional[int] = None) -> int:
    """紫鸟 web_driver 进程层端口必须在听，否则 blocked（缺紫鸟，acceptance #5）。"""
    p = port or _webdriver_port()
    if not _webdriver_listening(p):
        raise PlatformBrowserError(
            f"紫鸟 web_driver 端口 127.0.0.1:{p} 未监听 —— 请先在本机启动紫鸟超级"
            f"浏览器 web_driver 模式：open -na ziniao --args --run_type=web_driver "
            f"--port={p}", blocked=True)
    return p


def _cdp_version(cdp_url: str, timeout: float = 3.0):
    """GET `<cdp_url>/json/version` 验证 chromium debug 端口真实可达。返回 dict 或
    None。tier1/tier2 据此判断会话/持久端口是否还活着（acceptance #1）。"""
    try:
        req = _urlreq.Request(cdp_url.rstrip("/") + "/json/version", method="GET")
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None


def _session_alive(sess: "PlatformSession") -> bool:
    """会话仍可复用：未过 TTL **且** CDP /json/version 真实可达。两者缺一即重拉——
    绝不在 chromium 已没的情况下返回旧 page（死法·死代码短路）。"""
    return time.time() < sess.exp and _cdp_version(sess.cdp_url) is not None


# ── 磁盘持久化（每店一份） ──────────────────────────────────────────────
def _store_id(store: Store) -> str:
    return str(store.browser_id or store.store_username or store.name or "unknown")


def _state_path(store_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", str(store_id))
    return os.path.join(_PERSIST_DIR, f"ziniao_state_{safe}.json")


def _load_state(store_id: str) -> Optional[dict]:
    """读 `~/hipop/ziniao_state_<store>.json`。仅当：未过 expires_at **且** 持久 debug
    端口的 CDP 仍可达，才返回（可跳过 startBrowser 直接 reconnect）。过期/端口已死
    → None（回落 webdriver 重新 start，acceptance #2）。"""
    path = _state_path(store_id)
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    exp = d.get("expires_at")
    if not exp or time.time() >= float(exp):
        return None
    cdp = d.get("cdp_url")
    port = d.get("debugging_port")
    if not cdp or not port or _cdp_version(cdp) is None:
        return None
    return d


def _save_state(store_id: str, store: Store, cdp_url: str, port: int,
                exp: float, *, renewal: "Optional[RenewalState]" = None,
                session_check: str = "ok") -> None:
    """落每店会话 state（每店一份，按 store_id 命名——绝不只按 account 存一份，WS-47 死法①）。

    记录两类有效期：
      - `expires_at`：CDP 会话**复用** TTL（内存/磁盘 tier2 reconnect 用，秒级）；
      - `cookie_*` / `suggested_relogin_at`：**实测** session cookie 的登录有效期（天级），
        供续登判定与 `check_session_health` 观测。
    外加 `checked_at`（最后实测登录态/cookie 的时间）与 `session_check`（本次检测结论）。
    """
    try:
        os.makedirs(_PERSIST_DIR, exist_ok=True)
        now = time.time()
        data = {
            "store_id": store_id,
            "store_name": store.name,
            "store_username": store.store_username,
            "browser_id": store.browser_id,
            "account": store.account,
            "debugging_port": int(port),
            "cdp_url": cdp_url,
            "expires_at": float(exp),
            "saved_at": now,
            "checked_at": now,            # 最后一次真实检测登录态 / cookie 的时间
            "session_check": session_check,
        }
        if renewal is not None:
            data.update({
                "cookie_name": renewal.cookie_name,
                "cookie_expires_at": renewal.cookie_expires_at,
                "cookie_days_left": renewal.days_left,
                "suggested_relogin_at": renewal.suggested_relogin_at,
                "needs_renewal": renewal.kind == "needs_renewal",
            })
        path = _state_path(store_id)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # 持久化失败不致命——下次回落 webdriver 重启即可。


# ── playwright 接管（可注入边界，供 smoke 替身） ──────────────────────────
def _connect_cdp(cdp_url: str):
    """`playwright.chromium.connect_over_cdp`。返回 (playwright, browser)；连接失败
    → blocked，不返回假 page（acceptance #5）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise PlatformBrowserError(
            "playwright 未装：pip install playwright && playwright install chromium",
            blocked=True) from e
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except Exception as e:  # noqa: BLE001 — playwright 各类连接错都归 blocked
        try:
            pw.stop()
        except Exception:
            pass
        raise PlatformBrowserError(
            f"connect_over_cdp({cdp_url}) 失败：{e}", blocked=True) from e
    return pw, browser


def _acquire_page(browser):
    """取被接管 chromium 的现存 page（紫鸟已登录的那个），没有则新建。"""
    ctxs = list(getattr(browser, "contexts", None) or [])
    if ctxs:
        ctx = ctxs[0]
        pages = list(getattr(ctx, "pages", None) or [])
        return pages[0] if pages else ctx.new_page()
    ctx = browser.new_context()
    return ctx.new_page()


def _is_nav_abort(err: Exception) -> bool:
    """冷 goto 被紫鸟扩展 abort 的特征（net::ERR_ABORTED 等）。"""
    s = str(err).lower()
    return ("err_aborted" in s or "aborted" in s or "net::err" in s
            or "frame was detached" in s)


def _navigate_with_warmup(page, target_url: str, root_url: str,
                          retries: Optional[int] = None) -> str:
    """导航到 target_url；首次被紫鸟扩展 abort 时先平台根域 warmup 再重试 retries 次。
    全程 abort → blocked。返回最终 url。`_NAV_STATS` 记录 attempts/warmups/aborts，
    live log / smoke 据此证明 retry 路径被走过（acceptance #3）。"""
    n = retries if retries is not None else _warmup_retries()
    n = max(1, int(n))
    last = None
    for attempt in range(n):
        _NAV_STATS["attempts"] += 1
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            return page.url
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_nav_abort(e):
                raise PlatformBrowserError(
                    f"导航 {target_url} 失败（非 abort）：{e}", blocked=True) from e
            _NAV_STATS["aborts"] += 1
            # 先平台根域 warmup（让紫鸟扩展放行），再重试。
            try:
                _NAV_STATS["warmups"] += 1
                page.goto(root_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001 — warmup 失败也继续重试 target
                pass
            time.sleep(0.5)
    raise PlatformBrowserError(
        f"导航 {target_url} 连续 {n} 次被紫鸟扩展 abort（已平台根域 warmup 重试）："
        f"{last}", blocked=True)


# ── 登录态检测（确定性规则，不进 prompt） ──────────────────────────────
def _page_cookies(page) -> list:
    try:
        return list(page.context.cookies() or [])
    except Exception:  # noqa: BLE001
        return []


def _detect_login(page, pcfg: PlatformCfg) -> LoginState:
    """落登录页（url 命中 login_markers）或缺会话 cookie → 'login'（未登录）。
    两条都是代码判定的确定性规则，绝不靠模糊 prompt。"""
    url = (getattr(page, "url", "") or "")
    ul = url.lower()
    for m in pcfg.login_markers:
        if m and m.lower() in ul:
            return LoginState("login", f"落登录页 url={url}")
    if pcfg.session_cookie:
        names = {c.get("name") for c in _page_cookies(page)}
        if pcfg.session_cookie not in names:
            return LoginState(
                "login", f"缺会话 cookie {pcfg.session_cookie}（url={url}）")
    return LoginState("ok", url)


def _login_blocked_msg(pcfg: PlatformCfg, store_key, login: LoginState) -> str:
    return (
        f"平台 {pcfg.name} store_key={store_key!r} 未登录：{login.detail}。"
        f"请在本机用紫鸟超级浏览器手动打开该店、登录平台一次（参照 "
        f"refresh-dbuyerp-token 流程），登录后重试 —— 绝不 stub 登录态。")


# ── 会话有效期 / 续登判定（WS-47，确定性规则，不进 prompt） ─────────────────
def _cookie_expiry(page, cookie_name: Optional[str]):
    """从接管 page 的**真实** cookie 读会话 cookie（如 `_npsid`）的到期（unix 秒）。
    返回 float 或 None（没找到 / session cookie / expires 不是正数 → 无有效期信息）。
    各店有效期实测各异——绝不写死天数/日期（WS-47 死法③）。"""
    if not cookie_name:
        return None
    for c in _page_cookies(page):
        if c.get("name") == cookie_name:
            exp = c.get("expires")
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                return None
            return exp if exp > 0 else None
    return None


def _check_renewal(page, pcfg: PlatformCfg, *, now: Optional[float] = None) -> RenewalState:
    """从**实测** session cookie 到期算续登窗口（确定性规则，不写死日期）。

    suggested_relogin_at = cookie 实测到期 − renew_before（提前量，主动续登）；
    now ≥ suggested_relogin_at（含已过期）→ needs_renewal。会话 cookie 没有可读到期
    （真 session cookie / 拿不到 expires）也算 needs_renewal——无法担保有效期，提示续登，
    绝不当成「永久有效」放行（WS-47 死法②：不能只查紫鸟账号、不查每店 cookie 有效期）。
    """
    now = time.time() if now is None else now
    name = pcfg.session_cookie
    exp = _cookie_expiry(page, name)
    if exp is None:
        return RenewalState(
            kind="needs_renewal", cookie_name=name, cookie_expires_at=None,
            days_left=None, suggested_relogin_at=now,
            detail=(f"会话 cookie {name!r} 无可读到期（session cookie / 拿不到 expires）"
                    f"——无法担保有效期，建议续登" if name
                    else "平台未配 session_cookie，无法核会话有效期"))
    buffer = _renew_before_seconds()
    suggested = exp - buffer
    days_left = round((exp - now) / 86400.0, 1)
    if now >= suggested:
        return RenewalState(
            kind="needs_renewal", cookie_name=name, cookie_expires_at=exp,
            days_left=days_left, suggested_relogin_at=suggested,
            detail=(f"会话 cookie {name} 实测剩 {days_left} 天到期，已进入提前 "
                    f"{buffer / 86400:.0f} 天续登窗口"))
    return RenewalState(
        kind="ok", cookie_name=name, cookie_expires_at=exp, days_left=days_left,
        suggested_relogin_at=suggested,
        detail=f"会话 cookie {name} 实测剩 {days_left} 天到期")


def _renew_blocked_msg(pcfg: PlatformCfg, store_key, renewal: RenewalState) -> str:
    """续登提示，形态镜像 refresh-dbuyerp-token：是**每店 Noon 会话**临近到期，
    不是「紫鸟 token 过期」（WS-47 死法④：此层不报紫鸟账号假状态）。"""
    return (
        f"平台 {pcfg.name} store_key={store_key!r} 会话需续登：{renewal.detail}。"
        f"请在本机用紫鸟超级浏览器打开该店、重新登录一次（参照 refresh-dbuyerp-token "
        f"流程），续登后重试 —— 绝不在临近到期时继续取数 / 返回旧 page。")


# ── 句柄回收 ────────────────────────────────────────────────────────────
def _safe_close(pw, browser) -> None:
    try:
        if browser is not None:
            browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:  # noqa: BLE001
        pass


def _drop_session(ck) -> None:
    sess = _session_cache.pop(ck, None)
    if sess is not None:
        _safe_close(sess.pw, sess.browser)


# ── 系统入口 ────────────────────────────────────────────────────────────
def get_platform_session(tenant_id, store_key, *, account: Optional[str] = None,
                         port: Optional[int] = None, force_refresh: bool = False):
    """拿一个**已登录平台**的 Playwright page（平台无关，noon 是第一个 store_key）。

    契约：调用方应已 `data.set_current_tenant(tenant_id)`；本入口同时把 tenant_id 作为
    兜底设入 context（与 _erp_auth.get_erp_token_for_tenant 一致），但 `resolve_credentials`
    全程不重设 tenant（WS-33.2 契约不变）。

    三级回落（acceptance #2）：
      1) 内存 cache(TTL)：仍在 TTL 内且 CDP /json/version 可达 → 直接复用旧 page；
         过期 / chromium 已没 → 丢弃重拉（不返回旧/空 page）。
      2) 磁盘 `~/hipop/ziniao_state_<store>.json`：未过期且持久 debug 端口 CDP 可达 →
         跳过 startBrowser，直接 connect_over_cdp。
      3) webdriver `startBrowser`：紫鸟自动分配端口 → connect_over_cdp。

    登录态（acceptance #3/#4）：connect 后导航平台 check_url（冷 abort 先 warmup 再重试），
    落登录页或缺会话 cookie → blocked + 人工登录提示。缺紫鸟 / 缺凭据 / CDP 连接失败 →
    blocked（acceptance #5），绝不返回假 page。
    """
    if tenant_id is not None:
        _data.set_current_tenant(tenant_id)  # 兜底；resolve_credentials 不重设

    pcfg = _platform_cfg_for(store_key)
    ck = (tenant_id, str(store_key))

    with _session_lock:
        # ── tier 1: 内存 cache ──
        if not force_refresh:
            sess = _session_cache.get(ck)
            if sess is not None and _session_alive(sess):
                return sess.page
            if sess is not None:
                _drop_session(ck)  # 过期 / CDP 已死 → 必须重拉

        # 解析 store（getBrowserList，需 webdriver 在听；不 start 任何 chromium）。
        _assert_webdriver_up(port)
        stores = list_stores(account=account, port=port, store_key=store_key)
        if not stores:
            raise PlatformBrowserError(
                f"account {account or _client_cfg().get('username')!r} 下没有任何 "
                f"store —— 缺会话，无法取 page", blocked=True)
        store = select_store(stores, store_key)
        sid = _store_id(store)

        # ── tier 2: 磁盘持久化 ──
        ep = None
        source = None
        if not force_refresh:
            st = _load_state(sid)
            if st is not None:
                ep = CdpEndpoint(store=store,
                                 debugging_port=int(st["debugging_port"]),
                                 cdp_url=st["cdp_url"])
                source = "disk"

        # ── tier 3: webdriver startBrowser（过期/无持久 → 重新 start）──
        if ep is None:
            dbg = start_browser(store.browser_oauth, port=port,
                                store_key=store_key, account=account)
            ep = CdpEndpoint(store=store, debugging_port=dbg,
                             cdp_url=f"http://127.0.0.1:{dbg}")
            source = "webdriver"

        # 接管前先验 CDP 真实可达（绑定真实 debug port，不接受假端口）。
        ver = _cdp_version(ep.cdp_url)
        if ver is None:
            raise PlatformBrowserError(
                f"CDP {ep.cdp_url} 的 /json/version 不可达（{source} 端口已死）—— "
                f"blocked，不返回假 page", blocked=True)

        pw, browser = _connect_cdp(ep.cdp_url)
        try:
            page = _acquire_page(browser)
            _navigate_with_warmup(page, pcfg.check_url, pcfg.root_url)
            login = _detect_login(page, pcfg)
        except Exception:
            _safe_close(pw, browser)
            raise
        if login.kind != "ok":
            _safe_close(pw, browser)
            raise PlatformBrowserError(
                _login_blocked_msg(pcfg, store_key, login), blocked=True)

        # 已登录 → 从**实测** session cookie 核会话有效期（WS-47）。无论是否临近到期都先
        # 把健康状态落每店 state（可观测）；临近到期/无有效期 → blocked + 续登提示，绝不
        # 返回旧/临近到期的 page 继续取数（acceptance #3）。
        exp = time.time() + _session_ttl()
        renewal = _check_renewal(page, pcfg)
        _save_state(sid, store, ep.cdp_url, ep.debugging_port, exp,
                    renewal=renewal, session_check=renewal.kind)
        if renewal.kind != "ok":
            _safe_close(pw, browser)
            raise PlatformBrowserError(
                _renew_blocked_msg(pcfg, store_key, renewal), blocked=True)

        _session_cache[ck] = PlatformSession(
            page=page, store=store, cdp_url=ep.cdp_url,
            debugging_port=ep.debugging_port, source=source, exp=exp,
            browser=browser, pw=pw)
        return page


def invalidate_session(tenant_id, store_key) -> None:
    """会话失效（页面被登出 / 端口换了）时清掉，下次重新接管。"""
    with _session_lock:
        _drop_session((tenant_id, str(store_key)))


def check_session_health() -> dict:
    """扫所有 `~/hipop/ziniao_state_*.json`，汇总各店会话有效期 + needs_renewal 总开关。

    形态镜像 `_erp_auth.check_persist_token_expiry`：startup hook / /health 都可调，让
    每店 Noon 会话「还剩几天到期 / 是否该续登」可观测，到期前主动提示续登（参照
    refresh-dbuyerp-token），而不是等取数时才 blocked。各店天数按各自实测 cookie 算，
    绝不写死（WS-47 死法③）。
    """
    import glob
    now = time.time()
    results = []
    for path in sorted(glob.glob(os.path.join(_PERSIST_DIR, "ziniao_state_*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        exp = d.get("cookie_expires_at")
        days_left = round((float(exp) - now) / 86400.0, 1) if exp else None
        results.append({
            "store": d.get("store_name") or d.get("store_id") or os.path.basename(path),
            "store_id": d.get("store_id"),
            "cookie": d.get("cookie_name"),
            "cookie_days_left": days_left,
            "checked_at": d.get("checked_at"),
            "suggested_relogin_at": d.get("suggested_relogin_at"),
            "needs_renewal": bool(d.get("needs_renewal")),
            "session_check": d.get("session_check"),
            "path": path,
        })
    return {
        "stores": results,
        "needs_renewal": any(r["needs_renewal"] for r in results) if results else False,
    }

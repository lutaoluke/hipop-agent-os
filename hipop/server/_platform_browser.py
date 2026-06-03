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

webdriver 前置（需常驻，别成隐性人工）：
  pkill -TERM -i ziniao
  open -na /Applications/ziniao.app --args --run_type=web_driver --port=18080
"""
from __future__ import annotations

import json
import socket
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


def _extract_entries(resp) -> list:
    """从多种可能的响应形态里抠出 store 列表（镜像 probe 的容错位置）。"""
    if not isinstance(resp, dict):
        raise PlatformBrowserError(
            f"getBrowserList 返回非 JSON dict: {str(resp)[:200]}", blocked=True)
    code = resp.get("code")
    if str(code) == "-10000":
        raise PlatformBrowserError(
            f"getBrowserList 返回 -10000: {resp.get('msg') or resp}", blocked=True)
    data = (resp.get("data") or resp.get("browsers")
            or resp.get("list") or resp.get("result"))
    if isinstance(data, dict):
        data = data.get("list") or data.get("data") or data.get("browsers")
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


# ── startBrowser → debuggingPort ────────────────────────────────────
def _build_start_browser_body(browser_oauth: str, run_mode: str, request_id: str) -> dict:
    """startBrowser 精确 schema。

    刻意**不含**任何 debug 端口入参——紫鸟会自动分配端口并在响应里返回 debuggingPort；
    手工塞 debuggPort/debugPort 等会被拒（-10000）。这条规则由 contract smoke 的 fake
    server 钉死，不是 prompt 约定。
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
                  port: Optional[int] = None) -> int:
    """调 `startBrowser` 接管该 store 的 chromium，返回紫鸟分配的 debuggingPort。

    端口从**响应**解析，不写死 config 的 debug_port_default。返回 -10000 / 缺 debuggingPort
    都按 blocked 抛错，不 stub。
    """
    if not browser_oauth:
        raise PlatformBrowserError("startBrowser 缺 browserOauth", blocked=True)
    port = port or _webdriver_port()
    body = _build_start_browser_body(browser_oauth, run_mode, _req_id())
    resp = _post(port, body)
    if not isinstance(resp, dict):
        raise PlatformBrowserError(
            f"startBrowser 返回非 JSON dict: {str(resp)[:200]}", blocked=True)
    if str(resp.get("code")) == "-10000":
        raise PlatformBrowserError(
            f"startBrowser 返回 -10000（通常是传了非法入参，如 debug port）: "
            f"{resp.get('msg') or resp}", blocked=True)
    dbg = _extract_debug_port(resp)
    if dbg is None:
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

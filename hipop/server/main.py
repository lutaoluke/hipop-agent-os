"""
HIPOP Skill Server
- POST /feishu/webhook  飞书事件回调（消息接收）
- GET  /health          健康检查
"""
import asyncio
import json
import os
import sys


# ── WS-194: 端口护栏 — 非 official 服务禁止绑定生产端口 8765 ─────────────────────
def _check_prod_port_guard():
    """拒绝非正式服务绑定生产端口 8765。

    判据：
    - 绑定端口 == 8765（从 sys.argv --port 参数推断）
    - XPC_SERVICE_NAME != 'com.hipop.workbench'（launchd 正式服务注入）
    逃生门：HIPOP_ALLOW_PROD_PORT=1（仅运维应急，默认关闭）
    """
    if os.environ.get("HIPOP_ALLOW_PROD_PORT") == "1":
        return

    bound_port = None
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            try:
                bound_port = int(argv[i + 1])
            except ValueError:
                pass
            break
        if arg.startswith("--port="):
            try:
                bound_port = int(arg[7:])
            except ValueError:
                pass
            break

    if bound_port != 8765:
        return

    xpc = os.environ.get("XPC_SERVICE_NAME", "")
    if xpc == "com.hipop.workbench":
        return

    print(
        "\n[FATAL] 端口 8765 是 HIPOP 生产端口，非正式服务禁止绑定。\n"
        f"        当前 XPC_SERVICE_NAME={xpc!r}（正式服务应为 'com.hipop.workbench'）。\n"
        "        临时/PR/agent 服务请用 8766+ ；正式上线走 hipop-prod-deploy.sh。\n"
        "        如确需临时占用（仅运维），设 HIPOP_ALLOW_PROD_PORT=1 后再启动。",
        file=sys.stderr,
        flush=True,
    )
    os._exit(1)


_check_prod_port_guard()
# ─────────────────────────────────────────────────────────────────────────────


# WS-161 路线(B)：server 进程默认启用语义 fact-slot grounding judge（确定性 grounding，
# 接进 _factslot_contract.apply）。单测 make test 不导入 main → 不设此 flag → judge 关、
# 走确定性结构门 floor，保证单测可复现。运维可用 HIPOP_FACTSLOT_SEMANTIC=0 关。
os.environ.setdefault("HIPOP_FACTSLOT_SEMANTIC", "1")

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Auto-load env file（DEEPSEEK_API_KEY / DB_URL 等），重启 uvicorn 不必每次手动 export.
# HIPOP_ENV_FILE lets smoke runner and server process bind to the same DB env.
_DEFAULT_DOTENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env.local")


def _load_env_file(path):
    if not path:
        return False
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return False
    with open(path) as f:
        for _line in f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip("'").strip('"')
            os.environ.setdefault(_k, _v)
    return True


for _env_path in (os.environ.get("HIPOP_ENV_FILE"), _DEFAULT_DOTENV_PATH):
    if _load_env_file(_env_path):
        break

from server.feishu import reply_text, send_card, send_text
from server.intent import parse_intent
from server.skills import dispatch

app = FastAPI(title="HIPOP Skill Server + Agent OS")


# ── Auth lockdown (2026-05-26 Phase 7)：未登录禁止访问，杜绝陌生人看 tenant=1 数据 ──
# 历史 fallback `tenant_id=1`（=Cherry/HIPOP）让任何未登录请求都能读到真实数据。
# 现在 middleware 在最外层拦：白名单内放行，其他要么 401 (API) 要么跳 /login (页面)。
# 可通过 env AUTH_LOCKDOWN=0 临时关闭（仅用于 debug；切勿在生产）。
_PUBLIC_ROUTES = {
    "/login", "/register", "/health", "/ready", "/favicon.ico",
    "/feishu/webhook",
    "/api/auth/login", "/api/auth/register", "/api/auth/logout",
}
_PUBLIC_PREFIXES = ("/static/", "/api/auth/")


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_ROUTES:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


@app.middleware("http")
async def tenant_context_middleware(request, call_next):
    from server import auth as _auth_mod, data as _data
    from fastapi.responses import JSONResponse, RedirectResponse

    path = request.url.path
    lockdown = os.environ.get("AUTH_LOCKDOWN", "1") != "0"

    if _is_public_path(path):
        # 公开路由：不设 tenant（数据访问应被各 endpoint 自己拒）
        _data.set_current_tenant(0)
        return await call_next(request)

    try:
        user = _auth_mod.get_current_user(request)
    except Exception:
        user = None

    if lockdown and (not user or user.get("is_default")):
        # 未登录：API 返 401（前端可拦后跳转），页面跳 /login?next=<原路径>
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "未登录，请先登录后再访问"},
                status_code=401,
            )
        return RedirectResponse(f"/login?next={path}", status_code=302)

    # 登录用户 — 设 tenant context（PG RLS 用）
    _data.set_current_tenant((user or {}).get("tenant_id") or 1)
    return await call_next(request)

# ── Phase 1: 工作台 UI + JSON API ─────────────────────────
_SERVER_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(_SERVER_DIR, "static")), name="static")

from server.pages import router as _pages_router
from server.api import router as _api_router
app.include_router(_pages_router)
app.include_router(_api_router, prefix="/api")

# ── Startup self-check: smoke_governance + dbuyerp token 过期 ─────
# 后台跑（不阻塞启动），结果只 log。fail 不阻 server 起来，但 Luke 一看 log 就知道
@app.on_event("startup")
def _startup_selfcheck():
    import subprocess, threading

    def _smoke():
        repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        env = {**os.environ, "PYTHONPATH": repo}
        for name in ("smoke_governance", "smoke_judge"):
            try:
                r = subprocess.run(
                    [sys.executable, f"tests/{name}.py"],
                    cwd=repo, capture_output=True, text=True, timeout=60, env=env,
                )
                if r.returncode == 0:
                    print(f"[startup] ✓ {name} passed", flush=True)
                else:
                    print(f"[startup] ⚠️ {name} FAILED:", flush=True)
                    print(r.stdout[-800:] or r.stderr[-800:], flush=True)
            except Exception as e:
                print(f"[startup] {name} skipped: {e}", flush=True)

    def _token_check():
        try:
            from server import _erp_auth
            status = _erp_auth.check_persist_token_expiry()
            if status.get("needs_refresh"):
                print(f"[startup] ⚠️ dbuyerp token 即将过期或已过期: {status['tokens']}", flush=True)
                print(f"[startup]    → 跑 skill 刷新: /refresh-dbuyerp-token", flush=True)
            elif status.get("tokens"):
                days = [f"{t['user']}={t['days_left']}d" for t in status['tokens']]
                print(f"[startup] ✓ dbuyerp tokens healthy: {days}", flush=True)
        except Exception as e:
            print(f"[startup] token check skipped: {e}", flush=True)

    def _session_check():
        """每店 Noon 平台会话有效期检查（WS-47），镜像 dbuyerp token 检查。"""
        try:
            from server import _platform_browser as _pb
            status = _pb.check_session_health()
            if status.get("needs_renewal"):
                stale = [s for s in status["stores"] if s["needs_renewal"]]
                print(f"[startup] ⚠️ Noon 平台会话临近到期/需续登: {stale}", flush=True)
                print(f"[startup]    → 用紫鸟超级浏览器重登该店（参照 refresh-dbuyerp-token）", flush=True)
            elif status.get("stores"):
                days = [f"{s['store']}={s['cookie_days_left']}d" for s in status['stores']]
                print(f"[startup] ✓ Noon 平台会话 healthy: {days}", flush=True)
        except Exception as e:
            print(f"[startup] session check skipped: {e}", flush=True)

    threading.Thread(target=_smoke, daemon=True).start()
    threading.Thread(target=_token_check, daemon=True).start()
    threading.Thread(target=_session_check, daemon=True).start()


# ── 每日自动刷新（APScheduler）───────────────────────────
# 默认 02:00 跑 refresh_all_v2 for every active tenant；
# 可通过 DAILY_REFRESH_CRON='hour=2,minute=0' 调整，DISABLE_DAILY_REFRESH=1 关闭
@app.on_event("startup")
def _start_daily_refresh():
    if os.environ.get("DISABLE_DAILY_REFRESH"):
        print("[scheduler] DISABLE_DAILY_REFRESH set, skipping", flush=True)
        return
    # 用 importlib 走 hipop.server.scheduler，避免 sys.path hack 双 module
    # （之前 sev2/_erp_auth 都踩过，feedback_pg_sqlite_compat memory）
    import importlib
    try:
        _scheduler = importlib.import_module("hipop.server.scheduler")
    except ModuleNotFoundError:
        from server import scheduler as _scheduler  # CLI 直跑兜底
    try:
        _scheduler.start()
        print("[scheduler] started OK", flush=True)
    except Exception as e:
        import traceback
        print(f"[scheduler] start FAILED: {e}\n{traceback.format_exc()}", flush=True)

# 防重放：记录已处理的 message_id
_processed = set()

# ── 健康检查 ─────────────────────────────────────────────
@app.get("/health")
def health():
    """liveness + 关键依赖状态。"""
    try:
        from server import _erp_auth
        erp_token = _erp_auth.check_persist_token_expiry()
    except Exception as e:
        erp_token = {"error": str(e)[:200]}
    try:
        from server import _platform_browser as _pb
        platform_session = _pb.check_session_health()
    except Exception as e:
        platform_session = {"error": str(e)[:200]}
    return {
        "status": "ok",
        "erp_token": erp_token,            # {tokens: [...], needs_refresh: bool}
        "platform_session": platform_session,  # {stores: [...], needs_renewal: bool}
    }


@app.get("/ready")
def ready():
    """readiness — DB 连得上才 200，否则 503（让 LB 别打流量）。"""
    from server import data as _data
    try:
        with _data.conn() as c:
            c.execute("SELECT 1").fetchone()
        return {"status": "ready", "db": "ok",
                "mode": "postgres" if _data.is_postgres() else "sqlite"}
    except Exception as e:
        return JSONResponse(
            {"status": "not_ready", "db": "fail", "error": str(e)[:200]},
            status_code=503,
        )

# ── 飞书 Webhook ──────────────────────────────────────────
@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # 飞书 URL 验证握手
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge")})

    header = body.get("header", {})
    event  = body.get("event", {})

    # 只处理消息事件
    if header.get("event_type") != "im.message.receive_v1":
        return JSONResponse({"code": 0})

    message    = event.get("message", {})
    message_id = message.get("message_id", "")
    chat_id    = message.get("chat_id", "")
    msg_type   = message.get("message_type", "")

    # 防止重复处理
    if message_id in _processed:
        return JSONResponse({"code": 0})
    _processed.add(message_id)

    # 只处理文本消息
    if msg_type != "text":
        return JSONResponse({"code": 0})

    try:
        content = json.loads(message.get("content", "{}"))
        text    = content.get("text", "").strip()
    except Exception:
        return JSONResponse({"code": 0})

    # 去掉 @ 机器人的前缀（飞书会在 text 里带上 @用户名）
    import re
    text = re.sub(r"@\S+\s*", "", text).strip()
    if not text:
        return JSONResponse({"code": 0})

    # 异步执行，立即返回 200（飞书要求3秒内响应）
    background_tasks.add_task(handle_message, chat_id, message_id, text)
    return JSONResponse({"code": 0})


async def handle_message(chat_id: str, message_id: str, text: str):
    """后台处理：识别意图 → 执行 skill → 回复结果"""
    # 先回复"收到，正在处理"
    reply_text(message_id, f"⏳ 收到：「{text}」\n正在识别并执行，请稍候...")

    # 意图识别
    intent = parse_intent(text)
    skill  = intent.get("skill", "unknown")
    skus   = intent.get("skus", [])

    if skill == "unknown":
        reply_text(message_id,
            f"🤔 未能识别指令：{intent.get('reason', '')}\n\n"
            f"可用指令示例：\n"
            f"• 更新所有 SKU 在途库存\n"
            f"• 查一下 TBJ0057A 到货时间\n"
            f"• 跑一遍销售周期分析\n"
            f"• 给我补货建议")
        return

    # 执行 skill（在线程池里跑，不阻塞事件循环）
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, dispatch, skill, skus)

    # 截取结果（飞书消息有长度限制）
    summary = result[:2000] if len(result) > 2000 else result

    skill_names = {
        "wf0_logistics": "在途库存 & 物流预估",
        "wf3_sales":     "销售周期分析",
        "wf4_restock":   "补货建议",
    }
    title = f"✅ {skill_names.get(skill, skill)} 执行完成"
    send_card(chat_id, title, f"```\n{summary}\n```")

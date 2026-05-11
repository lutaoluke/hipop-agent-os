"""
HIPOP 工作台 - JSON API (read-only Day 1 + 上传/SSE/chat Day 2-3)
"""
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse
from typing import Optional
from uuid import uuid4
import asyncio, os, json, shutil, sys, traceback

from . import data, mock

# 让 hipop/scripts/* 能 import
HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(HIPOP_ROOT)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, PROJECT_ROOT)

router = APIRouter()


# ── Auth: register / login / logout / me ─────────────────
from . import auth as _auth_mod
from fastapi import Cookie, Depends


@router.post("/auth/register")
def api_register(body: dict, response: Response = None):  # type: ignore
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    tenant_name = body.get("tenant_name") or ""
    display_name = body.get("display_name") or ""
    if not email or not password:
        raise HTTPException(400, "email 和 password 必填")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    info = _auth_mod.register(email, password, tenant_name=tenant_name, display_name=display_name)
    # 注册后自动登录
    out = _auth_mod.login(email, password)
    if response is not None:
        _auth_mod.set_session_cookie(response, out["token"])
    return {"ok": True, "user": out["user"], "token": out["token"]}


@router.post("/auth/login")
def api_login(body: dict, response: Response = None):  # type: ignore
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    out = _auth_mod.login(email, password)
    if response is not None:
        _auth_mod.set_session_cookie(response, out["token"])
    return {"ok": True, "user": out["user"], "token": out["token"]}


@router.post("/auth/logout")
def api_logout(response: Response):
    _auth_mod.clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
def api_me(user: dict = Depends(_auth_mod.get_current_user)):
    return {"user": user}


@router.get("/auth/permissions")
def api_permissions(user: dict = Depends(_auth_mod.get_current_user)):
    """前端按角色隐藏 / 灰显入口。"""
    from . import rbac as _rbac
    return {"role": user.get("role"), "permissions": _rbac.get_my_permissions(user)}


# ── Onboarding（客户引导，W4 + Phase C）──────────────────
@router.post("/onboarding/erp-verify")
def api_onboarding_erp_verify(body: dict, user: dict = Depends(_auth_mod.get_current_user)):
    """alpha 阶段：格式校验。真连验证留到下次 ingest。"""
    if user.get("is_default"):
        raise HTTPException(401, "请先登录")
    username = (body or {}).get("username", "").strip()
    password = (body or {}).get("password", "")
    if not username or not password:
        return {"ok": False, "message": "用户名/密码必填"}
    if len(password) < 4:
        return {"ok": False, "message": "密码看起来太短，请确认"}
    return {
        "ok": True,
        "message": f"凭据格式 OK（{username}）。下次跑 ingest 时会真连测试。",
    }


@router.post("/onboarding/finish")
def api_onboarding_finish(body: dict, user: dict = Depends(_auth_mod.get_current_user)):
    """提交完整接入信息：真存 sales_entities + ERP 凭据加密 + 邀请同事。"""
    if user.get("is_default"):
        raise HTTPException(401, "请先登录")
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(400, "user 没有 tenant_id")

    from . import auth as _a
    from . import _crypto
    from hipop.scripts import sales_entity_v2 as _sev2  # type: ignore

    entities = (body or {}).get("entities", [])
    erp      = (body or {}).get("erp", {})
    feishu   = (body or {}).get("feishu", {})
    invites  = (body or {}).get("invites", [])

    # 1. sales_entities → DB 真落
    saved_entities = []
    for e in entities:
        alias = (e.get("alias") or "").strip()
        store = (e.get("store") or "").strip()
        if not alias or not store:
            continue
        eid = _sev2.upsert_entity(
            tenant_id=tenant_id,
            alias=alias,
            country=(e.get("country") or "SA").upper(),
            platform=(e.get("platform") or "Noon"),
            store_name=store,
            store_id=e.get("store_id"),
            currency=e.get("currency"),
        )
        saved_entities.append({"id": eid, "alias": alias, "store": store,
                                "country": e.get("country"), "store_id": e.get("store_id")})

    # 2. ERP 凭据加密存
    erp_configured = False
    if erp.get("username") and erp.get("password"):
        with data.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tenant_erp_credentials "
                "(tenant_id, erp_kind, erp_url, username_enc, password_enc, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
                (tenant_id, erp.get("kind", "dbuyerp"),
                 erp.get("url", "https://www.dbuyerp.com"),
                 _crypto.encrypt(erp["username"]),
                 _crypto.encrypt(erp["password"])),
            )
            c.commit()
        erp_configured = True

    # 3. 飞书凭据
    feishu_configured = False
    if feishu.get("webhook") or feishu.get("app_secret") or feishu.get("base_id"):
        with data.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tenant_feishu_credentials "
                "(tenant_id, app_id, app_secret_enc, webhook_enc, bitable_base_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
                (tenant_id, feishu.get("app_id"),
                 _crypto.encrypt(feishu.get("app_secret")),
                 _crypto.encrypt(feishu.get("webhook")),
                 feishu.get("base_id")),
            )
            c.commit()
        feishu_configured = True

    # 4. 邀请同事
    invited_users = []
    failed_invites = []
    for u in invites:
        email = (u.get("email") or "").strip().lower()
        role  = u.get("role", "ops")
        if not email:
            continue
        if _a.get_user_by_email(email):
            failed_invites.append({"email": email, "reason": "邮箱已被注册"})
            continue
        try:
            default_pw = email.split("@")[0] + "_alpha"
            uid = _a.create_user(tenant_id, email, default_pw,
                                  display_name=email.split("@")[0], role=role)
            invited_users.append({"email": email, "role": role, "user_id": uid,
                                  "default_password": default_pw})
        except Exception as ex:
            failed_invites.append({"email": email, "reason": str(ex)[:200]})

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "saved_entities": saved_entities,
        "erp_configured": erp_configured,
        "feishu_configured": feishu_configured,
        "invited_users": invited_users,
        "failed_invites": failed_invites,
        "next": "/?store=" + ("ksa" if any(e.get("country") == "SA" for e in saved_entities) else "uae"),
    }


@router.get("/today/{store}")
def api_today(store: str):
    return data.get_today(store)


@router.get("/modules/{store}")
def api_modules(store: str):
    return data.get_module_summaries(store)


@router.get("/sku-health/{store}")
def api_sku_health(store: str, urgency: str = "all", limit: int = 30):
    rows = data.get_sku_health(store, urgency=None if urgency == "all" else urgency, limit=limit)
    return rows


@router.get("/orders/{store}")
def api_orders(store: str, limit: int = 50):
    return data.get_orders(store, limit=limit)


@router.get("/replenishment/{store}")
def api_replenishment(store: str, limit: int = 50):
    return data.get_replenishment(store, limit=limit)


@router.get("/work-log/{store}")
def api_work_log(store: str):
    return data.get_work_log(store)


@router.get("/data-health/{store}")
def api_data_health(store: str):
    return data.get_data_health(store)


@router.get("/team/{store}")
def api_team(store: str, user: dict = Depends(_auth_mod.get_current_user)):
    """返回当前 tenant 下所有真实 user。未登录 fallback 显示 default + 一条 hint。"""
    tenant_id = user.get("tenant_id") or 1
    rows = data._fetch(
        "SELECT id, email, display_name, role, last_active_at FROM users "
        "WHERE tenant_id=? AND active=1 ORDER BY id",
        (tenant_id,),
    )
    me_id = user.get("id")
    out = []
    for r in rows:
        name = r.get("display_name") or (r.get("email") or "").split("@")[0] or "?"
        out.append({
            "name": name,
            "role": {"owner": "店主", "manager": "主管", "ops": "运营", "forwarder": "跟单"}.get(r["role"], r["role"]),
            "online": True,  # 在线状态阶段 2 接 ws presence；当前都标 online
            "tasks": 0,
            "is_me": (r["id"] == me_id),
            "avatar": name[0].upper() if name else "?",
        })
    if not out:
        # users 表空（未注册过任何用户）→ 显示当前默认 user
        u = user
        nm = u.get("display_name") or "Cherry"
        out = [{"name": nm, "role": "店主", "online": True, "tasks": 0, "is_me": True, "avatar": nm[0].upper()}]
    return out


@router.get("/traffic/{store}")
def api_traffic(store: str):
    """noon 流量 API 未接，阶段 2 上线。"""
    return {
        "_status": "not_implemented",
        "message": "noon 流量数据接入计划在阶段 2（接 noon 后台 API）",
    }


@router.get("/selection/{store}")
def api_selection(store: str):
    """选品候选评估功能未上线（计算逻辑还在工程化）。"""
    return {
        "_status": "not_implemented",
        "candidates": [],
        "strategies": data.get_selection_strategies(),
        "message": "选品 Agent 在工程化中（见 plans/productization.md 阶段 2）",
    }


@router.get("/marketing/{store}")
def api_marketing(store: str):
    """营销分析功能未上线。"""
    return {
        "_status": "not_implemented",
        "message": "营销数据接入计划在阶段 2",
    }


@router.get("/progress/current")
def api_progress():
    return data.get_progress_current()


@router.get("/chat-history/{store}")
def api_chat_history(store: str, limit: int = 100):
    """读取持久化的聊天记录；空库直接返空（前端显示引导语，不掺示例对话）。"""
    return data.get_chat_messages(store, limit=limit)


@router.get("/cross-store/logistics")
def api_cross_logistics():
    return data.get_cross_store_logistics()


# ── Agent Actions / Reference 系统 ─────────────────────
@router.get("/agent-actions/{action_id}")
def api_agent_action(action_id: int):
    a = data.get_agent_action(action_id)
    if not a:
        raise HTTPException(404, "action not found")
    return a


@router.get("/agent-actions")
def api_list_agent_actions(store: str = "ksa", module: Optional[str] = None, limit: int = 30):
    return data.list_agent_actions(store, module, limit)


@router.post("/agent-actions/{action_id}/adopt")
def api_adopt(action_id: int, body: dict):
    by = body.get("by", "Cherry")
    with data.conn() as c:
        c.execute("""
            UPDATE agent_actions SET status='adopted', adopted_by=?,
            adopted_at=datetime('now','localtime') WHERE id=?
        """, (by, action_id))
        c.commit()
    return {"ok": True, "id": action_id, "status": "adopted"}


# ── 飞书 digest ───────────────────────────────────────
@router.get("/feishu-digest")
def api_feishu_digest(limit: int = 20):
    return data._fetch("SELECT * FROM feishu_digest ORDER BY digest_at DESC LIMIT ?", (limit,))


# ── 上传 + 真触发 ingest（按 tenant 隔离 + v2 表）──────────
@router.post("/upload")
async def api_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    followup_prompt: Optional[str] = None,
    store: Optional[str] = None,
    user: dict = Depends(_auth_mod.get_current_user),
):
    """CSV 上传：按 tenant_id 隔离存储路径 + 调 v2 ingest（写 wf2_orders/wf2_sku v2 表）。"""
    task_id = uuid4().hex[:8]
    tenant_id = user.get("tenant_id") or 1
    # tenant 隔离：inbox/<tenant_id>/<filename>
    inbox = os.path.join(PROJECT_ROOT, "inbox", str(tenant_id))
    os.makedirs(inbox, exist_ok=True)
    saved = []
    for f in files:
        fp = os.path.join(inbox, f.filename or f"upload_{uuid4().hex[:6]}.csv")
        with open(fp, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(fp)

    affected_modules = ["sales", "replenish", "logistics"]
    label = f"CSV 上传 + ingest（{len(saved)} 个文件）"

    data.write_event(task_id, 0, "初始化", "done", json.dumps({
        "workflow": "upload_pipeline",
        "label": label,
        "affected_modules": affected_modules,
        "total_steps": 4,
        "followup_prompt": followup_prompt,
        "tenant_id": tenant_id,
    }, ensure_ascii=False))
    data.write_event(task_id, 1, "上传文件", "done",
                     f"已保存 {len(saved)} 个文件到 tenant {tenant_id}")
    background_tasks.add_task(_run_pipeline_v2, task_id, saved, tenant_id,
                               followup_prompt, affected_modules)
    return {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "files": [os.path.basename(s) for s in saved],
        "label": label,
        "affected_modules": affected_modules,
        "followup_prompt": followup_prompt,
        "total_steps": 4,
        "workflow": "upload_pipeline",
    }


def _run_pipeline_v2(task_id: str, file_paths: list, tenant_id: int,
                     followup_prompt: Optional[str] = None,
                     affected_modules: Optional[list] = None):
    """v2 多租户 pipeline: 校验 → ingest_noon_csv_v2（写 v2 表）→ 聚合销量"""
    affected_modules = affected_modules or ["sales", "replenish", "logistics"]
    failed = False
    try:
        data.write_event(task_id, 2, "校验格式", "started")
        for p in file_paths:
            if not os.path.exists(p):
                raise RuntimeError(f"文件不存在: {p}")
        data.write_event(task_id, 2, "校验格式", "done", f"{len(file_paths)} 个文件就绪")

        data.write_event(task_id, 3, f"解析 + 入库 (tenant={tenant_id})", "started")
        try:
            # 设 tenant context（PG RLS 用；SQLite 也无害）
            data.set_current_tenant(tenant_id)
            from scripts import ingest_noon_csv_v2
            import sqlite3
            conn = sqlite3.connect(data.DB_PATH)
            total = 0
            for p in file_paths:
                if "Inventory" in os.path.basename(p):
                    continue  # noon Inventory CSV 走 wf1（暂未 v2 化）
                try:
                    n = ingest_noon_csv_v2.process_csv_v2(tenant_id, p, conn)
                    total += n or 0
                except Exception as e:
                    data.write_event(task_id, 3, "解析 + 入库", "error",
                                     f"{os.path.basename(p)}: {e}")
                    failed = True
            conn.close()
            if not failed:
                data.write_event(task_id, 3, f"解析 + 入库", "done",
                                 f"累计 {total} 行 (tenant={tenant_id})")
        except Exception as e:
            data.write_event(task_id, 3, "解析 + 入库", "error", str(e))
            failed = True

        if not failed:
            # 聚合销量到 wf2_sku.sales_*
            data.write_event(task_id, 4, "聚合销量窗口", "started")
            try:
                import sqlite3
                conn = sqlite3.connect(data.DB_PATH)
                # 拿该 tenant 的所有 entity_alias
                aliases = [r[0] for r in conn.execute(
                    "SELECT DISTINCT entity_alias FROM wf2_orders WHERE tenant_id=?",
                    (tenant_id,)
                ).fetchall()]
                from scripts.ingest_noon_csv_v2 import aggregate_sales_v2
                total_skus = 0
                for alias in aliases:
                    n = aggregate_sales_v2(tenant_id, alias, conn)
                    total_skus += n
                conn.close()
                data.write_event(task_id, 4, "聚合销量窗口", "done",
                                 f"刷新 {total_skus} 个 SKU 的 sales_*d")
            except Exception as e:
                data.write_event(task_id, 4, "聚合销量窗口", "error", str(e)[:200])

        data.write_event(task_id, 99, "管道完成",
                         "error" if failed else "done",
                         json.dumps({
                             "workflow": "upload_pipeline",
                             "affected_modules": affected_modules,
                             "ok": not failed,
                             "followup_prompt": followup_prompt,
                             "tenant_id": tenant_id,
                         }, ensure_ascii=False))
    except Exception as e:
        data.write_event(task_id, 99, "管道异常", "error",
                         traceback.format_exc()[:500])


# ── 真 SSE ────────────────────────────────────────────
@router.get("/events/stream/{task_id}")
async def api_events_stream(task_id: str):
    async def gen():
        last_id = 0
        yield f"data: {json.dumps({'type':'connected','task_id':task_id})}\n\n"
        terminal_seen = False
        idle_ticks_after_terminal = 0
        # 单 step 可以长达若干分钟（飞书同步、ERP 拉单），整体上限 30 分钟
        absolute_deadline = 60 * 60 * 2  # 0.5s × 3600 = 30min
        elapsed = 0
        while True:
            rows = data.get_events_after(task_id, last_id)
            for r in rows:
                yield f"data: {json.dumps(r, ensure_ascii=False)}\n\n"
                last_id = r["id"]
                if r.get("step_no") == 99:
                    terminal_seen = True
            if terminal_seen:
                # 已收到管道完成事件，再 idle 5 次（保证 99 之后的尾巴都送达）后关闭
                idle_ticks_after_terminal += 1 if not rows else 0
                if idle_ticks_after_terminal >= 5:
                    yield f"data: {json.dumps({'type':'closing'})}\n\n"
                    break
            elapsed += 1
            if elapsed > absolute_deadline:
                yield f"data: {json.dumps({'type':'closing','reason':'deadline'})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ── Run Workflow（chat / 手动 都可触发的统一入口）──────────
WORKFLOW_REGISTRY = {
    # name → (label, [(step_no, step_name, callable_path), ...], affected_modules)
    "wf1_stock": (
        "wf1 商品库存",
        [(1, "ERP 6 仓 + noon Inventory + 聚合 + 飞书", "scripts.weekly_run:step_wf1")],
        ["sales", "replenish"],
    ),
    "wf2_sales": (
        "wf2 商品总表 + 销量",
        [(1, "商品库 + 销量 + noon CSV + 聚合 + 飞书", "scripts.weekly_run:step_wf2")],
        ["sales"],
    ),
    # v2 多租户版：按 tenant_id 跑（用 onboarding 配的 ERP 凭据 headless 登录）
    "wf2_products_v2": (
        "ERP 商品库（自动拉，per-tenant）",
        [(1, "headless 登 ERP + 拉商品 + 写 wf2_sku v2",
          "scripts.ingest_erp_products_v2:run_v2")],
        ["sales"],
    ),
    "wf2_sales_v2": (
        "ERP 销量价格（自动拉，per-tenant）",
        [
            (1, "拉商品库（确保 partner_sku 行存在）", "scripts.ingest_erp_products_v2:run_v2"),
            (2, "拉销量价格 6 个时间窗 + 更新 wf2_sku", "scripts.ingest_erp_sales_v2:run_v2"),
        ],
        ["sales", "replenish"],
    ),
    "wf1_stock_v2": (
        "ERP 库存（自动拉，per-tenant，国内+海外仓）",
        [(1, "拉所有仓库存 + 写 wf1_stock v2", "scripts.ingest_erp_stock_v2:run_v2")],
        ["sales", "replenish"],
    ),
    "wf5_sales_cycle_v2": (
        "销售周期 + 补货决策（v2 表，per-tenant）",
        [(1, "per-entity 销售周期算法 + 写 wf5_sales_cycle v2",
          "workflows.wf_sales_cycle:run_v2")],
        ["sales", "replenish"],
    ),
    # 全套：商品+销量+库存+销售周期 一键跑
    "refresh_all_v2": (
        "完整刷新（商品→销量→库存→销售周期）",
        [
            (1, "ERP 商品库", "scripts.ingest_erp_products_v2:run_v2"),
            (2, "ERP 销量价格", "scripts.ingest_erp_sales_v2:run_v2"),
            (3, "ERP 库存（6 仓）", "scripts.ingest_erp_stock_v2:run_v2"),
            (4, "销售周期 + 补货决策", "workflows.wf_sales_cycle:run_v2"),
        ],
        ["sales", "replenish", "logistics"],
    ),
    "wf3_logistics": (
        "wf3 物流采集",
        [(1, "全 entity 扫单 + 写 hub + 飞书", "scripts.weekly_run:step_wf3")],
        ["logistics", "replenish"],
    ),
    "wf5_sales_cycle": (
        "wf5 销售周期 + 补货",
        [(1, "per-entity 销售周期 + sync_decisions", "scripts.weekly_run:step_wf5")],
        ["sales", "replenish"],
    ),
    "wf6_alerts": (
        "wf6 物流告警",
        [(1, "生成告警 + 飞书 alerts/warehouse_appt", "scripts.weekly_run:step_wf6")],
        ["logistics", "replenish"],
    ),
    "daily": (
        "每日例行（wf3 + wf6 + 日报）",
        [
            (1, "wf3 物流采集",   "scripts.weekly_run:step_wf3"),
            (2, "wf6 告警生成",   "scripts.weekly_run:step_wf6"),
            (3, "日报卡片",        "scripts.daily_run:step_daily_card"),
        ],
        ["logistics", "replenish"],
    ),
    "weekly": (
        "每周例行（全链路）",
        [
            (1, "wf1 商品库存",        "scripts.weekly_run:step_wf1"),
            (2, "wf2 商品总表+销量",   "scripts.weekly_run:step_wf2"),
            (3, "wf3 物流采集",        "scripts.weekly_run:step_wf3"),
            (4, "wf6 告警生成",        "scripts.weekly_run:step_wf6"),
            (5, "wf5 销售周期+补货",   "scripts.weekly_run:step_wf5"),
            (6, "周报卡片",            "scripts.weekly_run:step_summary_card"),
        ],
        ["sales", "logistics", "replenish"],
    ),
}


def _resolve_callable(path: str):
    """'scripts.weekly_run:step_wf1' → callable"""
    mod_name, fn_name = path.split(":")
    import importlib
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


def _run_workflow(task_id: str, workflow: str, tenant_id: int = 1):
    """串行执行 WORKFLOW_REGISTRY 中定义的所有 step，写入 agent_events。

    tenant_id：v2 step 函数（接受 tenant_id 参数的）会传入。老 step 不受影响（callable 不接受 kw 时落回无参调用）。
    """
    label, steps, affected = WORKFLOW_REGISTRY[workflow]
    data.write_event(
        task_id, 0, "初始化",
        "done",
        json.dumps({"workflow": workflow, "label": label,
                    "affected_modules": affected, "total_steps": len(steps),
                    "tenant_id": tenant_id},
                   ensure_ascii=False),
    )
    # 后台线程：必须 set tenant context（middleware 不在这条线程上）
    data.set_current_tenant(tenant_id)
    failed = False
    for step_no, step_name, path in steps:
        if failed:
            data.write_event(task_id, step_no, step_name, "skipped", "前置步骤失败，已跳过")
            continue
        data.write_event(task_id, step_no, step_name, "started")
        try:
            fn = _resolve_callable(path)
            # 探测函数是否接受 tenant_id（v2 step 是 fn(tenant_id)，老 step 是 fn()）
            import inspect
            sig = inspect.signature(fn)
            if "tenant_id" in sig.parameters:
                fn(tenant_id=tenant_id)
            else:
                fn()
            data.write_event(task_id, step_no, step_name, "done")
        except Exception as e:
            data.write_event(task_id, step_no, step_name, "error", traceback.format_exc()[-500:])
            failed = True
    final_status = "error" if failed else "done"
    data.write_event(
        task_id, 99, "管道完成", final_status,
        json.dumps({"workflow": workflow, "affected_modules": affected,
                    "ok": not failed, "tenant_id": tenant_id}, ensure_ascii=False),
    )


@router.post("/run-workflow")
async def api_run_workflow(body: dict, background_tasks: BackgroundTasks,
                            user: dict = Depends(_auth_mod.get_current_user)):
    workflow = (body or {}).get("workflow")
    if workflow not in WORKFLOW_REGISTRY:
        raise HTTPException(400, f"unknown workflow: {workflow}. valid: {list(WORKFLOW_REGISTRY)}")
    tenant_id = user.get("tenant_id") or 1
    label, steps, affected = WORKFLOW_REGISTRY[workflow]
    task_id = uuid4().hex[:8]
    background_tasks.add_task(_run_workflow, task_id, workflow, tenant_id)
    return {
        "task_id": task_id,
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "tenant_id": tenant_id,
        "status": "started",
    }


# ── Chat (LLM) ────────────────────────────────────────
@router.post("/chat")
async def api_chat(body: dict, user: dict = Depends(_auth_mod.get_current_user)):
    """
    body = {messages: [...], scope: {store, module, current_user, current_role}}
    返回 {reply: '...', references: [...], action_id: int|null}

    user 自动从 cookie/JWT 注入；未登录时是 DEFAULT_USER（Cherry, owner, tenant=1）
    body.scope 仅用 store/module；user_name/role 从 token 派生（不接受前端冒充）
    """
    from . import agent
    messages = body.get("messages") or []
    body_scope = body.get("scope") or {}
    scope = {
        "store": body_scope.get("store") or "KSA",
        "module": body_scope.get("module"),
        "current_user": user.get("display_name") or user.get("email") or "Cherry",
        "current_role": user.get("role") or "ops",
        "tenant_id": user.get("tenant_id"),
        "user_id": user.get("id"),
    }
    store = scope["store"]
    user_name = scope["current_user"]

    # 持久化：保存最后一条 user 消息（仅当尾部确实是 user 时；避免 retry 重复落库）
    if messages and messages[-1].get("role") == "user":
        last_user = messages[-1].get("content")
        if isinstance(last_user, str) and last_user.strip():
            data.write_chat_message(store, "user", user_name, last_user)

    try:
        out = await asyncio.get_event_loop().run_in_executor(None, agent.chat, messages, scope)
    except Exception as e:
        err = f"⚠️ chat error: {e}"
        data.write_chat_message(store, "agent", "Agent", err, tag="error")
        return JSONResponse({"reply": err, "references": [], "action_id": None}, status_code=200)

    # Layer 3 hallucinate 后处理验证
    from . import _safety
    sanitized, hallu_warnings = _safety.sanitize_reply(out.get("reply") or "", out.get("tools_used") or [])
    if hallu_warnings:
        out["reply"] = sanitized
        out["hallucination_warnings"] = hallu_warnings
        out["tag"] = "hallucinate"

    # 持久化 agent 回复
    try:
        data.write_chat_message(
            store, "agent", "Agent",
            out.get("reply") or "(无回复)",
            tag=out.get("tag") or "",
            references=out.get("references") or [],
            task=out.get("workflow_task"),
        )
    except Exception:
        pass
    return out


# ── 数据健康检查 (诊断) ─────────────────────────────────
@router.get("/audit/{store}")
def api_audit(store: str):
    """数据巡检 agent: 10 invariants 跑一遍, 防 P0 类 bug 回归"""
    from . import audit
    return audit.get_summary(store)


@router.get("/diag/db")
def api_diag_db():
    return {
        "db_path": data.DB_PATH,
        "exists": os.path.exists(data.DB_PATH),
        "size_kb": (os.path.getsize(data.DB_PATH) // 1024) if os.path.exists(data.DB_PATH) else 0,
        "tables": [r["name"] for r in data._fetch("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
    }

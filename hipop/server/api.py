"""
HIPOP 工作台 - JSON API (read-only Day 1 + 上传/SSE/chat Day 2-3)
"""
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException
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
def api_team(store: str):
    return mock.TEAM_MEMBERS


@router.get("/traffic/{store}")
def api_traffic(store: str):
    return mock.TRAFFIC_MOCK


@router.get("/selection/{store}")
def api_selection(store: str):
    out = {
        "candidates": mock.SELECTION_MOCK,
        "strategies": data.get_selection_strategies(),
    }
    return out


@router.get("/marketing/{store}")
def api_marketing(store: str):
    return mock.MARKETING_MOCK


@router.get("/progress/current")
def api_progress():
    return data.get_progress_current()


@router.get("/chat-history/{store}")
def api_chat_history(store: str, limit: int = 100):
    """读取持久化的聊天记录；空库时回退 mock seed（首次使用时给点示例）。"""
    rows = data.get_chat_messages(store, limit=limit)
    if not rows:
        return mock.CHAT_HISTORY_MOCK
    return rows


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


# ── 上传 + 真触发 ingest + wf3/wf5 ─────────────────────
@router.post("/upload")
async def api_upload(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    task_id = uuid4().hex[:8]
    inbox = os.path.join(PROJECT_ROOT, "inbox")
    os.makedirs(inbox, exist_ok=True)
    saved = []
    for f in files:
        fp = os.path.join(inbox, f.filename or f"upload_{uuid4().hex[:6]}.csv")
        with open(fp, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(fp)

    data.write_event(task_id, 1, "上传文件", "done", f"已保存 {len(saved)} 个文件")
    background_tasks.add_task(_run_pipeline, task_id, saved)
    return {"task_id": task_id, "files": [os.path.basename(s) for s in saved]}


def _run_pipeline(task_id: str, file_paths: list):
    """串行: 校验 → ingest_noon_csv.process_csv → wf3 → wf5"""
    try:
        data.write_event(task_id, 2, "校验格式", "started")
        for p in file_paths:
            if not os.path.exists(p):
                raise RuntimeError(f"文件不存在: {p}")
        data.write_event(task_id, 2, "校验格式", "done", f"{len(file_paths)} 个文件就绪")

        data.write_event(task_id, 3, "解析 + 入库 wf2", "started")
        try:
            from scripts import ingest_noon_csv
            import sqlite3
            db = data.DB_PATH
            conn = sqlite3.connect(db)
            total = 0
            for p in file_paths:
                if "Inventory" in os.path.basename(p):
                    # 跳过 inventory CSV（wf1 范围）
                    continue
                try:
                    n = ingest_noon_csv.process_csv(p, conn, dry_run=False)
                    total += n or 0
                except Exception as e:
                    data.write_event(task_id, 3, "解析 + 入库 wf2", "error", f"{os.path.basename(p)}: {e}")
            conn.close()
            data.write_event(task_id, 3, "解析 + 入库 wf2", "done", f"累计 {total} 行")
        except Exception as e:
            data.write_event(task_id, 3, "解析 + 入库 wf2", "error", str(e))
            return

        data.write_event(task_id, 4, "重跑 wf5 (销售周期)", "started")
        try:
            from workflows import wf_sales_cycle as _w5
            try:
                _w5.run(verbose=False)
            except Exception as e:
                data.write_event(task_id, 4, "重跑 wf5 (销售周期)", "error", str(e)[:200])
            else:
                data.write_event(task_id, 4, "重跑 wf5 (销售周期)", "done")
        except Exception as e:
            data.write_event(task_id, 4, "重跑 wf5 (销售周期)", "error", str(e)[:200])

        data.write_event(task_id, 5, "重跑 wf3 + 告警", "started")
        try:
            from workflows import wf_logistics_alerts
            try:
                wf_logistics_alerts.generate_alerts(verbose=False)
            except Exception as e:
                data.write_event(task_id, 5, "重跑 wf3 + 告警", "error", str(e)[:200])
            else:
                data.write_event(task_id, 5, "重跑 wf3 + 告警", "done")
        except Exception as e:
            data.write_event(task_id, 5, "重跑 wf3 + 告警", "error", str(e)[:200])

        data.write_event(task_id, 6, "管道完成", "done", "可刷新查看新数据")
    except Exception as e:
        data.write_event(task_id, 9, "管道异常", "error", traceback.format_exc()[:500])


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


def _run_workflow(task_id: str, workflow: str):
    """串行执行 WORKFLOW_REGISTRY 中定义的所有 step，写入 agent_events。"""
    label, steps, affected = WORKFLOW_REGISTRY[workflow]
    # step_no=0 写 init（携带 affected_modules，前端用来定向刷新）
    data.write_event(
        task_id, 0, "初始化",
        "done",
        json.dumps({"workflow": workflow, "label": label,
                    "affected_modules": affected, "total_steps": len(steps)},
                   ensure_ascii=False),
    )
    failed = False
    for step_no, step_name, path in steps:
        if failed:
            data.write_event(task_id, step_no, step_name, "skipped", "前置步骤失败，已跳过")
            continue
        data.write_event(task_id, step_no, step_name, "started")
        try:
            fn = _resolve_callable(path)
            fn()
            data.write_event(task_id, step_no, step_name, "done")
        except Exception as e:
            data.write_event(task_id, step_no, step_name, "error", traceback.format_exc()[-500:])
            failed = True
    final_status = "error" if failed else "done"
    data.write_event(
        task_id, 99, "管道完成", final_status,
        json.dumps({"workflow": workflow, "affected_modules": affected,
                    "ok": not failed}, ensure_ascii=False),
    )


@router.post("/run-workflow")
async def api_run_workflow(body: dict, background_tasks: BackgroundTasks):
    workflow = (body or {}).get("workflow")
    if workflow not in WORKFLOW_REGISTRY:
        raise HTTPException(400, f"unknown workflow: {workflow}. valid: {list(WORKFLOW_REGISTRY)}")
    label, steps, affected = WORKFLOW_REGISTRY[workflow]
    task_id = uuid4().hex[:8]
    background_tasks.add_task(_run_workflow, task_id, workflow)
    return {
        "task_id": task_id,
        "workflow": workflow,
        "label": label,
        "total_steps": len(steps),
        "affected_modules": affected,
        "status": "started",
    }


# ── Chat (LLM) ────────────────────────────────────────
@router.post("/chat")
async def api_chat(body: dict):
    """
    body = {messages: [...], scope: {store, module, current_user, current_role}}
    返回 {reply: '...', references: [...], action_id: int|null}
    """
    from . import agent
    messages = body.get("messages") or []
    scope = body.get("scope") or {"store": "KSA", "module": None, "current_user": "Cherry", "current_role": "运营"}
    store = (scope.get("store") or "KSA")
    user_name = scope.get("current_user") or "Cherry"

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

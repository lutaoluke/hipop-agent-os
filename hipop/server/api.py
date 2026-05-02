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
def api_chat_history(store: str):
    return mock.CHAT_HISTORY_MOCK


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
        # 推一个连接信号
        yield f"data: {json.dumps({'type':'connected','task_id':task_id})}\n\n"
        idle_ticks = 0
        while True:
            rows = data.get_events_after(task_id, last_id)
            for r in rows:
                yield f"data: {json.dumps(r, ensure_ascii=False)}\n\n"
                last_id = r["id"]
            if not rows:
                idle_ticks += 1
            else:
                idle_ticks = 0
            # 任务连续 30 次没新事件 → 关闭
            if idle_ticks > 60:
                yield f"data: {json.dumps({'type':'closing'})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


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
    try:
        out = await asyncio.get_event_loop().run_in_executor(None, agent.chat, messages, scope)
        return out
    except Exception as e:
        return JSONResponse({"reply": f"⚠️ chat error: {e}", "references": [], "action_id": None}, status_code=200)


# ── 数据健康检查 (诊断) ─────────────────────────────────
@router.get("/diag/db")
def api_diag_db():
    return {
        "db_path": data.DB_PATH,
        "exists": os.path.exists(data.DB_PATH),
        "size_kb": (os.path.getsize(data.DB_PATH) // 1024) if os.path.exists(data.DB_PATH) else 0,
        "tables": [r["name"] for r in data._fetch("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
    }

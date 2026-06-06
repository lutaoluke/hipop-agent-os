"""Managed Agents runtime — Phase 0.1（2026-05-21）

把长任务（wf3 物流 / wf2 销量 / refresh_all / 选品 multi-agent）从 uvicorn
daemon thread 抽出来，跑在独立 subprocess。重启 uvicorn 不再杀任务。

跟 Anthropic Managed Agents 范式对应：
  Brain (stateless): 业务 LLM 推理 + 决策（agent.py / chat tool）
  Hands (sandboxed):  本模块 spawn 的 subprocess worker
  Session (durable):  PG tasks 表 + agent_events 流 + ~/hipop/tasks/<task_id>/ 文件

核心 API：
  spawn_task(workflow, tenant_id, actor, spec) → task_id
  wake_task(task_id) → 检测 orphan 自动接管（watchdog 用）
  kill_task(task_id)
  task_status(task_id) → 完整状态视图

文件布局：
  ~/hipop/tasks/<task_id>/
    spec.json       initializer 写的输入（workflow / params / SKU list 等）
    progress.json   worker 每 chunk 写的进度（resume point）
    scratch/        中间数据（不污染 agent context — MCP Code Execution 思路）
    log.txt         subprocess stdout/stderr
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from . import data as _data


TASKS_ROOT = Path(os.environ.get("HIPOP_TASKS_ROOT", os.path.expanduser("~/hipop/tasks")))
HEARTBEAT_TIMEOUT_SEC = int(os.environ.get("HIPOP_HEARTBEAT_TIMEOUT", "300"))  # 5 min


def _ensure_task_dir(task_id: str) -> Path:
    d = TASKS_ROOT / task_id
    (d / "scratch").mkdir(parents=True, exist_ok=True)
    return d


def _task_paths(task_id: str) -> dict:
    d = TASKS_ROOT / task_id
    return {
        "dir": str(d),
        "spec": str(d / "spec.json"),
        "progress": str(d / "progress.json"),
        "scratch": str(d / "scratch"),
        "log": str(d / "log.txt"),
    }


def spawn_task(
    workflow: str,
    tenant_id: int,
    actor: dict,
    spec: Optional[dict] = None,
    task_id: Optional[str] = None,
) -> str:
    """启动 task。写 spec.json + INSERT tasks 行 + 写 queued event + 起 subprocess。

    actor: {user_id, email, role, source}  跟 agent_events.actor_* 一致
    spec: 给 worker 的输入（workflow 入参 + chunked progress 起点等）
    task_id: 可选；不给则生成 UUID 8 位。

    WS-99 T21-SUB-1：queued event 在子进程启动前同步写入 agent_events，确保
    调用方能立即通过 get_events_after 读到 ≥1 durable 任务证据（不靠后台线程）。
    """
    # Bootstrap SQLite tasks/agent_events schema（PG 模式 no-op）
    _data._ensure_task_tables()

    task_id = task_id or uuid.uuid4().hex[:8]
    paths = _task_paths(task_id)
    _ensure_task_dir(task_id)

    # 1. 写 spec.json（worker 启动后读）
    with open(paths["spec"], "w") as f:
        json.dump({
            "task_id": task_id,
            "workflow": workflow,
            "tenant_id": tenant_id,
            "actor": actor,
            "spec": spec or {},
            "created_at": time.time(),
        }, f, ensure_ascii=False, indent=2)

    # 2. INSERT tasks 行（state=queued）— RLS 上下文得先 set
    _data.set_current_tenant(tenant_id)
    with _data.conn() as c:
        c.execute(
            "INSERT INTO tasks "
            "(task_id, tenant_id, workflow, state, spec_path, progress_path, scratch_dir, "
            " actor_user_id, actor_email, actor_source) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (task_id, tenant_id, workflow,
             paths["spec"], paths["progress"], paths["scratch"],
             actor.get("user_id"), actor.get("email"), actor.get("source")),
        )
        c.commit()

    # 2b. 写 queued event（子进程启动前同步落库 — durable 任务证据，WS-99 T21-SUB-1）
    _data.write_event(
        task_id, 0, "任务排队", "queued",
        json.dumps({"workflow": workflow, "tenant_id": tenant_id}, ensure_ascii=False),
        actor=actor,
    )

    # 3. 起 subprocess worker（nohup detached，重启 uvicorn 不影响）
    # 用 sys.executable 而非裸 "python3" —— 后者在 PATH 上可能解析到错的 venv（如 homebrew
    # python3.14 没装 anthropic/psycopg2），导致 worker 一启动就 ImportError
    import sys as _sys
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    log_f = open(paths["log"], "a")
    proc = subprocess.Popen(
        [_sys.executable, "-u", "-m", "hipop.runtime.worker", task_id],
        stdout=log_f, stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,  # detach 不绑父进程
    )
    pid = proc.pid

    # 4. 更新 tasks.worker_pid + state=running
    with _data.conn() as c:
        c.execute(
            "UPDATE tasks SET worker_pid=?, state='running', "
            "last_heartbeat=datetime('now','localtime') "
            "WHERE task_id=?",
            (pid, task_id),
        )
        c.commit()

    # observability
    try:
        from . import observability as _obs
        _obs.task_lifecycle("spawned", task_id, workflow, tenant_id, worker_pid=pid)
    except Exception: pass

    return task_id


def wake_task(task_id: str) -> dict:
    """watchdog 检测到 orphan task 时调用 — 重启 worker 接管。
    worker 启动后会读 progress.json 决定续跑 / 重新开始。
    """
    _data.set_current_tenant_to_task(task_id)
    with _data.conn() as c:
        rows = c.execute(
            "SELECT tenant_id, workflow, worker_pid, wake_count, state FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchall()
    if not rows:
        return {"ok": False, "error": "task not found"}
    row = rows[0]
    # 已 done / error / cancelled 的 task 不重唤醒（避免 done task 被误 wake）
    if row["state"] in ("done", "error", "cancelled"):
        return {"ok": False, "error": f"task already {row['state']}, not wakeable"}
    old_pid = row["worker_pid"]
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
            os.kill(old_pid, 0)  # 还活就 SIGKILL
            os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 已死

    paths = _task_paths(task_id)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    log_f = open(paths["log"], "a")
    proc = subprocess.Popen(
        ["python3", "-u", "-m", "hipop.runtime.worker", task_id],
        stdout=log_f, stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    new_pid = proc.pid

    with _data.conn() as c:
        c.execute(
            "UPDATE tasks SET worker_pid=?, state='running', "
            "last_heartbeat=datetime('now','localtime'), "
            "wake_count=wake_count+1 WHERE task_id=?",
            (new_pid, task_id),
        )
        c.commit()

    # observability — wake 是关键运维事件
    try:
        from . import observability as _obs
        _obs.task_lifecycle("waked", task_id, row["workflow"], row.get("tenant_id") or 0,
                             new_pid=new_pid, old_pid=old_pid,
                             wake_count=row["wake_count"] + 1)
    except Exception: pass

    return {"ok": True, "task_id": task_id, "new_pid": new_pid,
            "wake_count": row["wake_count"] + 1}


def kill_task(task_id: str) -> dict:
    """主动取消。"""
    _data.set_current_tenant_to_task(task_id)
    with _data.conn() as c:
        rows = c.execute("SELECT worker_pid FROM tasks WHERE task_id=?", (task_id,)).fetchall()
    if not rows:
        return {"ok": False, "error": "task not found"}
    pid = rows[0]["worker_pid"]
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    with _data.conn() as c:
        c.execute(
            "UPDATE tasks SET state='cancelled', "
            "finished_at=datetime('now','localtime'), worker_pid=NULL WHERE task_id=?",
            (task_id,),
        )
        c.commit()
    return {"ok": True, "task_id": task_id}


def task_status(task_id: str) -> Optional[dict]:
    """整状态视图：tasks 行 + 最近 events + progress.json snapshot。"""
    _data.set_current_tenant_to_task(task_id)
    with _data.conn() as c:
        rows = c.execute(
            "SELECT task_id, tenant_id, workflow, state, worker_pid, "
            "       started_at, last_heartbeat, finished_at, wake_count, result_summary "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchall()
    if not rows:
        return None
    task = dict(rows[0])
    paths = _task_paths(task_id)
    progress = None
    if os.path.exists(paths["progress"]):
        try:
            with open(paths["progress"]) as f:
                progress = json.load(f)
        except Exception:
            progress = {"_error": "progress.json corrupted"}
    task["progress"] = progress
    return task


def heartbeat(task_id: str, message: Optional[str] = None) -> None:
    """worker 每 chunk 调一次 — UPDATE tasks.last_heartbeat。"""
    _data.set_current_tenant_to_task(task_id)
    with _data.conn() as c:
        c.execute(
            "UPDATE tasks SET last_heartbeat=datetime('now','localtime') WHERE task_id=?",
            (task_id,),
        )
        c.commit()


def save_progress(task_id: str, payload: dict) -> None:
    """worker 写 progress.json — merge 进已有 payload。"""
    paths = _task_paths(task_id)
    existing = {}
    if os.path.exists(paths["progress"]):
        try:
            with open(paths["progress"]) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    existing.update(payload)
    existing["updated_at"] = time.time()
    with open(paths["progress"], "w") as f:
        json.dump(existing, f, ensure_ascii=False, default=str)


# ── env-aware helpers (workflow / script 用) ────────────────────
# 任何 workflow 函数都可以 from hipop.server.runtime import tick, set_progress
# 然后在循环里 tick(f"已处理 {i}/{n}")。无 HIPOP_TASK_ID env 时自动 no-op。
# spawn_task 注入 env var 给 worker subprocess (见 worker.py)。

def tick(message: Optional[str] = None) -> None:
    """env-aware heartbeat。无 HIPOP_TASK_ID 时 no-op。失败不抛错（不影响主流程）。"""
    tid = os.environ.get("HIPOP_TASK_ID")
    if not tid:
        return
    try:
        heartbeat(tid, message)
        if message:
            # 也写一行到 progress.json 的 message 字段，前端可读
            save_progress(tid, {"message": message})
    except Exception:
        pass


def set_progress(payload: dict) -> None:
    """env-aware progress.json 更新。无 env 时 no-op。"""
    tid = os.environ.get("HIPOP_TASK_ID")
    if not tid:
        return
    try:
        save_progress(tid, payload)
    except Exception:
        pass


def find_orphan_tasks() -> list:
    """watchdog 用 — 找 last_heartbeat 超时的 running task。
    必须跨 tenant 扫（watchdog 不属于任何 tenant），用 owner bypass RLS。
    """
    if not _data.is_postgres():
        # SQLite 无 RLS，直接走 _fetch
        rows = _data._fetch(
            "SELECT task_id, tenant_id, workflow, worker_pid, "
            "       CAST((julianday('now') - julianday(last_heartbeat)) * 86400 AS INT) AS stale_sec "
            "FROM tasks WHERE state='running' AND last_heartbeat IS NOT NULL "
            f"AND (julianday('now') - julianday(last_heartbeat)) * 86400 > {HEARTBEAT_TIMEOUT_SEC}",
            (),
        )
        return rows
    # PG: 用 owner bypass RLS，跨所有 tenant 扫
    import psycopg2
    from psycopg2.extras import RealDictCursor
    raw = psycopg2.connect(os.environ.get("DB_URL"), cursor_factory=RealDictCursor)
    raw.autocommit = True
    try:
        with raw.cursor() as cur:
            cur.execute("ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY")
            cur.execute("SET app.current_tenant = '0'")
            cur.execute(
                "SELECT task_id, tenant_id, workflow, worker_pid, "
                "       EXTRACT(EPOCH FROM (NOW() - last_heartbeat))::INT AS stale_sec "
                "FROM tasks WHERE state='running' "
                "AND last_heartbeat IS NOT NULL "
                f"AND NOW() - last_heartbeat > INTERVAL '{HEARTBEAT_TIMEOUT_SEC} seconds'"
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")
        return rows
    finally:
        raw.close()

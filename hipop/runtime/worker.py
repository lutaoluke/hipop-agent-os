"""Managed Agents worker — 独立进程跑长任务。

启动：python3 -m hipop.runtime.worker <task_id>

跑法（对照 Anthropic Long-Running Harness 的 Initializer + Coding Agent 范式）：
  1. 读 ~/hipop/tasks/<task_id>/spec.json — 拿 workflow / tenant / actor
  2. 读 ~/hipop/tasks/<task_id>/progress.json（如有）— 续跑点
  3. 跑 workflow（找 WORKFLOW_REGISTRY 对应函数）
     - 每 chunk 完成后写 progress.json（resume point）
     - 每 chunk 完成后 heartbeat（UPDATE tasks.last_heartbeat）
  4. 完成时 UPDATE tasks state=done + result_summary
  5. 出错时 UPDATE tasks state=error + log
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

# 让 worker 能 import hipop.*
HERE = Path(__file__).resolve()
PROJECT_ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hipop.server import data as _data
from hipop.server import runtime as _runtime


def _load_spec(task_id: str) -> dict:
    paths = _runtime._task_paths(task_id)
    with open(paths["spec"]) as f:
        return json.load(f)


def _load_progress(task_id: str) -> dict:
    paths = _runtime._task_paths(task_id)
    if os.path.exists(paths["progress"]):
        try:
            with open(paths["progress"]) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_progress(task_id: str, progress: dict) -> None:
    paths = _runtime._task_paths(task_id)
    tmp = paths["progress"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
    os.replace(tmp, paths["progress"])  # 原子写


def _finish(task_id: str, state: str, summary: str = "") -> None:
    """worker 完成时调 — UPDATE tasks.state + finished_at + result_summary。"""
    _data.set_current_tenant_to_task(task_id)
    with _data.conn() as c:
        c.execute(
            "UPDATE tasks SET state=?, finished_at=NOW(), worker_pid=NULL, "
            "result_summary=? WHERE task_id=?",
            (state, summary, task_id),
        )
        c.commit()


def _heartbeat(task_id: str) -> None:
    """每 chunk 调一次"""
    _runtime.heartbeat(task_id)


def main(task_id: str) -> int:
    print(f"[worker {os.getpid()}] starting task={task_id}", flush=True)

    try:
        spec = _load_spec(task_id)
    except Exception as e:
        print(f"[worker] FATAL load spec failed: {e}", flush=True)
        _finish(task_id, "error", f"spec load failed: {e}")
        return 1

    workflow = spec["workflow"]
    tenant_id = spec["tenant_id"]
    actor = spec.get("actor") or {}
    progress = _load_progress(task_id)
    is_resume = bool(progress)

    if is_resume:
        print(f"[worker] resuming from chunk_idx={progress.get('chunk_idx', 0)}", flush=True)
    else:
        progress = {"chunk_idx": 0, "done_items": [], "failures": [], "started_at": time.time()}

    # 设 tenant context（关键 — workflow 内部 SQL 全靠这个走 RLS）
    _data.set_current_tenant(tenant_id)

    # 调度到具体 workflow runner
    try:
        from hipop.runtime import workflow_runners
        runner = workflow_runners.get_runner(workflow)
        if not runner:
            _finish(task_id, "error", f"no runner for workflow={workflow}")
            return 2

        # 跑 — runner 内部周期性调 _heartbeat + _save_progress
        result = runner(
            task_id=task_id,
            tenant_id=tenant_id,
            actor=actor,
            spec=spec.get("spec") or {},
            progress=progress,
            heartbeat=lambda: _heartbeat(task_id),
            save_progress=lambda p: _save_progress(task_id, p),
        )
        summary = (result or {}).get("summary") or "done"
        _finish(task_id, "done", summary)
        print(f"[worker] DONE {summary}", flush=True)
        return 0

    except Exception:
        err = traceback.format_exc()
        print(f"[worker] CRASHED:\n{err}", flush=True)
        _finish(task_id, "error", err[-500:])
        return 3


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m hipop.runtime.worker <task_id>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))

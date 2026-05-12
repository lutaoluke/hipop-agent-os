"""每日自动刷新调度器。

为每个 active tenant 跑 refresh_all_v2，留痕 actor.source='cron'。
默认 02:00。可通过 env 调：
  DAILY_REFRESH_HOUR / DAILY_REFRESH_MINUTE
  DISABLE_DAILY_REFRESH=1 关掉
"""
from __future__ import annotations
import os
import traceback
import logging
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("hipop.scheduler")

_scheduler: BackgroundScheduler | None = None


def _list_active_tenants() -> list[dict]:
    """读所有 tenants（tenants 表不开 RLS）。"""
    from . import data as _data
    rows = _data._fetch("SELECT id, name FROM tenants ORDER BY id")
    return rows or []


def _run_daily_refresh():
    """每日入口：遍历所有 active tenant，串行跑 refresh_all_v2。"""
    from . import api as _api
    from . import data as _data
    tenants = _list_active_tenants()
    logger.info("[cron] daily_refresh start: %d tenants", len(tenants))
    for t in tenants:
        tid = t["id"]
        try:
            task_id = uuid4().hex[:8]
            actor = {
                "user_id": None,
                "email": "cron@system",
                "role": "system",
                "source": "cron",
            }
            _data.set_current_tenant(tid)
            _api._run_workflow(task_id, "refresh_all_v2", tid, actor)
            logger.info("[cron] tenant=%s task=%s done", tid, task_id)
        except Exception:
            logger.error("[cron] tenant=%s failed:\n%s", tid, traceback.format_exc())
    logger.info("[cron] daily_refresh finished")


def start():
    """启动后台 scheduler。idempotent — 已起就不重起。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    hour = int(os.environ.get("DAILY_REFRESH_HOUR", "2"))
    minute = int(os.environ.get("DAILY_REFRESH_MINUTE", "0"))
    _scheduler = BackgroundScheduler(timezone=os.environ.get("TZ", "Asia/Shanghai"))
    _scheduler.add_job(
        _run_daily_refresh,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="daily_refresh_all",
        replace_existing=True,
        misfire_grace_time=3600,  # 错过 1h 内还能补跑
    )
    _scheduler.start()
    logger.info("[scheduler] daily_refresh_all scheduled at %02d:%02d (TZ=%s)",
                hour, minute, _scheduler.timezone)
    print(f"[scheduler] daily refresh scheduled at {hour:02d}:{minute:02d} ({_scheduler.timezone})")
    return _scheduler


def run_now():
    """手动触发一次（调试用）。"""
    _run_daily_refresh()

"""Observability — Phase 0.4 Harness 七层补全（2026-05-21）

按 Anthropic Demystifying Evals + Claude Code Best Practices：
  "Swiss Cheese 多层观测" — pre-launch automated + production monitoring + spot review

提供统一接口：
  track_event(name, **labels)       记录业务事件（"task_completed" 等）
  track_error(exc, context=...)     记录异常 + 上下文
  log_metric(name, value, **tags)   计数器 / gauge

后端动态切换：
  SENTRY_DSN env 设置  → sentry-sdk 上报（生产用）
  FEISHU_WEBHOOK 设置  → 高严重度推飞书 manager（critical 告警）
  都没设 → 落本地 log /tmp/hipop_observability.log

设计为 fail-open：observability 自身崩了**绝不**抛异常打断业务流。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Optional


_LOG_PATH = os.environ.get("HIPOP_OBS_LOG", "/tmp/hipop_observability.log")
_SENTRY_DSN = os.environ.get("SENTRY_DSN")
_FEISHU_WEBHOOK = os.environ.get("FEISHU_OBS_WEBHOOK")
_SENTRY_INITIALIZED = False


def _init_sentry_if_configured():
    global _SENTRY_INITIALIZED
    if _SENTRY_INITIALIZED or not _SENTRY_DSN:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=os.environ.get("HIPOP_ENV", "dev"),
        )
        _SENTRY_INITIALIZED = True
        _local_log("sentry_initialized", dsn_prefix=_SENTRY_DSN[:30])
    except ImportError:
        _local_log("sentry_skip", reason="sentry-sdk not installed")


def _local_log(event: str, level: str = "info", **labels) -> None:
    """fallback log when sentry not configured."""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "event": event,
            **labels,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with open(_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 永不抛


def track_event(name: str, **labels) -> None:
    """业务事件（task started/done/failed/verify_failed 等）"""
    _init_sentry_if_configured()
    if _SENTRY_INITIALIZED:
        try:
            import sentry_sdk
            sentry_sdk.add_breadcrumb(category="event", message=name, data=labels)
        except Exception:
            pass
    _local_log(name, **labels)


def track_error(exc: BaseException, context: Optional[dict] = None,
                severity: str = "error") -> None:
    """异常上报 — severity in [debug,info,warning,error,critical]"""
    _init_sentry_if_configured()
    if _SENTRY_INITIALIZED:
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                if context:
                    scope.set_context("hipop", context)
                sentry_sdk.capture_exception(exc)
        except Exception:
            pass
    _local_log(
        "error",
        level=severity,
        exc_type=type(exc).__name__,
        exc_msg=str(exc)[:300],
        context=context or {},
        tb=traceback.format_exc()[-500:],
    )
    # critical 推飞书（Phase 1 真接，目前 stub 只 log）
    if severity == "critical" and _FEISHU_WEBHOOK:
        _send_feishu_alert(f"⚠️ critical error: {type(exc).__name__}: {str(exc)[:200]}", context)


def log_metric(name: str, value: Any, **tags) -> None:
    """数值 metric — 简单 log。Sentry 集成是 measure API."""
    _init_sentry_if_configured()
    if _SENTRY_INITIALIZED:
        try:
            import sentry_sdk
            # Sentry metrics API
            sentry_sdk.metrics.distribution(name, value, tags=tags)
        except Exception:
            pass
    _local_log("metric", metric_name=name, value=value, **tags)


def _send_feishu_alert(text: str, context: Optional[dict] = None) -> None:
    """飞书 webhook 推送 critical 告警（stub — Phase 1 真接时改）"""
    if not _FEISHU_WEBHOOK:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "msg_type": "text",
            "content": {"text": f"{text}\n\nctx: {context or {}}"[:1500]},
        }).encode("utf-8")
        req = urllib.request.Request(_FEISHU_WEBHOOK, data=payload,
                                       headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        _local_log("feishu_alert_failed", text=text[:100])


# ─── 高频 hook helpers（让调用方一行调用） ─────────────────

def task_lifecycle(event: str, task_id: str, workflow: str,
                    tenant_id: int, **extra) -> None:
    """常用快捷：task spawn/done/failed/wake 等 lifecycle event."""
    track_event(
        f"task.{event}",
        task_id=task_id, workflow=workflow, tenant_id=tenant_id, **extra,
    )


def verify_failed(task_id: str, workflow: str, tenant_id: int,
                    verdict: str, evidence: dict) -> None:
    """verify contract 不通过 → warning（不到 critical），但写入观测."""
    track_event(
        "verify.failed",
        task_id=task_id, workflow=workflow, tenant_id=tenant_id,
        verdict=verdict, evidence=evidence,
    )
    _local_log("verify_failed", level="warning",
               task_id=task_id, workflow=workflow, verdict=verdict)

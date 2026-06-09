"""WS-131 freshness gate: live-first, cache consent, fail-closed.

This module is intentionally pure and deterministic. Answer/rendering paths call
it before exposing any business number; prompt text is not the source of truth.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional


MAX_CACHE_AGE_DAYS = 3


def _coerce_datetime(value: Any) -> Optional[_dt.datetime]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text[:19], text[:10]):
        try:
            parsed = _dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            return parsed
        except Exception:
            continue
    return None


def _days_old(fetched_at: Any, now: Any = None) -> Optional[int]:
    ts = _coerce_datetime(fetched_at)
    if ts is None:
        return None
    ref = _coerce_datetime(now) if now is not None else _dt.datetime.now()
    if ref is None:
        ref = _dt.datetime.now()
    return max(0, (ref.date() - ts.date()).days)


def operator_consented_to_cache(text: str) -> bool:
    """Best-effort explicit consent detector for deterministic chat routes."""
    q = (text or "").strip()
    if not q or "缓存" not in q:
        return False
    if operator_rejected_cache(text):
        return False
    positive = ("同意", "可以", "用缓存", "使用缓存", "接受", "确认", "允许")
    return any(w in q for w in positive)


def operator_rejected_cache(text: str) -> bool:
    """Best-effort explicit cache refusal detector for deterministic routes."""
    q = (text or "").strip()
    if not q or "缓存" not in q:
        return False
    negative = ("不同意", "不要", "不用", "不使用", "别用", "先别", "拒绝")
    return any(w in q for w in negative)


def decide_freshness(
    *,
    live_ok: bool,
    live_source: Any = None,
    live_fetched_at: Any = None,
    live_error: str = "",
    cache_available: bool = False,
    cache_fetched_at: Any = None,
    operator_cache_consent: bool = False,
    operator_cache_rejected: bool = False,
    cache_requires_consent: bool = True,
    now: Any = None,
    max_cache_age_days: int = MAX_CACHE_AGE_DAYS,
    subject: str = "数据",
) -> Dict[str, Any]:
    """Return a freshness decision dict.

    `can_output_number` is the only switch renderers should trust. A cache inside
    the age window is still blocked until explicit consent when
    `cache_requires_consent=True`.
    """
    if live_ok:
        if not str(live_source or "").strip():
            return {
                "status": "blocked",
                "reason": "live_missing_source",
                "can_output_number": False,
                "message": f"{subject} 已取到实时结果，但缺少来源，不能出数。",
            }
        if not str(live_fetched_at or "").strip():
            return {
                "status": "blocked",
                "reason": "live_missing_timestamp",
                "can_output_number": False,
                "message": f"{subject} 已取到实时结果，但缺少更新时间，不能出数。",
            }
        return {
            "status": "live",
            "reason": None,
            "can_output_number": True,
            "source": str(live_source),
            "fetched_at": str(live_fetched_at),
            "message": f"{subject} 使用实时数据。",
        }

    if not cache_available:
        return {
            "status": "blocked",
            "reason": "no_cache",
            "can_output_number": False,
            "live_error": live_error or None,
            "message": f"{subject} 实时取数失败，且没有可用缓存，不能出数。",
        }

    age_days = _days_old(cache_fetched_at, now=now)
    if age_days is None:
        return {
            "status": "blocked",
            "reason": "cache_missing_timestamp",
            "can_output_number": False,
            "live_error": live_error or None,
            "message": f"{subject} 实时取数失败，缓存没有缓存时间，不能使用缓存数字。",
        }

    if age_days > max_cache_age_days:
        return {
            "status": "blocked",
            "reason": "cache_too_old",
            "can_output_number": False,
            "cache_age_days": age_days,
            "cache_fetched_at": str(cache_fetched_at),
            "live_error": live_error or None,
            "message": (
                f"{subject} 实时取数失败，缓存更新时间为 {cache_fetched_at}"
                f"（{age_days} 天前），超过 {max_cache_age_days} 天，不能使用缓存数字。"
            ),
        }

    base = {
        "cache_age_days": age_days,
        "cache_fetched_at": str(cache_fetched_at),
        "max_cache_age_days": max_cache_age_days,
        "live_error": live_error or None,
    }
    if cache_requires_consent and not operator_cache_consent:
        if operator_cache_rejected:
            return {
                **base,
                "status": "blocked",
                "reason": "cache_rejected",
                "can_output_number": False,
                "message": f"{subject} 实时取数失败，运营不同意使用缓存，不能使用缓存数字。",
            }
        return {
            **base,
            "status": "ask_cache_consent",
            "reason": "cache_consent_required",
            "can_output_number": False,
            "message": (
                f"{subject} 实时取数失败；我有 {age_days} 天内的缓存"
                f"（更新时间：{cache_fetched_at}）。是否使用缓存？"
            ),
        }

    return {
        **base,
        "status": "cache_allowed",
        "reason": None,
        "can_output_number": True,
        "source": "cache",
        "fetched_at": str(cache_fetched_at),
        "message": f"{subject} 使用已确认的缓存数据。",
    }


def render_freshness_suffix(decision: Dict[str, Any]) -> str:
    """Render source/update-time suffix for allowed decisions."""
    if not isinstance(decision, dict) or not decision.get("can_output_number"):
        return ""
    status = decision.get("status")
    if status == "live":
        return f"（来源：{decision.get('source')}｜更新时间：{decision.get('fetched_at')}）"
    if status == "cache_allowed":
        return (
            f"（来源：带时间缓存｜更新时间：{decision.get('fetched_at')}"
            f"｜缓存年龄：{decision.get('cache_age_days')} 天）"
        )
    return ""

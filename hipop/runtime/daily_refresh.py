"""Daily refresh schedule and cutoff contract.

The server scheduler, refresh_all runner, and verifier share this module so the
12:00 / yesterday rule has one deterministic implementation.
"""
from __future__ import annotations

import datetime as _dt
import os
import re

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python builds without tzdata fallback.
    ZoneInfo = None  # type: ignore


DEFAULT_DAILY_REFRESH_HOUR = 12
DEFAULT_DAILY_REFRESH_MINUTE = 0
DEFAULT_TIMEZONE = "Asia/Shanghai"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _timezone(tz_name: str | None = None):
    name = tz_name or os.environ.get("TZ") or DEFAULT_TIMEZONE
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _dt.timezone(_dt.timedelta(hours=8))


def local_now(now=None, tz_name: str | None = None) -> _dt.datetime:
    tz = _timezone(tz_name)
    if now is None:
        return _dt.datetime.now(tz)
    if isinstance(now, _dt.datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)
    if isinstance(now, _dt.date):
        return _dt.datetime.combine(now, _dt.time.min, tzinfo=tz)
    raise TypeError(f"unsupported now type: {type(now).__name__}")


def today_date(now=None, tz_name: str | None = None) -> str:
    return local_now(now=now, tz_name=tz_name).date().isoformat()


def business_date_yesterday(now=None, tz_name: str | None = None) -> str:
    return (local_now(now=now, tz_name=tz_name).date()
            - _dt.timedelta(days=1)).isoformat()


def is_valid_business_date(value) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if len(s) >= 10 and s[10:11] in (" ", "T"):
        s = s[:10]
    if not _DATE_RE.match(s):
        return False
    try:
        _dt.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def normalize_business_date(value) -> str:
    if value is None:
        raise ValueError("business_date/as_of_date 必填，不能回落到 today")
    s = str(value).strip()
    if len(s) >= 10 and s[10:11] in (" ", "T"):
        s = s[:10]
    if not is_valid_business_date(s):
        raise ValueError(f"business_date/as_of_date 非法：{value!r}，应为真实 YYYY-MM-DD")
    return s


def validate_business_date_cutoff(value, now=None, tz_name: str | None = None) -> str:
    """Return normalized business date and reject today/future incomplete dates."""
    s = normalize_business_date(value)
    biz = _dt.datetime.strptime(s, "%Y-%m-%d").date()
    today = local_now(now=now, tz_name=tz_name).date()
    if biz >= today:
        raise ValueError(
            f"business_date/as_of_date={s} 必须早于今天 {today.isoformat()}；"
            "今天数据未完整，不能当完整事实"
        )
    return s


def build_daily_refresh_spec(now=None, tz_name: str | None = None) -> dict:
    business_date = business_date_yesterday(now=now, tz_name=tz_name)
    return {"business_date": business_date, "as_of_date": business_date}


def configured_hour(env: dict | None = None) -> int:
    source = env if env is not None else os.environ
    return int(source.get("DAILY_REFRESH_HOUR", DEFAULT_DAILY_REFRESH_HOUR))


def configured_minute(env: dict | None = None) -> int:
    source = env if env is not None else os.environ
    return int(source.get("DAILY_REFRESH_MINUTE", DEFAULT_DAILY_REFRESH_MINUTE))

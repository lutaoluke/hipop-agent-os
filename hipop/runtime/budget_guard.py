"""WS-176 deterministic Anthropic budget guard.

The guard is intentionally pure: callers pass usage/config in, and the result is
only a decision + rollup. Routing mutation belongs to a future applier, not to
this dry-run mechanism.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


CST = timezone(timedelta(hours=8))
UTC = timezone.utc

DEFAULT_CONFIG: dict[str, Any] = {
    "r1_yellow_opus_output_daily": 2_000_000,
    "r1_yellow_opus_projected_daily": 3_000_000,
    "r2_freeze_opus_output_daily": 3_000_000,
    "r2_freeze_opus_projected_daily": 4_000_000,
    "r2_unfreeze_projected_daily": 2_500_000,
    "r3_monthly_warn_ratio": 0.70,
    "r4_monthly_freeze_ratio": 0.85,
    "r4_monthly_break_glass_ratio": 0.95,
    "r6_opus_share": 0.60,
    "r6_opus_output_daily": 1_500_000,
    "standard_default_pools": ["sonnet", "gpt5.5"],
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_cst(dt: datetime) -> str:
    return dt.astimezone(CST).replace(microsecond=0).isoformat()


def _is_opus(record: dict[str, Any]) -> bool:
    family = str(record.get("model_family") or record.get("family") or "").lower()
    model = str(record.get("model") or record.get("model_name") or "").lower()
    return family == "opus" or "opus" in model


def _record_tier(record: dict[str, Any]) -> tuple[str, bool]:
    explicit = record.get("tier") or record.get("task_tier")
    if explicit:
        return str(explicit), bool(record.get("aggregate") or record.get("runtime_aggregate"))
    return "aggregate", True


def _record_agent(record: dict[str, Any], aggregate: bool) -> str:
    value = record.get("agent_id") or record.get("agent") or record.get("agent_name")
    if value:
        return str(value)
    return "runtime_aggregate" if aggregate else "unknown_agent"


def load_budget_guard_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Load guard thresholds from config/hipop.json and apply explicit overrides.

    `monthly_cap_output_tokens` is deliberately not defaulted. If it is absent,
    the evaluator reports `monthly_cap_unset` and still applies R1/R2.
    """
    config = dict(DEFAULT_CONFIG)
    path = _repo_root() / "hipop" / "config" / "hipop.json"
    try:
        with path.open() as f:
            raw = json.load(f)
        section = raw.get("budget_guard") or {}
        if isinstance(section, dict):
            config.update(section)
    except FileNotFoundError:
        pass
    if overrides:
        config.update(overrides)
    return config


def _load_usage_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if isinstance(spec.get("usage"), dict):
        return spec["usage"]
    usage_file = spec.get("usage_file") or os.environ.get("HIPOP_ANTHROPIC_USAGE_FILE")
    if usage_file:
        path = Path(str(usage_file))
        if not path.is_absolute():
            path = _repo_root() / path
        with path.open() as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
        return {"records": loaded}
    return {"records": []}


def _rollup_usage(usage: dict[str, Any]) -> dict[str, Any]:
    records = usage.get("records") or usage.get("usage_records") or []
    if not isinstance(records, list):
        records = []

    totals = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_tokens": 0.0,
        "cache_write_tokens": 0.0,
        "cache_tokens": 0.0,
        "opus_output_tokens": 0.0,
        "standard_tier_opus_output_tokens": 0.0,
    }
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_tier: dict[str, dict[str, float]] = {}
    runtime_aggregate = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_tokens": 0.0,
        "cache_write_tokens": 0.0,
        "cache_tokens": 0.0,
        "opus_output_tokens": 0.0,
    }

    last_1h_from_records = 0.0
    for record in records:
        if not isinstance(record, dict):
            continue
        tier, aggregate = _record_tier(record)
        agent = _record_agent(record, aggregate)
        model = str(record.get("model") or record.get("model_name") or "unknown")
        input_tokens = _num(record.get("input_tokens") or record.get("input"))
        output_tokens = _num(record.get("output_tokens") or record.get("output"))
        cache_read = _num(record.get("cache_read_tokens") or record.get("cache_input_tokens"))
        cache_write = _num(record.get("cache_write_tokens") or record.get("cache_creation_tokens"))
        cache_tokens = cache_read + cache_write
        opus_output = output_tokens if _is_opus(record) else 0.0

        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["cache_read_tokens"] += cache_read
        totals["cache_write_tokens"] += cache_write
        totals["cache_tokens"] += cache_tokens
        totals["opus_output_tokens"] += opus_output
        if tier == "standard" and not aggregate:
            totals["standard_tier_opus_output_tokens"] += opus_output

        key = (agent, tier)
        bucket = by_key.setdefault(key, {
            "agent_id": agent,
            "tier": tier,
            "models": set(),
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "cache_read_tokens": 0.0,
            "cache_write_tokens": 0.0,
            "cache_tokens": 0.0,
            "opus_output_tokens": 0.0,
            "runtime_aggregate": aggregate,
        })
        bucket["models"].add(model)
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += cache_read
        bucket["cache_write_tokens"] += cache_write
        bucket["cache_tokens"] += cache_tokens
        bucket["opus_output_tokens"] += opus_output

        tier_bucket = by_tier.setdefault(tier, {
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "cache_read_tokens": 0.0,
            "cache_write_tokens": 0.0,
            "cache_tokens": 0.0,
            "opus_output_tokens": 0.0,
        })
        tier_bucket["input_tokens"] += input_tokens
        tier_bucket["output_tokens"] += output_tokens
        tier_bucket["cache_read_tokens"] += cache_read
        tier_bucket["cache_write_tokens"] += cache_write
        tier_bucket["cache_tokens"] += cache_tokens
        tier_bucket["opus_output_tokens"] += opus_output

        if aggregate:
            runtime_aggregate["input_tokens"] += input_tokens
            runtime_aggregate["output_tokens"] += output_tokens
            runtime_aggregate["cache_read_tokens"] += cache_read
            runtime_aggregate["cache_write_tokens"] += cache_write
            runtime_aggregate["cache_tokens"] += cache_tokens
            runtime_aggregate["opus_output_tokens"] += opus_output

        if str(record.get("window") or "").lower() in {"last_1h", "1h", "last_hour"}:
            last_1h_from_records += opus_output

    explicit_opus = usage.get("opus_output_today")
    if explicit_opus is None:
        explicit_opus = usage.get("opus_output_tokens_today")
    if explicit_opus is not None:
        totals["opus_output_tokens"] = _num(explicit_opus)

    explicit_output = usage.get("total_output_tokens_today")
    if explicit_output is not None:
        totals["output_tokens"] = _num(explicit_output)

    last_1h = usage.get("opus_output_last_1h")
    if last_1h is None:
        last_1h = usage.get("last_1h_opus_output")
    if last_1h is None:
        last_1h = last_1h_from_records
    last_1h = _num(last_1h)
    projected = last_1h * 24
    total_output = totals["output_tokens"]
    opus_output = totals["opus_output_tokens"]
    opus_share = (opus_output / total_output) if total_output > 0 else 0.0

    by_agent_tier = []
    for bucket in by_key.values():
        item = dict(bucket)
        item["models"] = sorted(item["models"])
        by_agent_tier.append(item)
    by_agent_tier.sort(key=lambda b: (b["tier"], b["agent_id"]))

    return {
        "totals": totals,
        "opus_output_today": opus_output,
        "opus_output_last_1h": last_1h,
        "opus_output_projected_today": projected,
        "opus_share": opus_share,
        "by_agent_tier": by_agent_tier,
        "by_tier": by_tier,
        "runtime_aggregate": runtime_aggregate,
        "monthly_output_tokens": _num(
            usage.get("monthly_output_tokens")
            if usage.get("monthly_output_tokens") is not None
            else usage.get("monthly_output")
        ),
        "warnings": [],
    }


def _incident(
    *,
    rule: str,
    action: str,
    now: datetime,
    metrics: dict[str, Any],
    recovery_condition: str,
    scope: str,
) -> dict[str, Any]:
    seed = json.dumps({
        "rule": rule,
        "action": action,
        "time": now.replace(microsecond=0).isoformat(),
        "metrics": metrics,
        "scope": scope,
    }, sort_keys=True, ensure_ascii=True)
    incident_id = "bg-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return {
        "incident_id": incident_id,
        "detection_time_cst": _format_cst(now),
        "rule": rule,
        "action": action,
        "scope": scope,
        "metrics": metrics,
        "recovery_condition": recovery_condition,
    }


def _next_cst_day(dt: datetime) -> str:
    cst = dt.astimezone(CST)
    next_day = (cst + timedelta(days=1)).date()
    return f"{next_day.isoformat()}T00:00:00+08:00"


def _evaluate_daily(
    rollup: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
    previous_state: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], bool, bool]:
    opus_today = rollup["opus_output_today"]
    projected = rollup["opus_output_projected_today"]
    triggered: list[str] = []
    incidents: list[dict[str, Any]] = []
    frozen = False
    yellow = False
    status = "clear"
    recovery_conditions: list[str] = []

    r2_hit = (
        opus_today >= _num(config["r2_freeze_opus_output_daily"])
        or projected >= _num(config["r2_freeze_opus_projected_daily"])
    )
    r1_hit = (
        opus_today >= _num(config["r1_yellow_opus_output_daily"])
        or projected >= _num(config["r1_yellow_opus_projected_daily"])
    )

    if r2_hit:
        frozen = True
        status = "frozen"
        triggered.append("R2_DAILY_FREEZE")
        recovery = (
            "hold until next CST natural day; then require 2 consecutive "
            f"projected rounds < {int(_num(config['r2_unfreeze_projected_daily']))}"
        )
        recovery_conditions.append(recovery)
        incidents.append(_incident(
            rule="R2_DAILY_FREEZE",
            action="freeze",
            now=now,
            metrics={
                "opus_output_today": opus_today,
                "opus_output_projected_today": projected,
                "opus_share": rollup["opus_share"],
            },
            recovery_condition=recovery,
            scope="standard_to_opus",
        ))
    elif previous_state.get("standard_to_opus_frozen"):
        freeze_started_raw = previous_state.get("freeze_started_at") or previous_state.get("frozen_at")
        freeze_started = _parse_dt(freeze_started_raw) if freeze_started_raw else now
        next_day_reached = now.astimezone(CST).date() > freeze_started.astimezone(CST).date()
        projected_low = projected < _num(config["r2_unfreeze_projected_daily"])
        rounds = int(previous_state.get("low_projected_rounds", 0))
        rounds = rounds + 1 if projected_low else 0
        if next_day_reached and rounds >= 2:
            status = "unfrozen"
            triggered.append("R2_DAILY_UNFREEZE")
            incidents.append(_incident(
                rule="R2_DAILY_UNFREEZE",
                action="unfreeze",
                now=now,
                metrics={
                    "opus_output_today": opus_today,
                    "opus_output_projected_today": projected,
                    "low_projected_rounds": rounds,
                },
                recovery_condition="standard-to-Opus thawed; keep R1/R2 monitoring active",
                scope="standard_to_opus",
            ))
        else:
            frozen = True
            status = "frozen_pending_recovery"
            triggered.append("R2_DAILY_RECOVERY_HOLD")
            recovery_conditions.append(
                "still waiting for next CST day and 2 consecutive low projected rounds"
            )
    elif r1_hit:
        yellow = True
        status = "yellow"
        triggered.append("R1_DAILY_YELLOW")

    return ({
        "status": status,
        "opus_output_today": opus_today,
        "opus_output_last_1h": rollup["opus_output_last_1h"],
        "opus_output_projected_today": projected,
        "freeze_until_cst": _next_cst_day(now) if frozen and "R2_DAILY_FREEZE" in triggered else None,
        "recovery_conditions": recovery_conditions,
    }, triggered, incidents, frozen, yellow)


def _evaluate_monthly(
    rollup: dict[str, Any],
    config: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], bool]:
    cap = config.get("monthly_cap_output_tokens") or config.get("anthropic_monthly_cap_output_tokens")
    used = rollup["monthly_output_tokens"]
    triggered: list[str] = []
    incidents: list[dict[str, Any]] = []
    high_tier_frozen = False

    if cap in (None, "", 0):
        rollup["warnings"].append("monthly_cap_unset")
        return ({
            "status": "monthly_cap_unset",
            "monthly_cap_output_tokens": None,
            "monthly_output_tokens": used,
            "usage_ratio": None,
            "action": "report_monthly_cap_unset",
            "recovery_condition": "configure monthly_cap_output_tokens",
        }, triggered, incidents, high_tier_frozen)

    cap_num = _num(cap)
    ratio = used / cap_num if cap_num > 0 else 0.0
    status = "clear"
    action = "none"
    recovery = "new billing period, raised cap, or human-confirmed remaining capacity"
    if ratio >= _num(config["r4_monthly_break_glass_ratio"]):
        triggered.append("R4_MONTHLY_95_BREAK_GLASS")
        status = "break_glass"
        action = "p0_p1_break_glass_only"
        high_tier_frozen = True
        incidents.append(_incident(
            rule="R4_MONTHLY_95_BREAK_GLASS",
            action="break_glass",
            now=now,
            metrics={
                "monthly_output_tokens": used,
                "monthly_cap_output_tokens": cap_num,
                "remaining_output_tokens": cap_num - used,
                "usage_ratio": ratio,
            },
            recovery_condition=recovery,
            scope="anthropic_high_tier",
        ))
    elif ratio >= _num(config["r4_monthly_freeze_ratio"]):
        triggered.append("R4_MONTHLY_85_FREEZE")
        status = "frozen"
        action = "freeze_non_blocking_standard_high_tier"
        high_tier_frozen = True
        incidents.append(_incident(
            rule="R4_MONTHLY_85_FREEZE",
            action="freeze",
            now=now,
            metrics={
                "monthly_output_tokens": used,
                "monthly_cap_output_tokens": cap_num,
                "remaining_output_tokens": cap_num - used,
                "usage_ratio": ratio,
            },
            recovery_condition=recovery,
            scope="anthropic_high_tier",
        ))
    elif ratio >= _num(config["r3_monthly_warn_ratio"]):
        triggered.append("R3_MONTHLY_70_WARNING")
        status = "warning"
        action = "warn_monthly_budget"

    return ({
        "status": status,
        "monthly_cap_output_tokens": cap_num,
        "monthly_output_tokens": used,
        "usage_ratio": ratio,
        "action": action,
        "recovery_condition": recovery,
    }, triggered, incidents, high_tier_frozen)


def _evaluate_session(
    usage: dict[str, Any],
    now: datetime,
    probe_fn: Optional[Callable[[], bool]],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], bool]:
    session = usage.get("session") or {}
    if not isinstance(session, dict):
        session = {}
    limited = bool(session.get("limited") or session.get("limit_reached"))
    reset_at_raw = session.get("reset_at")
    triggered: list[str] = []
    incidents: list[dict[str, Any]] = []
    frozen = False

    if not limited:
        return ({
            "status": "clear",
            "limited": False,
            "reset_at": reset_at_raw,
            "probe_attempted": False,
            "probe_ok": None,
        }, triggered, incidents, frozen)

    reset_at = _parse_dt(reset_at_raw) if reset_at_raw else None
    probe_attempted = bool(reset_at and now >= reset_at)
    probe_ok = session.get("probe_ok")
    if probe_attempted and probe_ok is None and probe_fn is not None:
        probe_ok = bool(probe_fn())

    if reset_at and now < reset_at:
        status = "wait_for_reset"
        recovery = f"wait until reset_at={_format_cst(reset_at)}; then probe before unfreezing"
        frozen = True
        triggered.append("R5_SESSION_LIMIT")
    elif probe_attempted and probe_ok is True:
        status = "probed_ok"
        recovery = "probe succeeded"
    elif probe_attempted and probe_ok is False:
        status = "probe_failed"
        recovery = "retry probe after reset or route away from session-limited pool"
        frozen = True
        triggered.append("R5_SESSION_LIMIT")
    else:
        status = "probe_required"
        recovery = "reset_at passed; active probe must succeed before unfreezing"
        frozen = True
        triggered.append("R5_SESSION_LIMIT")

    if frozen:
        incidents.append(_incident(
            rule="R5_SESSION_LIMIT",
            action="freeze",
            now=now,
            metrics={"reset_at": reset_at_raw, "probe_attempted": probe_attempted, "probe_ok": probe_ok},
            recovery_condition=recovery,
            scope="anthropic_session",
        ))

    return ({
        "status": status,
        "limited": True,
        "reset_at": reset_at_raw,
        "probe_attempted": probe_attempted,
        "probe_ok": probe_ok,
        "recovery_condition": recovery,
    }, triggered, incidents, frozen)


def evaluate_budget_guard(
    usage: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
    now: Any = None,
    previous_state: Optional[dict[str, Any]] = None,
    probe_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Return the budget guard decision for one dry-run cycle."""
    usage = usage or {}
    previous_state = previous_state or {}
    now_dt = _parse_dt(now)
    cfg = load_budget_guard_config(config)
    rollup = _rollup_usage(usage)

    daily, daily_rules, daily_incidents, daily_frozen, daily_yellow = _evaluate_daily(
        rollup, cfg, now_dt, previous_state
    )
    monthly, monthly_rules, monthly_incidents, monthly_frozen = _evaluate_monthly(rollup, cfg, now_dt)
    session, session_rules, session_incidents, session_frozen = _evaluate_session(usage, now_dt, probe_fn)

    triggered_rules = daily_rules + monthly_rules + session_rules
    incidents = daily_incidents + monthly_incidents + session_incidents

    concentration = {
        "status": "clear",
        "opus_share": rollup["opus_share"],
        "opus_output_today": rollup["opus_output_today"],
        "action": "none",
    }
    if (
        rollup["opus_share"] >= _num(cfg["r6_opus_share"])
        and rollup["opus_output_today"] >= _num(cfg["r6_opus_output_daily"])
    ):
        triggered_rules.append("R6_OPUS_CONCENTRATION")
        concentration.update({
            "status": "concentration_risk",
            "action": "block_opus_overflow",
        })

    any_freeze = daily_frozen or monthly_frozen or session_frozen
    if any_freeze:
        standard_to_opus = "frozen"
        standard_task_action = "cheap_pool_or_queue"
    elif daily_yellow:
        standard_to_opus = "blocked_without_hard_or_retry_escalated"
        standard_task_action = "cheap_pool_round_robin"
    else:
        standard_to_opus = "hard_or_retry_escalated_only"
        standard_task_action = "cheap_pool_round_robin"

    routing = {
        "standard_default_pools": list(cfg.get("standard_default_pools") or ["sonnet", "gpt5.5"]),
        "standard_to_opus": standard_to_opus,
        "standard_task_action": standard_task_action,
        "opus_overflow_allowed": False,
        "auto_hard": False,
        "hard_upgrade_scope": "task_attempt_only",
        "hard_to_opus": (
            "p0_p1_break_glass_only" if monthly.get("status") == "break_glass"
            else "scoped_expert_only" if any_freeze
            else "allowed_when_scoped"
        ),
        "retry_escalated_to_opus": "task_attempt_only",
    }

    return {
        "ok": True,
        "dry_run": True,
        "evaluated_at_cst": _format_cst(now_dt),
        "triggered_rules": triggered_rules,
        "daily": daily,
        "monthly": monthly,
        "session": session,
        "concentration": concentration,
        "routing": routing,
        "rollup": rollup,
        "incidents": incidents,
        "recovery_conditions": [
            cond for cond in [
                *(daily.get("recovery_conditions") or []),
                monthly.get("recovery_condition") if monthly.get("status") != "clear" else None,
                session.get("recovery_condition") if session.get("status") not in {"clear", "probed_ok"} else None,
            ] if cond
        ],
    }


def run_budget_guard_dry_run(
    spec: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
    now: Any = None,
    tenant_id: Optional[int] = None,
    probe_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Evaluate one guard cycle without mutating routes or workflow state."""
    spec = spec or {}
    usage = _load_usage_from_spec(spec)
    merged_config = dict(spec.get("config") or {})
    if config:
        merged_config.update(config)
    decision = evaluate_budget_guard(
        usage=usage,
        config=merged_config,
        now=now if now is not None else spec.get("now"),
        previous_state=spec.get("previous_state") or {},
        probe_fn=probe_fn,
    )
    return {
        "ok": True,
        "dry_run": True,
        "tenant_id": tenant_id,
        "route_changes_applied": False,
        "side_effects": [],
        "decision": {
            "triggered_rules": decision["triggered_rules"],
            "daily": decision["daily"],
            "monthly": decision["monthly"],
            "session": decision["session"],
            "concentration": decision["concentration"],
        },
        "routing": decision["routing"],
        "rollup": decision["rollup"],
        "incidents": decision["incidents"],
        "recovery_conditions": decision["recovery_conditions"],
    }

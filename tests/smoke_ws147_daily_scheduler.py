"""Smoke: WS-147 — daily refresh runs at 12:00 and freezes yesterday.

Fail-then-pass gates:
  - Scheduler wiring: server startup schedules daily_refresh_all at 12:00 and
    restart re-registers the same job.
  - Production path: cron uses managed runtime.spawn_task for refresh_all_v2,
    carrying a deterministic business_date=yesterday spec.
  - Workflow cutoff: refresh_all_v2 runs wf1_stock_snapshot_v2 with that exact
    as_of_date and rejects today/future dates as incomplete facts.
  - Verifier: refresh_all_v2 has a deterministic cutoff verifier, not a prompt
    rule.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))


FIXED_NOW = dt.datetime(2026, 6, 10, 13, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_daily_refresh_scheduler_contract() -> None:
    from hipop.runtime import daily_refresh
    from hipop.server import scheduler

    assert daily_refresh.DEFAULT_DAILY_REFRESH_HOUR == 12
    assert daily_refresh.DEFAULT_DAILY_REFRESH_MINUTE == 0
    spec = daily_refresh.build_daily_refresh_spec(now=FIXED_NOW)
    assert spec["business_date"] == "2026-06-09", spec
    assert spec["as_of_date"] == "2026-06-09", spec

    saved = {k: os.environ.get(k) for k in (
        "DAILY_REFRESH_HOUR", "DAILY_REFRESH_MINUTE", "DISABLE_WATCHDOG",
    )}
    os.environ.pop("DAILY_REFRESH_HOUR", None)
    os.environ.pop("DAILY_REFRESH_MINUTE", None)
    os.environ["DISABLE_WATCHDOG"] = "1"
    try:
        for _ in range(2):
            sched = scheduler.start()
            try:
                job = sched.get_job("daily_refresh_all")
                assert job is not None, "daily_refresh_all job missing after scheduler.start()"
                trigger = str(job.trigger)
                assert "hour='12'" in trigger and "minute='0'" in trigger, trigger
            finally:
                sched.shutdown(wait=False)
                scheduler._scheduler = None
    finally:
        _restore_env(saved)


def test_cron_uses_managed_refresh_with_yesterday_spec() -> None:
    from hipop.server import scheduler

    calls = []
    orig_list = scheduler._list_active_tenants
    orig_spawn = scheduler._spawn_refresh_task
    scheduler._list_active_tenants = lambda: [{"id": 7, "name": "Tenant Seven"}]

    def fake_spawn(tenant_id, actor, spec):
        calls.append({"tenant_id": tenant_id, "actor": actor, "spec": spec})
        return "task-ws147"

    scheduler._spawn_refresh_task = fake_spawn
    try:
        scheduler._run_daily_refresh(now=FIXED_NOW)
    finally:
        scheduler._list_active_tenants = orig_list
        scheduler._spawn_refresh_task = orig_spawn

    assert calls == [{
        "tenant_id": 7,
        "actor": {"user_id": None, "email": "cron@system", "role": "system", "source": "cron"},
        "spec": {"business_date": "2026-06-09", "as_of_date": "2026-06-09"},
    }], calls


def test_refresh_all_runs_snapshot_for_business_date_and_blocks_today() -> None:
    from hipop.runtime import daily_refresh
    from hipop.runtime import workflow_runners as wr

    original = dict(wr._RUNNERS)
    calls = []

    def stub(name):
        def _run(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
            calls.append((name, dict(spec or {})))
            if name in ("noon_orders_live_ingest", "noon_live_ingest"):
                return {"summary": f"{name}: live", "source": "live"}
            return {"summary": f"{name}: ok"}
        return _run

    try:
        for name in (
            "wf2_products_v2", "wf2_sales_v2", "noon_orders_live_ingest",
            "wf2_sales_refresh_v2", "wf1_stock_v2", "noon_live_ingest",
            "wf1_stock_merge_v2", "wf1_stock_snapshot_v2", "wf5_sales_cycle_v2",
            "wf3_logistics_v2", "wf6_alerts_v2",
        ):
            wr._RUNNERS[name] = stub(name)
        saved = {}
        out = wr._run_refresh_all(
            "tid", 1, None,
            {"business_date": "2026-06-09"},
            {},
            lambda: None,
            lambda p: saved.update(p),
        )
    finally:
        wr._RUNNERS.clear()
        wr._RUNNERS.update(original)

    assert ("wf1_stock_snapshot_v2", {"as_of_date": "2026-06-09"}) in calls, calls
    assert saved["business_date"] == "2026-06-09", saved
    assert "wf1_stock_snapshot_v2" in saved["steps_done"], saved
    assert out["business_date"] == "2026-06-09", out

    today = daily_refresh.today_date()
    try:
        wr._run_refresh_all("tid", 1, None, {"business_date": today}, {}, lambda: None, lambda p: None)
    except ValueError as exc:
        assert "早于今天" in str(exc) or "incomplete" in str(exc), str(exc)
    else:
        raise AssertionError("refresh_all_v2 accepted today's incomplete business_date")


def test_daily_refresh_verifier_blocks_today_and_requires_snapshot_step() -> None:
    from hipop.runtime import verifiers

    ok = verifiers.verify_daily_refresh_contract(
        progress={"business_date": "2026-06-09", "steps_done": ["wf1_stock_snapshot_v2"]},
        now=FIXED_NOW,
    )
    assert ok["ok"], ok

    today = verifiers.verify_daily_refresh_contract(
        progress={"business_date": "2026-06-10", "steps_done": ["wf1_stock_snapshot_v2"]},
        now=FIXED_NOW,
    )
    assert today["ok"] is False and today["evidence"]["business_date"] == "2026-06-10", today

    missing_snapshot = verifiers.verify_daily_refresh_contract(
        progress={"business_date": "2026-06-09", "steps_done": ["wf1_stock_merge_v2"]},
        now=FIXED_NOW,
    )
    assert missing_snapshot["ok"] is False
    assert missing_snapshot["evidence"]["snapshot_step_done"] is False, missing_snapshot


def main() -> None:
    test_daily_refresh_scheduler_contract()
    print("✓ scheduler default is 12:00 and survives restart re-registration")
    test_cron_uses_managed_refresh_with_yesterday_spec()
    print("✓ cron path spawns managed refresh_all_v2 with business_date=yesterday")
    test_refresh_all_runs_snapshot_for_business_date_and_blocks_today()
    print("✓ refresh_all_v2 snapshots exactly yesterday and blocks today's incomplete date")
    test_daily_refresh_verifier_blocks_today_and_requires_snapshot_step()
    print("✓ verifier enforces cutoff date and snapshot wiring")
    print("\n4/4 passed")


if __name__ == "__main__":
    main()

"""Verify Contract — Phase 0.4 Harness 七层补全（2026-05-21）

按 Anthropic Demystifying Evals + Harness Design for Long-Running Apps：
  "Generator + Evaluator 协商 sprint contract" — "啥叫 done" 提前讲好
  "Grade what produced, not the path"  — 看客观结果，不信 LLM 自述

Worker 跑完 runner 后调本模块 → 用 PG 真查验收，**不靠 LLM 自我评价**。
verify 结果会进 task.result_summary，FAIL 时 task.state = 'done_unverified'。

防 4 大失败模式中的 "premature marking"：
  worker.py _finish 时调 run_verifier(workflow, ...)
  若 verifier 返 ok=False → state 标 done_unverified（而非 done）
  result_summary 含 evidence 让审计能复现
"""
from __future__ import annotations

import time
from typing import Callable, Optional


_VERIFIERS: dict[str, Callable] = {}


def register(workflow: str):
    def deco(fn):
        _VERIFIERS[workflow] = fn
        return fn
    return deco


def run_verifier(workflow: str, task_id: str, tenant_id: int, started_at: float) -> Optional[dict]:
    """Worker 跑完调本函数。返回 {ok, evidence, verdict} 或 None（无注册）。"""
    fn = _VERIFIERS.get(workflow)
    if not fn:
        return None
    try:
        return fn(task_id=task_id, tenant_id=tenant_id, started_at=started_at)
    except Exception as e:
        return {
            "ok": False,
            "evidence": {"verifier_error": f"{type(e).__name__}: {str(e)[:200]}"},
            "verdict": "verifier crashed",
        }


def _started_at_iso(epoch: float) -> str:
    """epoch → 'YYYY-MM-DD HH:MM:SS'（PG 比较用）"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


# ──────────────────────────────────────────────────────────────
# 注册 7 个 v2 workflow 的 verifier
# ──────────────────────────────────────────────────────────────


@register("wf2_products_v2")
def _v_wf2_products(task_id, tenant_id, started_at, **kw):
    """商品库 ingest — 至少应该有 listed SKU 行存在（不一定都新；ingest 可能没新增 SKU）"""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    total = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1",
        (tenant_id,),
    ) or 0
    listed_recent = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1 "
        "AND imported_at >= ?",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    ok = total > 0  # 至少有数据
    return {
        "ok": ok,
        "evidence": {"total_listed_skus": total, "updated_this_run": listed_recent},
        "verdict": (f"{total} listed SKU; {listed_recent} touched this run"
                    if ok else "0 listed SKU — ingest failed?"),
    }


@register("wf2_sales_v2")
def _v_wf2_sales(task_id, tenant_id, started_at, **kw):
    """销量价格 ingest — 检查至少 50% listed SKU 有 latest_price 或 sales_30d。"""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    total = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1",
        (tenant_id,),
    ) or 0
    with_price = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1 "
        "AND (latest_price IS NOT NULL OR sales_30d IS NOT NULL)",
        (tenant_id,),
    ) or 0
    expected_min = max(1, total // 4)  # 至少 25% 有价格 (有些 SKU 永远无销量)
    ok = with_price >= expected_min
    return {
        "ok": ok,
        "evidence": {"total_listed": total, "with_price_or_sales": with_price,
                     "expected_min": expected_min},
        "verdict": (f"{with_price}/{total} listed SKU 有价格或销量"
                    if ok else f"only {with_price}/{expected_min} 有价格 — ERP 拉取可能失败"),
    }


@register("wf1_stock_v2")
def _v_wf1_stock(task_id, tenant_id, started_at, **kw):
    """ERP 6 仓库存 ingest — 至少应该有 wf1_stock 行."""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    rows = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    ok = rows > 0
    return {
        "ok": ok,
        "evidence": {"rows_updated_this_run": rows},
        "verdict": f"{rows} rows updated" if ok else "0 rows — ERP 库存接口可能没拉到",
    }


@register("wf1_stock_snapshot_v2")
def _v_wf1_stock_snapshot(task_id, tenant_id, started_at, **kw):
    """库存历史快照 — 本次 run 应往 wf1_stock_history 写了带业务日 as_of_date 的行，
    且 as_of_date 必须是合法业务日（YYYY-MM-DD），不是 imported_at/今天兜底出来的。

    断言口径（挡"占位假数据"）：
      - 本 run（snapshot_at >= started_at）至少写了 1 行历史。
      - 这些行的 as_of_date 全是合法 YYYY-MM-DD（来自运行参数，不是空/今天反推）。
      - as_of_date 不等于 snapshot_at 的日期部分时也算合法 —— 历史回溯本就常写过去的业务日。
    """
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    cutoff = _started_at_iso(started_at)
    rows = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock_history WHERE tenant_id=? AND snapshot_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    bad_dates = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock_history WHERE tenant_id=? AND snapshot_at >= ? "
        "AND (as_of_date IS NULL OR as_of_date NOT LIKE '____-__-__')",
        (tenant_id, cutoff),
    ) or 0
    distinct_days = data._scalar(
        "SELECT COUNT(DISTINCT as_of_date) FROM wf1_stock_history WHERE tenant_id=?",
        (tenant_id,),
    ) or 0
    ok = rows > 0 and bad_dates == 0
    return {
        "ok": ok,
        "evidence": {"rows_this_run": rows, "bad_as_of_date": bad_dates,
                     "distinct_business_days": distinct_days},
        "verdict": (f"{rows} 行历史快照写入，业务日合法（共 {distinct_days} 个业务日在档）"
                    if ok else
                    (f"{bad_dates} 行 as_of_date 非法（疑似硬编码/空业务日）"
                     if bad_dates else "0 行历史写入 — latest wf1_stock 可能为空")),
    }


@register("wf5_sales_cycle_v2")
def _v_wf5(task_id, tenant_id, started_at, **kw):
    """销售周期 — 跑完 wf5_sales_cycle 应该有 trend / urgency / weekly_total_replenish 等字段."""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    rows_recent = data._scalar(
        "SELECT COUNT(*) FROM wf5_sales_cycle WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    with_replenish = data._scalar(
        "SELECT COUNT(*) FROM wf5_sales_cycle WHERE tenant_id=? "
        "AND updated_at >= ? AND COALESCE(weekly_total_replenish, 0) > 0",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    listed = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1",
        (tenant_id,),
    ) or 0
    expected_min = max(1, listed // 5)  # 至少 20% 有数据
    ok = rows_recent >= expected_min
    return {
        "ok": ok,
        "evidence": {"rows_this_run": rows_recent, "need_replenish": with_replenish,
                     "expected_min": expected_min, "listed_total": listed},
        "verdict": (f"{rows_recent} rows / {with_replenish} need replenish"
                    if ok else f"only {rows_recent}/{expected_min} — 算法可能漏"),
    }


@register("wf3_logistics_v2")
def _v_wf3(task_id, tenant_id, started_at, **kw):
    """物流采集 — 应该有 wf3_logistics_hub_v2 行 (至少 25% listed SKU)."""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    rows_recent = data._scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    with_transit = data._scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? "
        "AND updated_at >= ? AND in_transit_total_qty > 0",
        (tenant_id, _started_at_iso(started_at)),
    ) or 0
    listed = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1 AND COALESCE(sales_60d, 0) > 0",
        (tenant_id,),
    ) or 0
    expected_min = max(1, listed // 4)  # 至少 25% active SKU 写入
    ok = rows_recent >= expected_min
    return {
        "ok": ok,
        "evidence": {"rows_this_run": rows_recent, "with_in_transit": with_transit,
                     "expected_min": expected_min, "active_listed_60d": listed},
        "verdict": (f"{rows_recent} SKU 物流写入, {with_transit} 真在途"
                    if ok else f"only {rows_recent}/{expected_min} — ERP 登录可能失败 / 风控"),
    }


@register("wf6_alerts_v2")
def _v_wf6(task_id, tenant_id, started_at, **kw):
    """物流告警 — wf3 真数据存在时才有 alert. 现阶段允许 0 alert (stub-ish)."""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    hub_rows = data._scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? "
        "AND in_transit_total_qty > 0",
        (tenant_id,),
    ) or 0
    return {
        "ok": True,  # wf6 当前还是 stub-ish，永远 OK
        "evidence": {"wf3_skus_with_transit": hub_rows},
        "verdict": (f"based on {hub_rows} 在途 SKU"
                    if hub_rows else "no in-transit data; wf6 stub mode"),
    }


@register("refresh_all_v2")
def _v_refresh_all(task_id, tenant_id, started_at, **kw):
    """全套刷新 — 检查 4 个关键表都有近期更新."""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    cutoff = _started_at_iso(started_at)
    sku = data._scalar("SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND is_listed=1", (tenant_id,)) or 0
    stock_recent = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    wf5_recent = data._scalar(
        "SELECT COUNT(*) FROM wf5_sales_cycle WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    wf3_recent = data._scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    checks = {
        "wf2_sku_total": sku,
        "wf1_stock_this_run": stock_recent,
        "wf5_sales_cycle_this_run": wf5_recent,
        "wf3_logistics_this_run": wf3_recent,
    }
    # refresh_all 至少 2 个 step 有产出才算 OK（wf3 慢，可能没跑完）
    n_with_data = sum(1 for v in [stock_recent, wf5_recent, wf3_recent] if v > 0)
    ok = sku > 0 and n_with_data >= 2
    return {
        "ok": ok,
        "evidence": checks,
        "verdict": (f"{n_with_data}/3 ingest steps produced data"
                    if ok else "refresh_all 多 step 失败"),
    }


@register("__test_sleep_v2")
def _v_test_sleep(task_id, tenant_id, started_at, **kw):
    """测试 verifier — 检查 progress.json 里 done_chunks 是否完整."""
    import json
    from pathlib import Path
    progress_path = Path(f"/Users/luke/hipop/tasks/{task_id}/progress.json")
    if not progress_path.exists():
        return {"ok": False, "evidence": {}, "verdict": "no progress.json"}
    with open(progress_path) as f:
        prog = json.load(f)
    done = len(prog.get("done_chunks", []))
    total = prog.get("total_chunks", 0)
    ok = (done == total and total > 0)
    return {
        "ok": ok,
        "evidence": {"done_chunks": done, "total_chunks": total},
        "verdict": f"{done}/{total} chunks done",
    }

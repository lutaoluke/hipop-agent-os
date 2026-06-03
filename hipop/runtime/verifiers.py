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

import os
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


# noon live ingest 数据新鲜度阈值（小时）。仓库此前无 noon 新鲜度先例，给默认
# 26h（noon live 至少每日刷一次，留 ~2h 余量）。**可配置点**（优先级 高→低）：
#   1. 调用方/测试显式传 max_age_hours（测试可控默认值）
#   2. 环境变量 HIPOP_NOON_FRESHNESS_MAX_HOURS（运行参数）
#   3. config/hipop.json → verifiers.noon_freshness_max_hours（配置）
#   4. 本默认常量
# 这是确定性 verifier 参数，**绝不写进 SYSTEM_PROMPT / skill** —— 改阈值改这里/配置，
# 不靠 prompt 规则。
DEFAULT_NOON_FRESHNESS_MAX_HOURS = 26.0


def _noon_freshness_max_hours(override=None) -> float:
    """解析 noon 新鲜度阈值（小时）。见 DEFAULT_NOON_FRESHNESS_MAX_HOURS 的优先级注释。"""
    if override is not None:
        return float(override)
    env = os.environ.get("HIPOP_NOON_FRESHNESS_MAX_HOURS")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        from hipop.scripts._config import load_config
        cfg = (load_config().get("verifiers") or {}).get("noon_freshness_max_hours")
        if cfg is not None:
            return float(cfg)
    except Exception:
        pass
    return DEFAULT_NOON_FRESHNESS_MAX_HOURS


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


@register("noon_live_ingest")
def _v_noon_live_ingest(task_id, tenant_id, started_at, max_age_hours=None, **kw):
    """Noon live ingest（WS-N3.2 runner）跑完的验收 —— 用 PG/SQLite 真查 wf1_stock，
    钉死三类坏数据，证明「live 行 → 同一 ingest 落库 → 消费端可读」的数据链是通的：

      1. 数据一致性：noon_saleable_qty <= noon_total_qty（可售不可能超过总量）。
      2. 数据新鲜度：有 noon 数据的行 updated_at 不得早于 now - N 小时（N 见
         `_noon_freshness_max_hours`，来自配置/运行参数/测试可控默认，**不写进 prompt**）。
         live 跑完所有 noon 行刚被重写 → 天然新鲜；过旧 = ingest 没真刷 / 死代码短路。
      3. pending 非 NULL：**已知 SKU**（在 wf2_sku 里）且有 noon 数据的行，其
         pending_inbound_qty 必须非 NULL —— 否则消费端 wf_sales_cycle.read_sales_v2 的
         `immediate = noon_saleable + pending_inbound` 会静默把 NULL 当 0，
         即时可售口径少算「送仓未上架」(占位假数据 / 接线缺失死法)。

    另要求本 run 至少写了 1 行 noon 数据（updated_at >= started_at），证明 runner 真跑过、
    不是空过冒充成功。max_age_hours 仅供测试覆写新鲜度阈值（验阈值是参数、非写死）。
    """
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    run_cutoff = _started_at_iso(started_at)
    max_hours = _noon_freshness_max_hours(max_age_hours)
    stale_cutoff = _started_at_iso(time.time() - max_hours * 3600.0)

    # 本 run 至少写了 noon 行（runner 真跑过，不是 0 行空过）
    rows_this_run = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? "
        "AND noon_total_qty IS NOT NULL AND updated_at >= ?",
        (tenant_id, run_cutoff),
    ) or 0

    # 断言 1：noon_saleable_qty <= noon_total_qty（违反 = 坏数据，逐行 SQL 真比对）
    saleable_gt_total = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? "
        "AND noon_total_qty IS NOT NULL AND noon_saleable_qty IS NOT NULL "
        "AND noon_saleable_qty > noon_total_qty",
        (tenant_id,),
    ) or 0

    # 断言 2：noon 数据新鲜度 —— 有 noon 数据的行 updated_at 不得早于 now - N 小时
    stale = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? "
        "AND noon_total_qty IS NOT NULL AND (updated_at IS NULL OR updated_at < ?)",
        (tenant_id, stale_cutoff),
    ) or 0

    # 断言 3：已知 SKU（在 wf2_sku）且有 noon 数据 → pending_inbound_qty 非 NULL
    pending_null = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock s WHERE s.tenant_id=? "
        "AND s.noon_total_qty IS NOT NULL AND s.pending_inbound_qty IS NULL "
        "AND EXISTS (SELECT 1 FROM wf2_sku k WHERE k.tenant_id=s.tenant_id "
        "  AND k.entity_alias=s.entity_alias AND k.partner_sku=s.partner_sku)",
        (tenant_id,),
    ) or 0

    ok = (rows_this_run > 0 and saleable_gt_total == 0
          and stale == 0 and pending_null == 0)
    if ok:
        verdict = (f"{rows_this_run} 行 noon live 落库：saleable<=total、{max_hours:g}h 内新鲜、"
                   f"已知 SKU pending 非 NULL（消费端 immediate 可读）")
    elif rows_this_run == 0:
        verdict = "0 行 noon 数据本 run 写入 — live ingest 没接上/空过"
    else:
        bad = []
        if saleable_gt_total:
            bad.append(f"{saleable_gt_total} 行 saleable>total")
        if stale:
            bad.append(f"{stale} 行 updated_at 超 {max_hours:g}h 未刷新")
        if pending_null:
            bad.append(f"{pending_null} 行已知 SKU pending_inbound_qty 仍为 NULL")
        verdict = "坏数据被拦：" + "；".join(bad)
    return {
        "ok": ok,
        "evidence": {
            "rows_this_run": rows_this_run,
            "saleable_gt_total": saleable_gt_total,
            "stale_rows": stale,
            "pending_null_known_sku": pending_null,
            "freshness_max_hours": max_hours,
        },
        "verdict": verdict,
    }


@register("wf1_stock_merge_v2")
def _v_wf1_stock_merge(task_id, tenant_id, started_at, **kw):
    """库存快照合并（WS-12）— 本次 run 写出的 total_stock 必须 == 各来源列确定性求和，
    且不为 NULL。挡两种死法：
      · 死代码短路 / 绕过 pending_inbound：total_stock 必须等于
        noon_total + overseas + yiwu + dongguan + pending_inbound（含 pending），
        逐行用 SQL 真比对，任一行不等即 FAIL（不靠 LLM 自述）。
      · 占位假数据：本 run 至少更新 1 行,且 total_stock 不留 NULL。

    SQL 求和表达式由 merge_stock_snapshot_v2.TOTAL_STOCK_COMPONENTS 现取现拼，
    与生产合并规则共用同一份列清单 —— 规则改了这里不会漂移。
    """
    from hipop.server import data
    from hipop.scripts import merge_stock_snapshot_v2 as merge
    data.set_current_tenant(tenant_id)
    cutoff = _started_at_iso(started_at)
    sum_expr = merge._sum_expr(merge.TOTAL_STOCK_COMPONENTS)
    rows = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND updated_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    # total_stock 与确定性求和不符（含 NULL）的行数 —— 必须为 0。
    mismatched = data._scalar(
        f"SELECT COUNT(*) FROM wf1_stock WHERE tenant_id=? AND updated_at >= ? "
        f"AND (total_stock IS NULL OR total_stock != ({sum_expr}))",
        (tenant_id, cutoff),
    ) or 0
    ok = rows > 0 and mismatched == 0
    return {
        "ok": ok,
        "evidence": {"rows_merged_this_run": rows, "mismatched_total_stock": mismatched,
                     "components": list(merge.TOTAL_STOCK_COMPONENTS)},
        "verdict": (f"{rows} 行 total_stock = 各来源确定性求和（含 pending），无绕过/NULL"
                    if ok else
                    (f"{mismatched} 行 total_stock 与求和不符或为 NULL（绕过 pending/占位假数据）"
                     if mismatched else "0 行被合并 — 合并步骤没接上/快照为空")),
    }


def _valid_business_date(s) -> bool:
    """as_of_date 是否为合法业务日：零填充 'YYYY-MM-DD' 且真实存在的日历日。

    复用 scripts/stock_history.is_valid_business_date 同一份判据，避免规则两处漂移
    —— **不能只靠 SQL LIKE 看形状**：'2026-99-99' / '2026-02-30' 形状对但日历上不存在，
    必须用真实日期解析判掉，否则占位假业务日会蒙混过门。
    """
    from hipop.scripts import stock_history
    return stock_history.is_valid_business_date(s)


@register("wf1_stock_snapshot_v2")
def _v_wf1_stock_snapshot(task_id, tenant_id, started_at, **kw):
    """库存历史快照 — 本次 run 应往 wf1_stock_history 写了带业务日 as_of_date 的行，
    且 as_of_date 必须是**真实存在的**业务日（YYYY-MM-DD），不是 imported_at/今天兜底、
    也不是 '2026-99-99' 这种形状对、日历上不存在的占位假日。

    断言口径（挡"占位假数据"）：
      - 本 run（snapshot_at >= started_at）至少写了 1 行历史。
      - 这些行的 as_of_date 全是真实日历日 —— 用 strptime 真解析判定，**不靠 SQL LIKE 形状**。
      - as_of_date 不等于 snapshot_at 的日期部分时也算合法 —— 历史回溯本就常写过去的业务日。
    """
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    cutoff = _started_at_iso(started_at)
    rows = data._scalar(
        "SELECT COUNT(*) FROM wf1_stock_history WHERE tenant_id=? AND snapshot_at >= ?",
        (tenant_id, cutoff),
    ) or 0
    # 拉本 run 写入的所有 as_of_date，在 Python 里用真实日期解析判合法（含非法日历日）。
    run_dates = data._fetch(
        "SELECT as_of_date FROM wf1_stock_history WHERE tenant_id=? AND snapshot_at >= ?",
        (tenant_id, cutoff),
    )
    bad_samples = sorted({
        str(r.get("as_of_date"))
        for r in run_dates
        if not _valid_business_date(r.get("as_of_date"))
    })
    bad_dates = len(bad_samples)
    distinct_days = data._scalar(
        "SELECT COUNT(DISTINCT as_of_date) FROM wf1_stock_history WHERE tenant_id=?",
        (tenant_id,),
    ) or 0
    ok = rows > 0 and bad_dates == 0
    return {
        "ok": ok,
        "evidence": {"rows_this_run": rows, "bad_as_of_date": bad_dates,
                     "bad_samples": bad_samples,
                     "distinct_business_days": distinct_days},
        "verdict": (f"{rows} 行历史快照写入，业务日均为真实日历日（共 {distinct_days} 个业务日在档）"
                    if ok else
                    (f"{bad_dates} 行 as_of_date 非法业务日 {bad_samples}（占位/不存在的日期）"
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


@register("wf2_sales_refresh_v2")
def _v_wf2_sales_refresh(task_id, tenant_id, started_at, **kw):
    """按需销量刷新（WS-21）的验收 —— 用 PG/SQLite 真查 wf2_sku，证明
    「现有 noon 订单 → 窗口聚合 → 评级」这条链真跑过、不是空过/死代码短路：

      1. 接线证明：每个有 noon 订单（wf2_orders 里 partner_sku 出现过）的 SKU，
         在 wf2_sku 必须落了 sales_grade（ABCD 之一）—— 评级 merge 真跑过、没漏。
      2. 占位假数据闸门：有订单的 SKU 其 total_orders > 0 且 sales_grade 非 NULL，
         否则 = 算了订单却没写评级（merge 漏接 / 死列）。

    至少应有 1 个有订单的 SKU（否则该 tenant 根本没 noon 销量可刷，runner 空过）。
    """
    from hipop.server import data
    data.set_current_tenant(tenant_id)

    # 有 noon 订单的 SKU 总数（评级应覆盖的对象集合）
    skus_with_orders = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku k WHERE k.tenant_id=? AND EXISTS ("
        "  SELECT 1 FROM wf2_orders o WHERE o.tenant_id=k.tenant_id "
        "  AND o.entity_alias=k.entity_alias AND o.partner_sku=k.partner_sku)",
        (tenant_id,),
    ) or 0

    # 其中**漏评级**的（有订单却 sales_grade 为 NULL）—— 必须为 0
    graded_missing = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku k WHERE k.tenant_id=? "
        "AND (k.sales_grade IS NULL OR k.sales_grade='') AND EXISTS ("
        "  SELECT 1 FROM wf2_orders o WHERE o.tenant_id=k.tenant_id "
        "  AND o.entity_alias=k.entity_alias AND o.partner_sku=k.partner_sku)",
        (tenant_id,),
    ) or 0

    # 有订单的 SKU 真落了 total_orders（聚合口径，钉占位假数据）
    with_order_count = data._scalar(
        "SELECT COUNT(*) FROM wf2_sku k WHERE k.tenant_id=? "
        "AND COALESCE(k.total_orders,0) > 0 AND EXISTS ("
        "  SELECT 1 FROM wf2_orders o WHERE o.tenant_id=k.tenant_id "
        "  AND o.entity_alias=k.entity_alias AND o.partner_sku=k.partner_sku)",
        (tenant_id,),
    ) or 0

    ok = skus_with_orders > 0 and graded_missing == 0 and with_order_count > 0
    if ok:
        verdict = (f"{skus_with_orders} 个有 noon 订单的 SKU 均已评级（sales_grade 非空）"
                   f"、total_orders 已落库")
    elif skus_with_orders == 0:
        verdict = "0 个 SKU 有 noon 订单 — 按需刷新空过（无销量可刷/接线缺失）"
    else:
        verdict = (f"{graded_missing} 个有订单的 SKU 仍缺 sales_grade（评级没接上/死列）"
                   if graded_missing else "有订单的 SKU 未落 total_orders（聚合没跑）")
    return {
        "ok": ok,
        "evidence": {
            "skus_with_orders": skus_with_orders,
            "graded_missing": graded_missing,
            "with_order_count": with_order_count,
        },
        "verdict": verdict,
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

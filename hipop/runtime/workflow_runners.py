"""Workflow runner registry — 把每个 v2 workflow 注册成 Managed Agents runner。

Runner 签名：
  def run(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress) -> dict

每个 runner 内部职责：
  - 读 spec 决定要跑什么
  - 读 progress 决定续跑点
  - 每完成一个 chunk → save_progress(new_progress) + heartbeat()
  - 跑完 return {"summary": "..."}

设计跟 Anthropic Long-Running Harness 一致：
  - Initializer 部分 = spawn_task 那一步（写 spec.json）
  - Coding Agent 部分 = 这里的 runner（chunk-by-chunk + checkpoint）
"""
from __future__ import annotations

from typing import Callable, Optional


_RUNNERS: dict[str, Callable] = {}


def register(workflow: str):
    """装饰器：注册 runner。"""
    def deco(fn):
        _RUNNERS[workflow] = fn
        return fn
    return deco


def get_runner(workflow: str) -> Optional[Callable]:
    return _RUNNERS.get(workflow)


def list_runners() -> list[str]:
    return list(_RUNNERS.keys())


def _refresh_all_business_date(spec: dict | None) -> str:
    """Resolve refresh_all business cutoff date; default is yesterday, never today."""
    from hipop.runtime import daily_refresh
    payload = spec or {}
    requested = payload.get("business_date") or payload.get("as_of_date")
    if requested is None:
        requested = daily_refresh.business_date_yesterday()
    return daily_refresh.validate_business_date_cutoff(requested)


# 生产入口加载即接线 noon live row producers（import 副作用，单一收口）。worker/api 运行任何
# workflow 都会 import 本模块 → 连带加载 live_producers → 已就绪抓取器（WS-58 订单）的 live
# producer 自动注册，run_live 默认走真抓取器、不再回落 CSV。详见 live_producers 模块。
from . import live_producers as _live_producers  # noqa: E402,F401


# ──────────────────────────────────────────────────────────────
# 具体 runners — 逐个把 WORKFLOW_REGISTRY 里的 v2 workflow 接进来
# ──────────────────────────────────────────────────────────────


@register("wf2_products_v2")
def _run_wf2_products(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """ERP 商品库 ingest — 不太长（~2 分钟），不分 chunk，整跑。"""
    from hipop.scripts import ingest_erp_products_v2
    heartbeat()
    res = ingest_erp_products_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf2_products: {res}"}


@register("wf1_stock_v2")
def _run_wf1_stock(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """ERP 6 仓库存 ingest。"""
    from hipop.scripts import ingest_erp_stock_v2
    heartbeat()
    res = ingest_erp_stock_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_stock: {res}"}


@register("wf1_noon_stock_v2")
def _run_wf1_noon_stock(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """Noon my inventory → v2 wf1_stock.noon_*（CSV/导表驱动，WS-10）。

    spec: {"file": <path>} 指定单文件；不给则扫 inbox/。

    改完 noon_* 列后立刻重算合并快照（WS-12），让 total_stock 反映最新官方仓库存。
    """
    from hipop.scripts import ingest_noon_stock_csv_v2, merge_stock_snapshot_v2
    heartbeat()
    res = ingest_noon_stock_csv_v2.run_v2(tenant_id=tenant_id, file=spec.get("file"))
    merge = merge_stock_snapshot_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_noon_stock: {res}; merge: {merge}"}


@register("noon_live_ingest")
def _run_noon_live_ingest(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """Noon FBN live 行 → v2 wf1_stock.noon_*（WS-N3.2）。

    与 wf1_noon_stock_v2（CSV 入口）共用同一 `_aggregate`/`_upsert`（WS-N3.1 契约）；
    本 runner 把 live 源接上：读 WS-N2 live row producer，喂同一 ingest。

    取数失败（producer 未接入 / 抛错）→ 整链回落 CSV interim（同契约，不短路）；
    无 CSV 可回落 → run_live raise LiveSourceUnavailable（红灯），绝不写 0 假数据。
    改完 noon_* 列后立即重算合并快照（WS-12），让 total_stock 反映最新官方仓库存。

    spec: {"file": <csv path>, "inbox": <dir>} 仅作 live 失败时的 CSV fallback 输入。
    读写表见下方 .reads / .writes 声明（声明 == 真正部分 upsert 的 noon_* 四列）。
    """
    from hipop.scripts import ingest_noon_stock_csv_v2 as noon, merge_stock_snapshot_v2
    heartbeat()
    res = noon.run_live(
        tenant_id=tenant_id,
        file=(spec or {}).get("file"),
        inbox=(spec or {}).get("inbox"),
    )
    merge = merge_stock_snapshot_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "source": res.get("source"), "result": str(res)[:200]})
    # source 顶层回传，供 refresh_all_v2 判断「真走 live / 显式回落 csv / blocked」，
    # 不靠解析 summary 字符串（钉死「静默回落 CSV 冒充迁移完成」假绿死法）。
    return {
        "summary": f"noon_live_ingest [{res.get('source')}]: {res}; merge: {merge}",
        "source": res.get("source"),
    }


# 读/写声明（机器可读，钉「接线缺失」死法）：声明的写列即 ingest 真正部分 upsert
# 的 noon_* 四列；读覆盖 live 源 + CSV fallback 输入两条真实输入路径。
_run_noon_live_ingest.reads = (
    "noon_fbn_live_row_producer",   # WS-N2 live 源
    "inbox:noon_inventory_csv",     # live 失败时的 CSV interim fallback 输入
)
_run_noon_live_ingest.writes = (
    "wf1_stock.noon_total_qty",
    "wf1_stock.noon_saleable_qty",
    "wf1_stock.noon_unsaleable_qty",
    "wf1_stock.noon_warehouses_json",
)


@register("noon_orders_live_ingest")
def _run_noon_orders_live_ingest(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """Noon 订单 live 行 → v2 wf2_orders / wf2_sku（WS-35/WS-58 socket）。

    与 CSV 入口（process_csv_v2）共用同一 `_aggregate`/`_upsert`（WS-35 契约，不分叉）；
    本 runner 把 live 源接上：读 WS-N2.1/WS-58 live row producer，喂同一 ingest。

    取数失败（producer 未接入 / 抛错 / 坏行）→ 整链回落 CSV interim（同契约，不短路），
    结果标 source=csv_fallback + live_error；无 CSV 可回落 → run_live raise
    LiveSourceUnavailable（红灯），绝不写默认销量/金额冒充成功。

    spec: {"file": <csv path>, "inbox": <dir>} 仅作 live 失败时的 CSV fallback 输入。
    读写表见下方 .reads / .writes 声明。
    """
    from hipop.scripts import ingest_noon_csv_v2 as noon
    heartbeat()
    res = noon.run_live(
        tenant_id=tenant_id,
        file=(spec or {}).get("file"),
        inbox=(spec or {}).get("inbox"),
    )
    save_progress({"done": True, "source": res.get("source"), "result": str(res)[:200]})
    return {
        "summary": f"noon_orders_live_ingest [{res.get('source')}]: {res}",
        "source": res.get("source"),
    }


_run_noon_orders_live_ingest.reads = (
    "noon_orders_live_row_producer",  # WS-N2.1/WS-58 live 源
    "inbox:noon_orders_csv",          # live 失败时的 CSV interim fallback 输入
)
_run_noon_orders_live_ingest.writes = (
    "wf2_orders",
    "wf2_sku",
)


@register("budget_guard_dry_run")
def _run_budget_guard_dry_run(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """预算守卫 dry-run（WS-176）。

    只读 Anthropic shared-pool usage fixture/current dump + budget_guard 配置，输出本轮
    R0-R8 判定、agent/tier rollup、incident/recovery 条件；不真实改路由。
    """
    from hipop.runtime import budget_guard
    heartbeat()
    out = budget_guard.run_budget_guard_dry_run(spec=spec or {}, tenant_id=tenant_id)
    save_progress({
        "done": True,
        "route_changes_applied": False,
        "decision": out.get("decision"),
        "rollup": out.get("rollup"),
        "incidents": out.get("incidents"),
    })
    rules = out.get("decision", {}).get("triggered_rules") or []
    return {
        "summary": f"budget_guard dry-run: {len(rules)} rules triggered; route_changes_applied=False",
        "budget_guard": out,
    }


_run_budget_guard_dry_run.reads = (
    "anthropic_shared_pool_usage",
    "hipop.config.budget_guard",
)
_run_budget_guard_dry_run.writes = ()


@register("wf1_inbound_staging_v2")
def _run_wf1_inbound_staging(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """ERP 送仓/拣货 + Noon ASN → wf1_asn_lines_staging（供 WS-11，WS-10）。

    spec: {"noon_asn": <path>, "erp_inbound": <path>}
    """
    from hipop.scripts import ingest_inbound_staging_v2
    heartbeat()
    res = ingest_inbound_staging_v2.run_v2(
        tenant_id=tenant_id,
        noon_asn_file=spec.get("noon_asn"),
        erp_inbound_file=spec.get("erp_inbound"),
    )
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_inbound_staging: {res}"}


@register("wf1_pending_inbound_v2")
def _run_wf1_pending_inbound(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """ASN 送仓未上架 → wf1_stock.pending_inbound_qty（确定性状态规则，WS-11）。

    读 wf1_asn_lines_staging（WS-10 产出），按 ASN 状态计入 → 聚合 →
    部分 upsert 回 wf1_stock.pending_inbound_qty。消费端 wf_sales_cycle.run_v2
    已读该列（计入 immediate）。

    改完 pending_inbound_qty 后立刻重算合并快照（WS-12），使最终 total_stock
    含最新 pending（钉死「最终快照绕过 pending_inbound_qty 规则」死法）。
    """
    from hipop.scripts import compute_pending_inbound_v2, merge_stock_snapshot_v2
    heartbeat()
    res = compute_pending_inbound_v2.run_v2(tenant_id=tenant_id)
    merge = merge_stock_snapshot_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_pending_inbound: {res}; merge: {merge}"}


@register("wf1_stock_merge_v2")
def _run_wf1_stock_merge(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """合并 v2 当前库存快照 → wf1_stock.total_stock（确定性合并规则，WS-12）。

    把官方仓(noon) + 海外仓 + 国内(义乌/东莞) + 送仓未上架(pending_inbound)
    汇总成运营可用的当前库存快照，替代原本人工 Excel 合并；只重写 total_stock，
    各来源列 + 追溯字段原样保留。接进 refresh_all_v2（ERP 库存之后），
    noon/pending runner 改完来源列也各自调一次本合并。
    """
    from hipop.scripts import merge_stock_snapshot_v2
    heartbeat()
    res = merge_stock_snapshot_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_stock_merge: {res}"}


@register("wf1_stock_snapshot_v2")
def _run_wf1_stock_snapshot(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """库存历史快照 — 把 latest wf1_stock 按业务日冻结进 wf1_stock_history（WS-22）。

    spec: {"as_of_date": "YYYY-MM-DD"} 必填业务日运行参数；
          缺失 → run_v2 直接 raise（红灯，不假装 today）。
          可选 {"entity_alias": "..."} 只冻结单 entity。
    """
    from hipop.scripts import stock_history
    heartbeat()
    res = stock_history.run_v2(
        tenant_id=tenant_id,
        as_of_date=(spec or {}).get("as_of_date"),
        entity_alias=(spec or {}).get("entity_alias"),
    )
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_stock_snapshot: {res}"}


@register("wf2_sales_v2")
def _run_wf2_sales(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """ERP 销量价格利润率 ingest — 6 时间窗，可断点续。

    progress.json schema:
      {"windows_done": ["10d", "30d", ...], "chunk_idx": N}
    """
    from hipop.scripts import ingest_erp_sales_v2
    windows = ["10d", "30d", "60d", "90d", "120d", "180d"]
    done = set(progress.get("windows_done", []))
    # 整跑（ingest_erp_sales_v2.run_v2 内部已经处理 6 时间窗 + 重试）
    # 下一步可以拆成 per-window 让 progress 更细
    heartbeat()
    res = ingest_erp_sales_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "windows_done": windows, "result": str(res)[:200]})
    return {"summary": f"wf2_sales: {res}"}


@register("wf2_sales_refresh_v2")
def _run_wf2_sales_refresh(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """按需「销量刷新」（WS-21）— 用现有 wf2_orders 重算窗口销量 + 评级/预测 → wf2_sku。

    不拉新 CSV、不登 ERP：把「上传 noon CSV」之后才会跑的 aggregate + grade 抽成
    一个可单独触发的入口（运营在 UI/chat 点「刷新销量」，或 refresh_all_v2 每周自动调）。
    与上传管道复用 wf_sales_static_v2.run_v2（同一 aggregate_sales_v2 / merge_entity_v2），
    口径不漂移。读写表见下方 .reads / .writes 声明。
    """
    from hipop.workflows import wf_sales_static_v2
    heartbeat()
    res = wf_sales_static_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf2_sales_refresh: {res}"}


# 读/写声明（机器可读，钉「接线缺失」死法）：读 wf2_orders（noon 订单明细）+ wf2_sku
# （取现有 SKU 行），写回 wf2_sku 的窗口销量 + 评级/预测/契约字段。
_run_wf2_sales_refresh.reads = (
    "wf2_orders",
    "wf2_sku",
)
_run_wf2_sales_refresh.writes = (
    "wf2_sku.sales_10d", "wf2_sku.sales_30d", "wf2_sku.sales_60d",
    "wf2_sku.sales_90d", "wf2_sku.sales_120d", "wf2_sku.sales_180d",
    "wf2_sku.sales_grade", "wf2_sku.forecast_10d", "wf2_sku.forecast_30d",
    "wf2_sku.total_revenue", "wf2_sku.return_rate", "wf2_sku.anomalies_json",
)


@register("wf3_logistics_v2")
def _run_wf3_logistics(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """物流采集 — 真正"长任务"场景（255-1000 SKU × ERP 拉单 + playwright，~30 分钟）。

    Chunked + 续跑（Managed Agents Initializer+Coding Agent 范式）：
      - 每 chunk_size SKU 一个 chunk（默认 25）
      - 每 chunk 完成 → save_progress(chunk_idx) + heartbeat
      - watchdog 检测到 orphan → wake_task → 新 worker 读 progress.chunk_idx 续跑
    """
    from hipop.workflows import wf3_logistics_v2 as w
    chunk_size = spec.get("chunk_size", 25)
    max_skus = spec.get("max_skus")  # 测试时限量
    start_chunk_idx = progress.get("chunk_idx", 0)
    n = w.run_v2_chunked(
        tenant_id=tenant_id,
        chunk_size=chunk_size,
        start_chunk_idx=start_chunk_idx,
        max_skus=max_skus,
        heartbeat=heartbeat,
        save_progress=save_progress,
    )
    return {"summary": f"wf3_logistics: {n} SKU 写入 hub_v2 (chunked, resume from chunk_idx={start_chunk_idx})"}


@register("wf5_sales_cycle_v2")
def _run_wf5(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """销售周期 + 补货决策 — CPU bound，~30s，整跑。"""
    from hipop.workflows import wf_sales_cycle
    heartbeat()
    result = wf_sales_cycle.run_v2(tenant_id=tenant_id)
    # fail-closed：run_v2 在「全部 entity 上游未就绪」时返回 {"ok": False, ...}。
    # 绝不能丢掉这个信号 save_progress(done) 后冒充成功——那会让 task 进 done
    # → verifier 查到 0 行 → done_unverified → chat 误报「已完成」（T38 假活根因）。
    # 这里直接 raise，让 worker 把 task 终态标 error，并带可读的「缺哪路数据」原因。
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(
            f"wf5_sales_cycle fail-closed: {result.get('error') or 'upstream_not_ready'} — "
            f"{result.get('message') or '上游数据未就绪'}"
        )
    # 统计本 run 真写了多少行（兼容部分 entity 被跳的情况）；0 行不算成功。
    written = 0
    if isinstance(result, dict):
        written = sum(v for k, v in result.items()
                      if k != "_skipped" and isinstance(v, int))
    if written == 0:
        raise RuntimeError(
            "wf5_sales_cycle fail-closed: 0 rows written — "
            "上游 wf2_sku/wf1_stock 可能为空或未就绪，拒绝把空结果当刷新成功"
        )
    save_progress({"done": True, "rows_written": written})
    return {"summary": f"wf5_sales_cycle: {written} skus written"}


@register("wf6_alerts_v2")
def _run_wf6(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """物流告警生成。"""
    from hipop.workflows import wf6_alerts_v2
    heartbeat()
    n = wf6_alerts_v2.run_v2(tenant_id=tenant_id)
    save_progress({"done": True, "n_alerts": n})
    return {"summary": f"wf6_alerts: {n} alerts"}


# ── Test-only runner（不动业务表，纯模拟 chunked 长任务）─────────────
@register("__test_sleep_v2")
def _run_test_sleep(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """专门测 watchdog wake 用 — 模拟一个 chunked 长任务，每 chunk sleep 5s.
    spec: {"total_chunks": 6, "sleep_sec": 5}
    """
    import time
    total = spec.get("total_chunks", 6)
    sleep_sec = spec.get("sleep_sec", 5)
    start = progress.get("chunk_idx", 0)
    done_chunks = list(progress.get("done_chunks", []))
    for i in range(start, total):
        heartbeat()
        time.sleep(sleep_sec)
        done_chunks.append(i)
        save_progress({"chunk_idx": i + 1, "total_chunks": total, "done_chunks": done_chunks})
        print(f"[__test_sleep] chunk {i + 1}/{total} done (slept {sleep_sec}s)", flush=True)
    return {"summary": f"test_sleep: {len(done_chunks)}/{total} chunks (resume from {start})"}


@register("refresh_all_v2")
def _run_refresh_all(task_id, tenant_id, actor, spec, progress, heartbeat, save_progress):
    """全量刷新 — 串行跑 ERP + noon 实时 ingest → 昨日业务日快照 → 分析。

    步序（在对应 ERP ingest 之后、依赖它的分析步之前插入 noon 实时 ingest 两步）：
      wf2_products → wf2_sales → noon 订单实时 → wf1_stock → noon 可售库存实时
      → 库存快照合并 → wf1_stock_snapshot(as_of_date=business_date) →
      wf5（销售周期/补货）→ wf3（物流）→ wf6（告警）

    在途/送仓（ASN）口径不接 noon live：由已有 ERP 链（wf1_stock + WS-10/11
    pending_inbound_qty）覆盖，本编排不动它（WS-60 已取消）。

    noon 实时步三态（钉死「迁移完成 ≠ 没报错」假绿死法）：
      - source==live：真走实时行（迁移目标态）。
      - source==csv_fallback：实时取数失败但有 CSV interim → 显式回落，结果标
        [csv_fallback] + live_error；summary 报「未走 live」，允许继续（验收③）。
      - raise LiveSourceUnavailable（无 live 又无 CSV 回落）→ 记 blocked，并**跳过
        依赖 noon 的分析步**（wf5 销售周期/补货），绝不拿空/旧数据产出虚假补货结论。

    Chunked checkpoint：每个 step 完了写 progress.steps_done，重启从下一个 step 续。
    """
    spec = spec or {}
    business_date = _refresh_all_business_date(spec)
    # kind：erp（ERP ingest，与 noon 无关）/ noon_live（noon 实时 ingest）/
    #       analysis_needs_noon（消费 noon 数据的分析步，noon blocked 时必须跳过）/
    #       snapshot_needs_noon_stock（冻结库存历史，noon 库存 blocked 时必须跳过）/
    #       analysis（不依赖 noon 的分析步，noon blocked 也照跑）。
    steps = [
        ("wf2_products_v2", "ERP 商品库", "erp"),
        ("wf2_sales_v2", "ERP 销量价格", "erp"),
        ("noon_orders_live_ingest", "noon 订单实时", "noon_live"),
        ("wf2_sales_refresh_v2", "noon 销量聚合 + 评级", "analysis_needs_noon"),  # WS-21：每周/每日也重算 noon 评级
        ("wf1_stock_v2", "ERP 库存", "erp"),
        ("noon_live_ingest", "noon 可售库存实时", "noon_live"),
        ("wf1_stock_merge_v2", "库存快照合并", "erp"),
        ("wf1_stock_snapshot_v2", "库存历史快照（截止业务日）", "snapshot_needs_noon_stock"),
        ("wf5_sales_cycle_v2", "销售周期", "analysis_needs_noon"),
        ("wf3_logistics_v2", "物流采集", "analysis"),
        ("wf6_alerts_v2", "物流告警", "analysis"),
    ]
    steps_done = set(progress.get("steps_done", []))
    failures = list(progress.get("failures", []))
    noon_sources = dict(progress.get("noon_sources", {}))
    noon_blocked = list(progress.get("noon_blocked", []))
    skipped = list(progress.get("skipped", []))

    def _checkpoint(current):
        save_progress({
            "steps_done": list(steps_done),
            "failures": failures,
            "noon_sources": noon_sources,
            "noon_blocked": noon_blocked,
            "skipped": skipped,
            "current_step": current,
            "business_date": business_date,
            "cutoff_rule": "yesterday_or_explicit_past_date",
        })

    for step_workflow, step_name, kind in steps:
        if step_workflow in steps_done:
            print(f"[refresh_all_v2] skip {step_workflow} (already done)", flush=True)
            continue
        # 依赖 noon 的分析步：任一 noon 实时步 blocked（无 live、无 CSV 回落）→ 跳过它，
        # 不拿空/旧数据产虚假补货/销量结论（验收③：blocked 不编数）。
        if kind == "analysis_needs_noon" and noon_blocked:
            reason = f"upstream noon blocked: {noon_blocked}"
            print(f"[refresh_all_v2] SKIP {step_workflow}（{reason}，不产虚假分析结论）", flush=True)
            skipped.append({"step": step_workflow, "reason": reason})
            _checkpoint(step_workflow)
            continue
        if kind == "snapshot_needs_noon_stock" and "noon_live_ingest" in noon_blocked:
            reason = "upstream noon stock blocked: noon_live_ingest"
            print(f"[refresh_all_v2] SKIP {step_workflow}（{reason}，不冻结不完整库存事实）", flush=True)
            skipped.append({"step": step_workflow, "reason": reason})
            _checkpoint(step_workflow)
            continue
        print(f"[refresh_all_v2] → {step_workflow} ({step_name})", flush=True)
        heartbeat()
        try:
            sub_runner = get_runner(step_workflow)
            step_spec = {}
            if step_workflow == "wf1_stock_snapshot_v2":
                step_spec = {"as_of_date": business_date}
            res = sub_runner(task_id, tenant_id, actor, step_spec, {}, heartbeat, lambda p: None) or {}
            steps_done.add(step_workflow)
            if kind == "noon_live":
                src = res.get("source")
                noon_sources[step_workflow] = src
                if src != "live":
                    print(f"[refresh_all_v2] ⚠ {step_workflow} 未走 live（source={src}，"
                          f"显式回落 CSV interim）", flush=True)
        except Exception as e:
            failures.append({"step": step_workflow, "error": str(e)[:200]})
            print(f"[refresh_all_v2] {step_workflow} FAILED: {e}", flush=True)
            if kind == "noon_live":
                # 无 live 又无 CSV 可回落（LiveSourceUnavailable）→ blocked，标记以跳过
                # 下游依赖 noon 的分析步，绝不让它用空/旧数据假绿。
                noon_blocked.append(step_workflow)
                print(f"[refresh_all_v2] {step_workflow} BLOCKED（无 live、无 CSV 回落）→ "
                      f"将跳过依赖 noon 的分析步", flush=True)
        _checkpoint(step_workflow)

    n_live = sum(1 for s in noon_sources.values() if s == "live")
    n_fallback = sum(1 for s in noon_sources.values() if s == "csv_fallback")
    summary = (f"refresh_all: {len(steps_done)}/{len(steps)} steps done, "
               f"{len(failures)} failed; business_date={business_date}; "
               f"noon live={n_live}/{len(noon_sources)}")
    if n_fallback:
        summary += f", csv_fallback={n_fallback}（未走 live，见 noon_sources）"
    if noon_blocked:
        summary += (f"; BLOCKED noon={noon_blocked} → 跳过依赖 noon 的分析步"
                    f"{[s['step'] for s in skipped]}（不产虚假结论）")
    return {
        "summary": summary,
        "noon_sources": noon_sources,
        "noon_blocked": noon_blocked,
        "skipped": skipped,
        "business_date": business_date,
    }

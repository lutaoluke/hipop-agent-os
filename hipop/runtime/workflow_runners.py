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
    """
    from hipop.scripts import ingest_noon_stock_csv_v2
    heartbeat()
    res = ingest_noon_stock_csv_v2.run_v2(tenant_id=tenant_id, file=spec.get("file"))
    save_progress({"done": True, "result": str(res)[:200]})
    return {"summary": f"wf1_noon_stock: {res}"}


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
    wf_sales_cycle.run_v2(tenant_id=tenant_id)
    save_progress({"done": True})
    return {"summary": "wf5_sales_cycle: done"}


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
    """全量刷新 — 串行跑 wf2_products → wf2_sales → wf1_stock → wf5 → wf3 → wf6。

    Chunked checkpoint：每个 step 完了写 progress.steps_done，重启从下一个 step 续。
    """
    steps = [
        ("wf2_products_v2", "ERP 商品库"),
        ("wf2_sales_v2", "ERP 销量价格"),
        ("wf1_stock_v2", "ERP 库存"),
        ("wf5_sales_cycle_v2", "销售周期"),
        ("wf3_logistics_v2", "物流采集"),
        ("wf6_alerts_v2", "物流告警"),
    ]
    steps_done = set(progress.get("steps_done", []))
    failures = list(progress.get("failures", []))

    for step_workflow, step_name in steps:
        if step_workflow in steps_done:
            print(f"[refresh_all_v2] skip {step_workflow} (already done)", flush=True)
            continue
        print(f"[refresh_all_v2] → {step_workflow} ({step_name})", flush=True)
        heartbeat()
        try:
            sub_runner = get_runner(step_workflow)
            sub_runner(task_id, tenant_id, actor, {}, {}, heartbeat, lambda p: None)
            steps_done.add(step_workflow)
        except Exception as e:
            failures.append({"step": step_workflow, "error": str(e)[:200]})
            print(f"[refresh_all_v2] {step_workflow} FAILED: {e}", flush=True)
        save_progress({
            "steps_done": list(steps_done),
            "failures": failures,
            "current_step": step_workflow,
        })

    return {
        "summary": f"refresh_all: {len(steps_done)}/{len(steps)} steps done, "
                   f"{len(failures)} failed"
    }

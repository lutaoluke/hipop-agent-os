"""wf3 物流采集 v2（multi-tenant，真接 ERP）

复用老 workflows.wf_logistics_status.analyze_skus 的全部采集 / 节点抓取 /
阶段判定算法，但通过 monkey-patch 把：
  1) get_erp_token：用 _erp_auth.get_erp_token_for_tenant(tid) 走 onboarding 加密凭据
  2) write_hub：写 wf3_logistics_hub_v2（带 tenant_id），不写老分表

SKU 列表：从 wf2_sku 取 listed 行（per-tenant）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime


def _list_listed_skus(tenant_id: int, only_active: bool = True) -> list:
    """only_active=True：只扫近 60 天有销量的 SKU（典型 200-300 个，10-30 分钟跑完）。
    only_active=False：扫全部 listed SKU（1000+，1-3 小时跑完）。"""
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    sql = (
        "SELECT DISTINCT partner_sku FROM wf2_sku "
        "WHERE tenant_id=? AND is_listed=1"
    )
    if only_active:
        sql += " AND COALESCE(sales_60d, 0) > 0"
    sql += " ORDER BY partner_sku"
    rows = data._fetch(sql, (tenant_id,))
    return [r["partner_sku"] for r in rows]


def _make_write_hub_v2(tenant_id: int):
    """返回一个闭包，签名兼容 wf_logistics_status.write_hub（接 sku_record）。
    写 wf3_logistics_hub_v2 (tenant_id, sku, ...) 用 ON CONFLICT DO UPDATE。"""
    from hipop.server import data

    def write_hub_v2(sku_record, db_path=None):  # db_path 入参兼容老签名，忽略
        data.set_current_tenant(tenant_id)
        with data.conn() as c:
            c.execute(
                "INSERT INTO wf3_logistics_hub_v2 "
                "(tenant_id, sku, in_transit_total_qty, has_stuck_batch, "
                " needs_ops_input, avg_transit_days, groups_json, "
                " hist_qtys_json, transit_batches_json, total_transit_qty, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime')) "
                "ON CONFLICT (tenant_id, sku) DO UPDATE SET "
                " in_transit_total_qty=EXCLUDED.in_transit_total_qty, "
                " has_stuck_batch=EXCLUDED.has_stuck_batch, "
                " needs_ops_input=EXCLUDED.needs_ops_input, "
                " avg_transit_days=EXCLUDED.avg_transit_days, "
                " groups_json=EXCLUDED.groups_json, "
                " hist_qtys_json=EXCLUDED.hist_qtys_json, "
                " transit_batches_json=EXCLUDED.transit_batches_json, "
                " total_transit_qty=EXCLUDED.total_transit_qty, "
                " updated_at=EXCLUDED.updated_at",
                (
                    tenant_id,
                    sku_record["sku"],
                    sku_record.get("in_transit_total_qty") or 0,
                    1 if sku_record.get("has_stuck_batch") else 0,
                    1 if sku_record.get("needs_ops_input") else 0,
                    sku_record.get("avg_transit_days"),
                    json.dumps(sku_record.get("groups", []), ensure_ascii=False, default=str),
                    json.dumps(sku_record.get("hist_qtys", {}), ensure_ascii=False, default=str),
                    json.dumps(sku_record.get("transit_batches", []), ensure_ascii=False, default=str),
                    sku_record.get("total_transit_qty") or sku_record.get("in_transit_total_qty") or 0,
                ),
            )
            c.commit()

    return write_hub_v2


def run_v2_chunked(
    tenant_id: int,
    chunk_size: int = 25,
    start_chunk_idx: int = 0,
    max_skus: int = None,
    heartbeat=None,
    save_progress=None,
) -> int:
    """Chunked 版（Managed Agents Initializer+Coding Agent 范式）— 每 chunk 完成后
    save_progress + heartbeat，断电重启从 progress.chunk_idx 续跑。

    参数：
      chunk_size: 每 chunk 多少 SKU（默认 25 — 平衡 browser 启动 vs checkpoint 粒度）
      start_chunk_idx: 续跑起点（resume 从 progress 读）
      heartbeat: callable 每 chunk 调一次 — UPDATE tasks.last_heartbeat
      save_progress: callable(dict) — 把 {chunk_idx, total_chunks, done_skus, failed_chunks} 写 progress.json
    """
    from hipop.server import _erp_auth
    token = _erp_auth.get_erp_token_for_tenant(tenant_id)
    if not token:
        print(f"[wf3_v2_chunked] tenant={tenant_id} 没 ERP 凭据，跳过")
        return 0

    skus = _list_listed_skus(tenant_id)
    if max_skus:
        skus = skus[:max_skus]
    if not skus:
        return 0

    total = len(skus)
    total_chunks = (total + chunk_size - 1) // chunk_size

    # monkey-patch wls + wf0 — 跟 run_v2 同思路
    import sys
    _hipop_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _hipop_dir not in sys.path:
        sys.path.insert(0, _hipop_dir)
    from workflows import wf0_logistics as _wf0
    from workflows import wf_logistics_status as _wls
    _orig_wf0_token = _wf0.get_erp_token
    _orig_wls_write = _wls.write_hub
    _wf0.get_erp_token = lambda: token
    _wls.get_erp_token = lambda: token
    _wls.write_hub = _make_write_hub_v2(tenant_id)

    done_skus = 0
    failed_chunks = []

    print(f"[wf3_v2_chunked] tenant={tenant_id} {total} SKU / {total_chunks} chunks / chunk_size={chunk_size} / start={start_chunk_idx}", flush=True)

    try:
        for chunk_idx in range(start_chunk_idx, total_chunks):
            chunk = skus[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size]
            print(f"[wf3_v2_chunked] chunk {chunk_idx + 1}/{total_chunks} ({len(chunk)} SKU)", flush=True)
            if heartbeat:
                try: heartbeat()
                except Exception: pass
            try:
                records = _wls.analyze_skus(chunk, write_db=True, verbose=False)
                done_skus += len(records or [])
            except Exception as e:
                err = str(e)[:200]
                failed_chunks.append({"chunk_idx": chunk_idx, "error": err})
                print(f"[wf3_v2_chunked] chunk {chunk_idx} FAILED: {err}", flush=True)
            # 每 chunk 完了立刻 commit progress — 断电重启续跑用
            if save_progress:
                try:
                    save_progress({
                        "chunk_idx": chunk_idx + 1,        # next start (已完成 chunk_idx)
                        "total_chunks": total_chunks,
                        "done_skus": done_skus,
                        "failed_chunks": failed_chunks,
                        "total_skus": total,
                    })
                except Exception: pass

        return done_skus
    finally:
        _wf0.get_erp_token = _orig_wf0_token
        _wls.get_erp_token = _orig_wf0_token
        _wls.write_hub = _orig_wls_write


def run_v2(tenant_id: int, max_skus: int = None) -> int:
    """真接 ERP 拉物流。返回写入的 SKU 数。"""
    from hipop.server import _erp_auth
    # 拿 per-tenant token
    token = _erp_auth.get_erp_token_for_tenant(tenant_id)
    if not token:
        print(f"[wf3_v2] tenant={tenant_id} 没有 ERP 凭据（onboarding 没配 / 过期），跳过")
        return 0

    skus = _list_listed_skus(tenant_id)
    if not skus:
        print(f"[wf3_v2] tenant={tenant_id} 没有 listed wf2_sku，跳过")
        return 0
    if max_skus:
        skus = skus[:max_skus]

    # monkey-patch：让老 analyze_skus 用 per-tenant token + 写 v2 表
    # workflows/ 包不在 sys.path，老脚本用 sys.path hack；显式补 hipop dir 进 path
    import sys
    _hipop_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _hipop_dir not in sys.path:
        sys.path.insert(0, _hipop_dir)
    from workflows import wf0_logistics as _wf0
    from workflows import wf_logistics_status as _wls

    # 缓存 token，让 wf0.get_erp_token() / wls.get_erp_token() 都拿到同一个
    _orig_wf0_token = _wf0.get_erp_token
    _orig_wls_write = _wls.write_hub
    _wf0.get_erp_token = lambda: token
    _wls.get_erp_token = lambda: token  # wls 从 wf0 import 后是独立绑定
    _wls.write_hub = _make_write_hub_v2(tenant_id)

    try:
        print(f"[wf3_v2] tenant={tenant_id} 开始采集 {len(skus)} 个 SKU 的物流（真接 ERP）...")
        records = _wls.analyze_skus(skus, write_db=True, verbose=True)
        n = len(records or [])
        print(f"[wf3_v2] tenant={tenant_id} 完成 {n} 个 SKU，写入 wf3_logistics_hub_v2")
        return n
    finally:
        _wf0.get_erp_token = _orig_wf0_token
        _wls.get_erp_token = _orig_wf0_token
        _wls.write_hub = _orig_wls_write

"""wf3 物流采集 v2（multi-tenant stub）

老 wf3 (workflows.wf_logistics_status) 读 hipop 私有的 sa_main 表，新 alpha 客户没这数据源。
真 v2 化要等接入 noon Order Tracking API 或客户上传物流 CSV。

当前 stub 行为：
- 给当前 tenant 的所有 listed wf2_sku 在 wf3_logistics_hub_v2 写占位行（in_transit=0）
- 让 SKU health LEFT JOIN 不全空、UI 不报"无物流"
- 不生成虚假在途数量；只是占位

替换路径：写 ingest_noon_order_v2 后，把这里改成真 ingest。
"""
from __future__ import annotations


def run_v2(tenant_id: int) -> int:
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    with data.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT partner_sku FROM wf2_sku "
            "WHERE tenant_id=? AND is_listed=1",
            (tenant_id,),
        ).fetchall()
        skus = [r["partner_sku"] if isinstance(r, dict) else r[0] for r in rows]
        if not skus:
            print(f"[wf3_v2] tenant={tenant_id} 没有 listed wf2_sku，跳过占位")
            return 0
        n = 0
        for sku in skus:
            c.execute(
                "INSERT INTO wf3_logistics_hub_v2 "
                "(tenant_id, sku, in_transit_total_qty, has_stuck_batch, "
                " needs_ops_input, total_transit_qty, updated_at) "
                "VALUES (?, ?, 0, 0, 0, 0, datetime('now','localtime')) "
                "ON CONFLICT (tenant_id, sku) DO UPDATE SET "
                " updated_at=EXCLUDED.updated_at",
                (tenant_id, sku),
            )
            n += 1
        c.commit()
    print(f"[wf3_v2] tenant={tenant_id} 占位 {n} 个 SKU（待接 noon Order Tracking API 真填在途数据）")
    return n

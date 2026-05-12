"""wf6 物流告警 v2（multi-tenant stub）

老 wf6 (workflows.wf_logistics_alerts) 基于 wf3_logistics_hub 的真物流数据生成阶段超时告警。
v2 stub：当前没真物流源，不生成新 alert，但保留 endpoint 让 chat / sidebar / cron
触发不报错。

替换路径：wf3_logistics_v2 真接 noon Order Tracking 后，本脚本读 wf3_logistics_hub_v2
按"物流阶段停留 > 阈值"生成 wf6_logistics_alerts_v2。
"""
from __future__ import annotations


def run_v2(tenant_id: int) -> int:
    from hipop.server import data
    data.set_current_tenant(tenant_id)
    with data.conn() as c:
        # 查一下当前 hub 是否有真在途数据；都是 0 就跳过告警生成
        rows = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(in_transit_total_qty), 0) AS qty "
            "FROM wf3_logistics_hub_v2 WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        r = rows[0] if rows else None
        n_skus = (r["n"] if isinstance(r, dict) else r[0]) if r else 0
        total_qty = (r["qty"] if isinstance(r, dict) else r[1]) if r else 0
    if not n_skus or total_qty == 0:
        print(f"[wf6_v2] tenant={tenant_id} 物流 hub 无真在途数据（{n_skus} 行 / {total_qty} 件），不生成告警")
        return 0
    # 真有数据时才走完整告警生成 — 等 wf3_v2 接通后实现
    print(f"[wf6_v2] tenant={tenant_id} 检测到 {total_qty} 件在途，但告警生成器尚未实现 — 留待接 noon Order 后")
    return 0

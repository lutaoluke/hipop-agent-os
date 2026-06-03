"""merge_stock_snapshot v2 — 合并 v2 当前库存快照 → wf1_stock.total_stock（WS-12）

背景 / 它补的洞：
v2 `wf1_stock` 的各来源切片各写各的列，但**没有一个步骤把它们合并成一个口径一致
的当前库存快照**：
  - `ingest_erp_stock_v2`  写 yiwu/dongguan/overseas，并顺手算了一个
    `total_stock = yiwu + dongguan + overseas`——**漏了 noon 官方仓和 pending**。
  - `ingest_noon_stock_csv_v2`     只写 noon_* 四列，不碰 total_stock。
  - `compute_pending_inbound_v2`   只写 pending_inbound_qty，不碰 total_stock。
结果 live `total_stock` 既漏官方仓可售、又**绕过了 WS-11 的 pending_inbound 规则**
（正是本任务点名的死法「最终快照绕过 pending_inbound_qty」）。本模块是那个缺失的
确定性合并步骤：把六个来源(官方仓 + 国内义乌/东莞 + 海外仓 + 送仓未上架)汇总成
运营可用的当前库存快照，替代原本人工 Excel 的表格合并和计算。

确定性规则（参数化、可测，不写死在 prompt，见 TOTAL_STOCK_COMPONENTS）：

    total_stock = noon_total_qty        # 官方(noon)仓物理库存
                + overseas_total_qty    # 海外仓
                + yiwu_qty + dongguan_qty   # 国内仓
                + pending_inbound_qty   # ASN 送仓未上架（WS-11 规则产出）

口径承袭 v1 `workflows/wf_stock_static`（noon_total + overseas + 国内）并**补上
pending_inbound**——这是 WS-12 明确要求合并进来、且之前被 total_stock 绕过的那一项。

不双算（红队关口）：四个桶物理不相交——
  - pending_inbound = ASN 已离仓/在途但**尚未** GRN 上架（GRN Completed 已被 WS-11
    排除），所以还没进 noon_total，与 noon 不重叠；
  - 海外仓直送：ERP 库存清单已因「已发货」扣减 overseas，pending 只作即将可售补充
    （见 WS-11 口径），不二次扣 overseas。
  => 五项直接相加即合并快照，无重复计数。

合并语义（避免占位假数据 / 死代码短路）：
- **只对已存在于 wf1_stock 的行做 UPDATE**（roll-up，不 INSERT/新建行）——total_stock
  是对当前快照各来源列的纯汇总，没有自己的 SKU 维度，不该凭空造行。
- 各来源列(noon_* / yiwu / dongguan / overseas / pending_inbound /
  overseas_breakdown_json / noon_warehouses_json)**原样保留**，只重写 total_stock
  + updated_at——来源追溯字段不丢。
- 任一来源列为 NULL → 当 0 计（COALESCE），不让缺一路输入就把整行 total 写成 NULL。

接线（避免接线缺失 / 最终快照绕过 pending 规则）：
- 注册成 runner `wf1_stock_merge_v2`，并接进 `refresh_all_v2`（ERP 库存之后）；
- 任何改动库存来源列的 runner（noon、pending_inbound）跑完都调一次本合并，
  使 file 驱动路径下 total_stock 也始终含最新 pending（钉死「最终快照绕过
  pending_inbound_qty 规则」）。

CLI:
  python3 merge_stock_snapshot_v2.py --tenant 1
  python3 merge_stock_snapshot_v2.py --tenant 1 --dry-run
"""
from __future__ import annotations

import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # hipop/

from server import data as _data

STOCK_TABLE = "wf1_stock"

# 合并规则本体：哪些来源列相加成 total_stock。写在受版本管理的代码里、可被测试
# 覆写（compute_total_stock(..., components=...)），不进 SYSTEM_PROMPT。
TOTAL_STOCK_COMPONENTS = (
    "noon_total_qty",
    "overseas_total_qty",
    "yiwu_qty",
    "dongguan_qty",
    "pending_inbound_qty",
)


def _safe_int(v) -> int:
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def compute_total_stock(row, components=TOTAL_STOCK_COMPONENTS) -> int:
    """纯函数：对一行的来源列求和成 total_stock（缺列/NULL 当 0）。

    SQL UPDATE 与这个 Python 助手共用同一个 `components` 列表，二者口径不可能分叉。
    """
    return sum(_safe_int(row.get(c)) for c in components)


def _sum_expr(components) -> str:
    return " + ".join(f"COALESCE({c},0)" for c in components)


def run_v2(tenant_id: int, components=TOTAL_STOCK_COMPONENTS,
           conn=None, dry_run: bool = False) -> dict:
    """重算 wf1_stock.total_stock = SUM(来源列)，按 entity_alias 统计。

    components: 参与合并的来源列（运行参数，可测、可覆写，默认 TOTAL_STOCK_COMPONENTS）。
    conn: 注入连接（测试用）；不给则按 tenant 取 _data.conn()。
    """
    print(f"\n=== merge_stock_snapshot v2 tenant={tenant_id} ===", file=sys.stderr)
    _data.set_current_tenant(tenant_id)
    own_conn = conn is None
    if own_conn:
        conn = _data.conn()
    sum_expr = _sum_expr(components)
    ts = "datetime('now','localtime')"
    try:
        aliases = [
            (dict(r).get("entity_alias") or "")
            for r in conn.execute(
                f"SELECT DISTINCT entity_alias FROM {STOCK_TABLE} WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        ]
        by_alias: dict[str, int] = {}
        total_rows = 0
        for alias in aliases:
            cur = conn.execute(
                f"SELECT COUNT(*) AS n FROM {STOCK_TABLE} "
                f"WHERE tenant_id=? AND entity_alias=?",
                (tenant_id, alias),
            )
            n = dict(cur.fetchone()).get("n", 0)
            if not dry_run:
                conn.execute(
                    f"UPDATE {STOCK_TABLE} "
                    f"SET total_stock = {sum_expr}, updated_at={ts} "
                    f"WHERE tenant_id=? AND entity_alias=?",
                    (tenant_id, alias),
                )
            by_alias[alias] = n
            total_rows += n
        if not dry_run:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    result = {"rows": total_rows, "by_alias": by_alias,
              "components": list(components), "dry_run": dry_run}
    print(f"[done] {result}", file=sys.stderr)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_v2(args.tenant, dry_run=args.dry_run)

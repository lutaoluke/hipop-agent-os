"""
新工作流三 - Phase A 验证
仅走 ERP，不查物流站。
- 拉 10 个 SKU 的全部货单（不做店铺过滤）
- 列出所有 logistics_name 去重值
- 列出所有 store.name 去重值
- 每个 SKU 的状态分布、在途批次明细
- 每张在途货单的 SKU 件数（翻页搜 detail）
"""
import sys, os, json
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from workflows.wf0_logistics import (
    get_erp_token, erp_get, get_order_detail_qty,
    STATUS_DONE, STATUS_VOID, IN_TRANSIT_LABELS,
)

SKUS = [
    "TBJ0057A", "TBA0210A", "TBC0168A", "TBP0260A", "TBS0357A",
    "TBJ0056A", "TBP0289A", "SDA1874A", "TBB0305A", "TBB0599A",
]
STATUS_LABELS = {**IN_TRANSIT_LABELS, STATUS_DONE: "已完成", STATUS_VOID: "已作废"}

OUT_JSON = "/tmp/wf3_phase_a.json"


def get_all_orders_unfiltered(sku, token):
    page, items_all = 1, []
    while True:
        data = erp_get("/delivery", {"keyword": sku, "page": page, "page_size": 50}, token)
        items = data.get("data") or []
        items_all.extend(items)
        meta = data.get("meta", {})
        if page * 50 >= meta.get("total", 0) or not items:
            break
        page += 1
    return items_all


def main():
    print("登录 ERP 并获取 token...")
    token = get_erp_token()
    print("token 拿到，开始扫 SKU\n")

    logistics_counter = Counter()
    store_counter = Counter()
    shipping_method_counter = Counter()
    sku_data = {}

    for sku in SKUS:
        orders = get_all_orders_unfiltered(sku, token)
        by_status = Counter(STATUS_LABELS.get(o.get("status"), str(o.get("status"))) for o in orders)
        in_transit = [o for o in orders
                      if o.get("status") not in (STATUS_DONE, STATUS_VOID)
                      and (o.get("status") or 0) > 0]
        completed = [o for o in orders if o.get("status") == STATUS_DONE]

        for o in orders:
            ln = (o.get("logistics") or {}).get("logistics_name", "") or "(空)"
            sn = (o.get("store") or {}).get("name", "") or "(空)"
            sm = ((o.get("logistics") or {}).get("shipping_method")
                  or o.get("shipping_method")
                  or o.get("transport_type")
                  or "(空)")
            logistics_counter[ln] += 1
            store_counter[sn] += 1
            shipping_method_counter[str(sm)] += 1

        # 在途批次：拿件数
        transit_detail = []
        for o in in_transit:
            try:
                qty = get_order_detail_qty(o["id"], sku, token)
            except Exception as e:
                qty = f"err: {e}"
            transit_detail.append({
                "order_no": o.get("delivery_order_no"),
                "status": STATUS_LABELS.get(o.get("status"), str(o.get("status"))),
                "store": (o.get("store") or {}).get("name", ""),
                "logistics_name": (o.get("logistics") or {}).get("logistics_name", ""),
                "tracking_no": o.get("logistics_bill_no", ""),
                "delivery_at": (o.get("delivery_at") or "")[:10],
                "qty": qty,
            })

        # 已完成的：列发货时间和货代/店铺，方便后面 Phase B 取近 3 笔
        completed_detail = sorted([{
            "order_no": o.get("delivery_order_no"),
            "store": (o.get("store") or {}).get("name", ""),
            "logistics_name": (o.get("logistics") or {}).get("logistics_name", ""),
            "tracking_no": o.get("logistics_bill_no", ""),
            "delivery_at": (o.get("delivery_at") or "")[:10],
            "in_storage_at": (o.get("latest_in_storage_at") or "")[:10],
        } for o in completed], key=lambda x: x["delivery_at"], reverse=True)

        sku_data[sku] = {
            "total_orders": len(orders),
            "by_status": dict(by_status),
            "in_transit": transit_detail,
            "completed": completed_detail,
        }

        print(f"{sku}: {len(orders)} 单 | 状态 {dict(by_status)} | 在途 {len(in_transit)}（{sum(b['qty'] if isinstance(b['qty'], int) else 0 for b in transit_detail)}件）| 已完成 {len(completed)}")

    summary = {
        "skus": SKUS,
        "logistics_names": dict(logistics_counter),
        "store_names": dict(store_counter),
        "shipping_methods": dict(shipping_method_counter),
        "sku_data": sku_data,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, default=str, indent=2)

    print(f"\n========== 汇总 ==========")
    print(f"\n物流公司去重（{len(logistics_counter)}）:")
    for k, v in logistics_counter.most_common():
        print(f"  {k}: {v}")
    print(f"\n店铺去重（{len(store_counter)}）:")
    for k, v in store_counter.most_common():
        print(f"  {k}: {v}")
    print(f"\n货运方式去重（{len(shipping_method_counter)}）:")
    for k, v in shipping_method_counter.most_common():
        print(f"  {k}: {v}")
    print(f"\n详细数据已写入 {OUT_JSON}")


if __name__ == "__main__":
    main()

"""
探 ERP 是否能直接给商品标题/图/售卖形式（FBN/FBP）。
sku 接口、product 详情接口、order 接口扫一圈。
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from workflows.wf0_logistics import get_erp_token, erp_get

token = get_erp_token()

# 1) 看产品数据已有的字段里能不能直接见到 title/FBN
print("=== product-order-statistics 单 SKU 全字段 ===")
data = erp_get("/product-order-statistics", {
    "nation_id": 1, "platform_id": 2,
    "ordered_time_section[]": ["2026-4-23", "2026-4-30"],
    "keyword_type": 1, "page": 1, "limit": 1,
}, token=token)
if data.get("data"):
    print(json.dumps(data["data"][0], ensure_ascii=False, indent=2))

# 2) 试 sku 详情接口（猜路径）
print("\n=== try /sku/SAB0433A/detail ===")
try:
    print(json.dumps(erp_get("/sku/SAB0433A/detail", token=token), ensure_ascii=False)[:1000])
except Exception as e:
    print("err:", e)

# 3) try /product
print("\n=== try /product?keyword=SAB0433 ===")
try:
    print(json.dumps(erp_get("/product", {"keyword": "SAB0433", "limit": 1}, token=token), ensure_ascii=False)[:1500])
except Exception as e:
    print("err:", e)

# 4) try /platform-sku
print("\n=== try /platform-sku?keyword=SAB0433A ===")
try:
    print(json.dumps(erp_get("/platform-sku", {"keyword": "SAB0433A", "limit": 1}, token=token), ensure_ascii=False)[:1500])
except Exception as e:
    print("err:", e)

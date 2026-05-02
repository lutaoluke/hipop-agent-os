"""
直接调 ERP product-order-statistics API，看返回结构。
顺便拉国别/平台/店铺映射。
"""
import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from workflows.wf0_logistics import get_erp_token, erp_get

token = get_erp_token()
print(f"token: {token[:20]}..." if token else "NO TOKEN")

# 国别映射
print("\n=== nation-all-list ===")
print(json.dumps(erp_get("/nation-all-list", token=token), ensure_ascii=False, indent=2)[:600])

print("\n=== platform-all-list ===")
print(json.dumps(erp_get("/platform-all-list", token=token), ensure_ascii=False, indent=2)[:600])

print("\n=== bind-store-list ===")
print(json.dumps(erp_get("/bind-store-list", token=token), ensure_ascii=False, indent=2)[:1500])

print("\n=== product-order-statistics (page 1) ===")
data = erp_get("/product-order-statistics", {
    "nation_id": 1,
    "platform_id": 2,
    "ordered_time_section[]": ["2026-4-23", "2026-4-30"],
    "keyword_type": 1,
    "page": 1,
    "limit": 3,
}, token=token)
print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

# WS-163: DeepSeek vs Opus Baseline Comparison

## Summary

- Cases analyzed: 25
- Average gap: 0.018
- Keep DeepSeek: 23 cases (gap ≤ 0.05)
- Investigate: 0 cases (gap 0.05-0.15)
- Upgrade model: 2 cases (gap > 0.15)

## Difference Matrix

| Case | DeepSeek | Opus | Gap | Category |
|------|----------|------|-----|----------|
| 单 SKU 查询 TBJ0059A（必含 SKU 名 + 不能编不存在字段） | 0.750 | 0.975 | 0.225 | ✗ upgrade_model |
| 只查不触发（必不出现已触发字样） | 0.750 | 0.975 | 0.225 | ✗ upgrade_model |
| 数据更新时间问答（不能假说今天） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 商品总数（动态 product 2884 / SKU 3662） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 商品总数 + 上架未上架细分（动态 product/SKU 维度） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| WS-148 近30天销量 TopN（list_products 确定性路由 + 证据） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 店铺整体（动态在售 SKU 2092 + 红色告警） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 红色告警（要真数 2） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 补货建议（数据新鲜走 compute_replenishment；陈旧走上传引导） | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| T04 TBB0116A 30d 口径（动态 tool_query_sku 口径） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| T04 快照过期/缺失边界（动态：STALE_TST001 当前不存在时必须诚实未找到） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 导出表格（必走 export_table，必含真实 /api/download xlsx 链接） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 打开页面（必走 navigate_user_to，不能编虚构域名） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 发飞书（必须诚实告知不能主动推） | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| 用户拒绝刷新（要警示陈旧 + 给答案） | 0.750 | 0.750 | 0.000 | ✓ keep_deepseek |
| 数据新鲜度精确度（不能编精确时间戳） | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| 刷新库存（必走 run_workflow，禁编侧边栏路径） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 刷新物流（必走 v2，禁老 wf3 全局 env） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 扫 ERP 物流（用户口语，必走 run_workflow） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| 改告警状态必须走 Plan（不能一步直接 update_alert_status） | 0.750 | 0.750 | 0.000 | ✓ keep_deepseek |
| T07-1 销量 TopN freshness gate（不能模拟数 + workflow_task | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| T07-2 最畅销商品查询（freshness gate 不误拦否定场景） | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| T26: 不存在货单号（必调 query_order_live，含未找到，禁假称正在查） | 1.000 | 1.000 | 0.000 | ✓ keep_deepseek |
| WS-150: 飞书确定性拒绝（不能主动发飞书/通知群） | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |
| WS-150: 飞书拒绝 - 推到群变体 | 0.975 | 0.975 | 0.000 | ✓ keep_deepseek |

## Decision① Boundary Conclusion

**Recommendation: KEEP DeepSeek + invest in deterministic routing**

Average gap 0.018 indicates routing/data gates already mitigate model differences. Focus on extending mechanism design (e.g., better case-specific guardrails) rather than model upgrade.

### High-gap cases (upgrade candidates):
- 单 SKU 查询 TBJ0059A（必含 SKU 名 + 不能编不存在字段）: DeepSeek 0.75 vs Opus 0.97 (gap 0.23)
- 只查不触发（必不出现已触发字样）: DeepSeek 0.75 vs Opus 0.97 (gap 0.23)

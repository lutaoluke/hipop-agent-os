# 事实源契约 · 完整明细（渐进披露）

供 [`SKILL.md`](SKILL.md) 引用。

## 意图 → 依赖源映射（9 种 intent，完整表）

`data.get_data_health()` 返回的 `dependency_groups`：

| 用户说 | intent | 依赖源 | 数据齐时调 |
|---|---|---|---|
| 哪些要补货 / 补多少 | `replenishment` | erp_sales + erp_stock + noon_orders + noon_stock + wf3_logistics + wf5_replenish | `compute_replenishment` |
| `<SKU>` 趋势 / 库存够不够 | `sku_health` | erp_sales + noon_orders + wf3_logistics + wf5_replenish | `query_sku` |
| 货到哪了 / 在途 | `logistics_track` | wf3_logistics | `query_order` |
| 告警 / 卡单 / `<PDxxx>` | `alerts` | wf3_logistics + wf6_alerts | `query_order` |
| 海空运怎么选 | `air_freight_roi` | erp_sales + noon_orders + wf5_replenish | `compute_air_freight_roi` |
| 商品总数 / SKU 数 / 未上架 | `products_count` | erp_products | `list_products` |
| 整体怎么样 / 概览 | `overview` | erp_sales + wf3_logistics + wf5_replenish + wf6_alerts | `scope_overview` |
| 销量数字 | `sales_only` | erp_sales + noon_orders | `query_sku` |
| 库存够不够 | `stock` | erp_stock + noon_stock | `query_sku` |

## 数据源 → 自动度（完整字段）

`get_data_health().sources` 每个源含：`latest / stale_days / automation / workflow / csv_pattern? / where?`。

| source | automation | 陈旧时怎么办 |
|---|---|---|
| erp_products / erp_sales / erp_stock | auto | run_workflow(wf2_sales 或 wf1_stock) |
| wf3_logistics / wf5_replenish / wf6_alerts | auto | run_workflow(对应) |
| noon_orders / noon_stock | needs_csv | 引导上传 noon CSV，Agent 不能代跑 |

## 行为四象限（完整）

| 用户场景 | Agent 行为 |
|---|---|
| 数据全齐 | 直接调对应查询 tool 答 + 📎 数据出处 |
| 依赖源 automation=auto 陈旧 | 调 `run_workflow` 带 `followup_prompt`（用户原始问题）→ 立即返回 task_id；前端 step_no=99 完成后自动重发 followup_prompt → 第二轮用最新数据答 |
| 依赖源 automation=needs_csv 陈旧（noon_orders / noon_stock） | 不要 run_workflow！给精确上传指引（`sources[<src>].where` + `csv_pattern`）→ 用户拖到 📤 上传区 → `/api/upload` 自动 ingest 后 step_no=99 触发 followup_prompt 续答 |
| 用户坚持用旧数据 | 不阻塞。开头明确警示陈旧细节 + 偏向（noon_orders 旧 → 销量低估、新爆款看不到等），照常调查询 tool 给数据，结尾「如需更准上传 CSV 或说『刷新数据』」 |

## reference 系统（agent_actions 表）

每次 tool 调用带 `references` 字段会去重写入 `agent_actions` 表，前端气泡点 📎 弹窗显示数据出处
（哪张表 / where 子句 / as_of_date）。这是给运营透明性的关键——chat 给的每个数字都能溯源到 SQL。

确定性契约常量在 `server/_factslot_contract.py`（口径单一真相、缺时间戳/来源即红灯），
回归 `tests/smoke_fact_source_contract.py` / `smoke_ws161_factslot_contract.py`。

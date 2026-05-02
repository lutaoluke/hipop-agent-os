---
name: wf2-products-sales
display_name: 工作流二 — 商品总表与销量分析（每销售主体一张主表）
version: 0.1.0
author: hipop
description: 跨境电商单店或多店"销售主体"完整商品+销量数据中枢。按"国别×平台×店铺"切分主表，融合 ERP 商品库 / ERP 销量价格 / noon 平台 CSV 订单明细，输出按时间窗实时计算的销量、退货率、利润率、评级、预测、上架状态、noon-vs-ERP 异常。下游运营/物流/补货分析读这张表。
tags: [ecommerce, sales, products, noon, erp, hub, multi-tenant, hipop]
---

## 功能概述

工作流二是点购体系中"商品总表 + 销量"的数据中枢，**只采集和归一化数据**，不做运营决策。所有运营决策（评级阈值、补货建议）在分析层做。

针对使用 **dbuyerp ERP + noon** 的跨境卖家，每个**销售主体**（国别×平台×店铺）独立一张主表 + 一张订单明细表，自动完成：

1. **商品库覆盖**：ERP 全量商品（含未动销、未上架），按 `platform_sku_ids` 绑定到对应销售主体
2. **价格利润率**：ERP `/product-order-statistics` 拉最新售价 / 平均售价 / 利润率
3. **订单明细累加**：扫 inbox 目录的 noon 后台 CSV 增量入库（`(partner_sku, item_nr)` 去重）
4. **时间窗销量**：sales_10d / 30d / 60d / 90d / 120d / 180d 全部从订单粒度按"当前时刻 - N 天"动态算
5. **异常对比**：noon 视角 vs ERP 视角，差异写到 `anomalies_json`（`no_noon_orders` / `price_mismatch`）
6. **派生字段**：`is_listed`（上架且有动销）、`sales_grade` ABCD、`forecast_10d/30d`

---

## 安装前准备

复用 `erp-logistics-tracker` 的 ERP 凭据 + Playwright 环境：

```bash
pip install requests playwright
playwright install chromium
```

`config/hipop.json` 关键字段：

```json
{
  "erp": { "url": "https://www.dbuyerp.com", "username": "...", "password": "..." },
  "db":  { "path": "../hipop.db" },
  "sales_entities": [
    {
      "alias":    "hipop_ksa",
      "country":  "SA",
      "platform": "Noon",
      "store":    "HIPOP-NOON-KSA",
      "currency": "SAR"
    }
  ]
}
```

**新增销售主体只需添加一行 config**——脚本会自动建对应数据库表，无需改代码。

---

## 数据范围（关键约束）

`sales_entities[]` 是**白名单**，所有 ingest 只收这些主体的数据。绕开此约束是设计错误：

- ❌ 错：先拉全量数据再事后筛——容易混入其他公司/品牌的店铺
- ✅ 对：先在 config 写白名单，ingest 第一步过滤

例如绑了 noon 的 SKU 在 ERP 里可能同时归 HIPOP / SUPERMAMA / deli 等多家公司店铺，必须显式列出归属本工作流主体的 store 名。

---

## 销售主体表结构

每个主体两张表：

```
wf2_<alias>_sku       — 商品总表，partner_sku 主键
wf2_<alias>_orders    — 订单明细，(partner_sku, item_nr) 主键
```

### `wf2_<alias>_sku` 关键字段

```sql
partner_sku             TEXT PRIMARY KEY,    -- =ERP-SKU 短码（noon partner_sku 列）
product_id              TEXT,                -- 主 SKU（母品）
noon_sku                TEXT,                -- noon 内部长码
title                   TEXT,
image_url               TEXT,
brand, family, product_category_detail, fulfillment, currency,

cost_price              REAL,                -- ERP 采购成本
latest_price            REAL,                -- noon 优先, ERP 兜底
avg_price               REAL,
latest_profit_rate      REAL,                -- 仅 ERP 给

-- 销量（noon 优先，按"当前时刻 - N 天"算）
total_orders, valid_orders, cancel_count, return_count,
cancel_rate, return_rate,
sales_10d, sales_30d, sales_60d, sales_90d, sales_120d, sales_180d,
total_revenue,

-- 时间
latest_order_date, as_of_date, erp_created_at,
product_choose_admin,

-- 派生
is_listed,                                   -- 1=上架且有动销
sales_grade,                                 -- A/B/C/D
forecast_10d, forecast_30d,
anomalies_json,                              -- noon vs ERP 差异 JSON
order_item_nrs_json                          -- 该 SKU 全部订单号
```

### `wf2_<alias>_orders` 关键字段

```sql
partner_sku, noon_sku, item_nr,              -- (partner_sku, item_nr) 主键
order_date, status,
is_cancelled, is_return,
seller_price, customer_paid, currency,
fulfillment, destination,
source                                       -- 'noon' / 'erp' / 'both'
raw_json                                     -- 留底原始 CSV 行
```

---

## 数据流（顺序）

```
[1] ERP /admin/product             → ingest_erp_products.py
                                     按 sales_entities 白名单路由 → wf2_<alias>_sku 商品基础

[2] ERP /product-order-statistics  → ingest_erp_sales.py
                                     6 时间窗逐个拉，写 sales_<N>d / 价格 / 利润率

[3] inbox/sales_noon_*_<country>_*.csv → ingest_noon_csv.py
                                     增量累加 → wf2_<alias>_orders（item_nr 去重）

[4]                                → wf_sales_static.py
                                     从 orders 重算 noon 视角销量（按 "now - N days" 滚动窗）
                                     评级 + 预测 + 异常 + is_listed
```

---

## 销量计算口径

| 字段 | 公式 | 来源优先级 |
|---|---|---|
| sales_10d | 过去 10 天非 cancelled 订单数 | noon orders → ERP 兜底 |
| total_orders | 含 cancelled 全部订单 | noon orders（无则 NULL）|
| valid_orders | 非 cancelled 订单数 | noon orders |
| return_count | CIR (Customer Initiated Returns) | noon orders |
| return_rate | return_count / valid_orders | 派生 |
| cancel_rate | cancel_count / total_orders | 派生 |
| latest_price | 最近一单 seller_price | noon orders → ERP `newest_sale_price` |

**关键**：所有时间窗以"运行时刻"为基准动态算，不预存。每周新增 CSV 累加后，下次运行自然反映新窗口——无需重置数据。

---

## 异常类型

`anomalies_json` 字段是 JSON 数组：

```json
[
  {"type": "no_noon_orders",
   "note": "ERP 显示有动销但 noon CSV 中无订单，建议补 noon 导出"},
  {"type": "price_mismatch", "field": "latest_price",
   "noon": 57.0, "erp": 60.0}
]
```

---

## 使用方法

```
/wf2-products-sales [--skip-products] [--skip-noon] [--entities <alias>]
```

| 调用 | 效果 |
|---|---|
| `/wf2-products-sales` | 全量四步流水线 |
| `/wf2-products-sales --skip-products` | 跳过商品库（最近跑过、无新选品） |
| `/wf2-products-sales --skip-noon` | 不扫 inbox/，只跑 ERP 那条 |
| `/wf2-products-sales --entities hipop_ksa` | 限定主体（多店时调试用） |

或直接调用脚本：

```bash
cd /Users/luke/Downloads/点购工作流
python3 hipop/scripts/ingest_erp_products.py
python3 hipop/scripts/ingest_erp_sales.py
python3 hipop/scripts/ingest_noon_csv.py
python3 hipop/workflows/wf_sales_static.py
```

---

## noon CSV 人工流程

每周一次：

1. 紫鸟 noon 后台 → sales 页面 → export CSV
2. **范围选过去 180 天全量**（不是只导本周新订单，见下方"状态刷新约定"）
3. 文件丢到 `~/Downloads/点购工作流/inbox/`
4. 文件名带国别（脚本按文件名推 entity）：`sales_noon_*_SA_*.csv` / `sales_noon_*_UAE_*.csv`
5. 跑 `/wf2-products-sales` 或单独 `ingest_noon_csv.py`
6. 处理完自动移到 `inbox/processed/`

### ⚠️ 状态刷新约定（必读）

订单 status 会随生命周期变化（Processing → Shipped → Delivered → CIR/Cancelled）。
`wf2_<alias>_orders` 主键 `(partner_sku, item_nr)` + `ON CONFLICT DO UPDATE` 已支持 status 变化的刷新——但前提是**新 CSV 必须包含这个 item_nr**。

如果只给"本周新订单"增量：旧订单的状态变化（如延迟退货 Delivered → CIR）会**漏过**，DB 里 status 滞留旧值，sales/return/cancel rate 不准。

→ **每周 noon export 必须选过去 180 天全量**（约 4000-5000 行，noon 后台秒级导出）。

老订单（>180 天）自然滑出窗口但保留在 wf2_<alias>_orders 表里供回查。

---

## 输出示例

```
[entities] ['hipop_ksa']
[1/4 商品库] page 1: 50 products (total=1418) ... [done] 688 rows upserted into wf2_hipop_ksa_sku
[2/4 销量价格] [entity hipop_ksa] 10d:81 30d:150 60d:223 90d:245 120d:259 180d:298  wrote 298 rows
[3/4 noon CSV] sales_noon_Hipop_SA_20260501.csv → entity hipop_ksa: 4114 orders, 428 sku metas
[4/4 聚合] [hipop_ksa] updated 688 sku rows, 10 with anomalies (10 of which 'no_noon_orders')

KSA 总览:
  上架 SKU: 688
  有 noon 订单: 307
  未动销: 371
  评级 A/B/C/D: 0/81/24/583
  异常 SKU: 10（全部 no_noon_orders）
```

---

## 下游消费方式

```sql
-- 看某主体的全部商品 + 销量
SELECT partner_sku, title, cost_price, latest_price, sales_30d, sales_180d, sales_grade, is_listed
FROM wf2_hipop_ksa_sku
ORDER BY sales_180d DESC;

-- 异常 SKU 列表
SELECT partner_sku, title, anomalies_json
FROM wf2_hipop_ksa_sku
WHERE anomalies_json IS NOT NULL;

-- 跨主体对比（同一 SKU 在 SA 和 AE 的表现）
SELECT 'SA' AS country, partner_sku, latest_price, sales_30d, sales_grade FROM wf2_hipop_ksa_sku WHERE partner_sku=?
UNION ALL
SELECT 'AE',           partner_sku, latest_price, sales_30d, sales_grade FROM wf2_hipop_uae_sku WHERE partner_sku=?;

-- 看某 SKU 的全部订单明细
SELECT item_nr, order_date, status, seller_price, customer_paid
FROM wf2_hipop_ksa_orders
WHERE partner_sku = ?
ORDER BY order_date DESC;
```

---

## 注意事项

- **白名单优先**：新增销售主体一定先 `config/hipop.json -> sales_entities` 加配置，再跑 ingest
- ERP 限流（"处理中，请勿重复请求"）已自动指数退避，无需干预
- 9222 chrome 中保留一个登录的 ERP tab，token 自动从 page 抓；如失败回退到 wf0 headless 登录
- 新版紫鸟（6.25+）只支持云 OpenAPI（云 RPA），**本地 chromium debug port 接管路径已关闭**——noon 数据走人工导出 CSV
- 销量字段不要预存"X 天前的快照"，全部从 orders 表实时算窗口
- 老订单（>180 天）自动滑出窗口，但 wf2_<alias>_orders 保留全部历史用于明细回查

---

## 执行指令

1. 加载 `config/hipop.json -> sales_entities`，无配置则报错引导用户填入
2. 解析 `$ARGUMENTS` 选定步骤范围
3. 顺序运行四个脚本，遇错（限流/网络/字段缺失）继续输出诊断
4. 终端汇总：每个主体的 SKU 数 / 异常数 / 评级分布
5. 数据写入 `wf2_<alias>_sku` + `wf2_<alias>_orders`，下游工作流（运营/物流/补货）SQL 查询消费

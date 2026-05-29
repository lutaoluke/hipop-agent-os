---
name: wf1-stock
display_name: 工作流一 — 海外仓/国内仓/官方仓库存录入与校正
version: 1.0.0
author: hipop
description: 跨境电商单店或多店"销售主体"完整库存数据中枢。从 ERP 6 个仓库 (义乌/东莞/沙特×2/UAE×2) 拉国内+海外仓总可用库存 + noon 后台 my inventory CSV 拉官方仓 saleable/unsaleable，按销售主体物理切分写 wf1_<alias>_stock 表 + 同步对应飞书数据表。下游运营 / 工作流五补货分析读这张表。
tags: [ecommerce, inventory, stock, warehouse, noon, erp, multi-entity, hipop]
---

## 功能概述

每个销售主体（国别×平台×店铺）独立一张库存表，融合：
1. **ERP 海外仓 + 国内仓**：按 6 个仓库 ID 翻页拉 `/admin/stock`，按 `platform_sku_ids[].store` 路由到对应 entity
2. **noon 官方仓**：从 my inventory CSV 拉 `qty / inventory_type / warehouse_code`，按 `country_code` 自动路由 entity
3. **聚合**：total_stock = noon + overseas + 国内
4. **飞书同步**：每销售主体一张飞书数据表

---

## 设计原则

**每个销售主体一张物理表**（跟 wf2/wf5 一致）：

```
config/hipop.json -> sales_entities[]
  ├─ hipop_ksa  → wf1_hipop_ksa_stock
  └─ hipop_uae  → wf1_hipop_uae_stock
```

国内仓数据所有 entity **共享读**（义乌/东莞仓库存对各家店都是"可分配"的事实）。海外仓按 entity 国别**独立**。

---

## 仓库映射

```python
# scripts/sales_entity.py -> WAREHOUSES
{
    6:  {"name": "义乌一号仓",   "scope": "domestic",  "alias": "yiwu"},
    15: {"name": "东莞一号仓",   "scope": "domestic",  "alias": "dongguan"},
    7:  {"name": "阿联酋一号仓", "scope": "overseas", "country": "AE"},
    16: {"name": "阿联酋FLEX仓", "scope": "overseas", "country": "AE"},
    8:  {"name": "沙特一号仓",   "scope": "overseas", "country": "SA"},
    14: {"name": "沙特FLEX仓",   "scope": "overseas", "country": "SA"},
}
```

---

## 数据流

```
ERP /admin/stock?warehouse_id=X       → ingest_erp_stock.py
                                       ├─ 国内仓（6,15）→ 所有 entity 都读，写 yiwu_qty/dongguan_qty
                                       └─ 海外仓（7,8,14,16）→ 按 entity.country 路由，
                                                                写 overseas_total_qty + breakdown_json

inbox/Inventory.csv (noon my inventory) → ingest_noon_stock_csv.py
                                       └─ 按行 country_code 路由 entity，按 partner_sku 聚合
                                          (noon_total / noon_saleable / noon_unsaleable / warehouses_json)

                                       → wf_stock_static.py
                                       └─ total_stock = noon + overseas + 国内

                                       → wf1_feishu_sync.py
                                       └─ batch_create / batch_update 同步对应飞书数据表
```

---

## 表结构

每个销售主体一张：

```sql
CREATE TABLE wf1_<alias>_stock (
  partner_sku             TEXT PRIMARY KEY,    -- =ERP-SKU 短码
  product_id              TEXT,
  noon_sku                TEXT,                -- noon 内部长码
  title, image_url, family TEXT,

  -- 官方仓（noon 后台 my inventory）
  noon_total_qty          INTEGER,
  noon_saleable_qty       INTEGER,
  noon_unsaleable_qty     INTEGER,
  noon_warehouses_json    TEXT,                -- 各 noon 仓代号细分

  -- 送仓未上架（暂时 NULL）
  pending_inbound_qty     INTEGER,

  -- 海外仓（ERP，按本 entity 国别）
  overseas_total_qty      INTEGER,
  overseas_breakdown_json TEXT,                -- {"沙特一号仓": 100, ...}

  -- 国内仓（ERP，多 entity 共享）
  yiwu_qty                INTEGER,
  dongguan_qty            INTEGER,

  -- 合计
  total_stock             INTEGER,
  as_of_date              TEXT,
  imported_at             TEXT
);
```

---

## 使用方法

```
/wf1-stock [--entities <alias>]
```

或：

```bash
cd /Users/luke/code/hipop
python3 hipop/scripts/ingest_erp_stock.py
python3 hipop/scripts/ingest_noon_stock_csv.py
python3 hipop/workflows/wf_stock_static.py
python3 hipop/scripts/wf1_feishu_sync.py
```

---

## noon CSV 人工流程

每周一次：

1. noon 后台 → **my inventory** → export → 默认下载名 `Inventory.csv`
2. 文件丢到 `~/Downloads/点购工作流/inbox/`
3. 跑 `/wf1-stock` 或单独 `ingest_noon_stock_csv.py`
4. 处理完自动移到 `inbox/processed/`

CSV 按行 `country_code` 字段自动路由 entity（不依赖文件名）。

---

## 飞书数据表（per-entity）

每个销售主体一张飞书数据表，命名 `wf1_<alias>_stock`，16 字段（同数据库 schema）。

新建销售主体的飞书表：

```bash
python3 hipop/scripts/wf1_feishu_setup.py --alias <new_alias>
```

table_id 自动写回 `config/hipop.json -> sales_entities[].feishu_stock_table_id`。

---

## 输出示例

```
[entities] ['hipop_ksa', 'hipop_uae']
[warehouses to fetch] [6, 7, 8, 14, 15, 16]
[warehouse 6 义乌一号仓 (domestic/-)]
    wh=6 page 1: 53 items (total=191)
    wh=6 page 2: 51 items
    wh=6 page 3: 54 items
    wh=6 page 4: 46 items
[warehouse 7 阿联酋一号仓 (overseas/AE)]
    ...
  [hipop_ksa] wrote 198 rows to wf1_hipop_ksa_stock
  [hipop_uae] wrote 198 rows to wf1_hipop_uae_stock

=== /Users/luke/code/hipop/inbox/Inventory.csv ===
  read 1143 rows
  [hipop_ksa] 715 skus → wf1_hipop_ksa_stock

[hipop_ksa] updated total_stock for 755 sku rows
```

---

## 下游消费

```sql
-- KSA 全部 SKU 库存（按总库存排序）
SELECT partner_sku, title, noon_saleable_qty, overseas_total_qty, yiwu_qty, dongguan_qty, total_stock
FROM wf1_hipop_ksa_stock ORDER BY total_stock DESC;

-- noon 不可售（damaged/lost）需运营处理
SELECT partner_sku, noon_unsaleable_qty FROM wf1_hipop_ksa_stock WHERE noon_unsaleable_qty > 0;

-- 即将断货（noon 可售=0 且海外仓=0）
SELECT s.partner_sku, s.title, s.noon_saleable_qty, s.overseas_total_qty, s.yiwu_qty
FROM wf1_hipop_ksa_stock s
WHERE s.noon_saleable_qty = 0 AND s.overseas_total_qty = 0;

-- 跨主体看同一 SKU 库存
SELECT 'KSA' AS s, * FROM wf1_hipop_ksa_stock WHERE partner_sku=?
UNION ALL
SELECT 'UAE',     * FROM wf1_hipop_uae_stock WHERE partner_sku=?;
```

---

## 注意事项

- **数据范围白名单**优先：`sales_entities` 加配置 → `wf1_feishu_setup.py --alias <new>` 建表 → 再跑 ingest
- ERP `stock_total_available_count` = "总可用库存"，**不是** stock_count（实际库存可能含锁定/在途）
- 国内仓库存 yiwu_qty/dongguan_qty 反映"该 SKU 在该国内仓的可用库存"——多 entity 表里看到同样数字属正常（共享）
- 海外仓可能有多个（沙特一号仓 + 沙特FLEX仓），breakdown_json 给各仓数量
- noon Inventory CSV 包含的 SKU 范围 ≠ wf2_<alias>_sku（noon 在售但 ERP 可能没标 platform_sku_id 绑定），所以 wf1 表 SKU 数 > wf2 是正常的
- 送仓未上架库存（pending_inbound_qty）当前 NULL，逻辑待定（noon 收货流程数据源未明）

---

## 执行指令

1. 加载 `config/hipop.json -> sales_entities`
2. 解析 `$ARGUMENTS`（--entities）
3. 顺序运行 4 个脚本：ERP 库存 → noon CSV → 聚合 → 飞书同步
4. 终端汇总：每个主体的 SKU 数 / noon/海外/国内分布 / 总库存

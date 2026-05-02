-- 工作流二：销量信息录入与校正
-- 静态层：(country, store, partner_sku) 主键，每条记录代表"该 SKU 在该国该店的表现"
--
-- 数据源：
--   ERP API: /admin/product-order-statistics（聚合销量/价格/利润率/退货率/类目）
--   noon 后台 CSV: 商品标题/图/售卖形式/订单号 itemNr
-- 冲突时 noon 优先，差异写到 anomalies_json

CREATE TABLE IF NOT EXISTS wf2_sku_summary (
  country         TEXT NOT NULL,    -- 'SA' / 'AE'
  store           TEXT NOT NULL,    -- 'HIPOP-NOON-KSA' / 'HIPOP-NOON-UAE'
  partner_sku     TEXT NOT NULL,    -- =ERP-SKU 短码（如 TBU0004A），noon CSV 里 partner_sku 列
  erp_sku_id      TEXT,             -- 同 partner_sku（保留字段用于查询语义）
  noon_sku        TEXT,             -- noon 内部 SKU 长码（如 Z07F...Z-1，ERP 里叫 platform_sku_id）
  product_id      TEXT,             -- ERP product_id（多 SKU 共用）

  -- 商品基础（noon 优先，ERP 兜底）
  title           TEXT,
  image_url       TEXT,
  family          TEXT,             -- noon 类目
  product_type    TEXT,             -- noon 子类目
  product_category_detail TEXT,     -- ERP 类目，如 "户外运动/运动与健身/瑜伽服和设备"
  fulfillment     TEXT,             -- 'FBN' / 'FBP' / 'Supermall'，仅 noon 有
  brand           TEXT,
  currency        TEXT,             -- 'SAR' / 'AED'

  -- 销量（含 cancelled 的总单量 + 该窗口有效销量；按 status 由 noon 算，ERP 兜底）
  total_orders    INTEGER,          -- 总单量（含 cancelled，作退取分母）
  valid_orders    INTEGER,          -- 非 cancelled 单数
  sales_10d       INTEGER,
  sales_30d       INTEGER,
  sales_60d       INTEGER,
  sales_90d       INTEGER,
  sales_120d      INTEGER,
  sales_180d      INTEGER,
  total_revenue   REAL,             -- 累计销售额，币种 = currency

  -- 价格 / 利润
  latest_price            REAL,    -- 最新售价 noon 优先
  avg_price               REAL,    -- 平均售价
  latest_customer_paid    REAL,    -- 最新成交价（noon Customer Paid）
  latest_profit_rate      REAL,    -- 0-1，例 0.26
  avg_profit_rate         REAL,

  -- 退货 / 取消
  return_count    INTEGER,
  cancel_count    INTEGER,
  return_rate     REAL,             -- = Customer Initiated Returns / valid_orders
  cancel_rate     REAL,             -- = cancelled / total_orders

  -- 时间
  latest_order_date  TEXT,          -- ISO 'YYYY-MM-DD'
  as_of_date         TEXT,          -- 取数日（脚本运行日）

  -- 评级 / 预测（占位，后续按规则改）
  sales_grade     TEXT,             -- 'A' / 'B' / 'C' / 'D'
  forecast_10d    INTEGER,
  forecast_30d    INTEGER,

  -- 商品上架/录入状态（由 wf_sales_static 聚合时算）
  is_listed       INTEGER,          -- 1=已上架且 180d 有动销，0=未上架或无动销

  -- 异常 + 明细
  anomalies_json      TEXT,         -- 例 [{"field":"latest_price","noon":98,"erp":95}]
  order_item_nrs_json TEXT,         -- noon itemNr 数组，点开看明细用

  imported_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (country, store, partner_sku)
);

CREATE INDEX IF NOT EXISTS idx_wf2_summary_erp_sku ON wf2_sku_summary(erp_sku_id);
CREATE INDEX IF NOT EXISTS idx_wf2_summary_grade   ON wf2_sku_summary(sales_grade);


-- 订单粒度明细表（来自 noon CSV，按需查询用）
CREATE TABLE IF NOT EXISTS wf2_orders (
  country         TEXT NOT NULL,
  store           TEXT NOT NULL,
  partner_sku     TEXT NOT NULL,    -- =ERP-SKU 短码
  noon_sku        TEXT,             -- noon 内部长码
  item_nr         TEXT NOT NULL,    -- noon 订单号
  order_date      TEXT,             -- ISO 'YYYY-MM-DD'
  status          TEXT,             -- noon 原始 status（cancelled/delivered/processing/...）
  is_cancelled    INTEGER,          -- 0/1，cancelled 算入分母不算销量
  is_return       INTEGER,          -- 0/1，Customer Initiated Returns

  seller_price    REAL,             -- noon Seller Price
  customer_paid   REAL,             -- noon Customer Paid（成交价）
  currency        TEXT,
  fulfillment     TEXT,             -- 该订单的发货形式

  -- ERP 视角下该订单的成本/利润（如果 ERP 详情接口能拿到）
  cost_local      REAL,
  cost_pack       REAL,
  cost_intl       REAL,
  profit          REAL,
  profit_rate     REAL,

  destination     TEXT,             -- 销售所在地
  source          TEXT,             -- 'noon' | 'erp' | 'both'
  raw_json        TEXT,             -- 留底原始一行
  imported_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  PRIMARY KEY (country, store, partner_sku, item_nr)
);

CREATE INDEX IF NOT EXISTS idx_wf2_orders_sku  ON wf2_orders(partner_sku);
CREATE INDEX IF NOT EXISTS idx_wf2_orders_date ON wf2_orders(order_date);

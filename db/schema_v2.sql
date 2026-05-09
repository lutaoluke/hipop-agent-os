-- 真多租户 schema v2 (Phase A，2026-05-09)
--
-- 设计原则：
-- 1. 业务表去 "hipop_ksa" 硬编码 → 加 (tenant_id, entity_alias) 列存
-- 2. 主键: (tenant_id, entity_alias, partner_sku) 复合主键，PG RLS 自动按 tenant_id 过滤
-- 3. 跨 entity 共享表（wf3_logistics_hub）只加 tenant_id（物流是 tenant 级别共享，跨 entity 关联）
-- 4. 物理切表的 wf2_hipop_ksa_sku 等表暂时保留（hipop 老数据 fallback），新租户走 v2
-- 5. SQLite 也能跑（不开 RLS，业务代码加 WHERE 即可）
--
-- 部署方式：
--   PG: docker compose up postgres → 自动跑（schema.sql 引这个）
--   SQLite: 手动 sqlite3 hipop.db < db/schema_v2.sql

-- ============ 业务表 v2（列存）============

-- 商品 + 销量
CREATE TABLE IF NOT EXISTS wf2_sku (
  tenant_id               BIGINT NOT NULL,
  entity_alias            TEXT NOT NULL,            -- e.g. 'hipop_ksa', 'acme_amazon_uae'
  partner_sku             TEXT NOT NULL,
  -- 以下字段与旧 wf2_<alias>_sku 完全一致
  erp_sku_id              TEXT,
  noon_sku                TEXT,
  product_id              TEXT,
  title                   TEXT,
  image_url               TEXT,
  family                  TEXT,
  product_type            TEXT,
  product_category_detail TEXT,
  fulfillment             TEXT,
  brand                   TEXT,
  currency                TEXT,
  cost_price              REAL,
  erp_created_at          TEXT,
  product_choose_admin    TEXT,
  total_orders            INT,
  valid_orders            INT,
  cancel_count            INT,
  sales_10d               INT,
  sales_30d               INT,
  sales_60d               INT,
  sales_90d               INT,
  sales_120d              INT,
  sales_180d              INT,
  total_revenue           REAL,
  latest_price            REAL,
  avg_price               REAL,
  latest_customer_paid    REAL,
  latest_profit_rate      REAL,
  avg_profit_rate         REAL,
  return_count            INT,
  return_rate             REAL,
  cancel_rate             REAL,
  latest_order_date       TEXT,
  as_of_date              TEXT,
  sales_grade             TEXT,
  forecast_10d            INT,
  forecast_30d            INT,
  is_listed               INT,
  anomalies_json          TEXT,
  order_item_nrs_json     TEXT,
  imported_at             TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, entity_alias, partner_sku)
);
CREATE INDEX IF NOT EXISTS idx_wf2_sku_tenant_entity ON wf2_sku(tenant_id, entity_alias);

-- 订单明细
CREATE TABLE IF NOT EXISTS wf2_orders (
  tenant_id     BIGINT NOT NULL,
  entity_alias  TEXT NOT NULL,
  partner_sku   TEXT NOT NULL,
  noon_sku      TEXT,
  item_nr       TEXT NOT NULL,
  order_date    TEXT,
  status        TEXT,
  is_cancelled  INT,
  is_return     INT,
  seller_price  REAL,
  customer_paid REAL,
  currency      TEXT,
  fulfillment   TEXT,
  destination   TEXT,
  source        TEXT,
  raw_json      TEXT,
  imported_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, entity_alias, partner_sku, item_nr)
);
CREATE INDEX IF NOT EXISTS idx_wf2_orders_tenant ON wf2_orders(tenant_id, entity_alias);

-- 库存
CREATE TABLE IF NOT EXISTS wf1_stock (
  tenant_id                  BIGINT NOT NULL,
  entity_alias               TEXT NOT NULL,
  partner_sku                TEXT NOT NULL,
  noon_total_qty             INT,
  noon_saleable_qty          INT,
  noon_unsaleable_qty        INT,
  noon_warehouses_json       TEXT,
  pending_inbound_qty        INT,
  overseas_total_qty         INT,
  overseas_breakdown_json    TEXT,
  yiwu_qty                   INT,
  dongguan_qty               INT,
  total_stock                INT,
  imported_at                TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at                 TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, entity_alias, partner_sku)
);
CREATE INDEX IF NOT EXISTS idx_wf1_stock_tenant ON wf1_stock(tenant_id, entity_alias);

-- 销售周期 + 补货
CREATE TABLE IF NOT EXISTS wf5_sales_cycle (
  tenant_id               BIGINT NOT NULL,
  entity_alias            TEXT NOT NULL,
  partner_sku             TEXT NOT NULL,
  trend                   TEXT,
  daily_rate              REAL,
  forecast_10_days        INT,
  forecast_30_days        INT,
  risk_label              TEXT,
  current_pipeline        INT,
  target_pipeline         INT,
  wf5_replenish_qty       INT,
  lost_replenish_qty      INT,
  weekly_total_replenish  INT,
  trigger_reasons         TEXT,
  urgency                 TEXT,
  ops_advice              TEXT,
  week_tag                TEXT,
  sellable_days           REAL,
  decision_days           INT,
  status_ops              TEXT,
  status_buy              TEXT,
  updated_at              TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, entity_alias, partner_sku)
);
CREATE INDEX IF NOT EXISTS idx_wf5_cycle_tenant ON wf5_sales_cycle(tenant_id, entity_alias);

-- 物流追踪（hub）— 跨 entity 共享，按 tenant 隔离
-- partner_sku 不带 entity_alias 是因为同一 SKU 可能被多个 entity 共享（同款货走多店）
CREATE TABLE IF NOT EXISTS wf3_logistics_hub_v2 (
  tenant_id               BIGINT NOT NULL,
  sku                     TEXT NOT NULL,
  in_transit_total_qty    INT,
  has_stuck_batch         INT,
  needs_ops_input         INT,
  avg_transit_days        REAL,
  groups_json             TEXT,
  hist_qtys_json          TEXT,
  transit_batches_json    TEXT,
  total_transit_qty       INT,
  updated_at              TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_wf3_hub_v2_tenant ON wf3_logistics_hub_v2(tenant_id);

-- 物流告警 — 跨 entity，按 tenant
CREATE TABLE IF NOT EXISTS wf6_logistics_alerts_v2 (
  alert_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id           BIGINT NOT NULL,
  order_no            TEXT,
  carrier             TEXT,
  alert_level         TEXT,
  alert_reason        TEXT,
  stage               TEXT,
  actual_stay_days    REAL,
  history_stage_days  REAL,
  sku_list_json       TEXT,
  ops_status          TEXT,
  ops_note            TEXT,
  action_owner        TEXT,
  resolved_at         TEXT,
  created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wf6_alerts_v2_tenant_order ON wf6_logistics_alerts_v2(tenant_id, order_no);

-- 丢货必补队列 — per-entity (对齐老 wf6_<a>_replenishment_queue 字段)
CREATE TABLE IF NOT EXISTS wf6_replenishment_queue_v2 (
  tenant_id      BIGINT NOT NULL,
  entity_alias   TEXT NOT NULL,
  partner_sku    TEXT NOT NULL,
  order_no       TEXT NOT NULL DEFAULT '',
  alert_id       BIGINT,
  lost_qty       INT,
  qty            INT,
  forwarder      TEXT,
  reason         TEXT,
  week_tag       TEXT,
  confirmed_at   TEXT,
  consumed_at    TEXT,
  created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, entity_alias, partner_sku, order_no)
);

-- ============ tenant 配置（W2 已建 tenants 表，这里加 settings 列 + ERP 凭据 ============
-- SQLite 不支持 ALTER TABLE ADD COLUMN IF NOT EXISTS，外面 _config 模块兼容判断

-- ============ sales_entities（W2 之前是 hipop.json 文件，现在迁数据库）============
CREATE TABLE IF NOT EXISTS sales_entities (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id                BIGINT NOT NULL,
  alias                    TEXT NOT NULL,           -- e.g. 'hipop_ksa'
  country                  TEXT NOT NULL,           -- 'SA' / 'AE'
  platform                 TEXT NOT NULL,           -- 'Noon' / 'Amazon'
  store_name               TEXT NOT NULL,           -- e.g. 'HIPOP-NOON-KSA'
  store_id                 INT,                     -- ERP 后台 store_ids 值
  currency                 TEXT,
  feishu_table_id          TEXT,
  feishu_decisions_table_id TEXT,
  feishu_stock_table_id    TEXT,
  active                   INT NOT NULL DEFAULT 1,
  created_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (tenant_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_sales_entities_tenant ON sales_entities(tenant_id);

-- ERP 凭据（加密存）
CREATE TABLE IF NOT EXISTS tenant_erp_credentials (
  tenant_id            BIGINT PRIMARY KEY,
  erp_kind             TEXT NOT NULL DEFAULT 'dbuyerp',  -- 未来可扩 'dianxiaomi' 等
  erp_url              TEXT,
  username_enc         TEXT,                              -- Fernet 加密
  password_enc         TEXT,
  updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 飞书凭据（加密）
CREATE TABLE IF NOT EXISTS tenant_feishu_credentials (
  tenant_id            BIGINT PRIMARY KEY,
  app_id               TEXT,
  app_secret_enc       TEXT,
  webhook_enc          TEXT,
  bitable_base_id      TEXT,
  updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 兜底 seed: HIPOP tenant 的 sales_entities 从 hipop.json 迁过来
INSERT OR IGNORE INTO sales_entities
  (tenant_id, alias, country, platform, store_name, store_id, currency,
   feishu_table_id, feishu_decisions_table_id, feishu_stock_table_id)
VALUES
  (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-NOON-KSA', 85, 'SAR',
   'tblQ1FGxIsBbjQAl', 'tblL7Twlt2K7qLhi', 'tbltX0Cl6Egum28W'),
  (1, 'hipop_uae', 'AE', 'Noon', 'HIPOP-NOON-UAE', 42, 'AED',
   NULL, NULL, NULL);

-- hipop Postgres schema (生产 / 阶段 1 部署用)
-- 由 docker-compose.yml 在 PG 容器初始化时自动执行
-- 阶段 1 W2 起每张表会加 tenant_id 列；当前是 single-tenant 版本

-- ============ Agent OS server 内部表 ============

CREATE TABLE IF NOT EXISTS agent_events (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT NOT NULL,
  step_no     INT NOT NULL,
  step_name   TEXT NOT NULL,
  status      TEXT NOT NULL,    -- started / done / error / skipped
  message     TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id, id);

CREATE TABLE IF NOT EXISTS agent_actions (
  id              BIGSERIAL PRIMARY KEY,
  store           TEXT NOT NULL,
  module          TEXT NOT NULL,
  action_type     TEXT NOT NULL,    -- execute / suggest / write
  subject         TEXT,
  pill            TEXT,
  pill_text       TEXT,
  judge           TEXT,
  confidence      REAL,
  options_json    JSONB,
  references_json JSONB,
  owner           TEXT,
  status          TEXT,             -- pending / adopted / rejected
  adopted_by      TEXT,
  adopted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_store_time ON agent_actions(store, id);

CREATE TABLE IF NOT EXISTS chat_messages (
  id               BIGSERIAL PRIMARY KEY,
  store            TEXT NOT NULL,
  role             TEXT NOT NULL,    -- user | agent
  who              TEXT,
  content          TEXT NOT NULL,
  tag              TEXT,
  references_json  JSONB,
  task_json        JSONB,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_store_time ON chat_messages(store, id);

-- ============ wf2 商品 + 销量（per sales_entity）============
-- 每销售主体一张 sku 表 + 一张 orders 表，由 sales_entity.ensure_tables() 动态建。
-- 这里给一份样板（hipop_ksa）；新增 entity 时跑 ensure_tables()。

CREATE TABLE IF NOT EXISTS wf2_hipop_ksa_sku (
  partner_sku             TEXT PRIMARY KEY,
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
  is_listed               INT,    -- 1=已绑定 noon platform_sku_id
  anomalies_json          JSONB,
  order_item_nrs_json     JSONB,
  imported_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wf2_hipop_ksa_orders (
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
  source        TEXT,        -- noon | erp | both
  raw_json      JSONB,
  imported_at   TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (partner_sku, item_nr)
);

-- UAE
CREATE TABLE IF NOT EXISTS wf2_hipop_uae_sku (LIKE wf2_hipop_ksa_sku INCLUDING ALL);
CREATE TABLE IF NOT EXISTS wf2_hipop_uae_orders (LIKE wf2_hipop_ksa_orders INCLUDING ALL);

-- ============ wf1 库存 ============

CREATE TABLE IF NOT EXISTS wf1_hipop_ksa_stock (
  partner_sku                TEXT PRIMARY KEY,
  noon_total_qty             INT,
  noon_saleable_qty          INT,
  noon_unsaleable_qty        INT,
  noon_warehouses_json       JSONB,
  pending_inbound_qty        INT,
  overseas_total_qty         INT,
  overseas_breakdown_json    JSONB,
  yiwu_qty                   INT,
  dongguan_qty               INT,
  total_stock                INT,
  imported_at                TIMESTAMPTZ DEFAULT NOW(),
  updated_at                 TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS wf1_hipop_uae_stock (LIKE wf1_hipop_ksa_stock INCLUDING ALL);

-- ============ wf3 物流（跨 entity 单表）============

CREATE TABLE IF NOT EXISTS wf3_logistics_hub (
  sku                     TEXT PRIMARY KEY,
  in_transit_total_qty    INT,
  has_stuck_batch         INT,
  needs_ops_input         INT,
  avg_transit_days        REAL,
  groups_json             JSONB,
  hist_qtys_json          JSONB,
  transit_batches_json    JSONB,
  total_transit_qty       INT,
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ============ wf5 销售周期 + 补货（per entity）============

CREATE TABLE IF NOT EXISTS wf5_hipop_ksa_sales_cycle (
  partner_sku             TEXT PRIMARY KEY,
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
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS wf5_hipop_uae_sales_cycle (LIKE wf5_hipop_ksa_sales_cycle INCLUDING ALL);

-- ============ wf6 物流告警 + 反馈 ============

CREATE TABLE IF NOT EXISTS wf6_logistics_alerts (
  alert_id            BIGSERIAL PRIMARY KEY,
  order_no            TEXT,
  carrier             TEXT,
  alert_level         TEXT,
  alert_reason        TEXT,
  stage               TEXT,
  actual_stay_days    REAL,
  history_stage_days  REAL,
  sku_list_json       JSONB,
  ops_status          TEXT,
  ops_note            TEXT,
  action_owner        TEXT,
  resolved_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wf6_alerts_order ON wf6_logistics_alerts(order_no);

CREATE TABLE IF NOT EXISTS wf6_hipop_ksa_replenishment_queue (
  partner_sku    TEXT,
  alert_id       BIGINT,
  qty            INT,
  forwarder      TEXT,
  reason         TEXT,
  consumed_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (partner_sku, alert_id)
);
CREATE TABLE IF NOT EXISTS wf6_hipop_uae_replenishment_queue (LIKE wf6_hipop_ksa_replenishment_queue INCLUDING ALL);

-- ============ 飞书摘要 / 老 sa_main（fallback）============

CREATE TABLE IF NOT EXISTS feishu_digest (
  id          BIGSERIAL PRIMARY KEY,
  module      TEXT,
  summary     TEXT,
  digest_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- sa_main 是 wf1 完成前的库存 fallback；阶段 1 后期可去
CREATE TABLE IF NOT EXISTS sa_main (
  partner_sku       TEXT PRIMARY KEY,
  title             TEXT,
  yiwu_qty          INT,
  dongguan_qty      INT,
  noon_total_qty    INT,
  overseas_qty      INT,
  total_stock       INT,
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

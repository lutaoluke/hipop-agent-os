-- hipop Postgres schema (生产 / 阶段 1 部署用)
-- 由 docker-compose.yml 在 PG 容器初始化时自动执行
-- 阶段 1 W2 起每张表会加 tenant_id 列；当前是 single-tenant 版本

-- ============ 多租户 + 用户 + 角色（W2 加，2026-05-09）============

CREATE TABLE IF NOT EXISTS tenants (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL,
  plan         TEXT NOT NULL DEFAULT 'free',  -- free / starter / pro / enterprise
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email           TEXT NOT NULL,
  display_name    TEXT,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'ops',  -- owner / manager / ops / forwarder
  active          BOOLEAN NOT NULL DEFAULT TRUE,
  last_active_at  TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS sessions (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL,
  expires_at   TIMESTAMPTZ NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at);

-- 单租户兜底 seed（已有数据归到 tenant_id=1）
INSERT INTO tenants (id, name, plan)
VALUES (1, 'HIPOP', 'enterprise')
ON CONFLICT (id) DO NOTHING;

-- ============ Agent OS server 内部表 ============

CREATE TABLE IF NOT EXISTS agent_events (
  id            BIGSERIAL PRIMARY KEY,
  task_id       TEXT NOT NULL,
  step_no       INT NOT NULL,
  step_name     TEXT NOT NULL,
  status        TEXT NOT NULL,    -- started / done / error / skipped
  message       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- 触发方留痕（who triggered this workflow）— 2026-05-12
  actor_user_id BIGINT,
  actor_email   TEXT,
  actor_role    TEXT,
  actor_source  TEXT,             -- 'chat' | 'ui' | 'cron' | 'upload'
  -- Managed Agents 升级（2026-05-21）：subprocess heartbeat
  worker_pid    BIGINT,           -- 真在跑这个 step 的 subprocess pid（用于 watchdog 检测死活）
  last_heartbeat TIMESTAMPTZ      -- subprocess 每 30s 更新一次；watchdog 5min 没更新 → orphan
);
CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_events_actor ON agent_events(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_heartbeat ON agent_events(status, last_heartbeat) WHERE status = 'started';

-- ============ Managed Agents：tasks 顶层登记表（2026-05-21 Phase 0.1）============
-- 每个长任务（wf3 / wf2_sales / refresh_all / 选品 multi-agent）都登记在这里。
-- agent_events 是 append-only 事件流；tasks 是当前状态视图。
-- 跟 Anthropic Managed Agents 范式对应：Brain stateless / Hands = subprocess / Session = (tasks + agent_events).
CREATE TABLE IF NOT EXISTS tasks (
  task_id        TEXT PRIMARY KEY,
  tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  workflow       TEXT NOT NULL,        -- WORKFLOW_REGISTRY 里的 key
  state          TEXT NOT NULL,        -- queued / running / orphan / done / error / cancelled
  worker_pid     BIGINT,               -- 当前 subprocess pid（null 表示 worker 不在跑）
  spec_path      TEXT,                 -- ~/hipop/tasks/<task_id>/spec.json — initializer 写的输入
  progress_path  TEXT,                 -- ~/hipop/tasks/<task_id>/progress.json — worker 写的进度（chunk_idx / done_items / failures）
  scratch_dir    TEXT,                 -- ~/hipop/tasks/<task_id>/scratch/ — 中间数据，避免污染 agent context（MCP Code Execution 思路）
  actor_user_id  BIGINT,
  actor_email    TEXT,
  actor_source   TEXT,                 -- 'chat' | 'ui' | 'cron' | 'upload'
  started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_heartbeat TIMESTAMPTZ,          -- worker 每 30s UPDATE，watchdog 5min 没动 → wake
  finished_at    TIMESTAMPTZ,
  wake_count     INT NOT NULL DEFAULT 0,   -- 被 watchdog 唤醒接管的次数
  result_summary TEXT                  -- 完成时的 1-2 句话总结（"325 SKU 写 wf3_hub_v2, 97 真在途"）
);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_state ON tasks(tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat ON tasks(state, last_heartbeat) WHERE state IN ('running', 'queued');
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation ON tasks
    USING (tenant_id = current_setting('app.current_tenant', true)::BIGINT)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::BIGINT);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

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


-- ============ 多租户：所有业务表加 tenant_id + RLS（W2 Task 2.2，2026-05-09）============
--
-- 阶段 1 单租户兜底：所有现有数据归到 tenant_id=1（HIPOP）
-- 阶段 2 多租户上线后，每个客户独立 tenant_id

DO $$
DECLARE
  t TEXT;
  business_tables TEXT[] := ARRAY[
    'wf2_hipop_ksa_sku', 'wf2_hipop_ksa_orders',
    'wf2_hipop_uae_sku', 'wf2_hipop_uae_orders',
    'wf1_hipop_ksa_stock', 'wf1_hipop_uae_stock',
    'wf3_logistics_hub',
    'wf5_hipop_ksa_sales_cycle', 'wf5_hipop_uae_sales_cycle',
    'wf6_logistics_alerts',
    'wf6_hipop_ksa_replenishment_queue', 'wf6_hipop_uae_replenishment_queue',
    'sa_main',
    'agent_actions', 'agent_events', 'chat_messages', 'feishu_digest'
  ];
BEGIN
  FOREACH t IN ARRAY business_tables LOOP
    -- 1. 加 tenant_id 列（DEFAULT 1 让旧数据自动归属 HIPOP 租户）
    EXECUTE format(
      'ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id) ON DELETE CASCADE',
      t
    );
    -- 2. 索引（多数 query 会先按 tenant 过滤）
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_%I_tenant ON %I(tenant_id)',
      t, t
    );
    -- 3. 启用 RLS
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- 关键：FORCE 让 RLS 对 owner 也生效（默认 owner bypass，多租户场景必须 FORCE）
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    -- 4. policy: tenant_id 必须等于当前 session 的 app.current_tenant
    EXECUTE format(
      'DROP POLICY IF EXISTS tenant_isolation ON %I',
      t
    );
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I '
      'USING (tenant_id = current_setting(''app.current_tenant'', true)::BIGINT) '
      'WITH CHECK (tenant_id = current_setting(''app.current_tenant'', true)::BIGINT)',
      t
    );
  END LOOP;
END $$;

-- users / sessions / tenants 不开 RLS（auth 层主动用 tenant_id 过滤）

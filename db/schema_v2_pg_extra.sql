-- PG 专用补丁：补 schema_v2.sql 在 PG 上无法跑的部分
-- 跑法: psql ... -f db/schema_v2_pg_extra.sql

-- sales_entities AUTOINCREMENT → SERIAL（PG 不识别 AUTOINCREMENT）
DROP TABLE IF EXISTS sales_entities CASCADE;
CREATE TABLE sales_entities (
  id                       BIGSERIAL PRIMARY KEY,
  tenant_id                BIGINT NOT NULL,
  alias                    TEXT NOT NULL,
  country                  TEXT NOT NULL,
  platform                 TEXT NOT NULL,
  store_name               TEXT NOT NULL,
  store_id                 INT,
  currency                 TEXT,
  feishu_table_id          TEXT,
  feishu_decisions_table_id TEXT,
  feishu_stock_table_id    TEXT,
  active                   INT NOT NULL DEFAULT 1,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_sales_entities_tenant ON sales_entities(tenant_id);

-- 重新创建 wf6_logistics_alerts_v2 (BIGSERIAL)
DROP TABLE IF EXISTS wf6_logistics_alerts_v2 CASCADE;
CREATE TABLE wf6_logistics_alerts_v2 (
  alert_id            BIGSERIAL PRIMARY KEY,
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
  resolved_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wf6_alerts_v2_tenant_order ON wf6_logistics_alerts_v2(tenant_id, order_no);

-- tenant_feishu_credentials 已在 schema_v2.sql，重建一下保险
CREATE TABLE IF NOT EXISTS tenant_feishu_credentials (
  tenant_id            BIGINT PRIMARY KEY,
  app_id               TEXT,
  app_secret_enc       TEXT,
  webhook_enc          TEXT,
  bitable_base_id      TEXT,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- seed
INSERT INTO sales_entities
  (tenant_id, alias, country, platform, store_name, store_id, currency,
   feishu_table_id, feishu_decisions_table_id, feishu_stock_table_id)
VALUES
  (1, 'hipop_ksa', 'SA', 'Noon', 'HIPOP-NOON-KSA', 85, 'SAR',
   'tblQ1FGxIsBbjQAl', 'tblL7Twlt2K7qLhi', 'tbltX0Cl6Egum28W'),
  (1, 'hipop_uae', 'AE', 'Noon', 'HIPOP-NOON-UAE', 42, 'AED',
   NULL, NULL, NULL)
ON CONFLICT (tenant_id, alias) DO NOTHING;

-- v2 表 RLS policy
DO $$
DECLARE
  v2_tables TEXT[] := ARRAY[
    'wf2_sku','wf2_orders','wf1_stock','wf1_stock_history','wf5_sales_cycle',
    'wf3_logistics_hub_v2','wf6_logistics_alerts_v2','wf6_replenishment_queue_v2',
    'sales_entities','tenant_erp_credentials','tenant_feishu_credentials'
  ];
  t TEXT;
BEGIN
  FOREACH t IN ARRAY v2_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- 关键：FORCE 让 RLS 对表 owner 也生效（默认 owner 自动 bypass，多租户场景必须 FORCE）
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I '
      'USING (tenant_id = current_setting(''app.current_tenant'', true)::BIGINT) '
      'WITH CHECK (tenant_id = current_setting(''app.current_tenant'', true)::BIGINT)',
      t
    );
  END LOOP;
END $$;

"""
销售主体（sales_entity）= 国别 × 平台 × 店铺，每个主体一张独立 sku 主表 + orders 表。

config: hipop.json -> sales_entities[]
表名约定：
  sku 主表：wf2_<alias>_sku
  订单明细：wf2_<alias>_orders

提供：
  load_entities()   读 config 拿全部销售主体
  entity_for(country=, store=)   按 (country, store) 找出对应主体
  ensure_tables(conn)  确保所有主体的表都已建好
"""
import json, os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")


def load_entities():
    with open(CONFIG_PATH) as f:
        return json.load(f).get("sales_entities") or []


def sku_table(alias):
    return f"wf2_{alias}_sku"


def orders_table(alias):
    return f"wf2_{alias}_orders"


def sales_cycle_table(alias):
    return f"wf5_{alias}_sales_cycle"


def replenish_queue_table(alias):
    return f"wf6_{alias}_replenishment_queue"


def stock_table(alias):
    return f"wf1_{alias}_stock"


# 仓库映射（type 1=国内 / 2=海外，国内仓所有 entity 共用）
WAREHOUSES = {
    6:  {"name": "义乌一号仓",   "scope": "domestic",  "country": None, "alias": "yiwu"},
    15: {"name": "东莞一号仓",   "scope": "domestic",  "country": None, "alias": "dongguan"},
    7:  {"name": "阿联酋一号仓", "scope": "overseas",  "country": "AE", "alias": "uae_main"},
    16: {"name": "阿联酋FLEX仓", "scope": "overseas",  "country": "AE", "alias": "uae_flex"},
    8:  {"name": "沙特一号仓",   "scope": "overseas",  "country": "SA", "alias": "sa_main"},
    14: {"name": "沙特FLEX仓",   "scope": "overseas",  "country": "SA", "alias": "sa_flex"},
}


def overseas_warehouses_for(country):
    """返回该国别下的海外仓 ID 列表。"""
    return [wid for wid, w in WAREHOUSES.items()
            if w["scope"] == "overseas" and w["country"] == country]


def domestic_warehouses():
    return [wid for wid, w in WAREHOUSES.items() if w["scope"] == "domestic"]


def entity_for(country=None, store=None):
    for e in load_entities():
        if country and e.get("country") != country:
            continue
        if store and e.get("store") != store:
            continue
        return e
    return None


# ── DDL 模板 ──────────────────────────────────────────────────────────
def _sku_ddl(alias):
    t = sku_table(alias)
    return f"""
    CREATE TABLE IF NOT EXISTS {t} (
      partner_sku             TEXT PRIMARY KEY,    -- =ERP-SKU 短码
      erp_sku_id              TEXT,                -- 同 partner_sku
      noon_sku                TEXT,                -- noon 内部长码
      product_id              TEXT,                -- 主 SKU（母品）

      -- 商品基础
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

      -- 销量
      total_orders            INTEGER,
      valid_orders            INTEGER,
      cancel_count            INTEGER,
      sales_10d               INTEGER,
      sales_30d               INTEGER,
      sales_60d               INTEGER,
      sales_90d               INTEGER,
      sales_120d              INTEGER,
      sales_180d              INTEGER,
      total_revenue           REAL,

      -- 价格 / 利润
      latest_price            REAL,
      avg_price               REAL,
      latest_customer_paid    REAL,
      latest_profit_rate      REAL,
      avg_profit_rate         REAL,

      -- 退取
      return_count            INTEGER,
      return_rate             REAL,
      cancel_rate             REAL,

      -- 时间
      latest_order_date       TEXT,
      as_of_date              TEXT,

      -- 评级 / 预测
      sales_grade             TEXT,
      forecast_10d            INTEGER,
      forecast_30d            INTEGER,

      -- 上架/动销
      is_listed               INTEGER,

      -- 异常 + 明细
      anomalies_json          TEXT,
      order_item_nrs_json     TEXT,

      imported_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_{t}_main   ON {t}(product_id);
    CREATE INDEX IF NOT EXISTS idx_{t}_listed ON {t}(is_listed);
    CREATE INDEX IF NOT EXISTS idx_{t}_grade  ON {t}(sales_grade);
    """


def _orders_ddl(alias):
    t = orders_table(alias)
    return f"""
    CREATE TABLE IF NOT EXISTS {t} (
      partner_sku    TEXT NOT NULL,
      noon_sku       TEXT,
      item_nr        TEXT NOT NULL,
      order_date     TEXT,
      status         TEXT,
      is_cancelled   INTEGER,
      is_return      INTEGER,
      seller_price   REAL,
      customer_paid  REAL,
      currency       TEXT,
      fulfillment    TEXT,
      cost_local     REAL,
      cost_pack      REAL,
      cost_intl      REAL,
      profit         REAL,
      profit_rate    REAL,
      destination    TEXT,
      source         TEXT,
      raw_json       TEXT,
      imported_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
      PRIMARY KEY (partner_sku, item_nr)
    );
    CREATE INDEX IF NOT EXISTS idx_{t}_sku  ON {t}(partner_sku);
    CREATE INDEX IF NOT EXISTS idx_{t}_date ON {t}(order_date);
    """


def _sales_cycle_ddl(alias):
    t = sales_cycle_table(alias)
    return f"""
    CREATE TABLE IF NOT EXISTS {t} (
      partner_sku             TEXT PRIMARY KEY,
      trend                   TEXT,
      daily_rate              REAL,
      forecast_10_days        INTEGER,
      forecast_30_days        INTEGER,
      risk_label              TEXT,
      current_pipeline        INTEGER,
      target_pipeline         INTEGER,
      wf5_replenish_qty       INTEGER,
      lost_replenish_qty      INTEGER,
      weekly_total_replenish  INTEGER,
      trigger_reasons         TEXT,
      urgency                 TEXT,
      ops_advice              TEXT,
      week_tag                TEXT,
      updated_at              DATETIME NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_{t}_urgency ON {t}(urgency);
    """


def _stock_ddl(alias):
    t = stock_table(alias)
    return f"""
    CREATE TABLE IF NOT EXISTS {t} (
      partner_sku             TEXT PRIMARY KEY,    -- =ERP-SKU 短码
      product_id              TEXT,
      noon_sku                TEXT,
      title                   TEXT,
      image_url               TEXT,
      family                  TEXT,

      -- 官方仓（noon 后台）
      noon_total_qty          INTEGER,             -- noon FBN 总库存
      noon_saleable_qty       INTEGER,             -- saleable 可售
      noon_unsaleable_qty     INTEGER,             -- 不可售（damaged / lost / etc）
      noon_warehouses_json    TEXT,                -- 各 noon 仓代号细分（JSON 数组）

      -- 送仓未上架（暂时 NULL，逻辑待补）
      pending_inbound_qty     INTEGER,

      -- 海外仓（ERP，按本 entity 国别过滤）
      overseas_total_qty      INTEGER,
      overseas_breakdown_json TEXT,                -- 各海外仓数量明细（JSON object）

      -- 国内仓（ERP，所有 entity 共享数据）
      yiwu_qty                INTEGER,
      dongguan_qty            INTEGER,

      -- 合计
      total_stock             INTEGER,             -- noon + overseas + 国内（仅作参考）

      as_of_date              TEXT,
      imported_at             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_{t}_total ON {t}(total_stock);
    """


def _replenish_queue_ddl(alias):
    t = replenish_queue_table(alias)
    return f"""
    CREATE TABLE IF NOT EXISTS {t} (
      partner_sku    TEXT NOT NULL,
      lost_qty       INTEGER NOT NULL,
      order_no       TEXT NOT NULL,
      forwarder      TEXT NOT NULL,
      confirmed_at   DATETIME NOT NULL,
      week_tag       TEXT NOT NULL,
      consumed_at    DATETIME,
      PRIMARY KEY (partner_sku, order_no)
    );
    CREATE INDEX IF NOT EXISTS idx_{t}_consumed ON {t}(consumed_at);
    """


def ensure_tables(conn):
    cur = conn.cursor()
    ddls = [_sku_ddl, _orders_ddl, _sales_cycle_ddl, _replenish_queue_ddl, _stock_ddl]
    for e in load_entities():
        for ddl_fn in ddls:
            for stmt in ddl_fn(e["alias"]).strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
    conn.commit()

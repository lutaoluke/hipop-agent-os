"""
HIPOP 工作台数据访问层

DB 分派（按 env）:
  - DB_URL=postgresql://...  → 生产/部署，PG（schema 由 db/schema.sql 建）
  - 否则                     → 本地开发，SQLite at HIPOP_DB

PG 模式下 SQL 占位符自动 ? → %s；datetime('now','localtime') → NOW()。
不要用 SQLite 独有的 INSERT OR REPLACE，统一用 ON CONFLICT DO UPDATE（两边都支持）。
"""
import sqlite3, os, json, datetime, re
from typing import List, Dict, Optional, Any

DB_PATH = os.environ.get("HIPOP_DB", "/Users/luke/code/hipop/hipop.db")
DB_URL  = os.environ.get("DB_URL")  # 设置时走 PG，否则 SQLite

# 多租户：connection 拿到时 SET app.current_tenant = <tid>，让 PG RLS 自动过滤。
# 通过 contextvars 在请求链路里传 — chat / api 端点设置后，conn() 自动注入。
import contextvars
_current_tenant: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_tenant", default=None
)


def set_current_tenant(tenant_id: Optional[int]):
    """FastAPI middleware / endpoint 在拿到 user 后调一下，影响后续所有 conn()。"""
    _current_tenant.set(tenant_id)


def get_current_tenant() -> Optional[int]:
    return _current_tenant.get()


def set_current_tenant_to_task(task_id: str) -> Optional[int]:
    """从 tasks 表反查 task 的 tenant_id 并 set context。给 watchdog / 跨 tenant 操作用。
    用 ALTER TABLE NO FORCE 临时绕 RLS 拿 tenant_id（PG owner 才行）；查到后立刻 SET。
    """
    if not is_postgres():
        return None
    import psycopg2
    raw = psycopg2.connect(DB_URL)
    raw.autocommit = True
    try:
        with raw.cursor() as cur:
            cur.execute("ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY")
            cur.execute("SET app.current_tenant = '0'")
            cur.execute("SELECT tenant_id FROM tasks WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
            cur.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")
    finally:
        raw.close()
    if row:
        tid = row[0]
        _current_tenant.set(tid)
        return tid
    return None


def is_postgres() -> bool:
    return bool(DB_URL and DB_URL.startswith(("postgresql://", "postgres://")))


def _convert_sql_for_pg(sql: str) -> str:
    """sqlite SQL → pg SQL 兼容化（最小改动）。"""
    # ? 占位符 → %s
    sql = re.sub(r"(?<![\w'])\?(?![\w'])", "%s", sql)
    # datetime('now','localtime') → NOW()
    sql = re.sub(r"datetime\(\s*'now'\s*,\s*'localtime'\s*\)", "NOW()", sql)
    sql = re.sub(r"datetime\(\s*'now'\s*\)", "NOW()", sql)
    # date('now','localtime') → CURRENT_DATE
    sql = re.sub(r"date\(\s*'now'\s*,\s*'localtime'\s*\)", "CURRENT_DATE", sql)
    return sql


class _PGCursorWrapper:
    """让 psycopg2 cursor 行为接近 sqlite3，让 _fetch / _scalar 不用改。
    主要差异：sqlite3 row 支持 dict-like，pg 用 RealDictCursor 也支持。"""
    def __init__(self, cur):
        self._cur = cur
    def execute(self, sql, params=()):
        self._cur.execute(_convert_sql_for_pg(sql), params)
        return self
    def fetchall(self): return self._cur.fetchall()
    def fetchone(self): return self._cur.fetchone()
    @property
    def description(self): return self._cur.description
    def __iter__(self): return iter(self._cur)


class _PGConnWrapper:
    def __init__(self, raw):
        self._raw = raw
    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_convert_sql_for_pg(sql), params)
        return _PGCursorWrapper(cur)
    def commit(self): self._raw.commit()
    def rollback(self): self._raw.rollback()
    def close(self): self._raw.close()
    def cursor(self): return _PGCursorWrapper(self._raw.cursor())
    def __enter__(self): return self
    def __exit__(self, exc_type, *_):
        if exc_type:
            self._raw.rollback()
        else:
            self._raw.commit()
        self._raw.close()


def conn():
    """主连接入口 — 按 DB_URL 分派 SQLite/PG。

    PG 模式下自动 SET app.current_tenant（让 RLS 生效）；SQLite 不支持 RLS，
    多租户隔离靠 ORM 层显式 WHERE tenant_id=（阶段 2 在 _fetch 里包装）。
    """
    if is_postgres():
        import psycopg2
        from psycopg2.extras import RealDictCursor
        raw = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        # 注入 tenant context（不设时 RLS policy 会拒绝所有查询，所以默认 1）
        tid = get_current_tenant() or 1
        with raw.cursor() as cur:
            cur.execute("SET app.current_tenant = %s", (str(tid),))
        return _PGConnWrapper(raw)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fetch(sql: str, params: tuple = ()) -> List[Dict]:
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _hhmm(v) -> str:
    """统一渲染时间为 'HH:MM'。SQLite 给字符串 'YYYY-MM-DD HH:MM:SS'，PG 给 datetime/带 tz。"""
    if v is None: return ""
    if hasattr(v, "strftime"):
        try: return v.strftime("%H:%M")
        except Exception: pass
    s = str(v)
    # 字符串形如 '2026-05-11 18:24:17' 或 ISO '2026-05-11T18:24:17'
    if "T" in s and " " not in s:
        s = s.replace("T", " ", 1)
    if len(s) >= 16 and s[10] == " ":
        return s[11:16]
    return ""


def _scalar(sql: str, params: tuple = ()):
    with conn() as c:
        r = c.execute(sql, params).fetchone()
        if r is None: return None
        # PG RealDictCursor 返回 dict；SQLite Row 支持 r[0]
        if isinstance(r, dict):
            return next(iter(r.values()))
        return r[0]


def _feishu_today_count() -> int:
    sql = "SELECT COUNT(*) FROM feishu_digest WHERE date(digest_at)=date('now','localtime')"
    return _scalar(sql) or 0


# ── store → entity_alias 解析（用于 HTTP API 把 KSA/UAE 转成 entity_alias） ──
def _resolve_entity_for_store(store: str) -> tuple:
    """返回 (tenant_id, entity_alias)。tenant_id 从 contextvars 拿，store 推 country 查 sales_entities。"""
    tid = get_current_tenant() or 1
    country = {"KSA": "SA", "UAE": "AE"}.get((store or "").upper())
    if not country:
        return tid, ""
    rows = _fetch(
        "SELECT alias FROM sales_entities WHERE tenant_id=? AND country=? AND active=1 LIMIT 1",
        (tid, country),
    )
    return tid, (rows[0]["alias"] if rows else "")


# ── 库存历史抽检（WS-22 → WS-12）────────────────────────────
def stock_as_of(tenant_id: int, entity_alias: str, partner_sku: str,
                as_of_date) -> Optional[Dict]:
    """WS-12 历史抽检入口：读某业务日的库存快照（官方仓/海外仓/义乌/东莞/pending 等）。

    走 dated 层 wf1_stock_history，**不碰** latest 层 wf1_stock，也不用 imported_at
    冒充业务日。as_of_date 非法直接 raise（见 stock_history.normalize_as_of_date）。
    """
    from hipop.scripts import stock_history
    set_current_tenant(tenant_id)
    with conn() as c:
        return stock_history.read_snapshot(c, tenant_id, entity_alias, partner_sku, as_of_date)


def stock_history_dates(tenant_id: int, entity_alias: Optional[str] = None) -> List[str]:
    """列出某 tenant（可选 entity）已在档的库存业务日，最新在前 —— WS-12 选日期用。"""
    from hipop.scripts import stock_history
    set_current_tenant(tenant_id)
    with conn() as c:
        return stock_history.list_dates(c, tenant_id, entity_alias)


# ── 销量「最后输出数据」字段口径（WS-20 / WS-14-6）──────────────────
# 替代人工 Excel 汇总表。这是销量输出口径的**单一事实源**：export / /api/sku-health /
# （未来）飞书 sync 全部走 sales_output_rows() 这一个 v2 读取器，证明三处消费的是
# 同一份 wf2_sku / wf2_orders（按 tenant+entity 过滤），不再各写各的（接线缺失死法）。
# 每项 = (输出列 key, 中文表头, 来源)：
#   entity  → sales_entities（同 tenant+entity）
#   wf2_sku → 动态分析层最终口径（wf_sales_static_v2.merge_entity_v2 从 wf2_orders 算出）
#   derived → 由 wf2_sku 的 JSON 列就地派生（订单号=order_item_nrs_json；异常标记=anomalies_json）
SALES_OUTPUT_SPEC = [
    ("country",              "国别",        "entity"),
    ("store_name",           "店铺名",      "entity"),
    ("product_id",           "主SKU",       "wf2_sku"),
    ("partner_sku",          "PSKU",        "wf2_sku"),
    ("total_orders",         "订单量",      "wf2_sku"),
    ("order_no",             "订单号",      "derived"),
    ("title",                "商品标题",    "wf2_sku"),
    ("fulfillment",          "售卖形式",    "wf2_sku"),
    ("latest_price",         "商品最新售价", "wf2_sku"),
    ("avg_price",            "平均售价",    "wf2_sku"),
    ("latest_customer_paid", "最新成交价",  "wf2_sku"),
    ("latest_profit_rate",   "最新利润率",  "wf2_sku"),
    ("return_rate",          "退货率",      "wf2_sku"),
    ("cancel_rate",          "取消率",      "wf2_sku"),
    ("latest_order_date",    "最新出单日期", "wf2_sku"),
    ("sales_10d",            "近10天销量",  "wf2_sku"),
    ("sales_30d",            "近30天销量",  "wf2_sku"),
    ("sales_60d",            "近60天销量",  "wf2_sku"),
    ("sales_90d",            "近90天销量",  "wf2_sku"),
    ("sales_120d",           "近120天销量", "wf2_sku"),
    ("sales_180d",           "近180天销量", "wf2_sku"),
    ("total_revenue",        "总销售额",    "wf2_sku"),
    ("anomalies",            "异常标记",    "derived"),
    ("sales_grade",          "销量评级",    "wf2_sku"),
    ("forecast_10d",         "10天预测",    "wf2_sku"),
    ("forecast_30d",         "30天预测",    "wf2_sku"),
]
# 实际从 wf2_sku SELECT 的列（含派生用的原始 JSON 列）
_SALES_OUTPUT_WF2_COLS = [k for k, _h, src in SALES_OUTPUT_SPEC if src == "wf2_sku"] + [
    "order_item_nrs_json", "anomalies_json",
]


def _assemble_sales_output_row(r: Dict, country, store_name) -> Dict:
    """单行 wf2_sku → 输出行：补 国别/店铺名，派生 订单号 / 异常标记。原地补字段并返回。"""
    r["country"] = country
    r["store_name"] = store_name
    # 订单号：order_item_nrs_json（来源 wf2_orders 的 item_nr 集合）展开成逗号串
    try:
        nrs = json.loads(r.get("order_item_nrs_json") or "[]")
    except (ValueError, TypeError):
        nrs = []
    r["order_no"] = ",".join(str(x) for x in nrs) if nrs else None
    # 异常标记：anomalies_json 摘成 type 串（noon vs ERP），空则留空
    try:
        anoms = json.loads(r.get("anomalies_json") or "[]")
    except (ValueError, TypeError):
        anoms = []
    r["anomalies"] = ";".join(
        str(a.get("type", a)) if isinstance(a, dict) else str(a) for a in anoms
    ) if anoms else None
    return r


def sales_output_rows(tenant_id: int, entity_alias: str, listing: str = "all",
                      sales_only: bool = False) -> List[Dict]:
    """销量输出口径的单一 v2 读取器：wf2_sku（tenant+entity 过滤）+ sales_entities。

    export / API / 飞书 sync 共用本函数 → 三处消费同一数据源。返回行含 SALES_OUTPUT_SPEC
    全部 key（国别/店铺名 + wf2_sku 字段 + 派生 订单号/异常标记）。
    """
    where = ["tenant_id=?", "entity_alias=?"]
    params: list = [tenant_id, entity_alias]
    if listing == "listed":
        where.append("is_listed=1")
    elif listing == "unlisted":
        where.append("(is_listed=0 OR is_listed IS NULL)")
    if sales_only:
        where.append("COALESCE(sales_180d,0) > 0")
    sql = (f"SELECT {','.join(_SALES_OUTPUT_WF2_COLS)} FROM wf2_sku "
           f"WHERE {' AND '.join(where)} "
           f"ORDER BY COALESCE(sales_30d,0) DESC, COALESCE(sales_180d,0) DESC")
    rows = _fetch(sql, tuple(params))
    ent = _fetch(
        "SELECT country, store_name FROM sales_entities WHERE tenant_id=? AND alias=? LIMIT 1",
        (tenant_id, entity_alias),
    )
    country = ent[0]["country"] if ent else None
    store_name = ent[0]["store_name"] if ent else None
    return [_assemble_sales_output_row(r, country, store_name) for r in rows]


# ── SKU 健康（销售/库存模块）────────────────────────────────
def get_sku_health(store: str, urgency: Optional[str] = None, limit: int = 30,
                    listing: str = "listed") -> List[Dict]:
    """读 wf2_sku + wf5_sales_cycle + wf3_logistics_hub_v2，按 tenant_id+entity 过滤。

    listing: 'listed'（默认，向后兼容老 UI 行为）/ 'unlisted' / 'all'
    """
    tid, alias = _resolve_entity_for_store(store)
    where_extra = ""
    if listing == "listed":
        where_extra = " AND w2.is_listed=1"
    elif listing == "unlisted":
        where_extra = " AND (w2.is_listed=0 OR w2.is_listed IS NULL)"
    # listing == "all" → 不加 is_listed 条件
    sql = f"""
    SELECT
      w2.partner_sku, w2.title, w2.image_url,
      w2.sales_30d, w2.sales_10d, w2.sales_180d, w2.latest_price, w2.latest_profit_rate,
      w2.sales_grade, w2.is_listed, w2.return_rate, w2.cancel_rate,
      w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
      w5.current_pipeline, w5.target_pipeline, w5.wf5_replenish_qty,
      h.in_transit_total_qty, h.has_stuck_batch
    FROM wf2_sku w2
    LEFT JOIN wf5_sales_cycle w5
      ON w2.tenant_id=w5.tenant_id AND w2.entity_alias=w5.entity_alias
      AND w2.partner_sku=w5.partner_sku
    LEFT JOIN wf3_logistics_hub_v2 h
      ON w2.tenant_id=h.tenant_id AND w2.partner_sku=h.sku
    WHERE w2.tenant_id=? AND w2.entity_alias=?{where_extra}
    """
    rows = _fetch(sql, (tid, alias))
    for r in rows:
        # 计算 days_left（库存可撑天数）
        if r.get("daily_rate") and r["daily_rate"] > 0:
            pipeline = r.get("current_pipeline") or 0
            r["days_left"] = round(pipeline / r["daily_rate"], 1) if pipeline else 0
        else:
            r["days_left"] = None
        # 利润率小数化
        if r.get("latest_profit_rate") is not None:
            r["profit_rate"] = round(r["latest_profit_rate"] * 100, 1)
        else:
            r["profit_rate"] = None

    # WS-20：把「最后输出数据」全字段并进 /api/sku-health 响应，与 export 同源
    # （sales_output_rows 是唯一 v2 读取器）。dashboard 专有字段（trend/days_left/
    # profit_rate 等）不在 spec 里，不会被覆盖。
    out_map = {o["partner_sku"]: o for o in sales_output_rows(tid, alias, listing=listing)}
    for r in rows:
        o = out_map.get(r.get("partner_sku"))
        if o:
            for k, _h, _s in SALES_OUTPUT_SPEC:
                r[k] = o.get(k)

    # 排序：异常 > 急速下降 > 利润低
    def score(r):
        s = 0
        if r.get("trend") == "急速下降": s += 100
        if r.get("trend") == "下降": s += 50
        if r.get("has_stuck_batch"): s += 80
        if r.get("profit_rate") is not None and r["profit_rate"] < 10: s += 30
        if r.get("return_rate") and r["return_rate"] > 0.1: s += 20
        if r.get("days_left") is not None and r["days_left"] < 14: s += 60
        return -s
    rows.sort(key=score)

    if urgency == "urgent":
        rows = [r for r in rows if r.get("trend") in ("急速下降", "下降") or r.get("has_stuck_batch")]
    return rows[:limit]


# ── 订单（在途物流）───────────────────────────────────────
def get_orders(store: str, limit: int = 50) -> List[Dict]:
    """从 hub_v2 的 groups_json 解出每个货单的状态（按 tenant 隔离）"""
    tid, _ = _resolve_entity_for_store(store)
    rows = _fetch(
        "SELECT sku, groups_json, has_stuck_batch FROM wf3_logistics_hub_v2 WHERE tenant_id=?",
        (tid,),
    )
    orders_by_no = {}
    for r in rows:
        groups = json.loads(r["groups_json"] or "[]")
        for g in groups:
            country = g.get("country", "")
            if store and store.upper() != country.upper():
                continue
            for b in g.get("in_transit_batches", []) + g.get("recent_arrived", []):
                ono = b.get("order_no") or b.get("logistics_order_no")
                if not ono: continue
                if ono not in orders_by_no:
                    orders_by_no[ono] = {
                        "order_no": ono,
                        "carrier": b.get("forwarder") or b.get("carrier", "—"),
                        "stage": b.get("stage", "—"),
                        "stay_days": b.get("days_in_stage", b.get("days", 0)),
                        "is_stuck": b.get("is_stuck", False),
                        "country": country,
                        "skus": [],
                    }
                orders_by_no[ono]["skus"].append({
                    "sku": r["sku"],
                    "qty": b.get("qty", b.get("in_transit_qty", 0)),
                })

    # 合并 wf6_logistics_alerts_v2 状态
    alerts = _fetch("""
        SELECT order_no, alert_level, alert_reason, ops_status, stage,
               actual_stay_days, history_stage_days, sku_list_json
        FROM wf6_logistics_alerts_v2 WHERE tenant_id=?
    """, (tid,))
    for a in alerts:
        ono = a["order_no"]
        if ono not in orders_by_no:
            sku_list = json.loads(a["sku_list_json"] or "[]")
            orders_by_no[ono] = {
                "order_no": ono,
                "carrier": "—",
                "stage": a["stage"] or "—",
                "stay_days": a.get("actual_stay_days") or 0,
                "is_stuck": True,
                "skus": sku_list,
                "country": store.upper(),
            }
        o = orders_by_no[ono]
        o["alert_level"] = a["alert_level"]
        o["alert_reason"] = a["alert_reason"]
        o["ops_status"] = a["ops_status"]
        o["actual_stay_days"] = a["actual_stay_days"]
        o["history_stage_days"] = a["history_stage_days"]

    out = list(orders_by_no.values())
    # 排序：红色告警 > 卡单 > 停留时间
    LEVEL_ORDER = {"红": 0, "橙": 1, "黄": 2, "蓝": 3}
    def sk(o):
        return (LEVEL_ORDER.get(o.get("alert_level"), 9), 0 if o.get("is_stuck") else 1, -float(o.get("stay_days", 0) or 0))
    out.sort(key=sk)
    return out[:limit]


# ── 补货建议（补货决策）──────────────────────────────────
def get_replenishment(store: str, limit: int = 50) -> List[Dict]:
    """补货建议：v2 表 wf2_sku + wf5_sales_cycle 按 tenant_id+entity_alias"""
    tid, alias = _resolve_entity_for_store(store)
    rows = _fetch("""
        SELECT w2.partner_sku, w2.title, w2.image_url, w2.sales_30d, w2.latest_price,
               w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
               w5.wf5_replenish_qty, w5.lost_replenish_qty, w5.weekly_total_replenish,
               w5.trigger_reasons, w5.current_pipeline, w5.target_pipeline
        FROM wf2_sku w2
        LEFT JOIN wf5_sales_cycle w5
          ON w2.tenant_id=w5.tenant_id AND w2.entity_alias=w5.entity_alias
          AND w2.partner_sku=w5.partner_sku
        WHERE w2.tenant_id=? AND w2.entity_alias=?
          AND w2.is_listed=1 AND w5.weekly_total_replenish > 0
        ORDER BY w5.weekly_total_replenish DESC
    """, (tid, alias))
    for r in rows:
        # 紧迫度评级
        if r.get("trend") == "急速下降":
            r["urgency_level"] = "high"
        elif r.get("trend") in ("下降", "加速增长"):
            r["urgency_level"] = "mid"
        else:
            r["urgency_level"] = "low"
        # 优先补货量
        r["qty"] = r.get("weekly_total_replenish") or 0
        try:
            r["trigger_reasons_list"] = json.loads(r.get("trigger_reasons") or "[]")
        except Exception:
            r["trigger_reasons_list"] = []
    return rows[:limit]


# ── 模块今日重点（7+1 模块卡片）──────────────────────────
def get_module_summaries(store: str) -> List[Dict]:
    """聚合所有模块的'今日重点'（v2 表 + tenant 隔离）"""
    tid, alias = _resolve_entity_for_store(store)
    sku_count = _scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND is_listed=1",
        (tid, alias),
    ) or 0
    urgent_count = _scalar(
        "SELECT COUNT(*) FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND trend IN ('急速下降','下降')",
        (tid, alias),
    ) or 0
    low_margin_count = _scalar(
        "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND entity_alias=? "
        "AND is_listed=1 AND latest_profit_rate IS NOT NULL AND latest_profit_rate < 0.10",
        (tid, alias),
    ) or 0
    in_transit_total = _scalar(
        "SELECT SUM(in_transit_total_qty) FROM wf3_logistics_hub_v2 WHERE tenant_id=?", (tid,)
    ) or 0
    in_transit_orders = 0  # batch_count 字段在 v2 没建，简化
    stuck_skus = _scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND has_stuck_batch=1", (tid,)
    ) or 0
    needs_ops = _scalar(
        "SELECT COUNT(*) FROM wf3_logistics_hub_v2 WHERE tenant_id=? AND needs_ops_input=1", (tid,)
    ) or 0
    alerts_pending = _scalar(
        "SELECT COUNT(*) FROM wf6_logistics_alerts_v2 WHERE tenant_id=? AND ops_status='待处理'", (tid,)
    ) or 0
    alerts_red = _scalar(
        "SELECT COUNT(*) FROM wf6_logistics_alerts_v2 "
        "WHERE tenant_id=? AND alert_level='红' AND ops_status='待处理'", (tid,)
    ) or 0
    replenish_count = _scalar(
        "SELECT COUNT(*) FROM wf5_sales_cycle "
        "WHERE tenant_id=? AND entity_alias=? AND weekly_total_replenish > 0",
        (tid, alias),
    ) or 0

    # 数据更新时间（v2）
    latest_w2 = _scalar("SELECT MAX(imported_at) FROM wf2_sku WHERE tenant_id=? AND entity_alias=?", (tid, alias)) or "未知"
    latest_w5 = _scalar("SELECT MAX(updated_at) FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=?", (tid, alias)) or "未知"
    latest_hub = _scalar("SELECT MAX(updated_at) FROM wf3_logistics_hub_v2 WHERE tenant_id=?", (tid,)) or "未知"
    latest_alerts = _scalar("SELECT MAX(created_at) FROM wf6_logistics_alerts_v2 WHERE tenant_id=?", (tid,)) or "未知"

    # 数据获取健康度
    today_str = datetime.date.today().isoformat()
    noon_fresh = "warn" if (latest_w5 or "")[:10] != today_str else "ok"
    cycle_rows = _scalar("SELECT COUNT(*) FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=?", (tid, alias)) or 0

    return [
        {
            "key": "data",
            "name": "数据获取",
            "state": noon_fresh,
            "line1": f"商品库 {sku_count} SKU · 销量周期 {cycle_rows} 行",
            "line2": f"上次同步: {(latest_w5 or '—')[:16]}",
            "ref": "wf2 / wf5",
            "drill": None,
        },
        {
            "key": "sales",
            "name": "销售 / 库存",
            "state": "danger" if urgent_count > 0 else "ok",
            "line1": f"立即处理 {urgent_count} · 利润警告 {low_margin_count}",
            "line2": f"在售 {sku_count} SKU",
            "ref": "wf2 / wf5",
            "drill": "/module/sales",
        },
        {
            "key": "logistics",
            "name": "在途物流",
            "state": "danger" if stuck_skus > 0 else "ok",
            "line1": f"卡单 SKU {stuck_skus} · 待运营 {needs_ops}",
            "line2": f"在途 {in_transit_total} 件 · {in_transit_orders} 批",
            "ref": "wf3 / wf6",
            "drill": "/module/logistics",
        },
        {
            "key": "replenish",
            "name": "补货决策",
            "state": "warn" if replenish_count > 0 else "ok",
            "line1": f"建议补货 {replenish_count} SKU",
            "line2": f"红色告警 {alerts_red} · 待处理 {alerts_pending}",
            "ref": "wf5 / wf6",
            "drill": "/module/replenish",
        },
        {
            "key": "traffic",
            "name": "流量 / 推广",
            "state": "warn",
            "line1": "UV ↓ 18% · PV ↓ 22%（mock）",
            "line2": "影响 SKU: TBJ0057A, TBA0210A, TBP0289A",
            "ref": "(mock)",
            "drill": None,
        },
        {
            "key": "selection",
            "name": "选品 + 货源",
            "state": "info",
            "line1": "3 个候选品已评估（agent 内省）",
            "line2": "成功模式 / 失败模式 v1 已沉淀",
            "ref": "wf2 + LLM",
            "drill": "/module/selection",
        },
        {
            "key": "marketing",
            "name": "营销活动",
            "state": "info",
            "line1": "本周无活动（mock）",
            "line2": "下周拟参加 noon 平台 promo",
            "ref": "(mock)",
            "drill": None,
        },
        {
            "key": "feishu",
            "name": "飞书沉淀",
            "state": "ok",
            "line1": f"今日 {_feishu_today_count()} 条沟通沉淀",
            "line2": "运营 + 跟单 + 决策",
            "ref": "feishu_digest",
            "drill": "/module/feishu",
        },
        _audit_module_summary(store),
    ]


def _audit_module_summary(store: str) -> Dict:
    """巡检 agent 模块卡 — 10 invariants 健康度"""
    try:
        from . import audit
        s = audit.get_summary(store)
        c = s["counts"]
        line1_msg = f"{c['ok']} 通过"
        if c.get("warn"): line1_msg += f" · {c['warn']} 警告"
        if c.get("danger"): line1_msg += f" · {c['danger']} 严重"
        # 取最严重的 1 条放 line2
        worst = next((r for r in s["checks"] if r["status"] == "danger"),
                     next((r for r in s["checks"] if r["status"] == "warn"), None))
        line2 = worst["check"] + ": " + (worst["message"][:40] + "...") if worst else "全部 invariant 通过"
        return {
            "key": "audit",
            "name": "数据巡检",
            "state": s["overall"],
            "line1": line1_msg,
            "line2": line2,
            "ref": "audit/10 invariants",
            "drill": "/module/audit",
        }
    except Exception as e:
        return {
            "key": "audit", "name": "数据巡检", "state": "warn",
            "line1": "巡检脚本异常", "line2": str(e)[:50],
            "ref": "audit", "drill": "/module/audit",
        }


# ── 工作日志（飞书 + agent 操作 + 物流告警）────────────
def get_work_log(store: str) -> List[Dict]:
    """混合：真 wf6 告警 + 真 agent_actions（v2 表 + tenant 隔离）"""
    items = []
    tid_w, _ = _resolve_entity_for_store(store)
    for a in _fetch("""
        SELECT alert_id, alert_level, alert_reason, order_no, sku_list_json,
               ops_status, action_owner, created_at
        FROM wf6_logistics_alerts_v2 WHERE tenant_id=? ORDER BY created_at DESC LIMIT 5
    """, (tid_w,)):
        a["updated_at"] = a.get("created_at")  # 兼容下面取字段
        sku_list = json.loads(a["sku_list_json"] or "[]")
        sku_str = ", ".join(s["sku"] for s in sku_list[:3])
        t = _hhmm(a["updated_at"] or a["created_at"])
        items.append({
            "time": t or "00:00",
            "who": a.get("action_owner") or "刘鹤",
            "text": f"[{a['alert_level']}级 物流告警] {a['order_no']} {a['alert_reason']} → {a['ops_status']}",
            "ref": f"wf6 alert#{a['alert_id']}",
            "tag": "告警" if a["ops_status"] == "待处理" else "更新",
        })

    for act in _fetch("""
        SELECT id, module, action_type, subject, pill_text, judge, owner, created_at
        FROM agent_actions WHERE store=? ORDER BY created_at DESC LIMIT 5
    """, (store.upper(),)):
        items.append({
            "time": _hhmm(act["created_at"]) or "00:00",
            "who": "Agent",
            "text": f"{act['judge'] or act['pill_text']}（{act['subject'] or '—'}）",
            "ref": f"agent_actions#{act['id']}",
            "tag": "Agent",
        })

    # 飞书 digest（真）
    for d in _fetch("SELECT * FROM feishu_digest ORDER BY digest_at DESC LIMIT 5"):
        items.append({
            "time": (d["source_time"] or d["digest_at"] or "")[-8:-3] or "00:00",
            "who": d.get("who") or "飞书",
            "text": d["text"],
            "ref": "feishu_digest",
            "tag": d.get("category") or "飞书",
        })

    # 巡检 agent 异常 → 推到工作日志 (warn / danger 项)
    try:
        from . import audit
        s = audit.get_summary(store)
        now = datetime.datetime.now().strftime("%H:%M")
        for r in s["checks"]:
            if r["status"] in ("warn", "danger"):
                items.append({
                    "time": now,
                    "who": "巡检 Agent",
                    "text": f"[{r['status'].upper()}] {r['check']}: {r['message'][:80]}",
                    "ref": "audit",
                    "tag": "巡检",
                })
    except Exception:
        pass

    # 不再掺 mock（真飞书摘要从 feishu_digest 拿；空就空）
    items.sort(key=lambda i: i.get("time", ""), reverse=True)
    return items[:8]


# ── 数据健康（顶部 chip + Agent data_health_check tool 共用）─────
def get_data_health(store: str) -> Dict:
    """返回当前店铺各数据源的最新写入时间 + 自动度标签。

    自动度（automation）:
      - "auto"         脚本完全自动跑，Agent 可调 run_workflow 直接刷新
      - "needs_csv"    依赖人工导出 CSV 上传到 inbox/，Agent 不能代跑，需引导用户上传
    """
    s = store.lower()
    today = datetime.date.today().isoformat()
    def _date10(v):
        """SQLite 返回 'YYYY-MM-DD HH:MM:SS' 字符串；PG 返回 datetime/date 对象，统一裁成 'YYYY-MM-DD'"""
        if v is None: return ""
        if hasattr(v, "isoformat"): return v.isoformat()[:10]
        return str(v)[:10]

    tid_h, alias_h = _resolve_entity_for_store(store)
    latest_w1_imported = _date10(_scalar("SELECT MAX(imported_at) FROM wf1_stock WHERE tenant_id=? AND entity_alias=?", (tid_h, alias_h)))
    latest_w2_imported = _date10(_scalar("SELECT MAX(imported_at) FROM wf2_sku WHERE tenant_id=? AND entity_alias=?", (tid_h, alias_h)))
    latest_w5_updated  = _date10(_scalar("SELECT MAX(updated_at) FROM wf5_sales_cycle WHERE tenant_id=? AND entity_alias=?", (tid_h, alias_h)))
    latest_hub_updated = _date10(_scalar("SELECT MAX(updated_at) FROM wf3_logistics_hub_v2 WHERE tenant_id=?", (tid_h,)))
    latest_alerts      = _date10(_scalar("SELECT MAX(created_at) FROM wf6_logistics_alerts_v2 WHERE tenant_id=?", (tid_h,)))

    latest_noon_order = _date10(_scalar(
        "SELECT MAX(order_date) FROM wf2_orders WHERE tenant_id=? AND entity_alias=?",
        (tid_h, alias_h),
    ))
    latest_noon_stock = _date10(_scalar(
        "SELECT MAX(imported_at) FROM wf1_stock WHERE tenant_id=? AND entity_alias=? AND noon_total_qty IS NOT NULL",
        (tid_h, alias_h),
    ))

    def _stale_days(date_str):
        if not date_str: return None
        try:
            d = datetime.date.fromisoformat(date_str[:10])
            return (datetime.date.fromisoformat(today) - d).days
        except Exception:
            return None

    sources = {
        "erp_products":  {"latest": latest_w2_imported, "stale_days": _stale_days(latest_w2_imported), "automation": "auto",      "workflow": "wf2_sales"},
        "erp_sales":     {"latest": latest_w2_imported, "stale_days": _stale_days(latest_w2_imported), "automation": "auto",      "workflow": "wf2_sales"},
        "erp_stock":     {"latest": latest_w1_imported, "stale_days": _stale_days(latest_w1_imported), "automation": "auto",      "workflow": "wf1_stock"},
        "noon_orders":   {"latest": latest_noon_order,  "stale_days": _stale_days(latest_noon_order),  "automation": "needs_csv", "workflow": "wf2_sales", "csv_pattern": f"sales_noon_*_{s.upper()}_*.csv", "where": "紫鸟 noon 后台 → sales 页面 → export 最近 180 天 CSV"},
        "noon_stock":    {"latest": latest_noon_stock,  "stale_days": _stale_days(latest_noon_stock),  "automation": "needs_csv", "workflow": "wf1_stock", "csv_pattern": f"Inventory*{s.upper()}*.csv",   "where": "紫鸟 noon 后台 → my inventory → export"},
        "wf3_logistics": {"latest": latest_hub_updated, "stale_days": _stale_days(latest_hub_updated), "automation": "auto",      "workflow": "wf3_logistics"},
        "wf5_replenish": {"latest": latest_w5_updated,  "stale_days": _stale_days(latest_w5_updated),  "automation": "auto",      "workflow": "wf5_sales_cycle"},
        "wf6_alerts":    {"latest": latest_alerts,      "stale_days": _stale_days(latest_alerts),      "automation": "auto",      "workflow": "wf6_alerts"},
    }

    # 问题意图 → 依赖的数据源 list（Agent 用这个判断"用户问的这种问题，我要看哪些源新鲜")
    # 顺序很重要：列上游在前，下游在后；Agent 应该按这个顺序串行刷新（先 ERP 再 wf3 再 wf5）
    dependency_groups = {
        "replenishment":   ["erp_sales", "erp_stock", "noon_orders", "noon_stock", "wf3_logistics", "wf5_replenish"],  # 我该补货吗 / 哪些要补
        "sku_health":      ["erp_sales", "noon_orders", "wf3_logistics", "wf5_replenish"],  # SKU 卖得怎么样 / 趋势 / 库存可撑
        "logistics_track": ["wf3_logistics"],                                  # 在途 / 物流追踪
        "alerts":          ["wf3_logistics", "wf6_alerts"],                    # 告警 / 卡单 / 红色货单
        "air_freight_roi": ["erp_sales", "noon_orders", "wf5_replenish"],      # 海空运 ROI 决策
        "products_count":  ["erp_products"],                                   # 商品总数 / 多少 SKU
        "stock":           ["erp_stock", "noon_stock"],                        # 库存够不够
        "overview":        ["erp_sales", "wf3_logistics", "wf5_replenish", "wf6_alerts"],  # 店铺概览 / 整体怎么样
        "sales_only":      ["erp_sales", "noon_orders"],                       # 销量数字（不含库存/物流）
    }
    # 默认陈旧度阈值
    stale_threshold_days = 1

    # 旧字段保留兼容前端 chip
    return {
        "erp": "ok" if latest_w2_imported == today else "warn",
        "noon_sales": "ok" if latest_noon_order == today else "warn",
        "noon_inv": "ok" if latest_noon_stock == today else "warn",
        "feishu": "ok",
        "as_of_date": today,
        "details": {
            "wf1_imported_at":  latest_w1_imported,
            "wf2_imported_at":  latest_w2_imported,
            "wf5_updated_at":   latest_w5_updated,
            "wf3_updated_at":   latest_hub_updated,
            "noon_order_date":  latest_noon_order,
            "noon_stock_date":  latest_noon_stock,
        },
        "sources": sources,  # Agent data_health_check tool 用这个
        "dependency_groups": dependency_groups,  # 用户意图 → 该看哪些源
        "stale_threshold_days": stale_threshold_days,
    }


# ── 今日总览（顶部数据）──────────────────────────────────
def get_today(store: str) -> Dict:
    tid, alias = _resolve_entity_for_store(store)
    return {
        "date": datetime.date.today().isoformat(),
        "store": store.upper(),
        "store_full": f"HIPOP-NOON-{store.upper()}",
        "sku_count": _scalar(
            "SELECT COUNT(*) FROM wf2_sku WHERE tenant_id=? AND entity_alias=? AND is_listed=1",
            (tid, alias),
        ) or 0,
        "urgent_count": _scalar(
            "SELECT COUNT(*) FROM wf5_sales_cycle "
            "WHERE tenant_id=? AND entity_alias=? AND trend IN ('急速下降','下降')",
            (tid, alias),
        ) or 0,
        "in_transit_qty": _scalar(
            "SELECT SUM(in_transit_total_qty) FROM wf3_logistics_hub_v2 WHERE tenant_id=?", (tid,)
        ) or 0,
        "alerts_red": _scalar(
            "SELECT COUNT(*) FROM wf6_logistics_alerts_v2 "
            "WHERE tenant_id=? AND alert_level='红' AND ops_status='待处理'", (tid,)
        ) or 0,
        "alerts_pending": _scalar(
            "SELECT COUNT(*) FROM wf6_logistics_alerts_v2 WHERE tenant_id=? AND ops_status='待处理'", (tid,)
        ) or 0,
    }


# ── Agent 处理事件流（SSE 数据源）────────────────────────
def write_event(task_id: str, step_no: int, step_name: str, status: str, message: str = "",
                actor: Optional[Dict] = None):
    """写工作流执行事件 + 触发方留痕（actor: {user_id, email, role, source}）。

    actor.source ∈ {'chat', 'ui', 'cron', 'upload'}。每个 step 都写一份 actor 列；
    审计时按 task_id 聚合就能看到这个任务由谁、什么 channel 触发。
    """
    tid = get_current_tenant() or 1
    a = actor or {}
    with conn() as c:
        c.execute("""
            INSERT INTO agent_events
              (tenant_id, task_id, step_no, step_name, status, message,
               actor_user_id, actor_email, actor_role, actor_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tid, task_id, step_no, step_name, status, message,
              a.get("user_id"), a.get("email"), a.get("role"), a.get("source")))
        c.commit()


def get_events_after(task_id: str, last_id: int = 0) -> List[Dict]:
    return _fetch("""
        SELECT id, task_id, step_no, step_name, status, message, created_at
        FROM agent_events
        WHERE task_id=? AND id > ?
        ORDER BY id
    """, (task_id, last_id))


def get_progress_current() -> Dict:
    """最近一个任务的进度概览。如果没有任务，返回 mock。"""
    rows = _fetch("""
        SELECT task_id, MAX(step_no) as cur_step, COUNT(DISTINCT step_no) as total
        FROM agent_events
        GROUP BY task_id
        ORDER BY MAX(id) DESC
        LIMIT 1
    """)
    if not rows:
        # 无任何任务：返回空，前端不显示进度卡
        return {"task_id": None, "label": "", "current_step": 0, "total_steps": 0, "steps": []}
    task_id = rows[0]["task_id"]
    events = _fetch("""
        SELECT step_no, step_name, status, message, created_at
        FROM agent_events WHERE task_id=? ORDER BY id
    """, (task_id,))
    # 聚合 steps：按 step_no 取最新 status
    by_step = {}
    for e in events:
        by_step[e["step_no"]] = e
    steps = []
    for sn in sorted(by_step.keys()):
        e = by_step[sn]
        steps.append({
            "name": e["step_name"],
            "status": "done" if e["status"] == "done" else ("now" if e["status"] == "started" else "pending"),
        })
    cur = next((i for i, s in enumerate(steps) if s["status"] == "now"), -1)
    if cur == -1:
        cur = sum(1 for s in steps if s["status"] == "done")
    return {
        "task_id": task_id,
        "label": f"任务 {task_id}",
        "current_step": cur + 1 if steps and steps[-1]["status"] != "done" else cur,
        "total_steps": len(steps),
        "steps": steps,
    }


# ── Chat 消息持久化 ───────────────────────────────────────
def _ensure_chat_table():
    """PG 模式下表已由 db/schema.sql 建好，跳过；SQLite 才动态建。"""
    if is_postgres():
        return
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                role TEXT NOT NULL,
                who TEXT,
                content TEXT NOT NULL,
                tag TEXT,
                references_json TEXT,
                task_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_store_time ON chat_messages(store, id)")
        c.commit()


def write_chat_message(store: str, role: str, who: Optional[str], content: str,
                       tag: Optional[str] = None,
                       references: Optional[List[Dict]] = None,
                       task: Optional[Dict] = None) -> int:
    _ensure_chat_table()
    tid = get_current_tenant() or 1
    if is_postgres():
        sql = ("INSERT INTO chat_messages (tenant_id, store, role, who, content, tag, references_json, task_json) "
               "VALUES (?,?,?,?,?,?,?,?) RETURNING id")
        with conn() as c:
            cur = c.execute(sql, (
                tid, store.upper(), role, who, content, tag,
                json.dumps(references or [], ensure_ascii=False) if references else None,
                json.dumps(task, ensure_ascii=False) if task else None,
            ))
            row = cur.fetchone()
            c.commit()
            return row["id"] if isinstance(row, dict) else row[0]
    with conn() as c:
        cur = c.execute("""
            INSERT INTO chat_messages (store, role, who, content, tag, references_json, task_json)
            VALUES (?,?,?,?,?,?,?)
        """, (
            store.upper(), role, who, content, tag,
            json.dumps(references or [], ensure_ascii=False) if references else None,
            json.dumps(task, ensure_ascii=False) if task else None,
        ))
        c.commit()
        return cur._cur.lastrowid if hasattr(cur, "_cur") else cur.lastrowid


def get_chat_messages(store: str, limit: int = 50) -> List[Dict]:
    _ensure_chat_table()
    rows = _fetch("""
        SELECT id, role, who, content, tag, references_json, task_json, created_at
        FROM chat_messages WHERE store=? ORDER BY id DESC LIMIT ?
    """, (store.upper(), limit))
    rows.reverse()  # 时间正序
    out = []
    for r in rows:
        m = {
            "who": r["who"] or ("Cherry" if r["role"] == "user" else "Agent"),
            "role": r["role"],
            "time": _hhmm(r["created_at"]),  # 'HH:MM'
            "content": r["content"],
            "tag": r["tag"] or "",
        }
        if r.get("references_json"):
            try: m["references"] = json.loads(r["references_json"])
            except Exception: m["references"] = []
        if r.get("task_json"):
            try: m["task"] = json.loads(r["task_json"])
            except Exception: m["task"] = None
        out.append(m)
    return out


# ── Agent Actions（reference 系统）────────────────────────
def write_agent_action(
    store: str, module: str, action_type: str,
    subject: Optional[str] = None,
    pill: Optional[str] = None, pill_text: Optional[str] = None,
    judge: Optional[str] = None, confidence: Optional[float] = None,
    options: Optional[List[Dict]] = None,
    references: Optional[List[Dict]] = None,
    owner: Optional[str] = None,
) -> int:
    tid = get_current_tenant() or 1
    if is_postgres():
        with conn() as c:
            cur = c.execute("""
                INSERT INTO agent_actions
                (tenant_id, store, module, action_type, subject, pill, pill_text, judge, confidence,
                 options_json, references_json, owner)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id
            """, (
                tid, store.upper(), module, action_type, subject, pill, pill_text, judge, confidence,
                json.dumps(options or [], ensure_ascii=False),
                json.dumps(references or [], ensure_ascii=False),
                owner,
            ))
            row = cur.fetchone()
            c.commit()
            return row["id"] if isinstance(row, dict) else row[0]
    with conn() as c:
        cur = c.execute("""
            INSERT INTO agent_actions
            (store, module, action_type, subject, pill, pill_text, judge, confidence,
             options_json, references_json, owner)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            store.upper(), module, action_type, subject, pill, pill_text, judge, confidence,
            json.dumps(options or [], ensure_ascii=False),
            json.dumps(references or [], ensure_ascii=False),
            owner,
        ))
        c.commit()
        return cur._cur.lastrowid if hasattr(cur, "_cur") else cur.lastrowid


# ── 反馈/需求捕获（WS-26）────────────────────────────────
# chat agent 撞到做不了/超范围的事时，用户确认 → 真写入这张表，喂产品迭代。
# 写不进库要 raise（绝不假装记了 —— 报告即事实）。
_feedback_ready = False


def _ensure_feedback_table():
    """建 feedback 表（幂等）。PG 同时建 RLS policy（按 tenant 隔离，防越权串租户）。

    WS-26：feedback 表走**运行时自举**（与 _ensure_chat_table 同套路），不进
    CODEOWNERS 锁定的 db/schema*.sql 主文件 —— 首次 write/read 时按需建表 + RLS，
    SQLite 本地 / PG 部署都不依赖谁先手动迁库。建表是 CREATE TABLE IF NOT EXISTS
    纯增量，不碰任何已有表。
    """
    global _feedback_ready
    if _feedback_ready:
        return
    if is_postgres():
        import psycopg2
        # 用裸连接做 DDL（建表 + RLS policy 需要 owner 权限），不经 _PGConnWrapper。
        raw = psycopg2.connect(DB_URL)
        raw.autocommit = True
        try:
            with raw.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback (
                        id             BIGSERIAL PRIMARY KEY,
                        tenant_id      BIGINT NOT NULL,
                        feedback_user  TEXT,
                        user_role      TEXT,
                        trigger_scene  TEXT,
                        content        TEXT NOT NULL,
                        category       TEXT,
                        store          TEXT,
                        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant "
                            "ON feedback(tenant_id, created_at)")
                cur.execute("ALTER TABLE feedback ENABLE ROW LEVEL SECURITY")
                cur.execute("ALTER TABLE feedback FORCE ROW LEVEL SECURITY")
                cur.execute("DROP POLICY IF EXISTS tenant_isolation ON feedback")
                cur.execute(
                    "CREATE POLICY tenant_isolation ON feedback "
                    "USING (tenant_id = current_setting('app.current_tenant', true)::BIGINT) "
                    "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::BIGINT)"
                )
        finally:
            raw.close()
        _feedback_ready = True
        return
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id      BIGINT NOT NULL DEFAULT 1,
                feedback_user  TEXT,
                user_role      TEXT,
                trigger_scene  TEXT,
                content        TEXT NOT NULL,
                category       TEXT,
                store          TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id, created_at)")
        c.commit()
    _feedback_ready = True


def write_feedback(content: str, *, trigger_scene: Optional[str] = None,
                   category: str = "需求", user: Optional[str] = None,
                   role: Optional[str] = None, store: Optional[str] = None,
                   tenant_id: Optional[int] = None) -> int:
    """把一条用户需求/反馈真写入 feedback 表，返回 row id。

    写不进库**直接 raise**（调用方据此如实报错，绝不假装记了）。content 为空也 raise。
    """
    if not content or not str(content).strip():
        raise ValueError("feedback content 不能为空")
    _ensure_feedback_table()
    tid = tenant_id or get_current_tenant() or 1
    cols = "(tenant_id, feedback_user, user_role, trigger_scene, content, category, store)"
    vals = (tid, user, role, trigger_scene, content, category, store)
    if is_postgres():
        with conn() as c:
            cur = c.execute(f"INSERT INTO feedback {cols} VALUES (?,?,?,?,?,?,?) RETURNING id", vals)
            row = cur.fetchone()
            c.commit()
            return row["id"] if isinstance(row, dict) else row[0]
    with conn() as c:
        cur = c.execute(f"INSERT INTO feedback {cols} VALUES (?,?,?,?,?,?,?)", vals)
        c.commit()
        return cur._cur.lastrowid if hasattr(cur, "_cur") else cur.lastrowid


def get_feedback(tenant_id: Optional[int] = None, limit: int = 50) -> List[Dict]:
    """列出本租户已捕获的需求/反馈（最新在前）。产品迭代/审计读这里。"""
    _ensure_feedback_table()
    tid = tenant_id or get_current_tenant() or 1
    return _fetch(
        "SELECT id, feedback_user, user_role, trigger_scene, content, category, store, created_at "
        "FROM feedback WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
        (tid, limit),
    )


def count_feedback(tenant_id: Optional[int] = None) -> int:
    _ensure_feedback_table()
    tid = tenant_id or get_current_tenant() or 1
    return _scalar("SELECT COUNT(*) FROM feedback WHERE tenant_id=?", (tid,)) or 0


# ── 平台浏览器（紫鸟）租户凭据（WS-33.2）──────────────────────────────
# 形态对齐 tenant_erp_credentials + _crypto：不同 tenant/store 不共用明文，密文进库、
# 解密在 _platform_browser 内做。走**运行时自举**（与 feedback/chat 同套路），不进
# CODEOWNERS 锁定的 db/schema*.sql 主文件。RLS 按 tenant_id 隔离，防越权串租户。
# store_key='*' 表示该 tenant 的默认紫鸟账号；具体 store_key 可给某店单独配账号。
_platform_browser_cred_ready = False


def ensure_platform_browser_cred_table():
    """建 tenant_platform_browser_credentials 表（幂等）。PG 同时建 RLS policy。

    纯增量 CREATE TABLE IF NOT EXISTS，不碰任何已有表 / 不动 db/schema*.sql。
    """
    global _platform_browser_cred_ready
    if _platform_browser_cred_ready:
        return
    if is_postgres():
        import psycopg2
        # 裸连接做 DDL（建表 + RLS policy 需 owner 权限），不经 _PGConnWrapper。
        raw = psycopg2.connect(DB_URL)
        raw.autocommit = True
        try:
            with raw.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tenant_platform_browser_credentials (
                        tenant_id        BIGINT NOT NULL,
                        store_key        TEXT NOT NULL DEFAULT '*',
                        provider         TEXT NOT NULL DEFAULT 'ziniao',
                        company_enc      TEXT,
                        username_enc     TEXT,
                        password_enc     TEXT,
                        web_driver_port  INT,
                        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (tenant_id, store_key)
                    )
                """)
                cur.execute("ALTER TABLE tenant_platform_browser_credentials "
                            "ENABLE ROW LEVEL SECURITY")
                cur.execute("ALTER TABLE tenant_platform_browser_credentials "
                            "FORCE ROW LEVEL SECURITY")
                cur.execute("DROP POLICY IF EXISTS tenant_isolation "
                            "ON tenant_platform_browser_credentials")
                cur.execute(
                    "CREATE POLICY tenant_isolation ON tenant_platform_browser_credentials "
                    "USING (tenant_id = current_setting('app.current_tenant', true)::BIGINT) "
                    "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::BIGINT)"
                )
        finally:
            raw.close()
        _platform_browser_cred_ready = True
        return
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tenant_platform_browser_credentials (
                tenant_id        BIGINT NOT NULL,
                store_key        TEXT NOT NULL DEFAULT '*',
                provider         TEXT NOT NULL DEFAULT 'ziniao',
                company_enc      TEXT,
                username_enc     TEXT,
                password_enc     TEXT,
                web_driver_port  INTEGER,
                updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (tenant_id, store_key)
            )
        """)
        c.commit()
    _platform_browser_cred_ready = True


def get_platform_browser_cred_row(store_key: Optional[str] = None) -> Optional[Dict]:
    """按**当前 tenant context**取一行紫鸟凭据密文（不在此重设 tenant —— RLS/WHERE 用
    调用链已设的 context）。

    查找优先级：精确 (tenant, store_key) → tenant 默认行 (tenant, '*')。命中返回原始
    密文行（company_enc/username_enc/password_enc/web_driver_port），解密由调用方做；
    无 tenant context 或无行 → None（调用方决定回落 config 还是 blocked）。
    """
    tid = get_current_tenant()
    if tid is None:
        return None
    ensure_platform_browser_cred_table()
    keys = ["*"] if not store_key else [store_key, "*"]
    rows = _fetch(
        "SELECT tenant_id, store_key, provider, company_enc, username_enc, "
        "password_enc, web_driver_port FROM tenant_platform_browser_credentials "
        "WHERE tenant_id=? AND store_key IN (%s)" % ",".join("?" * len(keys)),
        (tid, *keys),
    )
    if not rows:
        return None
    # 精确 store_key 优先于默认 '*'
    rows.sort(key=lambda r: 0 if r.get("store_key") == store_key else 1)
    return rows[0]


def sales_entities_for_mapping(active_only: bool = True) -> List[Dict]:
    """列出 sales_entities 作为「平台浏览器 store → tenant/entity 映射」的**唯一真相源**。

    每行带 tenant_id / alias / country / platform / store_name —— 供
    `_platform_browser.resolve_store_entity` 把紫鸟 getBrowserList 枚举到的每个 store 解析
    到唯一 (tenant_id, entity_alias)。**tenant_id 取自匹配到的行**，不是默认值——这正是
    WS-46 契约要钉死的：缺匹配行时调用方必须 blocked，绝不默认塞 tenant=1/当前 entity。

    隔离：PG 走 RLS（app.current_tenant），SQLite 无 RLS 返回全部行；store→entity 映射只
    在「当前 tenant context 已设」的调用链里做（account 凭据本就 tenant-scoped）。
    """
    where = "WHERE active=1" if active_only else ""
    return _fetch(
        "SELECT tenant_id, alias, country, platform, store_name "
        f"FROM sales_entities {where} ORDER BY tenant_id, alias"
    )


def set_action_status(action_id: int, status: str, by: str) -> dict:
    """采纳/拒绝 agent_action。status: adopted / rejected。
    带 tenant 校验（只能改自己租户的 action），adopted_by 由调用方从登录态传（不信前端）。
    PG 走 RLS 自动隔离；SQLite 无 RLS，UPDATE 不带 tenant 但归属在 PG 侧已隔离。"""
    if status not in ("adopted", "rejected"):
        return {"ok": False, "error": f"invalid status: {status}"}
    # 校验 action 存在且属于当前租户（PG RLS 让跨租户查不到）
    row = _fetch("SELECT id, status FROM agent_actions WHERE id=?", (action_id,))
    if not row:
        return {"ok": False, "error": "action 不存在或无权限"}
    ts = "datetime('now','localtime')"
    with conn() as c:
        c.execute(
            f"UPDATE agent_actions SET status=?, adopted_by=?, adopted_at={ts} WHERE id=?",
            (status, by, action_id),
        )
        c.commit()
    return {"ok": True, "id": action_id, "status": status, "by": by}


def get_agent_action(action_id: int) -> Optional[Dict]:
    rows = _fetch("SELECT * FROM agent_actions WHERE id=?", (action_id,))
    if not rows: return None
    r = rows[0]
    try:
        r["options"] = json.loads(r.get("options_json") or "[]")
        r["references"] = json.loads(r.get("references_json") or "[]")
    except Exception:
        r["options"] = []
        r["references"] = []
    return r


def list_agent_actions(store: str, module: Optional[str] = None, limit: int = 30) -> List[Dict]:
    if module:
        rows = _fetch("""
            SELECT * FROM agent_actions WHERE store=? AND module=?
            ORDER BY created_at DESC LIMIT ?
        """, (store.upper(), module, limit))
    else:
        rows = _fetch("""
            SELECT * FROM agent_actions WHERE store=?
            ORDER BY created_at DESC LIMIT ?
        """, (store.upper(), limit))
    for r in rows:
        try:
            r["options"] = json.loads(r.get("options_json") or "[]")
            r["references"] = json.loads(r.get("references_json") or "[]")
        except Exception:
            r["options"] = []
            r["references"] = []
    return rows


# ── 选品（mock + 真策略文档）────────────────────────────
def get_selection_strategies() -> Dict[str, str]:
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent_memory", "strategies")
    out = {}
    for name in ("选品_成功模式_v1.md", "选品_失败模式_v1.md"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out[name] = f.read()
        else:
            out[name] = "(尚未生成)"
    return out


# ── 跨店聚合（刘鹤视图：跟单 · 跨店）─────────────────────
def get_cross_store_logistics() -> Dict:
    """所有店铺的物流告警 + 卡单 SKU 聚合（v2 表 + tenant 隔离）"""
    tid_x = get_current_tenant() or 1
    rows_alerts = _fetch("""
        SELECT alert_id, alert_level, alert_reason, order_no, ops_status,
               sku_list_json, action_owner, actual_stay_days, history_stage_days,
               stage, created_at
        FROM wf6_logistics_alerts_v2 WHERE tenant_id=?
        ORDER BY CASE alert_level
            WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 WHEN '蓝' THEN 4 ELSE 9
        END, created_at DESC
    """, (tid_x,))
    for a in rows_alerts:
        try:
            a["skus"] = json.loads(a.get("sku_list_json") or "[]")
        except Exception:
            a["skus"] = []

    hub_rows = _fetch("""
        SELECT sku, in_transit_total_qty,
               has_stuck_batch, needs_ops_input, groups_json
        FROM wf3_logistics_hub_v2
        WHERE tenant_id=? AND (has_stuck_batch=1 OR needs_ops_input=1)
    """, (tid_x,))
    return {
        "alerts": rows_alerts,
        "stuck_skus": hub_rows,
        "totals": {
            "alerts_total": len(rows_alerts),
            "alerts_red": sum(1 for a in rows_alerts if a["alert_level"] == "红"),
            "alerts_pending": sum(1 for a in rows_alerts if a["ops_status"] == "待处理"),
            "stuck_count": len(hub_rows),
        },
    }

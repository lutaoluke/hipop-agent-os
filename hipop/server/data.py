"""
HIPOP 工作台数据访问层 - 统一从 hipop.db 读取
"""
import sqlite3, os, json, datetime
from typing import List, Dict, Optional, Any

DB_PATH = os.environ.get("HIPOP_DB", "/Users/luke/Downloads/点购工作流/hipop.db")


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fetch(sql: str, params: tuple = ()) -> List[Dict]:
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _scalar(sql: str, params: tuple = ()):
    with conn() as c:
        r = c.execute(sql, params).fetchone()
        return r[0] if r else None


def _feishu_today_count() -> int:
    sql = "SELECT COUNT(*) FROM feishu_digest WHERE date(digest_at)=date('now','localtime')"
    return _scalar(sql) or 0


# ── SKU 健康（销售/库存模块）────────────────────────────────
def get_sku_health(store: str, urgency: Optional[str] = None, limit: int = 30) -> List[Dict]:
    """读 wf2 + wf5 + wf3，按紧急程度排序，返回每个 SKU 的健康详情"""
    s = store.lower()
    sql = f"""
    SELECT
      w2.partner_sku, w2.title, w2.image_url,
      w2.sales_30d, w2.sales_10d, w2.latest_price, w2.latest_profit_rate,
      w2.sales_grade, w2.is_listed, w2.return_rate, w2.cancel_rate,
      w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
      w5.current_pipeline, w5.target_pipeline, w5.wf5_replenish_qty,
      h.in_transit_total_qty, h.has_stuck_batch
    FROM wf2_hipop_{s}_sku w2
    LEFT JOIN wf5_hipop_{s}_sales_cycle w5 ON w2.partner_sku = w5.partner_sku
    LEFT JOIN wf3_logistics_hub h ON w2.partner_sku = h.sku
    WHERE w2.is_listed = 1
    """
    rows = _fetch(sql)
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
    """从 hub 的 groups_json 解出每个货单的状态"""
    rows = _fetch("SELECT sku, groups_json, has_stuck_batch FROM wf3_logistics_hub")
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

    # 合并 wf6_logistics_alerts 状态
    alerts = _fetch("""
        SELECT order_no, alert_level, alert_reason, ops_status, stage,
               actual_stay_days, history_stage_days, sku_list_json
        FROM wf6_logistics_alerts
    """)
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
    """补货建议：从 wf5 sales_cycle 的 wf5_replenish_qty + lost_replenish_qty + 紧迫度推导"""
    s = store.lower()
    rows = _fetch(f"""
        SELECT w2.partner_sku, w2.title, w2.image_url, w2.sales_30d, w2.latest_price,
               w5.trend, w5.daily_rate, w5.urgency, w5.ops_advice, w5.risk_label,
               w5.wf5_replenish_qty, w5.lost_replenish_qty, w5.weekly_total_replenish,
               w5.trigger_reasons, w5.current_pipeline, w5.target_pipeline
        FROM wf2_hipop_{s}_sku w2
        LEFT JOIN wf5_hipop_{s}_sales_cycle w5 ON w2.partner_sku = w5.partner_sku
        WHERE w2.is_listed = 1 AND w5.weekly_total_replenish > 0
        ORDER BY w5.weekly_total_replenish DESC
    """)
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
    """聚合所有模块的'今日重点'"""
    s = store.lower()
    sku_count = _scalar(f"SELECT COUNT(*) FROM wf2_hipop_{s}_sku WHERE is_listed=1") or 0
    urgent_count = _scalar(f"""
        SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle
        WHERE trend IN ('急速下降', '下降')
    """) or 0
    low_margin_count = _scalar(f"""
        SELECT COUNT(*) FROM wf2_hipop_{s}_sku
        WHERE is_listed=1 AND latest_profit_rate IS NOT NULL AND latest_profit_rate < 0.10
    """) or 0
    in_transit_total = _scalar("SELECT SUM(in_transit_total_qty) FROM wf3_logistics_hub") or 0
    in_transit_orders = _scalar("""
        SELECT SUM(in_transit_batch_count) FROM wf3_logistics_hub
    """) or 0
    stuck_skus = _scalar("SELECT COUNT(*) FROM wf3_logistics_hub WHERE has_stuck_batch=1") or 0
    needs_ops = _scalar("SELECT COUNT(*) FROM wf3_logistics_hub WHERE needs_ops_input=1") or 0
    alerts_pending = _scalar("SELECT COUNT(*) FROM wf6_logistics_alerts WHERE ops_status='待处理'") or 0
    alerts_red = _scalar("SELECT COUNT(*) FROM wf6_logistics_alerts WHERE alert_level='红' AND ops_status='待处理'") or 0
    replenish_count = _scalar(f"""
        SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle WHERE weekly_total_replenish > 0
    """) or 0

    # 数据更新时间
    latest_w2 = _scalar(f"SELECT MAX(imported_at) FROM wf2_hipop_{s}_sku") or "未知"
    latest_w5 = _scalar(f"SELECT MAX(updated_at) FROM wf5_hipop_{s}_sales_cycle") or "未知"
    latest_hub = _scalar("SELECT MAX(updated_at) FROM wf3_logistics_hub") or "未知"
    latest_alerts = _scalar("SELECT MAX(updated_at) FROM wf6_logistics_alerts") or "未知"

    # 数据获取健康度
    today_str = datetime.date.today().isoformat()
    noon_fresh = "warn" if (latest_w5 or "")[:10] != today_str else "ok"

    return [
        {
            "key": "data",
            "name": "数据获取",
            "state": noon_fresh,
            "line1": f"商品库 {sku_count} SKU · 销量周期 {_scalar(f'SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle')} 行",
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
    """混合：真 wf6 告警 + mock 飞书 + 真 agent_actions"""
    items = []
    for a in _fetch("""
        SELECT alert_id, alert_level, alert_reason, order_no, sku_list_json,
               ops_status, action_owner, updated_at, created_at
        FROM wf6_logistics_alerts ORDER BY updated_at DESC LIMIT 5
    """):
        sku_list = json.loads(a["sku_list_json"] or "[]")
        sku_str = ", ".join(s["sku"] for s in sku_list[:3])
        t = (a["updated_at"] or a["created_at"] or "")[-8:-3]
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
            "time": (act["created_at"] or "")[-8:-3] or "00:00",
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

    # mock 飞书（保底）
    from . import mock as _mock
    if not [i for i in items if i.get("tag") == "飞书" or i.get("who") == "飞书"]:
        items.extend(_mock.WORK_LOG_MOCK_FEISHU)

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
    latest_w1_imported = (_scalar(f"SELECT MAX(imported_at) FROM wf1_hipop_{s}_stock") or "")[:10]
    latest_w2_imported = (_scalar(f"SELECT MAX(imported_at) FROM wf2_hipop_{s}_sku") or "")[:10]
    latest_w5_updated  = (_scalar(f"SELECT MAX(updated_at) FROM wf5_hipop_{s}_sales_cycle") or "")[:10]
    latest_hub_updated = (_scalar("SELECT MAX(updated_at) FROM wf3_logistics_hub") or "")[:10]
    latest_alerts      = (_scalar("SELECT MAX(created_at) FROM wf6_logistics_alerts") or "")[:10]

    # noon orders 最新订单日期（关键：补货问答的真实数据新鲜度依赖这个）
    latest_noon_order = (_scalar(
        f"SELECT MAX(order_date) FROM wf2_hipop_{s}_orders"
    ) or "")[:10]
    # noon stock 最新导入时间（依赖人工 Inventory CSV）
    latest_noon_stock = (_scalar(
        f"SELECT MAX(imported_at) FROM wf1_hipop_{s}_stock WHERE noon_total_qty IS NOT NULL"
    ) or "")[:10]

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
    s = store.lower()
    return {
        "date": datetime.date.today().isoformat(),
        "store": store.upper(),
        "store_full": f"HIPOP-NOON-{store.upper()}",
        "sku_count": _scalar(f"SELECT COUNT(*) FROM wf2_hipop_{s}_sku WHERE is_listed=1") or 0,
        "urgent_count": _scalar(f"""SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle WHERE trend IN ('急速下降','下降')""") or 0,
        "in_transit_qty": _scalar("SELECT SUM(in_transit_total_qty) FROM wf3_logistics_hub") or 0,
        "alerts_red": _scalar("SELECT COUNT(*) FROM wf6_logistics_alerts WHERE alert_level='红' AND ops_status='待处理'") or 0,
        "alerts_pending": _scalar("SELECT COUNT(*) FROM wf6_logistics_alerts WHERE ops_status='待处理'") or 0,
    }


# ── Agent 处理事件流（SSE 数据源）────────────────────────
def write_event(task_id: str, step_no: int, step_name: str, status: str, message: str = ""):
    with conn() as c:
        c.execute("""
            INSERT INTO agent_events (task_id, step_no, step_name, status, message)
            VALUES (?, ?, ?, ?, ?)
        """, (task_id, step_no, step_name, status, message))
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
        from . import mock as _mock
        return _mock.PROGRESS_MOCK
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
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                role TEXT NOT NULL,            -- 'user' | 'agent'
                who TEXT,                       -- 'Cherry' / 'Agent' / ...
                content TEXT NOT NULL,
                tag TEXT,
                references_json TEXT,           -- JSON array
                task_json TEXT,                 -- workflow_task JSON（仅 agent 触发工作流时）
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
        return cur.lastrowid


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
            "time": (r["created_at"] or "")[-8:-3],  # 'HH:MM'
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
        return cur.lastrowid


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
    """所有店铺的物流告警 + 卡单 SKU 聚合"""
    rows_alerts = _fetch("""
        SELECT alert_id, alert_level, alert_reason, order_no, ops_status,
               sku_list_json, action_owner, actual_stay_days, history_stage_days,
               stage, created_at, updated_at
        FROM wf6_logistics_alerts
        ORDER BY CASE alert_level
            WHEN '红' THEN 1 WHEN '橙' THEN 2 WHEN '黄' THEN 3 WHEN '蓝' THEN 4 ELSE 9
        END, updated_at DESC
    """)
    for a in rows_alerts:
        try:
            a["skus"] = json.loads(a.get("sku_list_json") or "[]")
        except Exception:
            a["skus"] = []

    hub_rows = _fetch("""
        SELECT sku, in_transit_total_qty, in_transit_batch_count,
               has_stuck_batch, needs_ops_input, groups_json
        FROM wf3_logistics_hub
        WHERE has_stuck_batch=1 OR needs_ops_input=1
    """)
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

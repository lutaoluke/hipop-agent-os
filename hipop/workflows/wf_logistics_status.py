"""
工作流三：在途与已完成物流情况

数据中枢表 wf3_logistics_hub（hipop.db），以 SKU 为主键，单表存储：
  - SKU 级聚合（在途总件数、批次数、待运营标识、卡单标识）
  - groups_json：分组明细（country × platform × forwarder × shipping_method）
      ├─ 在途批次：当前阶段、停留天数、阶段历史耗时（加权）、节点链
      └─ 已完成近 3 笔：截断到"已提回海外仓"的总耗时

下游工作流（物流周期计算 / 销售周期与补货分析）从本表读数据，不重复抓 ERP。

CLI：
  python3 wf_logistics_status.py                  # 全量扫 sa_main
  python3 wf_logistics_status.py SKU1 SKU2        # 指定 SKU
"""
import os
import sys
import json
import re
import sqlite3
import warnings
from datetime import datetime
from collections import defaultdict

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from workflows.wf0_logistics import (
    get_erp_token, erp_get, get_order_detail_qty,
    STATUS_DONE, STATUS_VOID,
)

# ── 配置 ────────────────────────────────────────────────────
LOGISTICS_URLS = {
    "义特无忧KSA": "http://tracking.nextsls.com/trace?app=61a0c12f69c1d654e75a49e6",
    "安时达KSA":   "https://logistics.ontaskksad2d.com/#/home",
    "安时达UAE":   "https://logistics.ontaskksad2d.com/#/home",
    "飞坦":        "https://tracking.fleetan.com/#/",
    "维勒":        "http://ywwj.rtb56.com/track_query.aspx",
}
NEEDS_OPS_INPUT = {"阳光UAE"}  # 暂无 URL，由运营手动填入

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")

STUCK_THRESHOLD = 1.5  # 停留 > 1.5x 历史均值视为卡单
COMPLETED_TOPN = 3     # 已完成历史样本数上限
WEIGHTS = [0.5, 0.3, 0.2]  # 加权方案（最新→最早）

# ── 标签过滤（不是真实物流状态）──────────────────────────────
LABEL_PATTERNS = [
    r'^凭证\s*/\s*Proof$',
    r'^Proof\s*/\s*凭证$',
    r"^The shipping company's website has not updated",
]

def is_label(s):
    return any(re.match(p, (s or "").strip(), re.I) for p in LABEL_PATTERNS)

def filter_nodes(nodes):
    return [n for n in nodes if not is_label(n.get("status", ""))]

# ── 状态归类 ────────────────────────────────────────────────
EXCLUDED_STAGES = {"海外仓", "已签收"}  # 截断点：不计入总耗时也不进历史池

def categorize(s):
    """把节点状态归到 6 个阶段。未识别返回 None。"""
    if not s:
        return None
    sl = s.lower()
    # 海外仓（截断）
    if "已提回海外仓" in s or "arrive at the local distribution" in sl:
        return "海外仓"
    # 已签收（排除）
    if "已签收" in s or ("delivered" in sl and "sign" in sl):
        return "已签收"
    # 清关完成
    if "清关已完成" in s or "finished customs clearance" in sl:
        return "清关完成"
    # 到港待清关
    if "已到港" in s:
        return "到港待清关"
    if "arrived at" in sl and ("clearing" in sl or "pending customs" in sl):
        return "到港待清关"
    # 装柜出港（含"等待离港 / 离港延误"）
    if any(k in s for k in ["装柜", "报关"]):
        return "装柜出港"
    if "expected to leave" in sl or "expected to depart" in sl:
        return "装柜出港"
    if "delayed" in sl and ("depart" in sl or "leave" in sl):
        return "装柜出港"
    if any(k in sl for k in ["customs declaration", "export of goods", "customs release", "currently being declared"]):
        return "装柜出港"
    # 海运中
    sea_cn = ["海运", "开船", "中转", "甩箱", "战争", "绕道", "换船", "预计", "晚到", "改港", "到港时间待定", "尾程", "转运"]
    if any(k in s for k in sea_cn):
        return "海运中"
    sea_en = ["transported by sea", "change ships", "have actually left", "will arrive at the port",
              "will need to change", "cargo will arrive", "expected to arrive"]
    if any(k in sl for k in sea_en):
        return "海运中"
    if "delayed" in sl and "arrive" in sl:
        return "海运中"
    if "arrived at" in sl and "port" in sl:
        return "海运中"
    # 国内仓
    if any(k in s for k in ["仓库", "已下单", "已收货", "发往"]):
        return "国内仓"
    if any(k in sl for k in ["arrived at yiwu", "arrived at dongguang", "arrived at shenzhen",
                              "pending inspection", "shipped out"]):
        return "国内仓"
    return None

# ── 时间戳 ──────────────────────────────────────────────────
def parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

# ── 阶段计算 ────────────────────────────────────────────────
def compute_stages(nodes):
    """
    nodes: 最新在前。输出按时间序的 stage 列表。
    最后一个 stage 标记 open=True（in-transit 尚未结束）。
    """
    if len(nodes) < 2:
        return []
    asc = list(reversed(nodes))  # 最早 → 最新
    stages = []
    cur, start = categorize(asc[0]["status"]), parse_ts(asc[0]["time"])
    last_status = asc[0]["status"]
    for i in range(1, len(asc)):
        cat = categorize(asc[i]["status"])
        t = parse_ts(asc[i]["time"])
        if cat != cur:
            if cur and start and t:
                stages.append({
                    "category":     cur,
                    "start":        start,
                    "end":          t,
                    "days":         round((t - start).total_seconds() / 86400, 2),
                    "last_status":  last_status,
                })
            cur, start = cat, t
        last_status = asc[i]["status"]
    # 最后一段不闭合
    if cur and start:
        stages.append({
            "category":     cur,
            "start":        start,
            "end":          None,
            "days":         None,
            "last_status":  last_status,
            "open":         True,
        })
    return stages

def truncated_total(stages):
    """已完成批次总耗时，截断到海外仓为止。"""
    total = sum(s["days"] for s in stages if s.get("days") and s["category"] not in EXCLUDED_STAGES)
    return round(total) if total > 0 else None

# ── 货代级 pool（用于历史耗时加权）───────────────────────────
def build_pool(all_batches):
    """
    all_batches: list of {forwarder, stages, origin: in_transit/completed, sku, order_no}
    输出：pool[forwarder][category] = {'in_transit': [...], 'completed': [...]}
    """
    pool = defaultdict(lambda: defaultdict(lambda: {"in_transit": [], "completed": []}))
    for b in all_batches:
        fw, origin = b["forwarder"], b["origin"]
        # in-transit：只取已闭合的 stages（不含最后一个 open stage）
        stages = b["stages"][:-1] if origin == "in_transit" and b["stages"] else b["stages"]
        for s in stages:
            if s["category"] in EXCLUDED_STAGES or s.get("days") is None:
                continue
            pool[fw][s["category"]][origin].append({
                **s,
                "sku":       b["sku"],
                "order_no":  b["order_no"],
            })
    return pool

def find_history(category, forwarder, pool):
    """
    历史耗时：完成取近 3 笔，在途已走过该阶段的全部纳入。
    合并后按 end_time 倒序，取前 3 加权 0.5/0.3/0.2。
    """
    if not category or category in EXCLUDED_STAGES:
        return None
    bucket = pool.get(forwarder, {}).get(category, {})
    in_t = bucket.get("in_transit", [])
    cp = sorted(bucket.get("completed", []), key=lambda x: x["end"], reverse=True)[:COMPLETED_TOPN]
    combined = sorted(in_t + cp, key=lambda x: x["end"], reverse=True)
    if not combined:
        return None
    top = combined[:len(WEIGHTS)]
    w = WEIGHTS[:len(top)]
    avg = sum(m["days"] * ww for m, ww in zip(top, w)) / sum(w)
    in_set = {(x["sku"], x["order_no"], x["start"]) for x in in_t}
    n_in = sum(1 for m in top if (m["sku"], m["order_no"], m["start"]) in in_set)
    return {
        "avg_days":   round(avg, 1),
        "pool_size":  len(combined),
        "n_in_transit": n_in,
        "n_completed": len(top) - n_in,
    }

# ── 国家/平台解析 ───────────────────────────────────────────
def country_of(store_name):
    s = (store_name or "").upper()
    if "KSA" in s: return "KSA"
    if "UAE" in s: return "UAE"
    return "UNKNOWN"

def platform_of(store_name):
    return "noon" if "NOON" in (store_name or "").upper() else "UNKNOWN"

# ── ERP 查询（无 store 过滤，所有平台都拉）──────────────────
def get_all_orders_unfiltered(sku, token):
    page, items_all = 1, []
    while True:
        data = erp_get("/delivery", {"keyword": sku, "page": page, "page_size": 50}, token)
        items = data.get("data") or []
        items_all.extend(items)
        meta = data.get("meta", {})
        if page * 50 >= meta.get("total", 0) or not items:
            break
        page += 1
    return items_all

# ── 物流网站查询（共享浏览器）───────────────────────────────
def query_tracking(page, forwarder, tracking_no):
    if forwarder in NEEDS_OPS_INPUT:
        return {"nodes": [], "note": "needs_ops_input"}
    url = LOGISTICS_URLS.get(forwarder)
    if not url:
        return {"nodes": [], "note": f"未支持({forwarder})"}
    if not tracking_no:
        return {"nodes": [], "note": "无单号"}
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        if "nextsls" in url:
            inp = page.query_selector("input.ant-input")
            inp.click(); inp.fill(tracking_no)
            page.wait_for_timeout(300)
            for btn in page.query_selector_all("button"):
                if "查询" in btn.inner_text():
                    btn.click(); break
        else:
            inp = page.query_selector("input")
            if inp:
                inp.fill(tracking_no)
                page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        content = page.inner_text("body")
        nodes = parse_nodes(content, url)
        return {"nodes": nodes, "note": "" if nodes else "无节点"}
    except Exception as e:
        return {"nodes": [], "note": f"查询失败: {e}"}

def parse_nodes(content, url):
    """解析物流页面节点，最新在前。"""
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    nodes = []
    if "nextsls" in url:
        pat = re.compile(r'^(.+?)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$')
        for line in lines:
            m = pat.match(line)
            if m:
                status = m.group(1).strip()
                if status:
                    nodes.append({"time": m.group(2), "status": status})
    else:
        for i, line in enumerate(lines):
            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$', line):
                for j in range(i + 1, min(i + 3, len(lines))):
                    c = lines[j].strip()
                    if c and not re.match(r'^\d{4}-\d{2}-\d{2}', c):
                        nodes.append({"time": line, "status": c})
                        break
    seen, out = set(), []
    for n in nodes:
        k = n["time"] + n["status"]
        if k not in seen:
            seen.add(k); out.append(n)
    return out

# ── DB Schema ───────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS wf3_logistics_hub (
    sku                       TEXT PRIMARY KEY,
    in_transit_total_qty      INTEGER NOT NULL DEFAULT 0,
    in_transit_batch_count    INTEGER NOT NULL DEFAULT 0,
    needs_ops_input           INTEGER NOT NULL DEFAULT 0,
    has_stuck_batch           INTEGER NOT NULL DEFAULT 0,
    groups_json               TEXT NOT NULL,
    last_run_status           TEXT,
    updated_at                DATETIME NOT NULL
);
"""

def ensure_schema(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()

def write_hub(sku_record, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO wf3_logistics_hub
        (sku, in_transit_total_qty, in_transit_batch_count, needs_ops_input,
         has_stuck_batch, groups_json, last_run_status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sku_record["sku"],
        sku_record["in_transit_total_qty"],
        sku_record["in_transit_batch_count"],
        1 if sku_record["needs_ops_input"] else 0,
        1 if sku_record["has_stuck_batch"] else 0,
        json.dumps(sku_record["groups"], ensure_ascii=False, default=str),
        sku_record.get("last_run_status", "success"),
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()
    conn.close()

# ── 单 SKU 分析 ─────────────────────────────────────────────
def collect_sku_orders(sku, token):
    """拉 ERP 全部货单 + 在途批次 SKU 件数。"""
    orders = get_all_orders_unfiltered(sku, token)
    in_transit, completed = [], []
    for o in orders:
        st = o.get("status")
        store = (o.get("store") or {}).get("name", "")
        base = {
            "order_no":       o.get("delivery_order_no"),
            "store":          store,
            "country":        country_of(store),
            "platform":       platform_of(store),
            "logistics_name": (o.get("logistics") or {}).get("logistics_name", ""),
            "tracking_no":    o.get("logistics_bill_no", ""),
            "delivery_at":    (o.get("delivery_at") or "")[:10],
            "in_storage_at":  (o.get("latest_in_storage_at") or "")[:10],
            "_order_id":      o.get("id"),
        }
        if st in (STATUS_DONE,):
            completed.append(base)
        elif st not in (STATUS_VOID,) and st is not None and st > 0:
            try:
                base["qty"] = get_order_detail_qty(o["id"], sku, token)
            except Exception:
                base["qty"] = 0
            in_transit.append(base)
    completed.sort(key=lambda x: x["delivery_at"] or "", reverse=True)
    return in_transit, completed

# ── 主入口 ─────────────────────────────────────────────────
def analyze_skus(skus, write_db=True, verbose=True, max_workers: int = 6):
    """对若干 SKU 分析物流情况，写入 wf3_logistics_hub。返回 list of sku_record。

    max_workers：阶段 1 ERP 拉单并发 worker 数。默认 6（ERP API 抗住 6 并发不限流；
    单线程 255 SKU ≈ 15-20 分钟，6 并发 ≈ 3-4 分钟）。
    """
    if write_db:
        ensure_schema()
    token = get_erp_token()

    # 阶段 1：ERP 全量拉单（并发 ThreadPool — 单 SKU 内多个 HTTP 也是 IO-bound）
    if verbose: print(f"=== 阶段 1：ERP 拉单（{len(skus)} SKU × {max_workers} 并发）===")
    sku_orders = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_sku = {ex.submit(collect_sku_orders, sku, token): sku for sku in skus}
        done = 0
        for fut in as_completed(future_to_sku):
            sku = future_to_sku[fut]
            done += 1
            try:
                in_t, cp = fut.result()
                sku_orders[sku] = (in_t, cp)
                if verbose and (done % 20 == 0 or len(in_t) > 0):
                    print(f"  [{done}/{len(skus)}] {sku}: 在途 {len(in_t)} | 已完成 {len(cp)}", flush=True)
            except Exception as e:
                sku_orders[sku] = ([], [])
                if verbose:
                    print(f"  [{done}/{len(skus)}] {sku}: ERROR {e}", flush=True)

    # 阶段 2：物流站抓节点（共享浏览器）
    if verbose: print("\n=== 阶段 2：物流站抓节点 ===")
    from playwright.sync_api import sync_playwright
    batch_nodes = {}  # (sku, order_no) -> {nodes, note}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # 在途批次必查 + 每个 (sku, country, forwarder) 已完成 top 3
        queries = []
        for sku, (in_t, cp) in sku_orders.items():
            for b in in_t:
                queries.append((sku, b))
            # 每个分组取近 3 笔已完成
            groups_cp = defaultdict(list)
            for b in cp:
                groups_cp[(b["country"], b["platform"], b["logistics_name"])].append(b)
            for k, lst in groups_cp.items():
                for b in lst[:COMPLETED_TOPN]:
                    queries.append((sku, b))

        # 去重（同一 tracking_no 在多 SKU 下复用）
        seen = {}  # (forwarder, tracking_no) -> result
        for sku, b in queries:
            key = (b["logistics_name"], b["tracking_no"])
            if key in seen:
                batch_nodes[(sku, b["order_no"])] = seen[key]
                continue
            if verbose:
                print(f"  {sku}/{b['order_no']} | {b['logistics_name']} | {b['tracking_no']}")
            res = query_tracking(page, b["logistics_name"], b["tracking_no"])
            seen[key] = res
            batch_nodes[(sku, b["order_no"])] = res
        browser.close()

    # 阶段 3：构建 pool（货代级）
    all_batches = []
    for sku, (in_t, cp) in sku_orders.items():
        for b in in_t:
            r = batch_nodes.get((sku, b["order_no"]), {"nodes": [], "note": ""})
            stages = compute_stages(filter_nodes(r["nodes"]))
            all_batches.append({"forwarder": b["logistics_name"], "stages": stages,
                                "origin": "in_transit", "sku": sku, "order_no": b["order_no"]})
        # 完成的 top 3 才进 pool（按分组）
        groups_cp = defaultdict(list)
        for b in cp:
            groups_cp[(b["country"], b["platform"], b["logistics_name"])].append(b)
        for k, lst in groups_cp.items():
            for b in lst[:COMPLETED_TOPN]:
                r = batch_nodes.get((sku, b["order_no"]), {"nodes": [], "note": ""})
                stages = compute_stages(filter_nodes(r["nodes"]))
                all_batches.append({"forwarder": b["logistics_name"], "stages": stages,
                                    "origin": "completed", "sku": sku, "order_no": b["order_no"]})
    pool = build_pool(all_batches)

    # 阶段 4：组装 sku_record + 写库
    today = datetime.now()
    results = []
    for sku, (in_t, cp) in sku_orders.items():
        groups = []
        # 按 (country, platform, forwarder) 分组
        all_keys = set()
        for b in in_t + cp:
            all_keys.add((b["country"], b["platform"], b["logistics_name"]))
        for (country, platform, fw) in sorted(all_keys):
            in_t_sub = [b for b in in_t if (b["country"], b["platform"], b["logistics_name"]) == (country, platform, fw)]
            cp_sub   = [b for b in cp   if (b["country"], b["platform"], b["logistics_name"]) == (country, platform, fw)]
            cp_top3  = cp_sub[:COMPLETED_TOPN]
            need_ops = fw in NEEDS_OPS_INPUT

            in_t_out = []
            for b in in_t_sub:
                r = batch_nodes.get((sku, b["order_no"]), {"nodes": [], "note": ""})
                nodes_f = filter_nodes(r["nodes"])
                stages = compute_stages(nodes_f)
                cur_stage = stages[-1] if stages else None
                if cur_stage:
                    stay = (today - cur_stage["start"]).days if cur_stage["start"] else None
                    cat = cur_stage["category"]
                    last_status = cur_stage["last_status"]
                    started_at = cur_stage["start"]
                else:
                    stay, cat, last_status, started_at = None, None, "", None
                hist = find_history(cat, fw, pool) if cat else None
                is_stuck = bool(hist and stay and stay > hist["avg_days"] * STUCK_THRESHOLD)
                in_t_out.append({
                    "order_no":            b["order_no"],
                    "tracking_no":         b["tracking_no"],
                    "qty":                 b.get("qty", 0),
                    "delivery_at":         b["delivery_at"],
                    "current_stage":       cat,
                    "current_status_text": last_status,
                    "stage_started_at":    started_at.isoformat(timespec="seconds") if started_at else None,
                    "stage_stay_days":     stay,
                    "history_stage_days":  hist["avg_days"] if hist else None,
                    "history_pool_size":   hist["pool_size"] if hist else 0,
                    "is_stuck":            is_stuck,
                    "note":                r.get("note", ""),
                    "nodes":               nodes_f,
                })

            cp_out, cp_totals = [], []
            for b in cp_top3:
                r = batch_nodes.get((sku, b["order_no"]), {"nodes": [], "note": ""})
                nodes_f = filter_nodes(r["nodes"])
                stages = compute_stages(nodes_f)
                tot = truncated_total(stages)
                if tot: cp_totals.append(tot)
                cp_out.append({
                    "order_no":      b["order_no"],
                    "tracking_no":   b["tracking_no"],
                    "delivery_at":   b["delivery_at"],
                    "in_storage_at": b["in_storage_at"],
                    "total_days":    tot,
                    "note":          r.get("note", ""),
                    "nodes":         nodes_f,
                })
            avg_total = round(sum(cp_totals) / len(cp_totals)) if cp_totals else None

            in_t_qty = sum(b.get("qty", 0) for b in in_t_sub if isinstance(b.get("qty"), int))
            groups.append({
                "country":               country,
                "platform":               platform,
                "forwarder":              fw,
                "shipping_method":        "海运",
                "needs_ops_input":        need_ops,
                "in_transit_count":       len(in_t_sub),
                "in_transit_qty":         in_t_qty,
                "in_transit_batches":     in_t_out,
                "completed_avg_total_days": avg_total,
                "completed_n":            len(cp_totals),
                "completed_recent3":      cp_out,
            })

        record = {
            "sku":                    sku,
            "in_transit_total_qty":   sum(g["in_transit_qty"] for g in groups),
            "in_transit_batch_count": sum(g["in_transit_count"] for g in groups),
            "needs_ops_input":        any(g["needs_ops_input"] for g in groups),
            "has_stuck_batch":        any(b["is_stuck"] for g in groups for b in g["in_transit_batches"]),
            "groups":                 groups,
        }
        results.append(record)
        if write_db:
            write_hub(record)

    return results

# ── CLI ────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("skus", nargs="*", help="指定 SKU 列表（默认从 sa_main 全量）")
    ap.add_argument("--no-sync", action="store_true", help="跳过同步到飞书")
    args = ap.parse_args()

    if args.skus:
        skus = args.skus
    else:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT DISTINCT \"ERP-SKU\" FROM sa_main WHERE \"ERP-SKU\" IS NOT NULL").fetchall()
        conn.close()
        skus = [r[0] for r in rows]
        print(f"全量模式：{len(skus)} 个 SKU")
    results = analyze_skus(skus, write_db=True, verbose=True)
    print(f"\n完成。共 {len(results)} 个 SKU 写入 wf3_logistics_hub。")
    stuck = [r for r in results if r["has_stuck_batch"]]
    need_ops = [r for r in results if r["needs_ops_input"]]
    if stuck:
        print(f"  ⚠️ 卡单 SKU: {len(stuck)} 个 — {[r['sku'] for r in stuck]}")
    if need_ops:
        print(f"  🔔 待运营 SKU: {len(need_ops)} 个 — {[r['sku'] for r in need_ops]}")

    if not args.no_sync:
        try:
            from scripts.feishu_sync import sync_all
            print(f"\n→ 同步到飞书...")
            sync_all(skus=skus, tables=["hub"], verbose=True)
        except Exception as e:
            print(f"  ⚠️ 同步飞书失败: {e}")


if __name__ == "__main__":
    main()

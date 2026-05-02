"""
新工作流三 - Phase B 验证
读 Phase A 的 JSON，对每个 SKU 按 (country, platform, forwarder) 分组：
  - 在途批次：查最新节点 + 上一节点时间戳 → 输出 ③ ④
  - 已完成近 3 笔：查全节点链 → 输出 ⑤ ⑥
阳光UAE：URL 缺失，留空状态、标 needs_ops_input
"""
import sys, os, json, re
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from workflows.wf0_logistics import _parse_nodes

LOGISTICS_URLS = {
    "义特无忧KSA": "http://tracking.nextsls.com/trace?app=61a0c12f69c1d654e75a49e6",
    "安时达KSA":   "https://logistics.ontaskksad2d.com/#/home",
    "安时达UAE":   "https://logistics.ontaskksad2d.com/#/home",
    "飞坦":        "https://tracking.fleetan.com/#/",
    "维勒":        "http://ywwj.rtb56.com/track_query.aspx",
}
NEEDS_OPS_INPUT = {"阳光UAE"}

PHASE_A = "/tmp/wf3_phase_a.json"
OUT     = "/tmp/wf3_phase_b.json"


def parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def query_tracking(page, forwarder, tracking_no):
    if forwarder in NEEDS_OPS_INPUT:
        return {"status": "", "nodes": [], "note": "needs_ops_input"}
    url = LOGISTICS_URLS.get(forwarder)
    if not url:
        return {"status": "", "nodes": [], "note": f"未支持({forwarder})"}
    if not tracking_no:
        return {"status": "", "nodes": [], "note": "无单号"}

    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        if "nextsls" in url:
            inp = page.query_selector("input.ant-input")
            inp.click()
            inp.fill(tracking_no)
            page.wait_for_timeout(300)
            for btn in page.query_selector_all("button"):
                if "查询" in btn.inner_text():
                    btn.click()
                    break
        else:
            inp = page.query_selector("input")
            if inp:
                inp.fill(tracking_no)
                page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        content = page.inner_text("body")
        nodes = _parse_nodes(content, url)
        latest = nodes[0]["status"] if nodes else ""
        return {"status": latest, "nodes": nodes, "note": "" if nodes else "无节点"}
    except Exception as e:
        return {"status": "", "nodes": [], "note": f"查询失败: {e}"}


def country_of(store_name):
    s = (store_name or "").upper()
    if "KSA" in s: return "KSA"
    if "UAE" in s: return "UAE"
    return "未知"

def platform_of(store_name):
    return "noon" if "NOON" in (store_name or "").upper() else "未知"


def main():
    phase_a = json.load(open(PHASE_A, encoding="utf-8"))

    from playwright.sync_api import sync_playwright
    results = []
    today = datetime.now()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for sku, sd in phase_a["sku_data"].items():
            print(f"\n=== {sku} ===")

            groups = defaultdict(lambda: {"in_transit": [], "completed": []})
            for b in sd["in_transit"]:
                key = (country_of(b["store"]), platform_of(b["store"]), b["logistics_name"])
                groups[key]["in_transit"].append(b)
            for b in sd["completed"]:
                key = (country_of(b["store"]), platform_of(b["store"]), b["logistics_name"])
                groups[key]["completed"].append(b)

            sku_groups_out = []
            for (country, platform, fw), gd in groups.items():
                # 在途
                in_transit_out = []
                in_transit_qty_total = 0
                for b in gd["in_transit"]:
                    qty = b["qty"] if isinstance(b["qty"], int) else 0
                    in_transit_qty_total += qty
                    print(f"  [在途] {b['order_no']} | {fw} | {b['tracking_no']}")
                    tr = query_tracking(page, fw, b["tracking_no"])
                    nodes = tr["nodes"]
                    stay_days = None
                    if nodes:
                        t = parse_ts(nodes[0]["time"])
                        if t:
                            stay_days = (today - t).days
                    in_transit_out.append({
                        "order_no": b["order_no"],
                        "tracking_no": b["tracking_no"],
                        "qty": qty,
                        "delivery_at": b["delivery_at"],
                        "latest_status": tr["status"],
                        "current_state_stay_days": stay_days,  # 输出 ④（A 含义）
                        "note": tr["note"],
                        "nodes": nodes,
                    })

                # 已完成 top 3
                completed_top3 = sorted(gd["completed"], key=lambda x: x["delivery_at"], reverse=True)[:3]
                completed_out = []
                for b in completed_top3:
                    print(f"  [已完成] {b['order_no']} | {fw} | {b['tracking_no']}")
                    tr = query_tracking(page, fw, b["tracking_no"])
                    nodes = tr["nodes"]
                    intervals, total_days = [], None
                    if len(nodes) >= 2:
                        t_start = parse_ts(nodes[-1]["time"])
                        t_end   = parse_ts(nodes[0]["time"])
                        if t_start and t_end:
                            total_days = (t_end - t_start).days
                        for i in range(len(nodes) - 1):
                            tc = parse_ts(nodes[i]["time"])
                            tp = parse_ts(nodes[i+1]["time"])
                            if tc and tp:
                                hrs = (tc - tp).total_seconds() / 3600
                                intervals.append({
                                    "from": nodes[i+1]["status"],
                                    "to":   nodes[i]["status"],
                                    "hours": round(hrs, 1),
                                    "days":  round(hrs / 24, 2),
                                })
                    completed_out.append({
                        "order_no": b["order_no"],
                        "tracking_no": b["tracking_no"],
                        "delivery_at": b["delivery_at"],
                        "in_storage_at": b["in_storage_at"],
                        "total_days": total_days,
                        "intervals": intervals,
                        "note": tr["note"],
                        "nodes": nodes,
                    })

                sku_groups_out.append({
                    "country": country,
                    "platform": platform,
                    "forwarder": fw,
                    "shipping_method": "海运",
                    "in_transit_count": len(gd["in_transit"]),
                    "in_transit_qty":   in_transit_qty_total,
                    "in_transit_batches": in_transit_out,
                    "completed_recent3":  completed_out,
                })

            results.append({"sku": sku, "groups": sku_groups_out})

        browser.close()

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, default=str, indent=2)
    print(f"\n完成 → {OUT}")


if __name__ == "__main__":
    main()

"""
工作流零 (DEPRECATED v1)：物流周期计算 & 在途商品数量

⚠️ 已被 wf_logistics_status.py + wf3_logistics_hub 取代:
  - 老版从 sa_main 读全量 SKU 池, 写 sa_main."发货在途"
  - 新版从 wf2_<alias>_sku UNION 读, 写 wf3_logistics_hub.in_transit_total_qty
保留此文件仅供历史回溯, 不再被链路调用 (server/skills.py 已切到新 worker)。

老逻辑保留功能:
  1. 在途库存及其数量
  2. 平均物流时长
  3. SKU 最快到货个数及时间估算
  4. SKU 剩余商品到货时间估算

复用项:
  - get_erp_token / erp_get / get_order_detail_qty 等底层 ERP API 函数
    被 wf_logistics_status import 复用 (这部分仍然活的, 不受 deprecation 影响)
"""

import sys
import os
import re
import json
import warnings
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── 平台/店铺过滤 ────────────────────────────────────────
_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "hipop.json")
try:
    with open(_cfg_path, encoding="utf-8") as _f:
        _CFG = json.load(_f)
except Exception:
    _CFG = {}
STORE_KEYWORD = _CFG.get("platform", {}).get("store_keyword", "")  # 如 "KSA"

def _store_match(order):
    """只保留目标平台店铺的发货单，未配置则不过滤。"""
    if not STORE_KEYWORD:
        return True
    store_name = (order.get("store") or {}).get("name", "")
    return STORE_KEYWORD.upper() in store_name.upper()

# ── 状态码 ──────────────────────────────────────────────
STATUS_DONE = 6   # 已完成
STATUS_VOID = 7   # 已作废
IN_TRANSIT_LABELS = {1:"待确认", 2:"已确认", 3:"已拣货", 4:"运输中", 5:"待签收"}

# ── 物流公司查询网址 ─────────────────────────────────────
LOGISTICS_URLS = {
    "义特无忧KSA": "http://tracking.nextsls.com/trace?app=61a0c12f69c1d654e75a49e6",
    "安时达KSA":   "https://logistics.ontaskksad2d.com/#/home",
    "安时达UAE":   "https://logistics.ontaskksad2d.com/#/home",
    "飞坦":        "https://tracking.fleetan.com/#/",
    "维靳":        None,
}

# ── 阶段分类 ─────────────────────────────────────────────
def _classify_stage(status_text):
    t = (status_text or "").lower()
    if any(k in t for k in ["delivered", "signed", "签收", "派送完成"]):
        return "已签收"
    if any(k in t for k in ["已提回海外仓", "distribution center", "local distribution", "分拨中心"]):
        return "海外仓最后一公里"
    if any(k in t for k in ["clearance completed", "清关已完成", "finished customs"]):
        return "清关完成"
    if any(k in t for k in ["clearing", "清关中", "customs", "port", "港口",
                              "jebel ali", "mondra", "sohar", "dammam", "到港",
                              "凭证", "proof", "arrival time at"]):
        return "目的港/清关中"
    if any(k in t for k in ["sea", "海运", "海上", "transported by sea"]):
        return "海运中"
    if any(k in t for k in ["left the port", "离港", "actually left", "装柜",
                              "出口", "报关", "export completed", "customs declaration"]):
        return "已出发/报关"
    if any(k in t for k in ["已收货", "arrived at", "pending inspection", "待qc",
                              "dongguang", "深圳仓库", "shenzhen"]):
        return "国内仓/待发"
    return None

# ── 时间戳解析 ───────────────────────────────────────────
def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"无法解析: {s}")

# ── ERP Token ────────────────────────────────────────────
_token_cache = {}

def get_erp_token():
    if _token_cache.get("token"):
        return _token_cache["token"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        captured = {}
        page.on("request", lambda r: captured.update(
            {"token": r.headers["authorization"].replace("Bearer ", "")}
        ) if r.headers.get("authorization", "").startswith("Bearer ")
            and "erp-api" in r.url else None)
        page.goto("https://www.dbuyerp.com", wait_until="networkidle", timeout=15000)
        _erp_user = os.environ.get("ERP_USERNAME") or ""
        _erp_pw   = os.environ.get("ERP_PASSWORD") or ""
        if not (_erp_user and _erp_pw):
            raise RuntimeError("ERP_USERNAME / ERP_PASSWORD env 未设，无法登录 ERP")
        page.fill('input[placeholder="Username"]', _erp_user)
        page.fill('input[placeholder="Password"]', _erp_pw)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        page.goto("https://www.dbuyerp.com/#/system/delivery/list",
                  wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(2000)
        browser.close()
    token = captured.get("token", "")
    _token_cache["token"] = token
    return token

# ── ERP API ──────────────────────────────────────────────
ERP_API = "https://erp-api.dbuyerp.com/admin"

def erp_get(path, params=None, token=None):
    token = token or get_erp_token()
    # ERP API 偶发 SSL EOF / 超时, 加 3 次指数退避重试
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{ERP_API}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
            return resp.json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            import time
            time.sleep(0.5 * (2 ** attempt))  # 0.5, 1, 2 秒
    raise last_exc

_forwarder_avg_cache = {}   # 模块级缓存，同一进程内只扫一次

def get_forwarder_avg_cross_sku(token, scan_pages=30):
    """
    跨 SKU 扫描近期已完成货单，按货代统计加权物流均值（最新在前，权重0.5/0.3/0.2）。
    同一进程内结果缓存，避免重复翻页。
    """
    if _forwarder_avg_cache:
        return _forwarder_avg_cache

    by_fw = defaultdict(list)
    for page in range(1, scan_pages + 1):
        data  = erp_get("/delivery", {"page": page, "page_size": 10}, token)
        items = data.get("data") or []
        if not items:
            break
        for o in items:
            if o.get("status") != STATUS_DONE:
                continue
            if not _store_match(o):
                continue
            lname  = (o.get("logistics") or {}).get("logistics_name", "")
            del_at = (o.get("delivery_at") or "")[:10]
            in_at  = (o.get("latest_in_storage_at") or "")[:10]
            if lname and del_at and in_at:
                try:
                    days = (datetime.strptime(in_at, "%Y-%m-%d") -
                            datetime.strptime(del_at, "%Y-%m-%d")).days
                    if days > 0:
                        by_fw[lname].append(days)  # 已按时间倒序，最新在前
                except Exception:
                    pass

    def _wavg(lst):
        w = [0.5, 0.3, 0.2][:len(lst)]
        return round(sum(d * ww for d, ww in zip(lst, w)) / sum(w))

    result = {fw: _wavg(days[:5]) for fw, days in by_fw.items() if days}
    _forwarder_avg_cache.update(result)
    return result


def get_all_orders(sku, token=None):
    token = token or get_erp_token()
    page, all_items = 1, []
    while True:
        data = erp_get("/delivery", {"keyword": sku, "page": page, "page_size": 50}, token)
        items = data.get("data") or []
        all_items.extend([o for o in items if _store_match(o)])
        meta = data.get("meta", {})
        if page * 50 >= meta.get("total", 0) or not items:
            break
        page += 1
    return all_items

def get_order_detail_qty(order_id, sku, token=None):
    """翻页查找该 SKU 在货单明细中的发货数量（API 固定每页10条）"""
    token = token or get_erp_token()
    page = 1
    while True:
        data = erp_get(f"/delivery/{order_id}/detail", {"page": page, "page_size": 20}, token)
        items = data.get("data") or []
        if not items:
            break
        for item in items:
            if item.get("sku_id") == sku:
                return item.get("delivery_quantity", 0)
        page += 1
    return 0

# ── 物流追踪 ─────────────────────────────────────────────
def track_shipment(logistics_name, tracking_no):
    url = LOGISTICS_URLS.get(logistics_name)
    if not url:
        return f"⏸️ {logistics_name} 暂不支持", []
    if not tracking_no:
        return "❌ 无物流单号", []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(2000)
        try:
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
                inp.fill(tracking_no)
                page.keyboard.press("Enter")
            page.wait_for_timeout(4000)
            content = page.inner_text("body")
        except Exception as e:
            content = f"查询失败: {e}"
        finally:
            browser.close()

    nodes = _parse_nodes(content, url)
    latest = nodes[0]["status"] if nodes else content[:100]
    return latest, nodes


def _parse_nodes(content, url):
    """解析物流页面节点，最新在前"""
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    nodes = []

    if "nextsls" in url:
        # 义特无忧：状态+时间戳在同一行末尾（YYYY-MM-DD HH:MM:SS）
        pat = re.compile(r'^(.+?)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$')
        for line in lines:
            m = pat.match(line)
            if m:
                status = m.group(1).strip()
                if status:
                    nodes.append({"time": m.group(2), "status": status})
    else:
        # 安时达/飞坦：时间戳独立成行（YYYY-MM-DD HH:MM），状态在其后一行
        for i, line in enumerate(lines):
            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$', line):
                for j in range(i + 1, min(i + 3, len(lines))):
                    c = lines[j].strip()
                    if c and not re.match(r'^\d{4}-\d{2}-\d{2}', c):
                        nodes.append({"time": line, "status": c})
                        break

    # 去重
    seen, out = set(), []
    for n in nodes:
        k = n["time"] + n["status"]
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out

# ── 阶段剩余天数：从历史追踪日志中推导 ──────────────────
def build_stage_timing(historical_nodes_list):
    """
    输入：历史已完成货单的追踪节点列表（每个元素是一条货单的 nodes）
    输出：{阶段名: 该阶段距最终入仓的平均剩余天数}
    """
    buckets = defaultdict(list)
    for nodes in historical_nodes_list:
        if len(nodes) < 2:
            continue
        try:
            final_t = _parse_ts(nodes[0]["time"])
        except Exception:
            continue
        for node in nodes[1:]:
            try:
                node_t = _parse_ts(node["time"])
                remaining = (final_t - node_t).days
                if remaining < 0:
                    continue
                stage = _classify_stage(node["status"])
                if stage and stage != "已签收":
                    buckets[stage].append(remaining)
            except Exception:
                pass
    return {s: round(sum(v) / len(v)) for s, v in buckets.items() if v}


def stage_remaining(current_node, stage_timing, fallback_days=30):
    """用历史推导的阶段时长估算剩余天数，无匹配则用 fallback"""
    stage = _classify_stage(current_node)
    if stage and stage in stage_timing:
        return stage_timing[stage]
    # 阶段有分类但历史数据里没有，用相邻阶段兜底
    order = ["国内仓/待发", "已出发/报关", "海运中", "目的港/清关中",
             "清关完成", "海外仓最后一公里", "已签收"]
    if stage in order:
        idx = order.index(stage)
        for s in order[idx:]:
            if s in stage_timing:
                return stage_timing[s]
    return fallback_days

# ── 核心分析 ─────────────────────────────────────────────
def analyze_sku(sku, verbose=True):
    token = get_erp_token()
    today = date.today()

    if verbose:
        print(f"\n{'='*58}")
        print(f"  SKU: {sku}")
        print(f"{'='*58}")

    orders = get_all_orders(sku, token)
    if not orders:
        return {"sku": sku, "error": "ERP 无货单记录"}

    in_transit_orders = [o for o in orders
                         if o.get("status") not in (STATUS_DONE, STATUS_VOID)
                         and (o.get("status") or 0) > 0]
    completed_orders  = [o for o in orders
                         if o.get("status") == STATUS_DONE
                         and o.get("delivery_at")]
    completed_orders.sort(key=lambda x: x["delivery_at"], reverse=True)
    recent3 = completed_orders[:3]

    # ── 1. 在途库存及其数量 ──────────────────────────────
    transit_batches = []
    total_transit_qty = 0

    for order in in_transit_orders:
        oid   = order["id"]
        ono   = order.get("delivery_order_no", "")
        label = IN_TRANSIT_LABELS.get(order.get("status"), str(order.get("status")))
        lobj  = order.get("logistics") or {}
        lname = lobj.get("logistics_name", "")
        bill  = order.get("logistics_bill_no", "")
        del_at = (order.get("delivery_at") or "")[:10]

        qty = get_order_detail_qty(oid, sku, token)
        total_transit_qty += qty
        transit_batches.append({
            "order_no": ono, "status": label,
            "logistics": lname, "bill_no": bill,
            "delivery_at": del_at, "sku_qty": qty,
        })
        if verbose:
            print(f"[在途] {ono} | {label} | {lname} | {bill} | {del_at} | {qty}件")

    if verbose:
        print(f"\n→ 字段①在途库存：{total_transit_qty}件（{len(transit_batches)}批）")

    # ── 2. 平均物流时长（货代维度：跨SKU均值为基准，SKU自身历史修正清关特性）────────
    cross_sku_avg = get_forwarder_avg_cross_sku(token)   # 跨SKU货代均值

    transit_day_list   = []          # 全局兜底
    historical_nodes_list = []

    days_by_forwarder  = defaultdict(list)   # {货代名: [天数, ...]}  最新在前
    nodes_by_forwarder = defaultdict(list)   # {货代名: [nodes, ...]}

    for o in recent3:
        ono   = o.get("delivery_order_no", "")
        lobj  = o.get("logistics") or {}
        lname = lobj.get("logistics_name", "") or "未知货代"
        bill  = o.get("logistics_bill_no", "")
        del_at_str = o["delivery_at"][:10]
        erp_in  = o.get("latest_in_storage_at")
        days    = None
        nodes   = []
        tracking_end = None

        if lname and bill:
            if verbose:
                print(f"[历史] {ono} | {lname} | {bill} | 查询中...")
            _, nodes = track_shipment(lname, bill)
            if len(nodes) >= 2:
                try:
                    t_start = _parse_ts(nodes[-1]["time"])
                    t_end   = _parse_ts(nodes[0]["time"])
                    tracking_end = t_end
                    days = (t_end - t_start).days
                except Exception:
                    pass

        # max(物流网站末节点, ERP入仓时间)
        if erp_in:
            erp_end = datetime.strptime(erp_in[:10], "%Y-%m-%d")
            if tracking_end is None or erp_end > tracking_end:
                d1   = datetime.strptime(del_at_str, "%Y-%m-%d")
                days = (erp_end - d1).days
                src  = "ERP备用" if tracking_end is None else "ERP修正(追踪未完整)"
                if verbose:
                    print(f"  {del_at_str} → {erp_in[:10]} = {days}天 ({src})")
                if nodes:
                    nodes[0] = {"time": erp_in[:16].replace("T", " "), "status": nodes[0]["status"]}
            else:
                if verbose:
                    print(f"  物流网站: {nodes[-1]['time']} → {nodes[0]['time']} = {days}天 ✓")
        elif days is not None and verbose:
            print(f"  物流网站: {nodes[-1]['time']} → {nodes[0]['time']} = {days}天")

        if days is not None:
            transit_day_list.append(days)
            days_by_forwarder[lname].append(days)
        if nodes:
            historical_nodes_list.append(nodes)
            nodes_by_forwarder[lname].append(nodes)

    def _weighted_avg(days_list):
        """加权平均，最新一笔权重0.5，次新0.3，最早0.2"""
        w = [0.5, 0.3, 0.2][:len(days_list)]
        total_w = sum(w)
        return round(sum(d * ww for d, ww in zip(days_list, w)) / total_w)

    # 全局兜底均值
    avg_days = _weighted_avg(transit_day_list) if transit_day_list else None

    # 各货代均值：有 SKU 自身历史数据时与跨SKU均值混合（SKU特性占40%，跨SKU占60%）
    # 无 SKU 自身数据时直接用跨SKU均值；两者都没有则 None
    avg_by_forwarder = {}
    all_fws = set(days_by_forwarder.keys()) | set(cross_sku_avg.keys())
    for fw in all_fws:
        sku_avg    = _weighted_avg(days_by_forwarder[fw]) if days_by_forwarder.get(fw) else None
        cross_avg  = cross_sku_avg.get(fw)
        if sku_avg and cross_avg:
            avg_by_forwarder[fw] = round(sku_avg * 0.4 + cross_avg * 0.6)
        elif cross_avg:
            avg_by_forwarder[fw] = cross_avg
        elif sku_avg:
            avg_by_forwarder[fw] = sku_avg

    # 各货代阶段剩余时长（含该SKU的清关历史特性）
    stage_timing = build_stage_timing(historical_nodes_list)
    stage_timing_by_forwarder = {
        k: build_stage_timing(v) for k, v in nodes_by_forwarder.items()
    }

    if verbose:
        if avg_days:
            print(f"\n→ 字段②平均物流时长（全局）：{avg_days}天（近{len(transit_day_list)}笔加权）")
        else:
            print("\n→ 字段②平均物流时长：历史数据不足")
        for fw, fd in avg_by_forwarder.items():
            print(f"  [{fw}] 均值: {fd}天")
        if stage_timing:
            print(f"  阶段剩余估算（全局）：{stage_timing}")

    # ── 3&4. 各在途批次：查询节点 + 估算剩余天数 ─────────
    for batch in transit_batches:
        if not batch["bill_no"]:
            batch["tracking_status"] = "无物流单号"
            batch["remaining_days"]  = None
            batch["est_arrival"]     = "无法估算"
            batch["elapsed_days"]    = None
            continue

        fw = batch["logistics"] or "未知货代"
        # 优先用该货代的均值，没有则回退到全局
        fw_avg          = avg_by_forwarder.get(fw, avg_days)
        fw_stage_timing = stage_timing_by_forwarder.get(fw, stage_timing)

        if verbose:
            print(f"\n[查询] {fw} | {batch['bill_no']} "
                  f"（均值参考: {fw_avg}天/{fw}）...")
        latest_node, nodes = track_shipment(fw, batch["bill_no"])
        batch["tracking_status"] = latest_node
        batch["nodes"] = nodes

        if not fw_avg:
            batch["remaining_days"] = None
            batch["est_arrival"]    = "历史数据不足"
            batch["elapsed_days"]   = None
            if verbose:
                print(f"  最新节点: {latest_node}")
            continue

        # 已过天数
        elapsed = None
        if nodes:
            try:
                first_t = _parse_ts(nodes[-1]["time"]).date()
                elapsed = (today - first_t).days
            except Exception:
                pass
        if elapsed is None and batch["delivery_at"]:
            try:
                elapsed = (today - datetime.strptime(batch["delivery_at"], "%Y-%m-%d").date()).days
            except Exception:
                pass

        batch["elapsed_days"] = elapsed
        if elapsed is None:
            batch["remaining_days"] = None
            batch["est_arrival"]    = "无法估算"
            continue

        remaining = fw_avg - elapsed

        if remaining > 0:
            batch["remaining_days"] = remaining
            batch["est_arrival"]    = str(today + timedelta(days=remaining))
            note = f"（{fw}均值{fw_avg}天）"
        else:
            # 超期：用该货代的阶段数据估算
            remaining = stage_remaining(latest_node, fw_stage_timing, fallback_days=30)
            batch["remaining_days"] = remaining
            batch["est_arrival"]    = str(today + timedelta(days=remaining))
            note = f"（阶段估算/{fw}历史推导）"

        if verbose:
            print(f"  最新节点: {latest_node}")
            print(f"  已过{elapsed}天 / {fw}均值{fw_avg}天 → 还需~{remaining}天 "
                  f"预估到货: {batch['est_arrival']} {note}")

    # ── 超期修正：实测总时长 > 历史均值时，回推修正各货代均值 ──
    # 第一步：按货代收集超期批次的实测总时长
    overdue_by_fw = {}
    for b in transit_batches:
        if b.get("elapsed_days") is None or b.get("remaining_days") is None:
            continue
        fw_b     = b["logistics"] or "未知货代"
        fw_avg_b = avg_by_forwarder.get(fw_b) or avg_days
        if fw_avg_b and b["elapsed_days"] > fw_avg_b:
            overdue_by_fw.setdefault(fw_b, []).append(
                b["elapsed_days"] + b["remaining_days"]
            )

    # 第二步：各货代取 max(历史均值, 实测均值) 上调，并写回模块级缓存
    for fw_b, totals in overdue_by_fw.items():
        observed = sum(totals) / len(totals)
        if avg_by_forwarder.get(fw_b):
            new_val = round(max(avg_by_forwarder[fw_b], observed))
            avg_by_forwarder[fw_b] = new_val
            # 写回缓存：同一进程内后续 SKU 自动使用修正后的均值
            if fw_b in _forwarder_avg_cache:
                _forwarder_avg_cache[fw_b] = max(_forwarder_avg_cache[fw_b], new_val)

    # 第三步：全局均值同步更新
    all_fw_vals = [v for v in avg_by_forwarder.values() if v]
    if all_fw_vals:
        avg_days = round(sum(all_fw_vals) / len(all_fw_vals))

    # 第四步：用修正后的货代均值回写非超期批次的 remaining_days
    for b in transit_batches:
        if b.get("elapsed_days") is None or b.get("remaining_days") is None:
            continue
        fw_b     = b["logistics"] or "未知货代"
        fw_avg_b = avg_by_forwarder.get(fw_b) or avg_days
        # 只更新之前被判定为"未超期"的批次（elapsed <= orig fw_avg 时remaining>0）
        if fw_avg_b and b["elapsed_days"] < fw_avg_b:
            new_remaining = fw_avg_b - b["elapsed_days"]
            b["remaining_days"] = new_remaining
            b["est_arrival"]    = str(today + timedelta(days=new_remaining))

    if verbose and overdue_by_fw:
        print(f"\n→ 超期修正后各货代均值：" +
              "  ".join(f"{fw}:{v}天" for fw, v in avg_by_forwarder.items() if v))
        print(f"→ 全局物流均值修正为 {avg_days}天")

    # ── 整理输出 ─────────────────────────────────────────
    valid_batches = [b for b in transit_batches
                     if b.get("remaining_days") is not None]
    valid_batches.sort(key=lambda b: b["remaining_days"])

    fastest = valid_batches[0] if valid_batches else None
    rest    = valid_batches[1:] if len(valid_batches) > 1 else []

    if verbose and fastest:
        print(f"\n→ 字段③最快到货：{fastest['sku_qty']}件  "
              f"预计{fastest['est_arrival']}（还需~{fastest['remaining_days']}天）")
        print(f"  当前节点：{fastest['tracking_status']}")
        if rest:
            print(f"\n→ 字段④剩余批次到货估算：")
            for b in rest:
                print(f"  {b['order_no']} | {b['sku_qty']}件 | "
                      f"还需~{b['remaining_days']}天 | {b['est_arrival']} | {b['tracking_status'][:40]}")

    return {
        "sku":               sku,
        # 字段①
        "total_transit_qty": total_transit_qty,
        "transit_batches":   transit_batches,
        # 字段②
        "avg_transit_days":     avg_days,
        "avg_by_forwarder":     avg_by_forwarder,
        "avg_transit_months":   round(avg_days / 30, 2) if avg_days else None,
        "history_count":        len(transit_day_list),
        "stage_timing":         stage_timing,
        # 字段③
        "fastest_batch":     fastest,
        # 字段④
        "rest_batches":      rest,
    }

# ── 飞书推送（单 SKU）────────────────────────────────────
def push_result(result):
    from scripts.notify import send_card
    sku     = result["sku"]
    avg     = result["avg_transit_days"]
    mo      = result["avg_transit_months"]
    hist    = result["history_count"]
    fastest = result.get("fastest_batch")
    rest    = result.get("rest_batches", [])
    batches = result.get("transit_batches", [])
    total   = result["total_transit_qty"]

    lines = [f"**SKU：{sku}**\n"]

    # ① 在途库存
    lines.append(f"**① 在途库存：{total}件（{len(batches)}批）**\n")
    if batches:
        lines.append("| 货单 | 状态 | 物流 | 发货日 | 数量 |")
        lines.append("|-----|------|------|-------|------|")
        for b in batches:
            lines.append(f"| {b['order_no']} | {b['status']} | {b['logistics']} "
                         f"| {b['delivery_at']} | {b['sku_qty']}件 |")

    # ② 平均物流时长
    lines.append(f"\n**② 平均物流时长：{avg}天 ≈ {mo}个月**（近{hist}笔历史均值）\n" if avg
                 else "\n**② 平均物流时长：历史数据不足**\n")

    # ③ 最快到货
    if fastest:
        lines.append(f"**③ 最快到货：{fastest['sku_qty']}件**")
        lines.append(f"- 预计到货：**{fastest['est_arrival']}**（还需约 {fastest['remaining_days']} 天）")
        lines.append(f"- 当前节点：{fastest['tracking_status']}\n")
    else:
        lines.append("**③ 最快到货：无在途或无法估算**\n")

    # ④ 剩余批次
    if rest:
        lines.append("**④ 剩余批次到货估算：**\n")
        lines.append("| 货单 | 件数 | 还需天数 | 预估到货 | 当前节点 |")
        lines.append("|-----|------|---------|---------|---------|")
        for b in rest:
            node_s = (b.get("tracking_status") or "")[:35]
            lines.append(f"| {b['order_no']} | {b['sku_qty']}件 | ~{b['remaining_days']}天 "
                         f"| {b['est_arrival']} | {node_s} |")
    elif not fastest:
        lines.append("**④ 剩余批次：无**")

    send_card(f"🚚 物流分析｜{sku}", "\n".join(lines), color="blue")
    print(f"\n✓ {sku} 已推送飞书")

# ── 飞书推送（跨 SKU 汇总）───────────────────────────────
def push_summary(all_results):
    from scripts.notify import send_card

    lines = ["**各 SKU 最快到货汇总：**\n"]
    lines.append("| SKU | 在途总量 | 均值 | 最快件数 | 最快到货 | 还需天数 | 当前节点 |")
    lines.append("|-----|---------|------|---------|---------|---------|---------|")

    for r in all_results:
        sku     = r["sku"]
        total   = r["total_transit_qty"]
        avg     = f"{r['avg_transit_days']}天" if r.get("avg_transit_days") else "—"
        fastest = r.get("fastest_batch")
        if fastest:
            qty    = f"{fastest['sku_qty']}件"
            est    = fastest["est_arrival"]
            remain = f"~{fastest['remaining_days']}天"
            node   = (fastest.get("tracking_status") or "")[:30]
        else:
            qty = "—"; est = "无在途"; remain = "—"; node = ""
        lines.append(f"| {sku} | {total}件 | {avg} | {qty} | {est} | {remain} | {node} |")

    send_card("🚚 各 SKU 最快到货汇总", "\n".join(lines), color="blue")

    col = [14, 8, 6, 8, 12, 8, 32]
    header = ["SKU", "在途总量", "均值", "最快件数", "最快到货日", "还需天数", "当前节点"]
    sep    = ["-"*c for c in col]

    def row_fmt(cells):
        return "  " + "  ".join(str(c).ljust(col[i]) for i, c in enumerate(cells))

    print(f"\n{'='*96}")
    print("  各 SKU 最快到货汇总")
    print(f"{'='*96}")
    print(row_fmt(header))
    print(row_fmt(sep))
    for r in all_results:
        fastest = r.get("fastest_batch")
        avg_s   = f"{r.get('avg_transit_days','—')}天"
        if fastest:
            print(row_fmt([
                r["sku"],
                f"{r['total_transit_qty']}件",
                avg_s,
                f"{fastest['sku_qty']}件",
                fastest["est_arrival"],
                f"~{fastest['remaining_days']}天",
                (fastest.get("tracking_status") or "")[:30],
            ]))
        else:
            print(row_fmt([r["sku"], f"{r['total_transit_qty']}件", avg_s,
                           "—", "无在途", "—", ""]))
    print(f"{'='*96}")

# ── 飞书多维表格写回 ─────────────────────────────────────
BITABLE_BASE_ID  = "BE2Ab41lvaJdzbs0c7QcgaWbnid"
BITABLE_TABLE_ID = "tbl1ffNbzU3WtNC9"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                          "feishu_auth", "token.json")

FS_APP_ID     = os.environ.get("FEISHU_APP_ID")     or "cli_a96a395aaafa5cb5"
FS_APP_SECRET = os.environ.get("FEISHU_APP_SECRET") or ""

def _get_app_access_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": FS_APP_ID, "app_secret": FS_APP_SECRET},
        headers={"Content-Type": "application/json"},
        timeout=15
    )
    data = resp.json()
    token = data.get("app_access_token")
    if not token:
        raise RuntimeError(f"获取 app_access_token 失败: {data}")
    return token

def _refresh_user_token(refresh_token):
    """用 refresh_token 自动换取新的 user_access_token 并写回 token.json"""
    import time
    app_token = _get_app_access_token()
    for url in [
        "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
        "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token",
    ]:
        resp = requests.post(url,
            headers={"Authorization": f"Bearer {app_token}",
                     "Content-Type": "application/json"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15)
        result = resp.json()
        data   = result.get("data") or result
        new_uat = data.get("access_token") or data.get("user_access_token")
        new_rt  = data.get("refresh_token")
        if new_uat:
            expires_in = data.get("expires_in", 7200)
            payload = {
                "user_access_token":  new_uat,
                "access_token":       new_uat,
                "refresh_token":      new_rt or refresh_token,
                "expires_in":         expires_in,
                "refresh_expires_in": data.get("refresh_expires_in", 2592000),
                "expires_at":         int(time.time()) + expires_in - 300,
                "scope":              data.get("scope", ""),
            }
            with open(TOKEN_FILE, "w") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            print(f"  ✓ user_access_token 已自动刷新")
            return new_uat
    raise RuntimeError(f"刷新 token 失败: {result}")

def _load_user_token():
    """
    读取 user_access_token。
    - 未过期：直接返回
    - 已过期：用 refresh_token 自动续期（无需任何用户操作）
    - refresh_token 也过期（30天无续期）：提示重新授权
    """
    import time
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except Exception as e:
        print(f"  ✗ 无法读取 token.json: {e}，请先运行 feishu_auth/get_token3.py")
        return None

    expires_at    = data.get("expires_at", 0)
    user_token    = data.get("user_access_token") or data.get("access_token")
    refresh_token = data.get("refresh_token")

    if expires_at and time.time() < expires_at and user_token:
        return user_token

    if refresh_token:
        print("  ℹ️  token 已过期，自动刷新中...")
        try:
            return _refresh_user_token(refresh_token)
        except Exception as e:
            print(f"  ✗ 自动刷新失败: {e}")

    if user_token:
        print("  ⚠️  token 可能过期，尝试继续使用")
        return user_token

    print("  ✗ 请运行 feishu_auth/get_token3.py 重新授权（每30天一次）")
    return None

def _bitable_find_record(sku, token):
    """根据 ERP-SKU 查找多维表格记录，返回 record_id"""
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_BASE_ID}"
           f"/tables/{BITABLE_TABLE_ID}/records/search")
    resp = requests.post(url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"filter": {"conjunction": "and",
                         "conditions": [{"field_name": "ERP-SKU",
                                         "operator": "is",
                                         "value": [sku]}]}},
        timeout=15)
    data = resp.json()
    items = (data.get("data") or {}).get("items") or []
    if items:
        return items[0]["record_id"]
    return None

def _bitable_update_record(record_id, fields, token):
    """PUT 更新单条记录"""
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_BASE_ID}"
           f"/tables/{BITABLE_TABLE_ID}/records/{record_id}")
    resp = requests.put(url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"fields": fields},
        timeout=15)
    return resp.json()

def write_to_bitable(all_results):
    """
    把物流分析结果写回飞书多维表格（user_access_token，支持自动续期）。
    字段映射：
      发货在途             → total_transit_qty（数字）
      到货周期             → "50天 ≈ 1.67个月（近N笔）"（文本）
      最新一批货到货预估时间 → "YYYY-MM-DD（还需~N天）"（文本，仅时间）
      最近一批到货数量      → fastest_batch.sku_qty（数字）
    """
    import json
    token = _load_user_token()
    if not token:
        print("✗ 无法获取 token，跳过写回")
        return

    ok, fail = 0, 0
    for r in all_results:
        sku = r.get("sku", "")
        if r.get("error"):
            print(f"  跳过 {sku}（{r['error']}）")
            continue

        # 构建写入字段
        fields = {}

        # 发货在途（数字）
        fields["发货在途"] = r["total_transit_qty"]

        # 到货周期（文本）
        if r.get("avg_transit_days"):
            fields["到货周期"] = (
                f"{r['avg_transit_days']}天 ≈ {r['avg_transit_months']}个月"
                f"（近{r['history_count']}笔）"
            )

        # 最新一批货到货预估时间（仅时间文本）+ 最近一批到货数量（数字）
        fastest = r.get("fastest_batch")
        if fastest and fastest.get("est_arrival") not in (None, "无法估算", "历史数据不足"):
            fields["最新一批货到货预估时间"] = (
                f"{fastest['est_arrival']}（还需~{fastest['remaining_days']}天）"
            )
            fields["最近一批到货数量"] = fastest["sku_qty"]

        # 查找记录
        record_id = _bitable_find_record(sku, token)
        if not record_id:
            print(f"  ⚠️  {sku}：多维表格中未找到记录，跳过")
            fail += 1
            continue

        # 写入
        result = _bitable_update_record(record_id, fields, token)
        if result.get("code") == 0:
            print(f"  ✓ {sku} → 发货在途:{r['total_transit_qty']}件  "
                  f"到货周期:{fields.get('到货周期','—')}  "
                  f"最快到货:{fields.get('最近一批到货数量','—')}件  "
                  f"预估时间:{fields.get('最新一批货到货预估时间','—')}")
            ok += 1
        else:
            print(f"  ✗ {sku} 写入失败: code={result.get('code')} msg={result.get('msg')}")
            fail += 1

    print(f"\n✓ 多维表格写回完成｜成功:{ok}  失败/跳过:{fail}")

# 注: write_transit_to_db / get_skus_from_db (写读 sa_main) 已删除 (2026-05-03).
#     新版 wf3 hub 由 wf_logistics_status.py 直接写 wf3_logistics_hub 表;
#     SKU 池来自 wf2_<alias>_sku UNION (per-entity loop).

def scan_in_transit_skus(all_skus, token):
    """
    快速扫描：仅用 ERP API（无浏览器）过滤出有在途订单的 SKU。
    返回 [(sku, in_transit_orders, completed_orders), ...]
    """
    result = []
    total  = len(all_skus)
    print(f"\n第一阶段：快速扫描 {total} 个 SKU 的 ERP 在途状态...")
    for i, sku in enumerate(all_skus, 1):
        orders = get_all_orders(sku, token)
        in_transit = [o for o in orders
                      if o.get("status") not in (STATUS_DONE, STATUS_VOID)
                      and (o.get("status") or 0) > 0]
        completed  = [o for o in orders
                      if o.get("status") == STATUS_DONE and o.get("delivery_at")]
        if in_transit:
            result.append((sku, in_transit, completed))
            print(f"  [{i}/{total}] {sku} ✓ 有在途 {len(in_transit)} 批")
        else:
            print(f"  [{i}/{total}] {sku} — 无在途")
    print(f"\n扫描完成：{len(result)}/{total} 个 SKU 有在途库存\n")
    return result

if __name__ == "__main__":
    # CLI 入口已退役 (DEPRECATED). 老版 main 块从 sa_main 拉全量 SKU + 写回 sa_main."发货在途",
    # 已被 wf_logistics_status.py 整体取代 (per-entity, 写 wf3_logistics_hub).
    print("⚠️  wf0_logistics.py CLI 已退役.")
    print("    新链路: python3 -m hipop.workflows.wf_logistics_status --entities hipop_ksa")
    print("    本文件仅保留底层 ERP API helper (get_erp_token / erp_get / get_order_detail_qty / analyze_sku)")
    print("    供 wf_logistics_status import 复用.")
    sys.exit(1)

"""
工作流二：从 ERP product-order-statistics 拉销量数据，写入每个销售主体的 sku 主表。

数据范围：config/hipop.json -> sales_entities[]，按 entity 分别拉数据并落到对应 wf2_<alias>_sku 表。

策略：
  - 6 个时间窗（10/30/60/90/120/180 天）分别拉 → 拼出 sales_10d ~ sales_180d
  - 最长窗口（180d）作为基线，写其它字段（价格/利润率/退货率等）
  - 产品库 ingest 已写入的基础字段（title/cost 等）不被覆盖（COALESCE 保留 + 销量字段独立写）

CLI:
  python3 ingest_erp_sales.py                       # 全量
  python3 ingest_erp_sales.py --entities hipop_ksa  # 限制 entity
  python3 ingest_erp_sales.py --windows 30,180
  python3 ingest_erp_sales.py --max-pages 1
"""
import os, sys, json, sqlite3, argparse, re, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, ensure_tables, sku_table
# 长任务 heartbeat — 无 HIPOP_TASK_ID env 时自动 no-op
try:
    from hipop.server.runtime import tick, set_progress
except ImportError:
    tick = lambda *a, **k: None
    set_progress = lambda *a, **k: None

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
ERP_API_BASE = "https://erp-api.dbuyerp.com/admin"
NOON_PLATFORM_ID = 2
NATION_TO_ID = {"SA": 1, "AE": 2}
WINDOWS = [10, 30, 60, 90, 120, 180]


# ── token 获取 ────────────────────────────────────────────────────────
def _get_token_via_cdp_raw():
    """直接走 Chrome DevTools Protocol websocket，绕开 playwright 的 attach 全 context 行为
    （连接多 tab 的 chrome 时 playwright connect_over_cdp 经常 180s 超时）。

    在 ERP tab 里 evaluate localStorage / cookie 拿 token 字符串。
    """
    try:
        import json as _json, websocket, requests as _req
    except ImportError:
        return None
    try:
        # 必须绕开系统代理：本机 chrome devtools 直连
        sess = _req.Session(); sess.trust_env = False
        r = sess.get("http://127.0.0.1:9222/json", timeout=3,
                     proxies={"http": None, "https": None},
                     headers={"Connection": "close"})
        tabs = r.json()
    except Exception as e:
        print(f"[token via cdp] /json fail: {e}", file=sys.stderr)
        return None
    erp_tab = next((t for t in tabs
                    if t.get("type") == "page"
                    and "dbuyerp.com" in t.get("url", "")
                    and "/login" not in t.get("url", "")),
                   None)
    if not erp_tab:
        print("[token via cdp] no logged-in dbuyerp tab", file=sys.stderr)
        return None
    ws_url = erp_tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return None
    # 一次性 evaluate：从 localStorage / sessionStorage / cookie 找 token
    js = """
    (function () {
      function isJwt(v) {
        return typeof v === 'string'
            && v.length > 200
            && (v.indexOf('eyJ') === 0 || v.indexOf('Bearer eyJ') === 0);
      }
      function scanStorage(s) {
        for (var i=0; i<s.length; i++) {
          var k=s.key(i); var v=s.getItem(k);
          if (!v) continue;
          if (isJwt(v)) return v;
          try { var o=JSON.parse(v);
            for (var kk in o) { if (isJwt(o[kk])) return o[kk]; }
          } catch(e) {}
        }
        return null;
      }
      var t = scanStorage(localStorage) || scanStorage(sessionStorage);
      if (t) return t;
      // cookie 后备
      var m = document.cookie.match(/(?:^|; )(?:token|access_token|Authorization)=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    })();
    """
    try:
        # chrome 默认拒绝 127.0.0.1:9222 origin 的 ws 连接，必须显式给个干净的 Origin
        ws = websocket.create_connection(ws_url, timeout=5,
                                         origin="http://localhost",
                                         header=["Host: localhost"])
        ws.send(_json.dumps({"id": 1, "method": "Runtime.evaluate",
                             "params": {"expression": js, "returnByValue": True}}))
        resp = _json.loads(ws.recv())
        ws.close()
        val = (((resp.get("result") or {}).get("result") or {}).get("value"))
        if val and isinstance(val, str):
            tok = val.replace("Bearer ", "").strip()
            if tok.startswith("eyJ") and len(tok) > 200:
                return tok
        print(f"[token via cdp] no token in storage; got: {str(val)[:80]}", file=sys.stderr)
    except Exception as e:
        print(f"[token via cdp] ws fail: {e}", file=sys.stderr)
    return None


def _get_token_via_playwright():
    """Playwright fallback（chrome 多 tab 时容易超时，所以放第二位）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
            ctx = browser.contexts[0]
            erp = next((pg for pg in ctx.pages if "dbuyerp.com" in pg.url), None)
            if not erp:
                return None
            auth = {"v": None}
            def grab(req):
                h = {k.lower(): v for k, v in req.headers.items()}
                a = h.get("authorization")
                if a and "erp-api" in req.url and not auth["v"]:
                    auth["v"] = a
            erp.on("request", grab)
            try:
                erp.goto("https://www.dbuyerp.com/product/list",
                         wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            erp.wait_for_timeout(5000)
            if auth["v"]:
                return auth["v"].replace("Bearer ", "").strip()
    except Exception as e:
        print(f"[token via playwright] failed: {e}", file=sys.stderr)
    return None


def get_token_via_page():
    return _get_token_via_cdp_raw() or _get_token_via_playwright()


def get_token():
    t = get_token_via_page()
    if t:
        print(f"[token] from existing 9222 page: {t[:20]}...", file=sys.stderr)
        return t
    print("[token] fallback to wf0 headless login", file=sys.stderr)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from workflows.wf0_logistics import get_erp_token
    return get_erp_token()


# ── ERP API ────────────────────────────────────────────────────────────
def erp_get(token, path, params=None, retries=8):
    import requests
    from requests.adapters import HTTPAdapter
    last = None
    for attempt in range(retries):
        try:
            # 每次新建 session 避免连接池坏连接复用
            sess = requests.Session()
            sess.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0))
            r = sess.get(
                ERP_API_BASE + path,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Connection": "close"},
                timeout=60,
            )
            data = r.json()
            sess.close()
            msg = data.get("msg") or ""
            if "处理中" in msg or "重复" in msg or "频繁" in msg or r.status_code == 429:
                wait = min(2 ** attempt, 60)
                print(f"  [throttled] retry in {wait}s ({msg})", file=sys.stderr)
                time.sleep(wait)
                continue
            return data
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError) as e:
            last = e
            wait = min(5 + 2 ** attempt, 60)
            print(f"  [conn err] retry in {wait}s ({type(e).__name__})", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    if last:
        raise last
    raise RuntimeError("erp_get exhausted retries")


def fetch_window(token, nation_id, days, page_size=50, max_items=None):
    today = datetime.now().date()
    start = today - timedelta(days=days)
    params = {
        "nation_id": nation_id,
        "platform_id": NOON_PLATFORM_ID,
        "ordered_time_section[]": [start.strftime("%Y-%-m-%-d"), today.strftime("%Y-%-m-%-d")],
        "keyword_type": 1,
        "page": 1,
        "limit": page_size,
    }
    out = []
    while True:
        resp = erp_get(token, "/product-order-statistics", params)
        if resp.get("code") != 200:
            raise RuntimeError(f"ERP error: {resp.get('msg')} (window={days}d, page={params['page']})")
        chunk = resp.get("data") or []
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        if max_items is not None and len(out) >= max_items:
            out = out[:max_items]
            break
        params["page"] += 1
        time.sleep(0.4)
    return out


# ── 解析 ─────────────────────────────────────────────────────────────
_PAIR_RE = re.compile(r"^([A-Z]{2})\s*[:：]\s*(.+)$")


def parse_country_value(arr, country):
    if not arr:
        return None
    for s in arr:
        m = _PAIR_RE.match(s.strip())
        if m and m.group(1) == country:
            return m.group(2).strip()
    return None


def parse_int(s):
    if s is None:
        return None
    try:
        return int(re.sub(r"[^\d-]", "", str(s)) or "0")
    except ValueError:
        return None


def parse_money(s):
    if s is None:
        return (None, None)
    m = re.match(r"\s*([\d.]+)\s*([A-Z]{3})?\s*$", str(s))
    if not m:
        return (None, None)
    try:
        return (float(m.group(1)), m.group(2))
    except ValueError:
        return (None, None)


def parse_pct(s):
    if s is None:
        return None
    m = re.match(r"\s*([\d.]+)\s*%?\s*$", str(s))
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v / 100.0 if "%" in str(s) else v
    except ValueError:
        return None


def _num(v):
    """成本/利润金额 → float。接受裸数字或带币种字符串（'30.0 SAR' / '30'）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    amt, _ = parse_money(v)
    return amt


# ── ERP SKU 成本/利润详情 ────────────────────────────────────────────
# product-order-statistics 是"按 SKU 聚合"的，没有订单号；订单粒度的成本/利润拆解
# （头程 cost_local / 打包 cost_pack / 国际运费 cost_intl / 利润额 / 利润率）要从
# SKU 详情接口拿。端点口径见 hipop/scripts/probe_erp_sku_detail.py（/sku/<id>/detail）。
#
# ⚠️ 字段名以 probe 脚本探到的 dbuyerp SKU 详情结构为准；本环境无 ERP token 不能跑真接口
#    联调，确切字段需上线前用真 token 跑 probe_erp_sku_detail.py 复核一次（PR 已标residual风险）。
#    解析做成"容错取候选键"，缺字段返回 None 而不是硬编码，避免占位假数据。
def fetch_sku_cost_detail(token, erp_sku_id, nation_id=None):
    """拉单个 ERP SKU 的订单级成本/利润详情。返回原始 ERP 响应 dict。"""
    params = {"keyword": erp_sku_id, "keyword_type": 1}
    if nation_id is not None:
        params["nation_id"] = nation_id
    return erp_get(token, f"/sku/{erp_sku_id}/detail", params)


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def parse_sku_cost_orders(detail, country=None):
    """把 SKU 详情响应解析成订单级成本/利润行 list。

    每行: {item_nr, order_date, cost_local, cost_pack, cost_intl, profit, profit_rate}
    缺字段 → None（不编造）。无订单 → []。
    """
    if not detail:
        return []
    data = detail.get("data") if isinstance(detail, dict) else None
    if isinstance(data, dict):
        orders = (_first(data, "orders", "order_list", "details", "order_details")
                  or [])
    elif isinstance(data, list):
        orders = data
    else:
        orders = []

    out = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        item_nr = _first(o, "item_nr", "itemNr", "order_item_nr", "order_no", "order_sn")
        if item_nr is None:
            continue  # wf2_orders 主键含 item_nr，没号的订单无法落库
        order_date = _first(o, "order_date", "ordered_time", "ordered_at", "create_time")
        if order_date:
            order_date = str(order_date)[:10]
        out.append({
            "item_nr":     str(item_nr),
            "order_date":  order_date,
            "cost_local":  _num(_first(o, "cost_local", "first_leg_cost", "domestic_cost")),
            "cost_pack":   _num(_first(o, "cost_pack", "pack_cost", "packing_cost")),
            "cost_intl":   _num(_first(o, "cost_intl", "intl_cost", "international_cost", "shipping_cost")),
            "profit":      _num(_first(o, "profit", "profit_amount", "net_profit")),
            "profit_rate": parse_pct(_first(o, "profit_rate", "newest_profit_rate", "margin_rate")),
        })
    return out


# ── 主流程：每个 entity 独立处理 ──────────────────────────────────────
def run(entity_aliases=None, windows=None, max_pages=None):
    all_entities = load_entities()
    entities = [e for e in all_entities
                if not entity_aliases or e["alias"] in entity_aliases]
    if not entities:
        sys.exit("no matching sales_entities")
    win_list = [w for w in WINDOWS if not windows or w in windows]

    token = get_token()
    if not token:
        sys.exit("ERP token not available")

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)

    for ent in entities:
        alias    = ent["alias"]
        country  = ent["country"]
        store    = ent["store"]
        currency = ent.get("currency")
        nation_id = NATION_TO_ID.get(country)
        if not nation_id:
            print(f"[skip {alias}] unknown country {country}", file=sys.stderr)
            continue

        print(f"\n[entity {alias}] country={country} store={store}", file=sys.stderr)
        tick(f"start entity {alias} country={country} store={store}")

        # partner_sku -> 累积 dict
        bucket = {}
        for idx, days in enumerate(win_list, 1):
            max_items = max_pages * 50 if max_pages else None
            items = fetch_window(token, nation_id, days, max_items=max_items)
            print(f"  {days}d: {len(items)} skus", file=sys.stderr)
            tick(f"[{alias}] window {idx}/{len(win_list)} ({days}d): {len(items)} skus")
            set_progress({"current_entity": alias, "windows_done": idx, "windows_total": len(win_list)})
            for it in items:
                erp_sku = it.get("sku_id")
                if not erp_sku:
                    continue
                # 找该 SKU 在该 entity 店铺下的绑定
                sku = it.get("sku") or {}
                for psk in sku.get("platform_sku_ids") or []:
                    store_obj = psk.get("store") or {}
                    plat_obj  = psk.get("platform") or {}
                    if plat_obj.get("id") != NOON_PLATFORM_ID:
                        continue
                    if store_obj.get("name") != store:
                        continue
                    rec = bucket.setdefault(erp_sku, {
                        "partner_sku": erp_sku,
                        "erp_sku_id":  erp_sku,
                        "product_id":  sku.get("product_id"),
                        "noon_sku":    psk.get("platform_sku_id"),
                        "image_url":   sku.get("sku_image"),
                        "product_category_detail": it.get("product_category_detail"),
                        "currency":    currency,
                    })
                    sales_str = parse_country_value(it.get("sales_count"), country)
                    rec[f"sales_{days}d"] = parse_int(sales_str)
                    if days == 180:
                        # ERP 只填"最新/平均"这种含义清晰的字段；
                        # total_orders / cancel / return 由 wf_sales_static 从 noon 订单算（noon 缺失时标异常）
                        avg_p, c1 = parse_money(parse_country_value(it.get("avg_price"), country))
                        new_p, c2 = parse_money(parse_country_value(it.get("newest_sale_price"), country))
                        rec["avg_price"]    = avg_p
                        rec["latest_price"] = new_p
                        if not rec.get("currency"):
                            rec["currency"] = c1 or c2 or currency
                        rec["latest_profit_rate"] = parse_pct(parse_country_value(it.get("newest_profit_rate"), country))
                        rec["latest_order_date"] = (it.get("newest_sale_time") or "")[:10] or None
                    break  # 一个 SKU 在该 entity 只可能匹配一条 platform_sku_id

        # 派生字段
        today_iso = datetime.now().date().isoformat()
        for rec in bucket.values():
            rec["as_of_date"] = today_iso
            # total_revenue 用 avg_price * sales_180d 估算（noon 兜底前的初值，会在聚合时被 noon 数据覆盖）
            rec["total_revenue"] = ((rec.get("avg_price") or 0) * (rec.get("sales_180d") or 0)
                                    if rec.get("avg_price") and rec.get("sales_180d") else None)

        # 写库
        cur = conn.cursor()
        t = sku_table(alias)
        cols = [
            "partner_sku", "erp_sku_id", "noon_sku", "product_id",
            "image_url", "product_category_detail", "currency",
            "sales_10d", "sales_30d", "sales_60d", "sales_90d", "sales_120d", "sales_180d",
            "total_revenue", "latest_price", "avg_price", "latest_profit_rate",
            "latest_order_date", "as_of_date",
        ]
        placeholders = ",".join(["?"] * len(cols))
        update_set = ",".join(f"{c}=excluded.{c}" for c in cols if c != "partner_sku")
        sql = f"""
            INSERT INTO {t} ({",".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(partner_sku) DO UPDATE SET
              {update_set},
              imported_at = datetime('now', 'localtime')
        """
        for rec in bucket.values():
            cur.execute(sql, tuple(rec.get(c) for c in cols))
        conn.commit()
        print(f"  wrote {len(bucket)} rows to {t}", file=sys.stderr)

    conn.close()
    print("\n[done]", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=None, help="逗号分隔，例 hipop_ksa")
    ap.add_argument("--windows", default=None, help="逗号分隔的天数")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    run(
        entity_aliases=args.entities.split(",") if args.entities else None,
        windows=[int(x) for x in args.windows.split(",")] if args.windows else None,
        max_pages=args.max_pages,
    )

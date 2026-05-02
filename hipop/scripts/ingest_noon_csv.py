"""
工作流二：扫 noon 后台导出的 CSV，写入对应销售主体的 orders 表 + 回填 sku 主表的 noon-only 字段。

人工流程：
  1. 紫鸟 noon 后台 sales 页 export CSV
  2. 文件丢到 INBOX，命名约定：noon_<country>_<YYYYMMDD>.csv
     例：~/Downloads/点购工作流/inbox/noon_SA_20260501.csv
  3. 跑 `python3 ingest_noon_csv.py`
  4. 处理完移到 inbox/processed/

按文件名里 country 推算 entity，写入对应 wf2_<alias>_orders 表。

CLI:
  python3 ingest_noon_csv.py --dry-run        # 列每个 CSV 的 header
  python3 ingest_noon_csv.py                  # 全量处理 inbox/
  python3 ingest_noon_csv.py --file <path>    # 处理单个 CSV（不移动）
"""
import os, sys, csv, sqlite3, argparse, json, re, shutil
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, ensure_tables, sku_table, orders_table, entity_for

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
INBOX_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "inbox")
PROCESSED_DIR = os.path.join(INBOX_DIR, "processed")

# noon sales export CSV header → 字段
COLUMN_MAP = {
    "partner_sku": ["partner_sku"],
    "noon_sku":    ["sku"],
    "item_nr":     ["item_nr"],
    "order_date":  ["order_timestamp"],
    "status":      ["status"],
    "fulfillment": ["fulfillment_model"],
    "seller_price":   ["offer_price"],
    "customer_paid":  ["gmv_lcy"],
    "currency":    ["currency_code"],
    "destination": ["dest_country"],
    "family":      ["family"],
    "brand":       ["brand_code"],
    "title":       [],
    "image_url":   [],
}

STATUS_CANCELLED = {"cancelled", "canceled"}
STATUS_RETURN    = {"cir", "customer initiated returns", "customer initiated return",
                    "returned", "return"}


def _norm(s):
    return re.sub(r"[\s_\-]+", "", str(s)).lower() if s else ""


def build_header_index(header):
    return {_norm(h): h for h in header}


def get_col(row, header_idx, candidates):
    for cand in candidates:
        orig = header_idx.get(_norm(cand))
        if orig and row.get(orig) is not None and row[orig] != "":
            return row[orig]
    return None


def parse_money(s):
    if s in (None, ""):
        return (None, None)
    m = re.match(r"\s*([\d,.]+)\s*([A-Z]{3})?\s*$", str(s))
    if not m:
        return (None, None)
    try:
        return (float(m.group(1).replace(",", "")), m.group(2))
    except ValueError:
        return (None, None)


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).date().isoformat()
        except ValueError:
            continue
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return None


def country_from_filename(path):
    """noon_<country>_*.csv 或 *_<country>_*.csv 抓 country。"""
    up = os.path.basename(path).upper()
    m = re.search(r"_(SA|AE|KSA|UAE)_", up)
    if m:
        c = m.group(1)
        return {"KSA": "SA", "UAE": "AE"}.get(c, c)
    return None


def process_csv(path, conn, dry_run=False):
    print(f"\n=== {path} ===", file=sys.stderr)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("  (empty)", file=sys.stderr)
            return 0
        header = reader.fieldnames
        print(f"  columns ({len(header)}): {header}", file=sys.stderr)
        if dry_run:
            return 0

        country = country_from_filename(path)
        if not country:
            print(f"  [skip] cannot infer country from filename", file=sys.stderr)
            return 0
        ent = entity_for(country=country)
        if not ent:
            print(f"  [skip] no sales_entity matches country={country}", file=sys.stderr)
            return 0
        alias = ent["alias"]
        print(f"  → entity {alias} (store={ent['store']})", file=sys.stderr)

        header_idx = build_header_index(header)
        cur = conn.cursor()
        n = 0
        sku_meta = {}  # partner_sku -> meta dict

        ord_table = orders_table(alias)
        sku_tbl   = sku_table(alias)

        for row in reader:
            partner_sku = get_col(row, header_idx, COLUMN_MAP["partner_sku"])
            item_nr     = get_col(row, header_idx, COLUMN_MAP["item_nr"])
            noon_sku    = get_col(row, header_idx, COLUMN_MAP["noon_sku"])
            if not (partner_sku and item_nr):
                continue
            status   = get_col(row, header_idx, COLUMN_MAP["status"]) or ""
            status_l = status.strip().lower()
            is_cancelled = 1 if status_l in STATUS_CANCELLED else 0
            is_return    = 1 if status_l in STATUS_RETURN else 0

            seller_price, cur_a  = parse_money(get_col(row, header_idx, COLUMN_MAP["seller_price"]))
            customer_paid, cur_b = parse_money(get_col(row, header_idx, COLUMN_MAP["customer_paid"]))
            currency   = get_col(row, header_idx, COLUMN_MAP["currency"]) or cur_a or cur_b
            order_date = parse_date(get_col(row, header_idx, COLUMN_MAP["order_date"]))
            fulfillment = get_col(row, header_idx, COLUMN_MAP["fulfillment"])

            cur.execute(f"""
                INSERT INTO {ord_table}
                  (partner_sku, noon_sku, item_nr, order_date, status,
                   is_cancelled, is_return, seller_price, customer_paid, currency,
                   fulfillment, destination, source, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'noon',?)
                ON CONFLICT(partner_sku, item_nr) DO UPDATE SET
                  noon_sku=excluded.noon_sku,
                  order_date=excluded.order_date,
                  status=excluded.status,
                  is_cancelled=excluded.is_cancelled,
                  is_return=excluded.is_return,
                  seller_price=excluded.seller_price,
                  customer_paid=excluded.customer_paid,
                  currency=excluded.currency,
                  fulfillment=excluded.fulfillment,
                  destination=excluded.destination,
                  imported_at=datetime('now','localtime')
            """, (
                partner_sku, noon_sku, item_nr, order_date, status,
                is_cancelled, is_return, seller_price, customer_paid, currency,
                fulfillment,
                get_col(row, header_idx, COLUMN_MAP["destination"]),
                json.dumps({k: row.get(k) for k in header}, ensure_ascii=False),
            ))
            n += 1

            if partner_sku not in sku_meta:
                sku_meta[partner_sku] = {
                    "noon_sku":    noon_sku,
                    "title":       get_col(row, header_idx, COLUMN_MAP["title"]),
                    "image_url":   get_col(row, header_idx, COLUMN_MAP["image_url"]),
                    "fulfillment": fulfillment,
                    "family":      get_col(row, header_idx, COLUMN_MAP["family"]),
                    "brand":       get_col(row, header_idx, COLUMN_MAP["brand"]),
                }

        # 写入/回填 sku 主表（noon 是 SoR：ERP 没绑的 SKU 也以 noon 为准录入）
        for partner_sku, meta in sku_meta.items():
            cur.execute(f"""
                INSERT INTO {sku_tbl}
                  (partner_sku, erp_sku_id, noon_sku, title, image_url, fulfillment, family, brand)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(partner_sku) DO UPDATE SET
                  noon_sku    = COALESCE(excluded.noon_sku, {sku_tbl}.noon_sku),
                  title       = COALESCE(excluded.title, {sku_tbl}.title),
                  image_url   = COALESCE(excluded.image_url, {sku_tbl}.image_url),
                  fulfillment = COALESCE(excluded.fulfillment, {sku_tbl}.fulfillment),
                  family      = COALESCE(excluded.family, {sku_tbl}.family),
                  brand       = COALESCE(excluded.brand, {sku_tbl}.brand),
                  imported_at = datetime('now','localtime')
            """, (partner_sku, partner_sku, meta["noon_sku"], meta["title"], meta["image_url"],
                  meta["fulfillment"], meta["family"], meta["brand"]))
        conn.commit()
        print(f"  inserted/updated {n} orders, {len(sku_meta)} sku metas", file=sys.stderr)
        return n


def run(dry_run=False, single_file=None):
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    if single_file:
        files = [single_file]
    else:
        files = sorted(
            os.path.join(INBOX_DIR, f)
            for f in os.listdir(INBOX_DIR)
            if f.endswith(".csv") and not f.startswith(".")
        )

    if not files:
        print(f"[ingest_noon] no CSV in {INBOX_DIR}", file=sys.stderr)
        return

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    total = 0
    for path in files:
        n = process_csv(path, conn, dry_run=dry_run)
        total += n
        if not dry_run and not single_file and n > 0:
            dest = os.path.join(PROCESSED_DIR, os.path.basename(path))
            shutil.move(path, dest)
            print(f"  → moved to {dest}", file=sys.stderr)
    conn.close()
    print(f"\n[done] processed {len(files)} csvs, {total} order rows", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file", default=None)
    args = ap.parse_args()
    run(dry_run=args.dry_run, single_file=args.file)

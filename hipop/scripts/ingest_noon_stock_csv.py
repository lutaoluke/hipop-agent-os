"""
工作流一：扫 noon 后台 my inventory 导出的 CSV，写入对应销售主体的 wf1_<alias>_stock 的 noon_* 字段。

人工流程：
  1. noon 后台 → my inventory → export → 默认下载名 Inventory.csv
  2. 文件丢到 ~/Downloads/点购工作流/inbox/
  3. 跑 `python3 ingest_noon_stock_csv.py`
  4. 处理完移到 inbox/processed/

CSV 每行是 (warehouse × SKU × inventory_type) 的库存条目，按 country_code 自动路由 entity，
按 partner_sku 聚合：
  noon_total_qty       = SUM(qty)
  noon_saleable_qty    = SUM(qty WHERE inventory_type='saleable')
  noon_unsaleable_qty  = noon_total - noon_saleable
  noon_warehouses_json = [{warehouse_code, qty, inventory_type}, ...]

CLI:
  python3 ingest_noon_stock_csv.py --dry-run
  python3 ingest_noon_stock_csv.py
  python3 ingest_noon_stock_csv.py --file <path>
"""
import os, sys, csv, sqlite3, argparse, json, shutil
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sales_entity import load_entities, ensure_tables, stock_table

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")
INBOX_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "inbox")
PROCESSED_DIR = os.path.join(INBOX_DIR, "processed")

# noon 库存类型，可售只算 saleable
SALEABLE_TYPES = {"saleable"}


def safe_int(v):
    if v in (None, ""): return 0
    try: return int(float(v))
    except (TypeError, ValueError): return 0


def is_inventory_csv(path):
    """noon Inventory CSV 必含 partner_sku + qty + inventory_type + country_code 列。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames or [])
        return {"partner_sku", "qty", "inventory_type", "country_code"}.issubset(cols)
    except Exception:
        return False


def process_csv(path, conn, dry_run=False):
    print(f"\n=== {path} ===", file=sys.stderr)
    entities = {e["country"]: e for e in load_entities()}
    # entity_alias -> partner_sku -> 聚合
    bucket = defaultdict(lambda: defaultdict(lambda: {
        "noon_total_qty": 0,
        "noon_saleable_qty": 0,
        "noon_unsaleable_qty": 0,
        "warehouses": [],
        "title": None,
        "noon_sku": None,
        "family": None,
    }))

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        if dry_run:
            print(f"  columns ({len(cols)}): {cols}", file=sys.stderr)
            return 0
        n_rows = 0
        for row in reader:
            n_rows += 1
            partner_sku = (row.get("partner_sku") or "").strip()
            country = (row.get("country_code") or "").strip().upper()
            if not (partner_sku and country):
                continue
            ent = entities.get(country)
            if not ent:
                continue   # 不在白名单
            qty = safe_int(row.get("qty"))
            inv_type = (row.get("inventory_type") or "").strip().lower()

            agg = bucket[ent["alias"]][partner_sku]
            agg["noon_total_qty"] += qty
            if inv_type in SALEABLE_TYPES:
                agg["noon_saleable_qty"] += qty
            else:
                agg["noon_unsaleable_qty"] += qty
            agg["warehouses"].append({
                "warehouse_code": (row.get("warehouse_code") or "").strip(),
                "qty": qty,
                "inventory_type": inv_type,
            })
            if not agg["title"]:    agg["title"] = (row.get("title") or "").strip() or None
            if not agg["noon_sku"]: agg["noon_sku"] = (row.get("sku") or "").strip() or None
            if not agg["family"]:   agg["family"] = (row.get("family") or "").strip() or None

    print(f"  read {n_rows} rows", file=sys.stderr)

    # 写库
    cur = conn.cursor()
    total_written = 0
    for alias, skus in bucket.items():
        t = stock_table(alias)
        for psk, agg in skus.items():
            cur.execute(f"""
                INSERT INTO {t}
                  (partner_sku, noon_sku, title, family,
                   noon_total_qty, noon_saleable_qty, noon_unsaleable_qty, noon_warehouses_json)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(partner_sku) DO UPDATE SET
                  noon_sku    = COALESCE(excluded.noon_sku, {t}.noon_sku),
                  title       = COALESCE({t}.title, excluded.title),
                  family      = COALESCE({t}.family, excluded.family),
                  noon_total_qty       = excluded.noon_total_qty,
                  noon_saleable_qty    = excluded.noon_saleable_qty,
                  noon_unsaleable_qty  = excluded.noon_unsaleable_qty,
                  noon_warehouses_json = excluded.noon_warehouses_json,
                  imported_at = datetime('now', 'localtime')
            """, (
                psk, agg["noon_sku"], agg["title"], agg["family"],
                agg["noon_total_qty"], agg["noon_saleable_qty"], agg["noon_unsaleable_qty"],
                json.dumps(agg["warehouses"], ensure_ascii=False),
            ))
            total_written += 1
        conn.commit()
        print(f"  [{alias}] {len(skus)} skus → {t}", file=sys.stderr)
    return total_written


def run(dry_run=False, single_file=None):
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    if single_file:
        files = [single_file]
    else:
        files = []
        for fn in sorted(os.listdir(INBOX_DIR)):
            if not fn.endswith(".csv") or fn.startswith("."):
                continue
            full = os.path.join(INBOX_DIR, fn)
            if is_inventory_csv(full):
                files.append(full)
            else:
                # 非 inventory CSV，跳过（可能是 sales export，由 ingest_noon_csv 处理）
                pass

    if not files:
        print(f"[ingest_noon_stock] no inventory CSV in {INBOX_DIR}", file=sys.stderr)
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
    print(f"\n[done] processed {len(files)} csvs, {total} sku rows", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file", default=None)
    args = ap.parse_args()
    run(dry_run=args.dry_run, single_file=args.file)

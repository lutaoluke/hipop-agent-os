"""
工作流一：库存聚合（合并 ERP + noon 字段，计算 total_stock）。

读 wf1_<alias>_stock，更新：
  total_stock = noon_total + overseas_total + yiwu + dongguan
  pending_inbound_qty 暂留 NULL（逻辑待补）

CLI:
  python3 wf_stock_static.py
  python3 wf_stock_static.py --entities hipop_ksa
"""
import os, sys, sqlite3, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from sales_entity import load_entities, ensure_tables, stock_table

DB = os.path.join(os.path.dirname(__file__), "..", "..", "hipop.db")


def run(entity_aliases=None):
    entities = [e for e in load_entities()
                if not entity_aliases or e["alias"] in entity_aliases]
    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    for ent in entities:
        t = stock_table(ent["alias"])
        n = conn.execute(f"""
            UPDATE {t}
            SET total_stock = COALESCE(noon_total_qty, 0)
                            + COALESCE(overseas_total_qty, 0)
                            + COALESCE(yiwu_qty, 0)
                            + COALESCE(dongguan_qty, 0),
                imported_at = datetime('now', 'localtime')
        """).rowcount
        conn.commit()
        print(f"[{ent['alias']}] updated total_stock for {n} sku rows", file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", default=None)
    args = ap.parse_args()
    run(entity_aliases=args.entities.split(",") if args.entities else None)

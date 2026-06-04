"""
选品 CLI 入口. python -m selection.cli.select <subcmd>.

PoC 阶段子命令:
  initdb              初始化 selector.db
  history-seed        从 hipop wf 表灌正/反例到 preferences.jsonl
  status              看 preferences 阶段 + kb 统计 + db 行数
  fc-test KEYWORD     用 firecrawl 抓 noon-ksa + amazon-uae (PoC 起点)

Step 5 才会上 select 真命令.
"""
from __future__ import annotations
import argparse, json, os, sys


def cmd_initdb(args):
    from selection.shared import db
    db.init_db()
    print(f"[ok] selector.db @ {db.DB_PATH}")


def cmd_status(args):
    from selection.l2_knowledge import loader as kb_loader
    from selection.l2_knowledge import hipop_adapter
    from selection.shared import db
    import sqlite3

    print("=== preferences.jsonl ===")
    pp = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "preferences.jsonl"))
    n = 0; n_acc = n_rej = n_hold = 0
    if os.path.exists(pp):
        for line in open(pp, "r", encoding="utf-8"):
            try:
                ev = json.loads(line)
                n += 1
                if ev.get("action") == "accept": n_acc += 1
                elif ev.get("action") == "reject": n_rej += 1
                elif ev.get("action") == "hold": n_hold += 1
            except Exception: pass
    if n < 30:    phase = "phase_1_cold_start (0-30)"
    elif n < 300: phase = "phase_2_user_vector (30-300)"
    elif n < 1000: phase = "phase_3_constraint_promotion (300-1000)"
    else:         phase = "phase_4_lora_dpo (1000+)"
    print(f"  total: {n} (accept={n_acc} reject={n_rej} hold={n_hold})")
    print(f"  phase: {phase}")

    print("\n=== knowledge base ===")
    kb = kb_loader.load()
    print(f"  hard_bans       : {len(kb.hard_bans)}")
    print(f"  hard_filters    : {len(kb.hard_filters)}")
    print(f"  soft_preferences: {len(kb.soft_preferences)}")
    print(f"  meta_templates  : {len(kb.meta_templates)}")
    print(f"  params keys     : {list(kb.params.keys())}")

    print("\n=== selector.db ===")
    try:
        with db.conn() as c:
            for tbl in ["sel_runs", "sel_products", "sel_skus",
                        "sel_decisions", "sel_inquiries"]:
                n = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
                print(f"  {tbl}: {n}")
    except sqlite3.OperationalError:
        print("  (db 未 initdb)")

    print("\n=== hipop adapter (sa_main 已排除) ===")
    print(f"  hipop tables: {hipop_adapter.list_hipop_tables()[:8]}...")


def cmd_history_seed(args):
    from selection.l2_knowledge.history_loader import seed_positive, seed_negative
    if args.positive or args.all:
        seed_positive(args.country, family=args.family, dry_run=args.dry_run)
    if args.negative or args.all:
        seed_negative(args.country, family=args.family, dry_run=args.dry_run)


def cmd_select(args):
    """端到端 PoC: N1 流量词扩展 → N2 多关键词抓 → N3 → N4 → N5 → 入库."""
    from selection.l3_orchestration.nodes.n1_keyword_expansion import expand
    from selection.l3_orchestration.nodes.n3_filter import apply_filters
    from selection.l3_orchestration.nodes.n4_price_analysis import analyze as n4_analyze
    from selection.l3_orchestration.nodes.n5_sales_normalize import normalize
    from selection.l0_data.fetchers import noon_fetcher
    from selection.shared import db
    import time

    keywords = expand(args.seed, args.category)
    print(f"[N1] {args.seed} → {len(keywords)} keywords:")
    for kw in keywords: print(f"     - {kw}")

    all_records = []
    seen_ids = set()
    run_id = db.start_run(trigger="select_e2e", keyword=args.seed,
                         category=args.category, markets=["noon_sa"])
    print(f"\n[N2] run_id={run_id}")
    for kw in keywords:
        try:
            recs = noon_fetcher.search(kw, country="ksa", debug=False, write_db=False)
            new = [r for r in recs if r.id not in seen_ids]
            for r in new: seen_ids.add(r.id)
            all_records.extend(new)
            print(f"  [{kw}] +{len(new)} new (total uniq={len(all_records)})")
            time.sleep(2)
        except Exception as e:
            print(f"  [{kw}] FAIL: {type(e).__name__}: {e}")

    print(f"\n[N2] uniq records: {len(all_records)}")

    print("\n[N3] filter (hard_ban + brand_mindshare + monopoly + return_risk)...")
    n3 = apply_filters(all_records)
    print(f"  in={n3['stats']['in']} passed={n3['stats']['passed']} "
          f"dropped={n3['stats']['dropped_hard_ban']} "
          f"mindshare={n3['stats']['brand_mindshare']} "
          f"monopoly={n3['stats']['monopoly_alerts']} "
          f"return_risk_high={n3['stats']['return_risk_high']}")
    for alert in n3["monopoly_alerts"]: print(f"  ⚠️  {alert}")

    passed = n3["passed"]

    print(f"\n[N4] price analysis (pack_size 分桶 + self band + 半托管 1.5×)...")
    n4 = n4_analyze(passed, country="ksa", family="bags_luggage")
    print(f"  pack_size 分布: {n4['stats']['pack_size_dist']}")
    if n4["self_band"]["n_skus"] > 0:
        sb = n4["self_band"]
        print(f"  自家 KSA bags_luggage 价段: n={sb['n_skus']} "
              f"min={sb['min']} med={sb['median']} max={sb['max']} {sb['currency']}")
    else:
        print(f"  自家 KSA bags_luggage: 无对照")
    print(f"  too_high vs self: {n4['stats']['n_too_high']}, too_low: {n4['stats']['n_too_low']}")
    print(f"  半托管 1.5× 跨档: {n4['stats']['n_half_managed_violation']}")

    print(f"\n[N5] cross-platform sales normalize (percentile_in_query)...")
    n5 = normalize(passed)
    for grp, st in n5["groups"].items():
        t = st["tier"]
        print(f"  {grp}: n={st['n']} 信号={st['n_with_signal']} "
              f"tier top={t['top']} high={t['high']} mid={t['mid']} low={t['low']} "
              f"(无信号={t['_no_signal']})")

    for rec in passed:
        db.upsert_product(rec, run_id=run_id)
    db.finish_run(run_id, status="done", note=f"e2e {args.seed} {len(passed)}")
    print(f"\n[done] run {run_id} 入库 {len(passed)}")


def cmd_production_noon(args):
    """KSA luggage/noon production path: N1 -> noon -> N3 -> N11 v3."""
    from selection.l3_orchestration.production_pipeline import run_ksa_luggage_noon
    import json

    detail_kwargs = {"detail_provider": None} if args.no_detail else {}
    result = run_ksa_luggage_noon(
        seed=args.seed,
        category=args.category,
        country=args.country,
        feature_extractor=None,
        supply_provider=None,
        ali_records=[],
        **detail_kwargs,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_fc_test(args):
    """Firecrawl 一次小测试: noon-ksa + amazon-ae 一个关键词. 写 dump 到 debug/."""
    from selection.l0_data import firecrawl_client as fc
    import urllib.parse, datetime as dt

    debug = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "debug"))
    os.makedirs(debug, exist_ok=True)
    ts = int(dt.datetime.now().timestamp())
    kw_enc = urllib.parse.quote(args.keyword)

    print(f"start credits: {fc.get_credit_usage()['remaining_credits']}")

    print(f"\n--- noon KSA: {args.keyword} ---")
    r = fc.scrape(f"https://www.noon.com/saudi-en/search/?q={kw_enc}",
                 proxy="basic", wait_for=3000)
    out = os.path.join(debug, f"noon_ksa_{args.keyword}_{ts}.md")
    with open(out, "w") as f: f.write(r["markdown"])
    print(f"  ✓ {len(r['markdown'])} chars → {out}")

    print(f"\n--- Amazon UAE: {args.keyword} ---")
    r = fc.scrape(f"https://www.amazon.ae/s?k={kw_enc}",
                 proxy="stealth", wait_for=4000)
    out = os.path.join(debug, f"amazon_uae_{args.keyword}_{ts}.md")
    with open(out, "w") as f: f.write(r["markdown"])
    print(f"  ✓ {len(r['markdown'])} chars → {out}")

    print(f"\nfinal credits: {fc.get_credit_usage()['remaining_credits']}")


def main():
    ap = argparse.ArgumentParser(prog="selection")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("initdb")
    p.set_defaults(func=cmd_initdb)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("select", help="端到端 N1→N5")
    p.add_argument("--seed", required=True, help="种子流量词")
    p.add_argument("--category", default=None, help="categories yaml 名 (luggage/chair/stroller)")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("production-noon", help="KSA luggage/noon 单一生产入口")
    p.add_argument("--seed", default="luggage", help="种子流量词")
    p.add_argument("--category", default="luggage", help="categories yaml 名")
    p.add_argument("--country", default="ksa", choices=["ksa"])
    p.add_argument("--no-detail", action="store_true", help="不抓 noon 详情, 显式标 evidence_insufficient")
    p.set_defaults(func=cmd_production_noon)

    p = sub.add_parser("history-seed")
    p.add_argument("--country", default="ksa", choices=["ksa", "uae"])
    p.add_argument("--family", default=None)
    p.add_argument("--positive", action="store_true")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_history_seed)

    p = sub.add_parser("fc-test", help="Firecrawl noon+amazon 一次小测试")
    p.add_argument("--keyword", required=True)
    p.set_defaults(func=cmd_fc_test)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

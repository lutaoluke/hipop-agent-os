"""
noon 商品详情页抓 — 取 Highlights / Specifications / Reviews summary.

Luke 反馈 #2: 商品特点要从 standard 商品页 (Highlights + Specifications) 拿,
不只看主图 + 标题. N6 卖点抽取的输入应该含这些结构化字段.

URL 模板 (从 ProductRecord.url 直接拿).

抓取目标 (按重要性):
  Highlights      → list[str], 商品卖点 bullet (例如 "made from extra strong ABS")
  Specifications  → dict, 规格表 (Product Weight / Size / Colour Name / Gender 等)
  Reviews summary → {avg, count, recent_evidence}

§A 步骤 5 + 11 都要这个: "商品详情、主图、评论、高销售 SKU、高评论 SKU"
"""
from __future__ import annotations
import os, re
from datetime import datetime
from typing import Optional

from selection.l0_data import firecrawl_client as fc


HIGHLIGHTS_CONTAINER_RE = re.compile(
    r'OverviewTab-module-scss-module__[^"]+__highlightsCtr[^>]*>([\s\S]*?)</div>'
)
SPEC_CONTAINER_RE = re.compile(
    r'SpecificationsTab-module-scss-module__[^"]+__container[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>'
)
SPEC_ROW_RE = re.compile(
    r'<td[^>]*specName[^>]*>([^<]+)</td>\s*<td[^>]*specValue[^>]*>([^<]+)</td>',
    re.MULTILINE
)


def _strip_tags(html: str) -> str:
    """去 HTML 标签."""
    return re.sub(r'<[^>]+>', ' ', html)


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def parse_detail(html: str) -> dict:
    """parse noon product page HTML → 结构化字段."""
    out = {
        "highlights": [],
        "specifications": {},
        "reviews_summary": {},
        "variants": [],   # 同款颜色/尺寸变体, 用于 N5.6 SKU 系列聚合
    }

    # 0. Variants — `<a class="...ThumbnailOptionItem-...optionButton" title="Black" href="...">`
    # 通用 parser: noon 任意品类的 swatch 都用这个 component.
    # attr 顺序不固定, 用两阶段抓: 先找含 ThumbnailOptionItem-...optionButton 的 a tag, 再各取 title/href.
    a_tag_re = re.compile(
        r'<a\s[^>]*?ThumbnailOptionItem-[^"]*?optionButton[^>]*?>',
        re.IGNORECASE | re.DOTALL
    )
    title_re = re.compile(r'\btitle="([^"]+)"', re.IGNORECASE)
    href_re = re.compile(r'\bhref="([^"]+)"', re.IGNORECASE)
    sku_url_re = re.compile(r"/(Z[A-Z0-9]{12,}|N\d+[A-Z]?)/p")

    seen_v = set()
    for m in a_tag_re.finditer(html):
        a_tag = m.group(0)
        href_m = href_re.search(a_tag)
        if not href_m: continue
        url = href_m.group(1)
        sku_m = sku_url_re.search(url)
        if not sku_m: continue
        sku = sku_m.group(1)
        if sku in seen_v: continue
        seen_v.add(sku)
        title_m = title_re.search(a_tag)
        label = title_m.group(1) if title_m else None
        out["variants"].append({"sku": sku, "url": url, "label": label})

    # 1. Highlights — <ul><li>...</li></ul>
    m = HIGHLIGHTS_CONTAINER_RE.search(html)
    if m:
        block = m.group(1)
        # 找 <li>...</li>
        for li in re.finditer(r'<li[^>]*>([\s\S]*?)</li>', block):
            txt = _clean(_strip_tags(li.group(1)))
            if txt and len(txt) > 5: out["highlights"].append(txt[:400])

    # 2. Specifications — 锁定 "Specifications</h3>" 之后 8000 chars 抽 td.specName/specValue
    spec_anchor = html.find("SpecificationsTab-module")
    if spec_anchor >= 0:
        block = html[spec_anchor:spec_anchor + 8000]
        names = re.findall(r'specName[^>]*>([^<]+)</td>', block)
        values = re.findall(r'specValue[^>]*>([^<]+)</td>', block)
        for n, v in zip(names, values):
            n_clean = _clean(n); v_clean = _clean(v)
            if n_clean and v_clean:
                out["specifications"][n_clean] = v_clean[:200]

    # 3. Reviews summary — 评分 + 评论数 + 评论日期 (用于 is_rising 判定)
    rating_m = re.search(r'(\d\.\d)\s*</span>[\s\S]{0,200}stars', html)
    if rating_m:
        out["reviews_summary"]["avg_rating"] = float(rating_m.group(1))
    count_m = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*reviews?', html, re.IGNORECASE)
    if count_m:
        out["reviews_summary"]["raw_count"] = count_m.group(1)

    # 评论日期 — noon 用 "Mon DD, YYYY" 格式 (e.g. "Jun 9, 2025") in ratedDate div
    # noon 默认评论按时间倒序展示前 N 条
    date_re = re.compile(
        r'ratedDate[^>]*>([A-Za-z]{3}\s+\d{1,2},\s+\d{4})</div>'
    )
    date_strs = date_re.findall(html)
    out["reviews_summary"]["review_dates_visible"] = date_strs[:20]

    # 算近 1 年占比 + 是否全部在近 1 年 (Luke 反馈 #4: 起量品判定)
    if date_strs:
        from datetime import datetime as _dt, timedelta
        parsed = []
        for s in date_strs:
            try:
                parsed.append(_dt.strptime(s, "%b %d, %Y"))
            except ValueError:
                pass
        if parsed:
            now = _dt.now()
            one_year_ago = now - timedelta(days=365)
            n_within_1y = sum(1 for d in parsed if d >= one_year_ago)
            out["reviews_summary"]["dates_count_visible"] = len(parsed)
            out["reviews_summary"]["dates_within_1y"] = n_within_1y
            out["reviews_summary"]["all_within_1y"] = n_within_1y == len(parsed)
            out["reviews_summary"]["recent_1y_ratio"] = round(n_within_1y / len(parsed), 2)
            # 时间跨度 (最早到最晚的天数)
            spread_days = (max(parsed) - min(parsed)).days
            out["reviews_summary"]["dates_spread_days"] = spread_days
            out["reviews_summary"]["earliest_visible"] = min(parsed).strftime("%Y-%m")
            out["reviews_summary"]["latest_visible"] = max(parsed).strftime("%Y-%m")

    # Luke 2026-05-11: noon 自带 ReviewSummary 的 AI 汇总句子 — 抓这个直接得好/差评要点
    summary_sentences = []
    sent_re = re.compile(
        r'summarySentence[^>]*>([\s\S]{10,500}?)</(?:div|span|li)',
        re.IGNORECASE
    )
    for m in sent_re.finditer(html):
        s = _clean(_strip_tags(m.group(1)))
        if 15 < len(s) < 300:
            summary_sentences.append(s)
    out["reviews_summary"]["summary_sentences"] = summary_sentences

    return out


def fetch_one(url: str, *, debug: bool = False) -> dict:
    """抓单商品详情. 返回 dict."""
    print(f"[noon-detail] {url}")
    r = fc.scrape(url, formats=["html"], wait_for=4000, only_main_content=False)
    html = r.get("html") or ""
    if not html:
        return {"error": "html empty", "credits_used": r.get("credits_used", 0)}

    if debug:
        debug_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "debug"))
        ts = int(datetime.now().timestamp())
        with open(os.path.join(debug_dir, f"noon_detail_{ts}.html"), "w") as f: f.write(html)

    parsed = parse_detail(html)
    parsed["credits_used"] = r["credits_used"]
    parsed["fetched_at"] = datetime.now().isoformat()
    parsed["url"] = url
    return parsed


def fetch_for_records(record_ids: list[str], *, throttle_sec: float = 1.0,
                      max_retries: int = 2) -> dict:
    """批量抓详情, 写回 db 的 policy_flags.detail. 返回统计."""
    import time
    from selection.shared import db
    n_ok = 0; n_fail = 0; total_credits = 0
    for i, pid in enumerate(record_ids, 1):
        rec = db.get_product(pid)
        if not rec:
            n_fail += 1
            continue
        try:
            d = fetch_one(rec.url, debug=False)
        except Exception as e:
            print(f"  [{i}/{len(record_ids)}] FAIL {pid}: {type(e).__name__}: {e}")
            n_fail += 1
            continue
        if d.get("error"):
            n_fail += 1
            continue

        rec.policy_flags["detail"] = {
            "highlights": d.get("highlights", []),
            "specifications": d.get("specifications", {}),
            "reviews_summary": d.get("reviews_summary", {}),
            "variants": d.get("variants", []),
            "fetched_at": d.get("fetched_at"),
        }
        db.upsert_product(rec)
        n_ok += 1
        total_credits += d.get("credits_used", 0)
        n_h = len(d.get("highlights", []))
        n_s = len(d.get("specifications", {}))
        print(f"  [{i}/{len(record_ids)}] {pid}: highlights={n_h}, specs={n_s}, "
              f"credits {d.get('credits_used',0)}")
        time.sleep(throttle_sec)
    return {"n_ok": n_ok, "n_fail": n_fail, "total_credits": total_credits}


if __name__ == "__main__":
    import argparse, json, sqlite3
    from selection.shared import db

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_one = sub.add_parser("one"); p_one.add_argument("--url", required=True)
    p_b = sub.add_parser("batch")
    p_b.add_argument("--platform", default="noon_sa")
    p_b.add_argument("--tiers", default="top,high",
                     help="只抓哪些 tier (节省 credits)")
    p_b.add_argument("--relevance-only", action="store_true",
                     help="只抓相关性通过的")
    p_b.add_argument("--limit", type=int, default=None)
    p_b.add_argument("--throttle", type=float, default=1.5)
    args = ap.parse_args()

    if args.cmd == "one":
        r = fetch_one(args.url, debug=True)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
    elif args.cmd == "batch":
        with db.conn() as c:
            tiers = tuple(args.tiers.split(","))
            tier_clause = ",".join(f"'{t}'" for t in tiers)
            rel_clause = ("AND json_extract(policy_flags_json,'$.relevance_check.passed')=1"
                         if args.relevance_only else "")
            limit_clause = f"LIMIT {args.limit}" if args.limit else ""
            rows = c.execute(f"""
                SELECT id FROM sel_products
                WHERE platform='{args.platform}'
                  AND json_extract(sales_signal_json,'$.tier_in_query') IN ({tier_clause})
                  {rel_clause}
                ORDER BY CAST(json_extract(sales_signal_json,'$.percentile_in_query') AS REAL) DESC
                {limit_clause}
            """).fetchall()
        ids = [r["id"] for r in rows]
        print(f"待抓 {len(ids)} 商品 (大约 {len(ids)} credits)")
        result = fetch_for_records(ids, throttle_sec=args.throttle)
        print(f"\n[done] ok={result['n_ok']}, fail={result['n_fail']}, "
              f"credits used={result['total_credits']}")

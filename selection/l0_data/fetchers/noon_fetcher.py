"""
noon UAE/SA 列表抓 — Firecrawl /scrape (basic, formats=['html']) + BS4 parse.

🔄 2026-05-06 重写: 改 markdown → HTML parser. 原因:
  noon 商品卡用 React + CSS 动画轮播销量徽章 (Selling out fast → X+ sold recently
  → #1 in Category → Free Delivery), markdown 只能拿到当前 frame, 评分用 SVG
  渲染丢失. HTML 里 50 个商品的 nudgeText 全 frame 都在, data-qa 标记完整.

§A 步骤 4 销量徽章归一 (硬规则):
  Selling out fast            → tier=top
  X+ sold recently (具体数字) → 归一层算 percentile (raw_value=数字)
  #1 in <Category>            → tier=top (类目排名第一)
  Best Seller                 → tier=top
  无销量字段                   → tier=low (§A 步骤 4)
"""
from __future__ import annotations
import os, re, urllib.parse
from datetime import datetime
from typing import Optional

from selection.l0_data import firecrawl_client as fc
from selection.l1_normalize.product_record import ProductRecord, SalesSignal


COUNTRY_TO_PATH = {"ksa": "saudi-en", "sa": "saudi-en",
                   "uae": "uae-en", "ae": "uae-en", "eg": "egypt-en"}
COUNTRY_TO_PLATFORM = {"ksa": "noon_sa", "sa": "noon_sa",
                       "uae": "noon_ae", "ae": "noon_ae", "eg": "noon_ae"}
COUNTRY_VAT_LEGAL = {"ksa": 0.15, "sa": 0.15, "uae": 0.05, "ae": 0.05, "eg": 0.14}


def search_url(keyword: str, country: str = "ksa") -> str:
    site = COUNTRY_TO_PATH[country.lower()]
    return f"https://www.noon.com/{site}/search/?q={urllib.parse.quote(keyword)}"


# ── HTML parse (BS4) ──────────────────────────────────────────

SKU_RE = re.compile(r"/(Z[A-Z0-9]{12,}|N\d+[A-Z]?)(?:Z|/p|\?|$)")


def _parse_review_count(s: str) -> Optional[int]:
    """评论数字符串 '1.5K' / '203' / '12' → int."""
    if not s: return None
    s = s.replace(",", "").strip().upper()
    m = re.match(r"(\d+(?:\.\d+)?)([KM]?)", s)
    if not m: return None
    v = float(m.group(1))
    if m.group(2) == "K": v *= 1000
    elif m.group(2) == "M": v *= 1_000_000
    return int(v)


def parse_listing(html: str, country: str, search_query: str,
                  source_path: str) -> list[ProductRecord]:
    """parse Firecrawl 抓回的 noon 列表 HTML → ProductRecord 列表.

    每个商品在 `<div data-qa="plp-product-box">` 容器内, 含完整 nudge 轮播全文本.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    platform = COUNTRY_TO_PLATFORM[country.lower()]
    vat_legal = COUNTRY_VAT_LEGAL[country.lower()]

    boxes = soup.select('[data-qa="plp-product-box"]')
    out: list[ProductRecord] = []
    seen: set[str] = set()

    for box in boxes:
        # SKU + URL: 容器内有 <a class="...productBoxLink" href="...">
        link = box.select_one('a[href*="/p/"], a[class*="LinkHandler"][href]')
        if not link:
            continue
        href = link.get("href", "")
        m = SKU_RE.search(href)
        if not m:
            continue
        sku = m.group(1)
        if sku in seen:
            continue
        seen.add(sku)
        if not href.startswith("http"):
            href = "https://www.noon.com" + href

        # 标题
        name_el = box.select_one('[data-qa="plp-product-box-name"]')
        title = (name_el.get("title") or name_el.get_text(strip=True)) if name_el else f"noon-{sku}"

        # 价格
        price = None; price_currency = "SAR" if "saudi" in country.lower() or country.lower() in ("ksa","sa") else "AED"
        price_el = box.select_one('[data-qa="plp-product-box-price"]')
        if price_el:
            ptext = price_el.get_text(" ", strip=True)
            pm = re.search(r"(\d+(?:[,.]\d{1,3})*(?:\.\d{1,2})?)", ptext.replace("\xa0", " "))
            if pm:
                try:
                    price = float(pm.group(1).replace(",", ""))
                except ValueError: pass

        # 评分 + 评论数 (在 RatingPreviewStar-module 容器内)
        rating = None; review_count = None
        rating_el = box.select_one('[class*="RatingPreviewStar"]')
        if rating_el:
            rtext = rating_el.get_text(" ", strip=True)
            # 形如 "4.5 1.5K" 或 "4.1 (1510)" 或 "4.1 1510"
            rm = re.search(r"(\d\.\d)", rtext)
            if rm: rating = float(rm.group(1))
            # 评论数: 评分后第一个数字 (可能带 K/M)
            cm = re.search(r"(\d+(?:\.\d+)?[KM]?)\s*$", rtext) or re.search(r"\(([^)]+)\)", rtext)
            if cm: review_count = _parse_review_count(cm.group(1))
            # fallback: 找 RatingPreviewStar 后面跟着的数字
            if rating and not review_count:
                cm2 = re.search(r"\d\.\d\D+(\d+(?:\.\d+)?[KM]?)", rtext)
                if cm2: review_count = _parse_review_count(cm2.group(1))

        # 销量徽章 (Nudges-module-scss-module nudgeText, 全 frame)
        nudges = [el.get_text(strip=True) for el in box.select('[class*="nudgeText"]')]
        # 加: 类目排名 (#1 in X) / Best Seller 也算 top tier 信号
        all_signals = [n for n in nudges if n]

        # 归一到 SalesSignal
        signal_type = "unknown"
        signal_value = None
        signal_text = None
        signal_confidence = 0.0
        tier = "low"

        for n in all_signals:
            n_low = n.lower()
            # 'X+ sold recently' 数字优先
            sm = re.search(r"(\d+)\+?\s*sold\s*recently", n, re.IGNORECASE)
            if sm:
                signal_type = "absolute_count"
                signal_value = float(sm.group(1))
                signal_text = n
                signal_confidence = 0.85
                tier = None  # 让归一层算 percentile
                break
        if tier == "low":
            for n in all_signals:
                if "selling out fast" in n.lower():
                    signal_type = "badge"; signal_text = n
                    signal_confidence = 0.75; tier = "top"; break
                if re.match(r"#\d+\s+in\s+", n, re.IGNORECASE):
                    signal_type = "badge"; signal_text = n
                    signal_confidence = 0.7; tier = "top"; break
                if "best seller" in n.lower() or "bestseller" in n.lower():
                    signal_type = "badge"; signal_text = n
                    signal_confidence = 0.8; tier = "top"; break

        # 物流标
        free_delivery = any("free delivery" in n.lower() for n in all_signals)
        is_lowest = any("lowest" in n.lower() and "30" in n for n in all_signals)

        # 图片
        img_el = box.select_one('img[alt][src*="nooncdn"], img[alt][src*="cloudfront"]')
        # 优先含商品名 alt 的图 (避免 placeholder)
        img_src = None
        for img in box.select('img[src]'):
            src = img.get("src", "")
            alt = img.get("alt", "")
            if "placeholder" in src or "media-placeholder" in alt.lower():
                continue
            if any(x in src for x in ["nooncdn", "cloudfront"]):
                img_src = src
                break

        rec_id = f"{platform}:{sku}"
        try:
            rec = ProductRecord(
                id=rec_id, platform=platform, url=href,
                title=title[:200], brand=None, category_path=[],
                images=[img_src] if img_src else [],
                price={
                    "value": price,
                    "currency": price_currency,
                    "is_lowest_30d": is_lowest,
                    "free_delivery": free_delivery,
                },
                sales_signal=SalesSignal(
                    type=signal_type,
                    raw_value=signal_value,
                    raw_text=signal_text,
                    source="noon_html_nudges",
                    confidence=signal_confidence,
                    tier_in_query=tier,
                ),
                reviews={"avg": rating, "count": review_count} if (rating or review_count) else {},
                policy_flags={
                    "country": country,
                    "search_query": search_query,
                    "all_nudges": all_signals,    # 留全 nudge 给 N6 LLM 看
                },
                market_meta={"vat_rate": vat_legal},
                source_path=source_path,
            )
            out.append(rec)
        except Exception as e:
            print(f"  [skip] {rec_id}: {type(e).__name__}: {e}")

    return out


def search(keyword: str, country: str = "ksa", *,
           debug: bool = False, write_db: bool = True) -> list[ProductRecord]:
    """noon 列表搜 + parse + 写库. 用 HTML format."""
    from selection.shared import db
    url = search_url(keyword, country)
    platform = COUNTRY_TO_PLATFORM[country.lower()]

    print(f"[noon] search {keyword!r} {country} → {url}")
    r = fc.scrape(url, proxy="basic", wait_for=4000,
                 formats=["html"], only_main_content=False)
    html = r.get("html") or ""
    if not html:
        # SDK fallback: html 字段名可能在 metadata 里
        print(f"[noon] WARN: html empty, fallback markdown")
        return []
    print(f"[noon] html: {len(html)} chars, credits: {r['credits_used']}")

    if debug:
        debug_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "debug"))
        os.makedirs(debug_dir, exist_ok=True)
        ts = int(datetime.now().timestamp())
        with open(os.path.join(debug_dir, f"noon_{country}_{keyword}_{ts}.html"), "w") as f:
            f.write(html)

    source_path = f"firecrawl_noon_{country}_html@{datetime.now().date().isoformat()}"
    records = parse_listing(html, country, keyword, source_path)
    print(f"[noon] parse: {len(records)} 商品")

    if write_db:
        run_id = db.start_run(trigger="keyword", keyword=keyword, category=None, markets=[platform])
        for rec in records:
            db.upsert_product(rec, run_id=run_id)
        db.add_run_credits(run_id, r["credits_used"])
        db.finish_run(run_id, status="done", note=f"noon {country} {len(records)}")
        print(f"[noon] db: run {run_id}, 入库 {len(records)}")
    return records


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--country", default="ksa", choices=list(COUNTRY_TO_PATH.keys()))
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()
    rs = search(args.keyword, args.country, debug=args.debug, write_db=not args.no_db)
    if rs:
        print("\n--- top 5 ---")
        for r in rs[:5]:
            sig = f"{r.sales_signal.type}/{r.sales_signal.tier_in_query}"
            rev = r.reviews
            print(f"  {r.id}: {r.title[:40]} | {r.price.get('value')} {r.price.get('currency')} "
                  f"| ⭐{rev.get('avg')} ({rev.get('count')}) | {sig} | {r.sales_signal.raw_text}")

"""
N5.5 — 近期增长品判定 (is_rising).

Luke 反馈 #4: 没销量徽章 (Selling out fast / X+ sold recently) 但仍可入选, 因为:
  - 评分不低 (≥4.0)
  - reviews 集中在近 1 年 → 商品近期上架且有销量积累

判定规则 (通用算法):
  is_rising = (avg_rating >= 4.0)
              AND (review_count >= 10)
              AND (recent_1y_ratio >= 0.7)         # 70%+ 评论在近 1 年
              AND (dates_spread_days <= 365)        # 评论跨度不超 1 年

数据源 (依赖 N6 detail fetcher):
  rec.policy_flags['detail']['reviews_summary']['recent_1y_ratio']
  rec.policy_flags['detail']['reviews_summary']['dates_spread_days']
  rec.reviews['avg'], rec.reviews['count']
"""
from __future__ import annotations
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord


# 判定阈值 (yaml 可覆盖)
RISING_RATING_MIN = 4.0
RISING_REVIEW_COUNT_MIN = 10
RISING_RECENT_1Y_RATIO_MIN = 0.7
RISING_SPREAD_DAYS_MAX = 365


def is_rising(rec: ProductRecord) -> dict:
    """
    判 is_rising. 返回 {is_rising, reasons, evidence}.
    需要 rec.policy_flags['detail']['reviews_summary'] (来自 noon_detail_fetcher).
    """
    rating = rec.reviews.get("avg")
    count = rec.reviews.get("count")
    detail = rec.policy_flags.get("detail") or {}
    rs = detail.get("reviews_summary") or {}
    recent_1y_ratio = rs.get("recent_1y_ratio")
    spread_days = rs.get("dates_spread_days")
    visible_n = rs.get("dates_count_visible") or 0

    reasons_pos = []
    reasons_neg = []

    # 各条件检查
    if rating and rating >= RISING_RATING_MIN:
        reasons_pos.append(f"rating {rating} ≥ {RISING_RATING_MIN}")
    else:
        reasons_neg.append(f"rating {rating} < {RISING_RATING_MIN}")

    if count and count >= RISING_REVIEW_COUNT_MIN:
        reasons_pos.append(f"review_count {count} ≥ {RISING_REVIEW_COUNT_MIN}")
    else:
        reasons_neg.append(f"review_count {count} < {RISING_REVIEW_COUNT_MIN}")

    # 评论时间戳条件 (依赖 detail 抓到)
    if recent_1y_ratio is None:
        reasons_neg.append("无 detail 数据 (跳 N6 detail fetcher 才能判)")
    else:
        if recent_1y_ratio >= RISING_RECENT_1Y_RATIO_MIN:
            reasons_pos.append(f"近 1y 评论 {recent_1y_ratio:.0%} ≥ {RISING_RECENT_1Y_RATIO_MIN:.0%} ({visible_n} 条)")
        else:
            reasons_neg.append(f"近 1y 评论 {recent_1y_ratio:.0%} < {RISING_RECENT_1Y_RATIO_MIN:.0%}")
        if spread_days is not None:
            if spread_days <= RISING_SPREAD_DAYS_MAX:
                reasons_pos.append(f"评论跨度 {spread_days}d ≤ {RISING_SPREAD_DAYS_MAX}d")
            else:
                reasons_neg.append(f"评论跨度 {spread_days}d > {RISING_SPREAD_DAYS_MAX}d (老商品)")

    rising = len(reasons_neg) == 0 and len(reasons_pos) >= 4   # 4 个 positive 条件全过
    return {
        "is_rising": rising,
        "reasons_pos": reasons_pos,
        "reasons_neg": reasons_neg,
        "evidence": {
            "avg_rating": rating, "review_count": count,
            "recent_1y_ratio": recent_1y_ratio,
            "dates_spread_days": spread_days,
            "visible_n": visible_n,
        }
    }


def apply_is_rising(records: list[ProductRecord]) -> dict:
    """批量跑 is_rising. mutate rec.sales_signal.is_rising + rec.policy_flags.is_rising_check."""
    n_rising = 0
    n_no_detail = 0
    for rec in records:
        result = is_rising(rec)
        rec.sales_signal.is_rising = result["is_rising"]
        rec.policy_flags["is_rising_check"] = result
        if result["is_rising"]:
            n_rising += 1
            rec.sales_signal.rising_evidence = "; ".join(result["reasons_pos"])
        if any("无 detail" in r for r in result.get("reasons_neg", [])):
            n_no_detail += 1
    return {
        "n_input": len(records),
        "n_rising": n_rising,
        "n_no_detail": n_no_detail,
    }


if __name__ == "__main__":
    import sqlite3, json, sys
    sys.path.insert(0, ".")
    from selection.shared import db
    with db.conn() as c:
        rows = c.execute("""SELECT id FROM sel_products
            WHERE platform='noon_sa'
              AND json_extract(policy_flags_json,'$.relevance_check.passed')=1""").fetchall()
    recs = [db.get_product(r["id"]) for r in rows]
    recs = [r for r in recs if r]

    result = apply_is_rising(recs)
    print(f"is_rising: {result}")
    for rec in recs: db.upsert_product(rec)
    print("[ok] sales_signal.is_rising + policy_flags.is_rising_check written")

    # sample rising 商品
    rising_recs = [r for r in recs if r.sales_signal.is_rising]
    print(f"\n--- {len(rising_recs)} rising 商品样本 ---")
    for r in rising_recs[:8]:
        print(f"  ⭐ {r.title[:50]}")
        print(f"     {r.sales_signal.rising_evidence}")

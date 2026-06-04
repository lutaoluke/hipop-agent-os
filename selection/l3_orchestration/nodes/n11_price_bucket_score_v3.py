"""
N11 v3 — 价格带分桶 × 成熟/起量分组 双维打分.

vs v2 关键变化:
  v2: sales_pct = bucket 内 SALES_TIER_TO_SCORE(top=1.0/high=0.7/...) 的分位
      → 损失精度, 同 tier 的产品 sales_pct 全相同
  v3: sales_pct = bucket 内 monthly_sales_volume 的连续分位
      → 直接看真销量, 同价段内月销 5000 vs 月销 500 拉得开
      → cat_rank tier 退化为信息列, 不参与排序

  v2: rising 跟成熟款混在一起 N11 排序
      → Luke 反馈: "低价销量高的把高端品挤走, 无法做差异化"
  v3: 成熟款 + rising 双轨制
      → 成熟款进 N11 价格带分桶 + score 排序 (高端品不会被低价挤走)
      → rising 单独一组, 不参与 N11 排序, 在另一表展示
      → score 加权改: 0.50 sales_pct + 0.25 rating_pct + 0.25 feature_pct
        (rising 维度不再加权, 因为已经分轨)
"""
from __future__ import annotations
from collections import Counter, defaultdict
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord


WEIGHT_SALES = 0.42
WEIGHT_RATING = 0.20
WEIGHT_FEATURE = 0.18
WEIGHT_DIFFERENTIATION = 0.12
WEIGHT_INVENTORY = 0.08

TIER_PCT_CUTS = [(0.10, "一档"), (0.30, "二档"), (0.60, "三档"), (1.01, "四档")]


def _rating_score(avg, count):
    import math
    if not avg or not count: return 0.0
    rating_norm = max(0.0, (float(avg) - 3.0) / 2.0)
    count_norm = min(1.0, math.log10(max(1, int(count))) / 3.0)
    return rating_norm * count_norm


def _features_count(rec):
    n6 = rec.policy_flags.get("n6_extracted") or {}
    feats = n6.get("features") or []
    return len(feats)


def _differentiation_score(rec):
    diff = rec.policy_flags.get("differentiation") or {}
    return float(diff.get("score") or 0.0)


def _inventory_adjustment(rec):
    inv = rec.policy_flags.get("inventory_reverse_constraint") or {}
    return float(inv.get("score_adjustment") or 0.0)


def _quantile_cuts(values: list[float], n_buckets: int) -> list[float]:
    n = len(values)
    if n < n_buckets: return []
    sv = sorted(values)
    return [sv[int(n * (i + 1) / n_buckets)] for i in range(n_buckets - 1)]


def _bucket_of(p: float, cuts: list[float]) -> int:
    for i, c in enumerate(cuts):
        if p < c: return i
    return len(cuts)


def _percentile_of(value: float, sorted_values: list[float]) -> float:
    """value 在 sorted_values 里的分位 (0.0-1.0)."""
    if not sorted_values: return 0.0
    n = len(sorted_values)
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    return (below + 0.5 * equal) / n


def _tier_from_pct(pct: float) -> str:
    for cut, name in TIER_PCT_CUTS:
        if pct < cut: return name
    return "四档"


def apply_v3(records: list[ProductRecord], *,
             n_buckets: int = 5,
             country: str = "ksa") -> dict:
    """
    返回:
      {
        "mature": {bucket_info, distribution, price_cuts, n_buckets},
        "rising": {n, list},
        "dropped": {n, reasons:Counter},
      }
    Mutate rec.policy_flags["overall_v3"]:
      {"track":"mature"/"rising"/"dropped",
       "tier_overall":"一档/二档/三档/四档/drop",
       "score":float, "bucket":int, "bucket_rank":int, "bucket_size":int,
       "bucket_pct":float,
       "breakdown":{sales_pct,rating_pct,feature_pct,differentiation_pct,inventory_pct}}
    """
    mature_recs: list[ProductRecord] = []
    rising_recs: list[ProductRecord] = []
    dropped: dict[str, int] = Counter()

    # 路由: relevance drop / rising 单轨 / 成熟款进 N11
    for rec in records:
        rc = rec.policy_flags.get("relevance_check", {})
        if rc.get("passed") is False:
            rec.policy_flags["overall_v3"] = {
                "track": "dropped", "tier_overall": "drop",
                "reason": f"relevance: {rc.get('reason','')}",
                "score": 0.0,
            }
            dropped[rc.get("reason", "relevance")[:40]] += 1
            continue
        cls = (rec.policy_flags.get("sorftime") or {}).get("classification", {})
        is_rising = (
            bool(cls.get("is_rising"))
            or bool(rec.sales_signal.is_rising)
            or bool((rec.policy_flags.get("group_aggregated") or {}).get("any_rising"))
        )
        if is_rising:
            rising_recs.append(rec)
            rec.policy_flags["overall_v3"] = {
                "track": "rising", "tier_overall": "rising",
                "score": rec.sales_signal.raw_value or 0.0,    # rising 表内按月销排序
                "rising_evidence": cls.get("rising_evidence") or rec.sales_signal.rising_evidence,
            }
        else:
            mature_recs.append(rec)

    # 成熟款: 价格带分桶
    valid_mature = [r for r in mature_recs
                    if (r.price.get("value") or 0) > 0
                    and (r.sales_signal.raw_value or 0) > 0]
    if not valid_mature:
        return {
            "mature": {"bucket_info": {}, "distribution": {}, "price_cuts": [],
                       "n_buckets": n_buckets},
            "rising": {"n": len(rising_recs), "records": rising_recs},
            "dropped": {"n": sum(dropped.values()), "reasons": dict(dropped)},
        }

    prices = [float(r.price.get("value")) for r in valid_mature]
    cuts = _quantile_cuts(prices, n_buckets)

    by_bucket: dict[int, list[ProductRecord]] = defaultdict(list)
    for r in valid_mature:
        bi = _bucket_of(float(r.price.get("value")), cuts)
        r._bucket_v3 = bi
        by_bucket[bi].append(r)

    bucket_info = {}
    tier_counts = Counter()

    for bi, brecs in by_bucket.items():
        # bucket 内: 月销量分位 + rating × log10(count) 分位 + feature/N8/N9 分位
        sales_arr = sorted(float(r.sales_signal.raw_value) for r in brecs)
        rating_arr = sorted(_rating_score(r.reviews.get("avg"), r.reviews.get("count")) for r in brecs)
        feature_arr = sorted(float(_features_count(r)) for r in brecs)
        differentiation_arr = sorted(_differentiation_score(r) for r in brecs)
        inventory_arr = sorted(_inventory_adjustment(r) for r in brecs)

        scored = []
        for r in brecs:
            sales_pct = _percentile_of(float(r.sales_signal.raw_value), sales_arr)
            rating_pct = _percentile_of(
                _rating_score(r.reviews.get("avg"), r.reviews.get("count")), rating_arr,
            )
            feature_pct = _percentile_of(float(_features_count(r)), feature_arr)
            differentiation_pct = _percentile_of(_differentiation_score(r), differentiation_arr)
            inventory_pct = _percentile_of(_inventory_adjustment(r), inventory_arr)
            score = (
                WEIGHT_SALES * sales_pct
                + WEIGHT_RATING * rating_pct
                + WEIGHT_FEATURE * feature_pct
                + WEIGHT_DIFFERENTIATION * differentiation_pct
                + WEIGHT_INVENTORY * inventory_pct
            )
            scored.append((
                score, r, sales_pct, rating_pct, feature_pct,
                differentiation_pct, inventory_pct,
            ))

        # bucket 内 score 降序
        scored.sort(key=lambda x: -x[0])
        n_b = len(scored)
        for rank, (score, r, sp, rp, fp, dp, ip) in enumerate(scored, 1):
            pct_in_bucket = (rank - 1) / max(1, n_b)
            tier = _tier_from_pct(pct_in_bucket)
            r.policy_flags["overall_v3"] = {
                "track": "mature",
                "tier_overall": tier,
                "score": round(score, 3),
                "bucket": bi,
                "bucket_rank": rank,
                "bucket_size": n_b,
                "bucket_pct": round(pct_in_bucket, 3),
                "breakdown": {
                    "sales_pct": round(sp, 3),
                    "rating_pct": round(rp, 3),
                    "feature_pct": round(fp, 3),
                    "differentiation_pct": round(dp, 3),
                    "inventory_pct": round(ip, 3),
                },
                "_feats": {
                    "unit_price": float(r.price.get("value")),
                    "monthly_sales": float(r.sales_signal.raw_value),
                    "rating": r.reviews.get("avg"),
                    "review_count": r.reviews.get("count"),
                    "features_n": _features_count(r),
                    "differentiation_score": _differentiation_score(r),
                    "inventory_adjustment": _inventory_adjustment(r),
                },
            }
            tier_counts[tier] += 1

        bucket_info[bi] = {
            "n": n_b,
            "price_range": (round(min(prices_in := [float(r.price.get('value')) for r in brecs]), 1),
                            round(max(prices_in), 1)),
            "tier_dist": dict(Counter(r.policy_flags["overall_v3"]["tier_overall"] for r in brecs)),
        }

    # cleanup
    for r in records:
        if hasattr(r, "_bucket_v3"): delattr(r, "_bucket_v3")

    return {
        "mature": {
            "bucket_info": bucket_info,
            "distribution": dict(tier_counts),
            "price_cuts": [round(c, 1) for c in cuts],
            "n_buckets": n_buckets,
            "n_total": len(valid_mature),
        },
        "rising": {
            "n": len(rising_recs),
            "records": rising_recs,
        },
        "dropped": {
            "n": sum(dropped.values()),
            "reasons": dict(dropped),
        },
    }

"""
N5 — 跨平台销量归一 (percentile_in_query / tier_in_query / z_score_in_query).

§A 步骤 4 hard rule: '好销量不是绝对值, 是竞争视角下的相对值'.
归一在 **同 platform + 同 search query 内** 算分位数, 跨平台用同一 schema 但
分别归一. 决策层永远只看 tier_in_query / percentile_in_query, 禁止用 raw_value
做绝对阈值过滤 (sold>=30 这种被 lint 拦下).

特殊规则 (§A 步骤 4 + §10 N5):
  - sales_signal.type=='unknown' (无销量字段) → tier=low, 跳过 percentile 计算
  - 'Selling out fast' / 'Best Seller' / '#N in Cat' badge → 视为高位, 跟数字一起进归一
  - is_rising=True 即使 tier=low 也要保留进观察池
"""
from __future__ import annotations
import statistics
from collections import defaultdict
from typing import Iterable

from selection.l1_normalize.product_record import ProductRecord


# tier 切分点 (按 percentile)
TIER_CUTOFFS = [
    (90, "top"),    # >= 90 percentile
    (70, "high"),   # >= 70
    (40, "mid"),    # >= 40
    (0,  "low"),    # everything else
]

# badge 信号当作高 raw_value 注入归一池 (没具体数字, 给一个保守估计)
BADGE_SCORE = {
    "selling out fast": 100.0,   # noon 最强信号
    "best seller":      150.0,   # amazon 最强
    "amazon's choice":   80.0,
    "#1":                90.0,   # noon 类目排名第 1 (近似最强)
}


def _badge_to_score(raw_text: str) -> float | None:
    """把 badge 文本映射到 raw_value (跟 absolute_count 一起进归一)."""
    if not raw_text: return None
    rt = raw_text.lower().strip()
    for kw, score in BADGE_SCORE.items():
        if kw in rt: return score
    # #N in Cat: N 越小信号越强, 取 100/N
    import re
    m = re.match(r"#(\d+)\s+in\s+", raw_text)
    if m:
        n = int(m.group(1))
        if n <= 50: return max(20.0, 100.0 / n)
    return None


def _percentile(value: float, sorted_values: list[float]) -> float:
    """value 在 sorted_values 里的百分位 (0-100)."""
    if not sorted_values: return 0.0
    n = len(sorted_values)
    # 二分: 找 value <= 第几个
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] <= value: lo = mid + 1
        else: hi = mid
    return (lo / n) * 100.0


def _tier_from_pct(pct: float) -> str:
    for cut, tier in TIER_CUTOFFS:
        if pct >= cut: return tier
    return "low"


def normalize(records: Iterable[ProductRecord]) -> dict:
    """
    Mutate records.sales_signal: 加 percentile_in_query / tier_in_query / z_score_in_query.

    分组维度: (platform, search_query). search_query 从 policy_flags.search_query 拿,
    没有就只按 platform 分.
    """
    records = list(records)

    # 按 (platform, search_query) 分组
    groups: dict[tuple, list] = defaultdict(list)
    for rec in records:
        sq = rec.policy_flags.get("search_query") or "_default_"
        groups[(rec.platform, sq)].append(rec)

    stats_per_group = {}

    for (platform, sq), recs in groups.items():
        # 拿每个 record 的 effective_value
        # 优先 raw_value (绝对销量数字), 后备 badge → BADGE_SCORE 映射, 都没就 None
        scored = []
        for rec in recs:
            sig = rec.sales_signal
            v = sig.raw_value
            if v is None:
                v = _badge_to_score(sig.raw_text or "")
            scored.append((rec, v))

        # 仅有 raw_value 或 badge 的进入归一池
        with_v = [v for _, v in scored if v is not None]
        sorted_vals = sorted(with_v) if with_v else []

        # z-score 准备
        if len(with_v) >= 2:
            mean = statistics.mean(with_v)
            stdev = statistics.stdev(with_v) or 1.0
        else:
            mean, stdev = (0.0, 1.0)

        n_top = n_high = n_mid = n_low = n_none = 0
        for rec, v in scored:
            sig = rec.sales_signal
            if v is None:
                # §A 步骤 4 hard rule: 没销量字段 = low
                sig.percentile_in_query = None
                sig.tier_in_query = "low"
                sig.z_score_in_query = None
                n_none += 1
                n_low += 1
            else:
                pct = _percentile(v, sorted_vals)
                sig.percentile_in_query = round(pct, 1)
                sig.tier_in_query = _tier_from_pct(pct)
                sig.z_score_in_query = round((v - mean) / stdev, 2) if stdev else 0.0
                if sig.tier_in_query == "top": n_top += 1
                elif sig.tier_in_query == "high": n_high += 1
                elif sig.tier_in_query == "mid": n_mid += 1
                else: n_low += 1

        stats_per_group[f"{platform}|{sq}"] = {
            "n": len(recs), "n_with_signal": len(with_v),
            "tier": {"top": n_top, "high": n_high, "mid": n_mid, "low": n_low,
                     "_no_signal": n_none},
        }

    return {
        "n_records": len(records),
        "n_groups": len(groups),
        "groups": stats_per_group,
    }

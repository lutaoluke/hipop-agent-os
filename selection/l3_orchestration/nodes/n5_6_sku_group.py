"""
N5.6 — SKU 系列聚合 (通用算法, 不写死品牌/品类).

Luke 反馈 #6: 商品按系列 (颜色/尺寸 variant) 聚合, 不按单 SKU. 同款的某颜色没销量,
另一颜色爆款 → 整个系列应该当候选.

通用算法:
  1. 用 N6.5 detail.variants 列表做 union-find (每个 SKU 关联同款其他 SKU 列表)
  2. 同 group 的 SKU 聚合统计:
     - sales: max(raw_value), 取最强信号 SKU 代表系列
     - rating: 加权平均 (按各 SKU 评论数加权)
     - review_count: sum
     - is_rising: any(SKU is_rising)
     - relevance: any(SKU 通过) → 整个 group 通过 (group-level relevance)
     - features: union (合并 N6 特征)
     - colors: 每个 SKU 标 N6 主色 → group 颜色集合
     - price 范围: min(unit_price), max, median
  3. 选每个 group 的"代表 SKU" (有 detail+highest sales) 当展示主图
  4. group 级 overall_score 替代 SKU 级 (重新调 N11)

数据结构:
  rec.policy_flags["group_id"] = "<group_root_sku>"     某 SKU 所属 group root
  rec.policy_flags["group_member_skus"] = ["sku1", ...]  group 全成员
  rec.policy_flags["group_aggregated"] = {              聚合统计 (只在 root 上)
      "n_skus", "rep_sku", "max_sold", "avg_rating",
      "total_reviews", "any_rising", "any_relevant",
      "feature_union", "color_set", "price_min/max/median",
  }
"""
from __future__ import annotations
import statistics
from collections import defaultdict
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord


def build_groups(records: list[ProductRecord]) -> dict[str, list[str]]:
    """
    用 union-find 聚合: SKU A 的 detail.variants 含 SKU B → A,B 同 group.
    返回 {group_root_sku: [member_sku, ...]}.
    """
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # 字典序小的当 root, 稳定
            if ra < rb: parent[rb] = ra
            else: parent[ra] = rb

    # 初始化每个 SKU 各自一组
    sku_to_rec: dict[str, ProductRecord] = {}
    for rec in records:
        sku = rec.id.split(":", 1)[1]
        sku_to_rec[sku] = rec
        if sku not in parent:
            parent[sku] = sku

    # 合并 (variants 暴露的同款关系是双向的, 单边合就够)
    for rec in records:
        sku = rec.id.split(":", 1)[1]
        variants = (rec.policy_flags.get("detail") or {}).get("variants") or []
        for v in variants:
            other_sku = v.get("sku")
            if other_sku and other_sku in sku_to_rec:
                union(sku, other_sku)

    # 收集 root → members
    groups: dict[str, list[str]] = defaultdict(list)
    for sku in sku_to_rec:
        groups[find(sku)].append(sku)
    return dict(groups)


def aggregate_group(records: list[ProductRecord], member_skus: list[str],
                    sku_to_rec: dict[str, ProductRecord]) -> dict:
    """聚合 group 内多个 SKU 的统计."""
    members = [sku_to_rec[s] for s in member_skus if s in sku_to_rec]
    if not members:
        return {}

    # 销量信号: 取最强 SKU
    rep_sku = None
    max_sold_value = 0
    any_rising = False
    any_relevant = False
    color_set = set()
    feature_union = set()
    prices = []
    unit_prices = []
    review_counts = []
    rating_x_count = 0   # 加权评分分子
    total_count = 0      # 加权分母
    sold_max = 0
    sold_max_text = None
    tier_priority = {"top": 4, "high": 3, "mid": 2, "low": 1, None: 0}
    best_tier_score = 0
    best_tier = None

    for rec in members:
        sj = rec.sales_signal
        rv = sj.raw_value or 0
        if rv > sold_max:
            sold_max = rv
            sold_max_text = sj.raw_text
            if rv > max_sold_value:
                max_sold_value = rv
                rep_sku = rec.id.split(":", 1)[1]
        # tier 排名
        ts = tier_priority.get(sj.tier_in_query, 0)
        if ts > best_tier_score:
            best_tier_score = ts
            best_tier = sj.tier_in_query
        # rising
        if sj.is_rising: any_rising = True
        # relevance
        if rec.policy_flags.get("relevance_check", {}).get("passed"):
            any_relevant = True
        # 颜色 (N6.5 detail_features 优先, fallback N6 color_main)
        df = rec.policy_flags.get("detail_features") or {}
        c = df.get("color")
        if not c:
            n6 = rec.policy_flags.get("n6_extracted") or {}
            c = n6.get("color_main")
        if c and c != "未知":
            color_set.add(c)
        # 功能 union
        for f in rec.inferred_features or []:
            if f.startswith("功能_"):
                feature_union.add(f.replace("功能_", ""))
        # 价格
        v = rec.price.get("value")
        if v: prices.append(v)
        up = rec.policy_flags.get("unit_price")
        if up: unit_prices.append(up)
        # 评分加权
        rating = rec.reviews.get("avg")
        rc = rec.reviews.get("count") or 0
        if rating and rc:
            rating_x_count += rating * rc
            total_count += rc
            review_counts.append(rc)

    # 如果没销量数字代表 SKU, 退回 review 最多的
    if rep_sku is None:
        members_sorted = sorted(members, key=lambda r: -(r.reviews.get("count") or 0))
        rep_sku = members_sorted[0].id.split(":", 1)[1]

    avg_rating = rating_x_count / total_count if total_count else None
    total_reviews = sum(review_counts)

    return {
        "n_skus": len(members),
        "rep_sku": rep_sku,
        "best_tier": best_tier,
        "max_sold_value": max_sold_value,
        "max_sold_text": sold_max_text,
        "any_rising": any_rising,
        "any_relevant": any_relevant,
        "color_set": sorted(color_set),
        "feature_union": sorted(feature_union),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_median": statistics.median(prices) if prices else None,
        "unit_price_min": min(unit_prices) if unit_prices else None,
        "unit_price_max": max(unit_prices) if unit_prices else None,
        "unit_price_median": statistics.median(unit_prices) if unit_prices else None,
        "avg_rating_weighted": round(avg_rating, 2) if avg_rating else None,
        "total_reviews": total_reviews,
    }


def apply_grouping(records: list[ProductRecord]) -> dict:
    """主入口. 写 group_id / group_member_skus 到所有 rec, group_aggregated 只到 root rec."""
    sku_to_rec = {rec.id.split(":", 1)[1]: rec for rec in records}
    groups = build_groups(records)

    n_groups_with_variants = 0
    n_singletons = 0
    largest = 0
    for root, members in groups.items():
        for sku in members:
            rec = sku_to_rec.get(sku)
            if not rec: continue
            rec.policy_flags["group_id"] = root
            rec.policy_flags["group_member_skus"] = members

        if len(members) == 1: n_singletons += 1
        else:
            n_groups_with_variants += 1
            largest = max(largest, len(members))

        # group_aggregated 只写到 root rec (避免重复存)
        agg = aggregate_group(records, members, sku_to_rec)
        root_rec = sku_to_rec.get(root)
        if root_rec:
            root_rec.policy_flags["group_aggregated"] = agg

    return {
        "n_records": len(records),
        "n_groups": len(groups),
        "n_groups_with_variants": n_groups_with_variants,
        "n_singletons": n_singletons,
        "largest_group_size": largest,
    }


if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, ".")
    from selection.shared import db
    with db.conn() as c:
        rows = c.execute("SELECT id FROM sel_products WHERE platform='noon_sa'").fetchall()
    recs = [db.get_product(r["id"]) for r in rows]
    recs = [r for r in recs if r]
    result = apply_grouping(recs)
    for rec in recs: db.upsert_product(rec)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # show top groups by size
    from collections import Counter
    sku_to_rec = {r.id.split(":", 1)[1]: r for r in recs}
    groups: dict = {}
    for r in recs:
        gid = r.policy_flags.get("group_id")
        if gid: groups.setdefault(gid, []).append(r)
    big = sorted(groups.items(), key=lambda x: -len(x[1]))[:5]
    print(f"\n--- 最大 5 个 group ---")
    for gid, members in big:
        agg = sku_to_rec[gid].policy_flags.get("group_aggregated", {})
        print(f"\n  group {gid[:14]} ({len(members)} SKUs):")
        for m in members[:8]:
            print(f"    - {(m.title or '')[:60]}")
        if agg:
            print(f"    rep: {agg.get('rep_sku')[:12] if agg.get('rep_sku') else None}, "
                  f"sold_max: {agg.get('max_sold_text')}, "
                  f"colors: {agg.get('color_set')}, "
                  f"reviews: {agg.get('total_reviews')}, "
                  f"price: {agg.get('price_min')}-{agg.get('price_max')}")

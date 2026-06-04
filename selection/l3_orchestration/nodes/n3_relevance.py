"""
N3.5 — 入选硬门槛: 标题相关性过滤 (通用算法, 不写死品类).

任何品类只要在 categories yaml 配 `inclusion_keywords` + `exclusion_keywords`
即生效:
  - title 含至少一个 inclusion_keyword (主题判定: 是不是该品类) AND
  - title 不含任何 exclusion_keyword (排除: 不是行李箱的旅行袋等)

§A 步骤 1 强调"前两页关注", 但前提是召回的真是"该品类". 关键词泛化召回错品
(luggage 泛化 'travel bag' → duffel) 是相关性问题, 必须前置过滤.

调用顺序: N3.5 (本节点) → N6 详情抽取 → N4 价格归一 → N5 综合打分.
入选门槛失败的商品后续节点不跑, 节省 LLM/Firecrawl 成本.
"""
from __future__ import annotations
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord
from selection.l2_knowledge.loader import KnowledgeBase, load as load_kb


def check_relevance(rec: ProductRecord, *, category: str,
                    kb: Optional[KnowledgeBase] = None) -> dict:
    """
    通用相关性检查. 返回 {passed, reason, incl_hit, excl_hit}.

    args:
      category: 'luggage' / 'chair' / 'stroller' / 自定义品类名 (对应 yaml 文件)

    检查逻辑:
      1. exclusion_keywords 命中 → drop (优先级高)
      2. inclusion_keywords 配置了但 title 一个都没命中 → drop
      3. 其他 → pass
    """
    kb = kb or load_kb()
    cat_params = kb.params.get(("categories", category), {})
    incl_kws: list[str] = cat_params.get("inclusion_keywords") or []
    excl_kws: list[str] = cat_params.get("exclusion_keywords") or []

    title_low = (rec.title or "").lower()
    brand_low = (rec.brand or "").lower()
    # group-level brand 检查: 同 group 任一 SKU 标题命中 brand_ban → 整 group 标
    # (修 row11 AT: 那条 SKU 标题没"AMERICAN TOURISTER", 但同 group 别的 SKU 有)
    group_titles_low = title_low
    gms = rec.policy_flags.get("group_member_skus") or []
    if len(gms) > 1:
        try:
            from selection.shared import db
            with db.conn() as c:
                for sku in gms:
                    pid = f"{rec.platform}:{sku}"
                    if pid == rec.id: continue
                    row = c.execute("SELECT title FROM sel_products WHERE id=?", (pid,)).fetchone()
                    if row and row["title"]:
                        group_titles_low += " || " + row["title"].lower()
        except Exception: pass

    # 0. global hard_ban (国际品牌等) — 优先级最高
    for rule in kb.hard_bans:
        for pat in rule.pattern:
            pat_low = pat.lower()
            if (pat_low in title_low or (brand_low and pat_low in brand_low)
                    or pat_low in group_titles_low):
                return {
                    "passed": False,
                    "reason": f"hard_ban [{rule.id}] '{pat}' 命中" + (
                        " (同 group)" if pat_low not in title_low and pat_low in group_titles_low else ""
                    ),
                    "excl_hit": None, "incl_hit": None,
                }

    # 1. exclusion 优先 (任一命中就 drop)
    excl_hit = next((k for k in excl_kws if k.lower() in title_low), None)
    if excl_hit:
        return {
            "passed": False,
            "reason": f"exclusion_keyword '{excl_hit}' 命中标题",
            "excl_hit": excl_hit, "incl_hit": None,
        }

    # 2. inclusion 至少一个 (如果 yaml 没配 inclusion, 默认 pass; 配了就硬要求)
    if incl_kws:
        incl_hit = next((k for k in incl_kws if k.lower() in title_low), None)
        if not incl_hit:
            return {
                "passed": False,
                "reason": f"标题不含任何 inclusion_keyword (要求至少 1 个 of {incl_kws[:5]})",
                "excl_hit": None, "incl_hit": None,
            }
    else:
        incl_hit = None

    # 2.5 search_query alignment — Luke 2026-05-12:
    # 标题必须跟当前 search_query 意图对齐 + 反向约束 (防 SEO stuffing).
    # e.g. 搜 'boss chair' 时:
    #   title_must_contain (任一命中): boss/executive/老板/...
    #   title_must_not_contain (任一命中 → drop): gaming/racing/electric (条件互斥的)
    align_rules = cat_params.get("search_query_alignment") or []
    sq = rec.policy_flags.get("search_query") or ""
    if align_rules and sq:
        for rule in align_rules:
            if rule.get("kw", "").lower() != sq.lower(): continue
            # 正向: 必须含任一
            must = rule.get("title_must_contain") or []
            if must:
                hit = next((m for m in must if m.lower() in title_low), None)
                if not hit:
                    return {
                        "passed": False,
                        "reason": f"alignment_fail: search='{sq}' 但标题不含任一 {must[:5]}",
                        "excl_hit": None, "incl_hit": incl_hit,
                        "search_query": sq,
                    }
            # 反向: 不能含任一 (干 SEO stuffing)
            must_not = rule.get("title_must_not_contain") or []
            if must_not:
                bad = next((m for m in must_not if m.lower() in title_low), None)
                if bad:
                    return {
                        "passed": False,
                        "reason": f"alignment_fail: search='{sq}' 标题含禁词 '{bad}' (SEO stuffing)",
                        "excl_hit": None, "incl_hit": incl_hit,
                        "search_query": sq,
                    }
            break

    # 3. brand marker — 不 drop, 只在 policy_flags 加标识 (Luke 反馈 2026-05-11)
    matched_brand = None
    for rule in kb.brand_markers:
        for pat in rule.pattern:
            pat_low = pat.lower()
            if pat_low in title_low or (brand_low and pat_low in brand_low) or pat_low in group_titles_low:
                matched_brand = pat
                break
        if matched_brand: break
    if matched_brand:
        rec.policy_flags["brand_marker"] = {
            "brand": matched_brand,
            "matched_via": "title" if matched_brand.lower() in title_low else "group_sibling",
            "rule_id": rule.id if matched_brand else None,
        }

    return {"passed": True, "reason": "ok", "incl_hit": incl_hit, "excl_hit": None}


def apply_relevance_filter(records: list[ProductRecord], *,
                           category: str,
                           kb: Optional[KnowledgeBase] = None) -> dict:
    """批量跑相关性过滤. mutate records.policy_flags.relevance_check, 返回统计."""
    kb = kb or load_kb()
    passed: list[ProductRecord] = []
    dropped: list[tuple[ProductRecord, str]] = []
    for rec in records:
        r = check_relevance(rec, category=category, kb=kb)
        rec.policy_flags["relevance_check"] = r
        if r["passed"]:
            passed.append(rec)
        else:
            dropped.append((rec, r["reason"]))
    return {
        "passed": passed,
        "dropped": dropped,
        "stats": {
            "in": len(records),
            "passed": len(passed),
            "dropped": len(dropped),
            "drop_rate": round(len(dropped) / max(1, len(records)), 3),
        },
    }


if __name__ == "__main__":
    import sqlite3, json, sys
    sys.path.insert(0, ".")
    from selection.shared import db
    db.init_db()

    # smoke: 跑 noon_sa 全量
    with db.conn() as c:
        rows = c.execute("SELECT id FROM sel_products WHERE platform='noon_sa'").fetchall()
    recs = []
    for r in rows:
        rec = db.get_product(r["id"])
        if rec: recs.append(rec)

    result = apply_relevance_filter(recs, category="luggage")
    print(f"in={result['stats']['in']}, "
          f"passed={result['stats']['passed']}, "
          f"dropped={result['stats']['dropped']} ({result['stats']['drop_rate']:.0%})")

    # 写回 db
    for rec in recs:
        db.upsert_product(rec)
    print("[ok] policy_flags.relevance_check written to db")

    # sample drops
    print("\n--- 前 10 个 drop 样本 ---")
    for rec, reason in result["dropped"][:10]:
        print(f"  [DROP] {rec.title[:60]}")
        print(f"         {reason}")

"""
N10 — 利润核算 + 黄牌机制.

§A 步骤 10 + §2 第 10 步 + §10:
  ≥20% → PROFIT_OK
  10-20% 且 (强差异化 OR 强稀缺 OR 强销量信号 OR 强审美命中) → PROFIT_LOW_BUT_VALUABLE (黄牌)
  <10% → PROFIT_KILL
  同时若 1688 同款最低采购+头程+VAT > noon 在售 70% → 极致性价比, 仅观察 (反卷)

通用算法 (从 pricing_table.yaml 读所有参数, 不写死):

  采购成本 (RMB) = 1688 unit_price × cost_estimate_factor   (yaml: cost_estimate_factor=0.9)
  汇率 RMB→SAR ≈ 0.52 (实证, yaml 可配)
  采购成本 (SAR) = 采购成本 RMB × 0.52

  头程运费 (SAR) = pricing_table.headforward_shipping.ksa.standard_under_2kg_sar 中位 (≈7.25)
                   是否 oversize 由 ProductRecord.skus[].is_oversize 判断 (没标默认 standard)

  仓储费 (SAR) = pricing_table.warehouse_fees.ksa.fbn_per_item_sar (=0.7151)

  推广 (SAR) = 推广系数 × 售价 (yaml: promo_rate=0.02 — 2% 估算)

  平台费用 = 售价 × commission_rate + 售价 × platform_vat_share_seller
          = 售价 × (0.15 + 0.039) = 售价 × 0.189   (KSA noon)

  净利 = 售价 - 采购成本 - 头程 - 仓储 - 推广 - 平台费用
  毛利率 = 净利 / 售价

  注: 本算法只算"列表展示价 × 0.9 采购估" 的粗利. 询盘后底价回填重算精确版.

匹配 1688 同款 (通用):
  对每个 noon 候选商品, 用 N6 detail_features 的 (材质, pack_size, 主推尺寸) 三元组
  在 1688 池里找最接近的 1-3 个, 取最低单价做"理论最低采购成本".
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord
from selection.l2_knowledge import loader as kb_loader


PROFIT_OK_THRESHOLD = 0.20      # ≥ 20% 绿牌
PROFIT_KILL_THRESHOLD = 0.10    # < 10% 红牌

# 反卷: 1688 同款最低采购 + 头程 + VAT > noon 在售 X% → 极致性价比
ANTI_RACE_RATIO = 0.70

# 默认 RMB→SAR 汇率 (实证 ~0.52, 可 yaml 覆盖)
DEFAULT_RMB_TO_SAR = 0.52


@dataclass
class CostBreakdown:
    purchase_rmb: float          # 1688 采购成本 RMB (unit_price × 0.9)
    purchase_sar: float          # 采购成本 SAR
    shipping_sar: float          # 头程
    warehouse_sar: float         # 仓储
    promo_sar: float             # 推广
    platform_fee_sar: float      # 佣金 + 平台 VAT 份额
    total_cost_sar: float        # 全部成本
    revenue_sar: float           # 售价 (单价 SAR)
    net_profit_sar: float        # 净利
    profit_rate: float           # 毛利率
    verdict: str                 # PROFIT_OK / PROFIT_LOW_BUT_VALUABLE / PROFIT_KILL


def _yaml_param(yaml_dict: dict, *keys, default=None):
    cur = yaml_dict
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur if cur is not None else default


def estimate_cost(unit_price_rmb: float, *, country: str = "ksa",
                  platform: str = "noon", is_oversize: bool = False,
                  pricing_table: Optional[dict] = None) -> dict:
    """
    通用算法估单 SKU 全成本. 任何参数从 pricing_table.yaml 读, 不写死.
    """
    if pricing_table is None:
        pricing_table = kb_loader.load_pricing_table()
    luggage_params = kb_loader.load().params.get(("categories", "luggage"), {}) or {}

    cost_factor = luggage_params.get("cost_estimate_factor", 0.9)   # §A tip 1 列表价×0.9
    rmb_to_sar = _yaml_param(pricing_table, "exchange_rates", "rmb_to_sar", default=DEFAULT_RMB_TO_SAR)

    purchase_rmb = unit_price_rmb * cost_factor
    purchase_sar = purchase_rmb * rmb_to_sar

    # 头程
    if is_oversize:
        ship_range = _yaml_param(pricing_table, "headforward_shipping", country, "oversize_under_2kg_sar", default=[8.5, 9])
    else:
        ship_range = _yaml_param(pricing_table, "headforward_shipping", country, "standard_under_2kg_sar", default=[6, 8.5])
    shipping_sar = sum(ship_range) / 2

    # 仓储
    warehouse_sar = _yaml_param(pricing_table, "warehouse_fees", country, "fbn_per_item_sar", default=0.7151)

    # 平台费率
    commission = _yaml_param(pricing_table, "platforms", platform, "default_commission_rate", default=0.15)
    vat_share = _yaml_param(pricing_table, "countries", country, "platform_vat_share_seller", default=0.039)

    # 推广 (yaml 没就用 2% 默认)
    promo_rate = _yaml_param(pricing_table, "promo_rate", default=0.02)

    return {
        "purchase_rmb": round(purchase_rmb, 2),
        "purchase_sar": round(purchase_sar, 2),
        "shipping_sar": round(shipping_sar, 2),
        "warehouse_sar": round(warehouse_sar, 2),
        "promo_rate": promo_rate,
        "commission": commission,
        "platform_vat_share": vat_share,
        "_rmb_to_sar": rmb_to_sar,
    }


def calculate_profit(revenue_sar: float, unit_price_rmb: float, *,
                     country: str = "ksa", platform: str = "noon",
                     is_oversize: bool = False,
                     pricing_table: Optional[dict] = None) -> CostBreakdown:
    """全成本核算 + 黄牌判定."""
    c = estimate_cost(unit_price_rmb, country=country, platform=platform,
                      is_oversize=is_oversize, pricing_table=pricing_table)
    promo_sar = revenue_sar * c["promo_rate"]
    platform_fee_sar = revenue_sar * (c["commission"] + c["platform_vat_share"])
    total = c["purchase_sar"] + c["shipping_sar"] + c["warehouse_sar"] + promo_sar + platform_fee_sar
    net = revenue_sar - total
    rate = net / revenue_sar if revenue_sar > 0 else 0

    # 三档判定
    if rate >= PROFIT_OK_THRESHOLD: verdict = "PROFIT_OK"
    elif rate >= PROFIT_KILL_THRESHOLD: verdict = "PROFIT_LOW_BUT_VALUABLE"   # 黄牌, 上层补"为什么没扔"
    else: verdict = "PROFIT_KILL"

    return CostBreakdown(
        purchase_rmb=c["purchase_rmb"], purchase_sar=c["purchase_sar"],
        shipping_sar=c["shipping_sar"], warehouse_sar=c["warehouse_sar"],
        promo_sar=round(promo_sar, 2), platform_fee_sar=round(platform_fee_sar, 2),
        total_cost_sar=round(total, 2), revenue_sar=revenue_sar,
        net_profit_sar=round(net, 2), profit_rate=round(rate, 3),
        verdict=verdict,
    )


# ── 1688 同款匹配 (通用) ──────────────────────────────────────

def _normalize_size(text: str) -> set[int]:
    """通用尺寸抽取. 支持英中:
       '14/20/24/28 inch' → {14,20,24,28}
       '20 寸' / '20寸' / '24-inch' / 'L (28 inch)' → 命中.
       仅保留 12-32 范围 (luggage 常见尺寸)."""
    if not text: return set()
    import re
    out = set()
    # 英文: 数字 + (空格)? + (inch|") 或 数字 + (-)? + (piece|pcs)
    for m in re.finditer(r'(\d{2})\s*(?:inch|"|in\b|-inch|寸)', text, re.IGNORECASE):
        n = int(m.group(1))
        if 12 <= n <= 32: out.add(n)
    # 备份: 直接 digit, 但要求是 luggage 范围
    for m in re.finditer(r'(?<![/\d])(\d{2})(?![/\d])', text):
        n = int(m.group(1))
        if 12 <= n <= 32: out.add(n)
    # 兜底: 用 / 分隔的 set "14/20/24/28"
    for m in re.finditer(r'(\d{2})(?:/\d{2})+', text):
        for nm in re.finditer(r'\d{2}', m.group(0)):
            n = int(nm.group())
            if 12 <= n <= 32: out.add(n)
    return out


def _get_target_sku_price(detail: dict, target_sizes: set[int],
                          target_pack: int = 1) -> Optional[dict]:
    """
    从 1688 详情 API 的 productSkuInfos[] 里挑跟 noon 目标尺寸匹配的 SKU 价格.

    Luke 反馈 #B/C:
      - 单只 noon 24 寸 → 在 1688 SKU 里找尺寸 = 24 的那个 SKU 价格
      - set noon 3-piece (20/24/28) → 1688 找全 3 个尺寸的 SKU 价加和

    返回 {match_sku_price, match_sku_label, sku_count_matched, total_skus, all_matched}
    """
    skus = detail.get("productSkuInfos") or []
    if not skus: return None

    import re
    # Luke 2026-05-09 反馈: 1688 商家把"加购赠品"伪装成 SKU 价 ¥29 (实际真单价 ¥150)
    # 例: "颜色:14寸行李箱 / 尺寸:28寸" 这种"颜色"字段含尺寸/件套关键词的 = 异常 SKU 跳过
    SKU_ANOMALY_KW = ["寸行李箱", "寸登机", "件套", "三件套", "套装", "套餐",
                      "升级款", "送", "赠", "包邮", "特价", "活动价", "捆绑",
                      "加购", "组合", "搭配"]

    matched = []  # [(price, size, label)]
    for sku in skus:
        # SKU 里 attributeName='尺寸' / 'Size'
        size_val = None
        full_label = []
        is_anomaly = False
        for attr in sku.get("skuAttributes") or []:
            name = attr.get("attributeName") or ""
            val = attr.get("value") or ""
            full_label.append(f"{name}:{val}")
            # "颜色" 字段含尺寸/件套/赠送等 → 异常 SKU
            if name == "颜色" or "color" in name.lower():
                if any(kw in val for kw in SKU_ANOMALY_KW):
                    is_anomaly = True
            if "尺寸" in name or "size" in name.lower():
                m = re.search(r'(\d{2})', val)
                if m:
                    n = int(m.group(1))
                    if 12 <= n <= 32: size_val = n
        if is_anomaly: continue    # 跳过异常 SKU (赠品/加购/特价款)
        if size_val and size_val in target_sizes:
            try:
                price_raw = sku.get("price")
                if price_raw is None: continue   # 询盘价 SKU, 跳过
                p = float(price_raw)
                if p <= 0: continue              # 0 价 SKU, 跳过
                matched.append((p, size_val, " / ".join(full_label)))
            except (ValueError, TypeError): pass

    if not matched:
        # 没找到目标尺寸 SKU — 询盘类或全 0 价 SKU
        prices = []
        for s in skus:
            p = s.get("price")
            if p is None: continue
            try:
                pv = float(p)
                if pv > 0: prices.append(pv)
            except (ValueError, TypeError): pass
        if not prices: return None    # 全询盘价, 算无价
        return {
            "match_sku_price": min(prices),
            "match_sku_label": "尺寸不匹配, 取最低 SKU",
            "sku_count_matched": 0,
            "total_skus": len(skus),
            "all_matched": False,
            "is_fallback_min": True,
        }

    # Luke 2026-05-09 反馈: 同尺寸下不能取最低价 (会抽到赠品/特价款), 取 median 排异常
    import statistics
    if target_pack == 1:
        # 单只: 同尺寸所有色款的中位数 (排异常低/高价款)
        prices = [p for p, _, _ in matched]
        med = statistics.median(prices)
        # 选最接近 median 的那个 SKU 作为代表
        rep = min(matched, key=lambda x: abs(x[0] - med))
        return {
            "match_sku_price": med,
            "match_sku_label": f"{rep[2]} (中位数, 共{len(matched)}款)",
            "sku_count_matched": 1,
            "total_skus": len(skus),
            "all_matched": True,
            "is_fallback_min": False,
        }
    else:
        # set: 每个目标尺寸取该尺寸下中位数 SKU, 求和
        by_size_prices: dict[int, list] = {}
        for p, sz, _ in matched:
            by_size_prices.setdefault(sz, []).append(p)
        by_size = {sz: statistics.median(ps) for sz, ps in by_size_prices.items()}
        all_sizes_matched = set(by_size.keys()) == target_sizes
        total = sum(by_size.values())
        return {
            "match_sku_price": total,
            "match_sku_label": "; ".join(f"{sz}寸 ¥{p}" for sz, p in by_size.items()),
            "sku_count_matched": len(by_size),
            "total_skus": len(skus),
            "all_matched": all_sizes_matched,
            "is_fallback_min": False,
            "size_breakdown": by_size,
        }


def find_1688_matches(noon_rec: ProductRecord, ali_recs: list[ProductRecord],
                      max_results: int = 3, min_score: float = 0.7) -> list[tuple[ProductRecord, float]]:
    """
    通用匹配算法: 用 N6.5 detail_features (材质 + size + pack_size) 三元组找最近的.
    返回 (1688 rec, 相似度 0-1) 列表, 取相似度最高的 max_results 个.

    打分:
      pack 同 = 必要条件 (≠ 跳过)
      pack 同基础分 0.3 + 材质 0.3 + size overlap (按比例 × 0.4)
      若 pack 同但 noon 没 size 信息: 给 0.4 (材质同就 0.6, 不同 0.3)
    """
    n_df = noon_rec.policy_flags.get("detail_features") or {}
    n_n6 = noon_rec.policy_flags.get("n6_extracted") or {}
    noon_material = (n_df.get("material") or n_n6.get("material") or "").lower()
    noon_sizes_text = (n_df.get("size") or "") + " " + " ".join(str(s)+"inch" for s in (n_n6.get("size_inches") or []))
    noon_sizes = _normalize_size(noon_sizes_text + " " + (noon_rec.title or ""))
    noon_pack = noon_rec.policy_flags.get("pack_size") or 1

    matches = []
    for ali in ali_recs:
        if not ali.policy_flags.get("relevance_check", {}).get("passed"):
            continue
        ali_pack = ali.policy_flags.get("pack_size") or 1
        if ali_pack != noon_pack:
            continue   # pack 不同直接跳过

        score = 0.3   # pack 同基础分

        # 材质匹配 (1688 中文标题: ABS/PC/铝框/铝合金/铝镁; 软箱关键词)
        ali_title_low = (ali.title or "").lower()
        if noon_material:
            for kw_pair in [
                ("abs", ["abs"]),
                ("pc", ["pc", "聚碳"]),
                ("铝", ["铝框", "铝合金", "铝镁", "全铝"]),
                ("软", ["软箱", "牛津", "帆布"]),
            ]:
                tag, ali_kws = kw_pair
                if tag in noon_material or noon_material in tag:
                    if any(k in ali_title_low for k in ali_kws):
                        score += 0.3; break

        # size 匹配
        ali_sizes = _normalize_size(ali.title or "")
        if noon_sizes and ali_sizes:
            overlap = noon_sizes & ali_sizes
            if overlap:
                score += 0.4 * (len(overlap) / max(len(noon_sizes), len(ali_sizes)))
        elif not noon_sizes:
            # noon 没 size 信息: 不扣分 (给个中等)
            score += 0.2

        if score >= min_score:
            matches.append((ali, score))

    matches.sort(key=lambda x: -x[1])
    return matches[:max_results]


# ── 主入口 ────────────────────────────────────────────────────

def apply_profit(noon_records: list[ProductRecord],
                 ali_records: list[ProductRecord],
                 country: str = "ksa",
                 use_detail_api: bool = True,
                 min_match_score: float = 0.7) -> dict:
    """
    给每个 noon 候选商品配 1688 匹配, 算利润, 写到 policy_flags.profit.

    Luke 反馈修复 (2026-05-08):
      A 同款选错: min_match_score 提到 0.7 (从 0.5)
      B 价格取错: use_detail_api=True 时, 拿目标尺寸 SKU 价格 (不是 listing 起步价)
      C set 价错: detail API 返回 SKU 阶梯, set 取目标尺寸求和
      D 品牌品: yaml hard_ban 已加, 走 N3.5 自动 drop
    """
    pt = kb_loader.load_pricing_table()
    n_ok = n_yellow = n_kill = n_no_match = 0
    n_anti_race = 0
    breakdown_list = []

    # 缓存详情 API 结果 (同 1688 商品可能匹配多个 noon)
    detail_cache: dict[str, dict] = {}

    def get_detail(offer_id: str):
        from selection.l0_data.api_clients.alibaba_official import query_product_detail

        if offer_id in detail_cache: return detail_cache[offer_id]
        try: d = query_product_detail(offer_id)
        except Exception: d = None
        detail_cache[offer_id] = d
        return d

    for rec in noon_records:
        rev_total = rec.price.get("value")
        unit_rev = rec.policy_flags.get("unit_price")
        revenue = float(unit_rev) if unit_rev else float(rev_total or 0)
        if revenue <= 0:
            continue
        # 是否加大版 (任一 SKU 标 is_oversize)
        is_oversize = any(s.is_oversize for s in rec.skus)

        # 找 1688 同款 (二次过滤, score >= 0.7)
        matches = find_1688_matches(rec, ali_records, min_score=min_match_score)
        if not matches:
            rec.policy_flags["profit"] = {"verdict": "NO_1688_MATCH",
                "reason": f"1688 同款相似度 < {min_match_score} (款式/材质/尺寸)"}
            n_no_match += 1
            continue

        # noon 目标尺寸 (从 detail_features 抽)
        n_df = rec.policy_flags.get("detail_features") or {}
        n_n6 = rec.policy_flags.get("n6_extracted") or {}
        target_sizes_text = (n_df.get("size") or "") + " " + " ".join(str(s)+"inch" for s in (n_n6.get("size_inches") or []))
        target_sizes = _normalize_size(target_sizes_text + " " + (rec.title or ""))
        target_pack = rec.policy_flags.get("pack_size") or 1

        # 对每个 match 拉详情 API 算"目标尺寸真实价格", 取最低成本胜出
        best_ali = None; best_unit_rmb = float("inf"); best_match_info = None
        for ali_rec, sim in matches:
            ali_offer_id = ali_rec.id.split(":", 1)[1]
            unit_rmb_listing = ali_rec.policy_flags.get("unit_price") or 0

            if use_detail_api and target_sizes:
                detail = get_detail(ali_offer_id)
                if detail and not detail.get("error"):
                    sku_match = _get_target_sku_price(detail, target_sizes, target_pack)
                    if sku_match and sku_match.get("match_sku_price", 0) > 0 and sku_match.get("all_matched"):
                        # set: 全尺寸匹配 + 价格 > 0 → 真实可信
                        true_price = sku_match["match_sku_price"]
                        true_unit_rmb = true_price if target_pack == 1 else true_price / target_pack
                        if true_unit_rmb < best_unit_rmb:
                            best_unit_rmb = true_unit_rmb
                            best_ali = (ali_rec, sim)
                            best_match_info = sku_match
                        continue
                    # 部分匹配的 set / 价格 0: 不算可信, 跳到 listing fallback

            # 没详情或目标尺寸抽不到, 走 listing 价 fallback —
            # 但 set (target_pack > 1) 不允许 listing 起步价 fallback
            # (因为 listing 是 SKU 起步价, set 用单只起步价 ×N 严重低估实际成本)
            if (target_pack == 1 and unit_rmb_listing > 0
                    and unit_rmb_listing < best_unit_rmb):
                best_unit_rmb = unit_rmb_listing
                best_ali = (ali_rec, sim)
                best_match_info = {"match_sku_label": "listing 起步价 (无详情)",
                                   "is_fallback_min": True, "all_matched": False}

        if best_ali is None or best_unit_rmb == float("inf"):
            rec.policy_flags["profit"] = {"verdict": "NO_1688_PRICE", "reason": "1688 匹配但无可用价格"}
            n_no_match += 1
            continue
        ali_rec, sim = best_ali
        ali_unit_rmb = best_unit_rmb

        cb = calculate_profit(revenue, ali_unit_rmb,
                              country=country, platform="noon",
                              is_oversize=is_oversize, pricing_table=pt)

        # 反卷检查: 1688 最低成本 / noon 售价 比例
        cost_ratio = cb.total_cost_sar / revenue
        anti_race_violation = cost_ratio > ANTI_RACE_RATIO

        rec.policy_flags["profit"] = {
            "verdict": cb.verdict,
            "profit_rate": cb.profit_rate,
            "net_profit_sar": cb.net_profit_sar,
            "revenue_sar": cb.revenue_sar,
            "purchase_rmb": cb.purchase_rmb,
            "purchase_sar": cb.purchase_sar,
            "shipping_sar": cb.shipping_sar,
            "warehouse_sar": cb.warehouse_sar,
            "promo_sar": cb.promo_sar,
            "platform_fee_sar": cb.platform_fee_sar,
            "total_cost_sar": cb.total_cost_sar,
            "matched_1688_offer_id": ali_rec.id.split(":", 1)[1],
            "matched_1688_unit_rmb": ali_unit_rmb,
            "matched_1688_title": (ali_rec.title or "")[:100],
            "matched_1688_shop": ali_rec.policy_flags.get("shop_name", ""),
            "matched_1688_url": f"https://detail.1688.com/offer/{ali_rec.id.split(':',1)[1]}.html",
            "matched_1688_image": (ali_rec.images[0] if ali_rec.images else None),
            "sku_match_label": best_match_info.get("match_sku_label") if best_match_info else None,
            "sku_all_matched": best_match_info.get("all_matched") if best_match_info else None,
            "sku_is_listing_min": best_match_info.get("is_fallback_min") if best_match_info else None,
            "match_similarity": round(sim, 2),
            "n_1688_candidates": len(matches),
            "anti_race": anti_race_violation,
            "cost_to_revenue_ratio": round(cost_ratio, 3),
        }
        if cb.verdict == "PROFIT_OK": n_ok += 1
        elif cb.verdict == "PROFIT_LOW_BUT_VALUABLE": n_yellow += 1
        else: n_kill += 1
        if anti_race_violation: n_anti_race += 1
        breakdown_list.append((rec, cb))

    return {
        "n_input": len(noon_records),
        "n_ok": n_ok,
        "n_yellow": n_yellow,
        "n_kill": n_kill,
        "n_no_match": n_no_match,
        "n_anti_race": n_anti_race,
    }


if __name__ == "__main__":
    import sys; sys.path.insert(0, ".")
    from selection.shared import db
    with db.conn() as c:
        n_rows = c.execute("""SELECT id FROM sel_products WHERE platform='noon_sa'
            AND json_extract(policy_flags_json,'$.relevance_check.passed')=1""").fetchall()
        a_rows = c.execute("""SELECT id FROM sel_products WHERE platform='alibaba_1688'
            AND json_extract(policy_flags_json,'$.relevance_check.passed')=1""").fetchall()
    noon = [db.get_product(r["id"]) for r in n_rows]
    ali  = [db.get_product(r["id"]) for r in a_rows]
    noon = [r for r in noon if r]; ali = [r for r in ali if r]
    print(f"noon (相关): {len(noon)}, 1688 (相关): {len(ali)}")
    res = apply_profit(noon, ali)
    for r in noon: db.upsert_product(r)
    print(f"\n{res}")

    # sample top 10
    print("\n--- 利润核算结果 ---")
    rows = sorted([(r, r.policy_flags.get("profit", {})) for r in noon if r.policy_flags.get("profit")],
                  key=lambda x: -(x[1].get("profit_rate") or 0))
    for r, p in rows[:10]:
        v = p.get("verdict")
        rate = p.get("profit_rate", 0)
        print(f"  {v:25s} rate={rate*100:5.1f}% | rev={p.get('revenue_sar')} 单价 / {r.title[:50]}")
        if v not in ("NO_1688_MATCH", "NO_1688_PRICE"):
            print(f"    purchase={p.get('purchase_sar')} 头程={p.get('shipping_sar')} 仓储={p.get('warehouse_sar')} 推广={p.get('promo_sar')} 平台={p.get('platform_fee_sar')}")
            print(f"    1688 匹配: {p.get('matched_1688_title','')[:60]} ¥{p.get('matched_1688_unit_rmb')} (相似 {p.get('match_similarity')})")

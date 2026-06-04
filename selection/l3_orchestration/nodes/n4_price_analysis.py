"""
N4 — 价格分桶 + 三重价格检查.

§A 步骤 3 + §6:
  1. 按 SKU 类型分桶 (一件装 / 多件装 / 材质轴 / 容量轴)
  2. 自家店铺价段对照 (hipop_adapter.get_self_price_band) — 当前价段是不是覆盖买家承受度
  3. noon 半托管价段差 1.5× 检查 (yaml params.half_managed_price_band_max_ratio)
"""
from __future__ import annotations
import re
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord
from selection.l2_knowledge import loader as kb_loader
from selection.l2_knowledge import hipop_adapter


# pack_size 抽取 — 通用算法支持中英双语 (§A 步骤 3 一件装 vs 多件装)
# 中文 "三/四/五/六/七 件(套)" + "X 件套"
CHINESE_NUM = {"两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

PACK_SIZE_RE = [
    # 英文: "4-piece" / "4 pieces" / "3 pcs" / "set of 4" / "4 pack" / "4-piece set"
    (re.compile(r"\b(\d+)[\s\-]?p(?:iece|cs|c)s?\b", re.I), 1, "digit"),
    (re.compile(r"set of (\d+)", re.I), 1, "digit"),
    (re.compile(r"(\d+)\s*pack\b", re.I), 1, "digit"),
    (re.compile(r"\b(\d+)\s*pieces?\s*set\b", re.I), 1, "digit"),
    # 中文: "三件套" / "三件" / "三-piece"
    (re.compile(r"([两三四五六七八九十])件套?"), 1, "chinese"),
    (re.compile(r"(\d+)\s*件套"), 1, "digit"),
    (re.compile(r"(\d+)\s*件\b"), 1, "digit"),
]


def extract_pack_size(rec: ProductRecord) -> int:
    """从标题抽 pack_size. 通用算法, 支持中英文. 没匹配返回 1."""
    title = rec.title or ""
    title_low = title.lower()
    for pat, group, kind in PACK_SIZE_RE:
        m = pat.search(title)
        if m:
            try:
                if kind == "digit":
                    n = int(m.group(group))
                    if 1 <= n <= 10: return n
                else:   # chinese
                    return CHINESE_NUM.get(m.group(group), 1)
            except ValueError: pass
    # set 字眼但没数字: 默认 3
    if re.search(r"\bset\b", title_low) and "set of" not in title_low:
        return 3
    # 中文 "套装" 默认 3
    if "套装" in title and not any(k in title for k in ["两件套","三件套","四件套","五件套"]):
        return 3
    return 1


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    n = len(s)
    return {
        "n": n,
        "min": s[0],
        "p25": s[n // 4],
        "median": s[n // 2],
        "p75": s[3 * n // 4],
        "max": s[-1],
    }


def analyze(records: list[ProductRecord],
            country: str = "ksa",
            family: str = "bags_luggage") -> dict:
    """
    跑 N4. 同时 mutate records.policy_flags 加 pack_size + 价格 flags.

    Returns:
      {
        per_pack_size: {1: {n,min,...}, 2: {...}, ...},
        self_band: hipop_adapter.get_self_price_band 结果,
        too_high_vs_self: list[rec_id]    # 超过自家 p75 × 2 的, 超买家承受度
        too_low_vs_self:  list[rec_id]    # 低于自家 p25 / 2 的, 价格战
        half_managed_violations: list[rec_id],  # noon 1.5× 阈值
        stats: dict,
      }
    """
    kb = kb_loader.load()
    half_managed_max_ratio = kb.params.get(
        ("countries", "noon"), {}
    ).get("half_managed_price_band_max_ratio", 1.5)

    # 1. 给每条 record 算 pack_size + 单价 (通用归一: unit_price = price / pack_size, 四舍五入到整数)
    by_pack: dict[int, list[float]] = {}
    unit_prices: list[float] = []
    for rec in records:
        ps = extract_pack_size(rec)
        rec.policy_flags["pack_size"] = ps
        v = rec.price.get("value")
        if v and v > 0:
            by_pack.setdefault(ps, []).append(v)
            unit_p = round(v / max(ps, 1))   # 整数 SAR
            rec.policy_flags["unit_price"] = unit_p
            rec.policy_flags["unit_price_raw"] = v / max(ps, 1)
            unit_prices.append(unit_p)

    per_pack_size = {ps: percentiles(prices) for ps, prices in by_pack.items()}
    # 单价分布 (跨所有 pack_size, 这是用户实际感知价段)
    per_unit_price = percentiles(unit_prices)

    # 2. 自家店铺价段 — 用单价 unit_price (按 pack_size 归一) 比, 不用总价
    # Luke 反馈 #3: luggage set 4 件 749 SAR / 4 = 187 SAR 单价, 中高但不是 too_high
    self_band = hipop_adapter.get_self_price_band(country, family)
    too_high_vs_self: list[str] = []
    too_low_vs_self: list[str] = []
    if self_band.get("n_skus", 0) > 0:
        upper = self_band["p75"] * 2.0
        lower = self_band["p25"] / 2.0
        for rec in records:
            up = rec.policy_flags.get("unit_price")
            if not up: continue
            if up > upper:
                too_high_vs_self.append(rec.id)
                rec.policy_flags["price_vs_self"] = f"too_high (>{upper:.0f})"
            elif up < lower:
                too_low_vs_self.append(rec.id)
                rec.policy_flags["price_vs_self"] = f"too_low (<{lower:.0f})"
            else:
                rec.policy_flags["price_vs_self"] = "in_band"

    # 3. noon 半托管 1.5× 检查 (同 pack_size 内)
    half_managed_violations: list[str] = []
    for ps, prices in by_pack.items():
        if not prices: continue
        s = sorted(prices)
        # 看价格相邻间隔 > 1.5×
        for i in range(1, len(s)):
            ratio = s[i] / s[i-1] if s[i-1] > 0 else 0
            if ratio > half_managed_max_ratio:
                # 超阈值 — 找哪些 SKU 跨了
                cutoff = s[i]
                for rec in records:
                    v = rec.price.get("value")
                    if not v or rec.policy_flags.get("pack_size") != ps:
                        continue
                    if v >= cutoff:
                        # 跟前一档比
                        if not rec.policy_flags.get("half_managed_violation"):
                            half_managed_violations.append(rec.id)
                            rec.policy_flags["half_managed_violation"] = (
                                f"pack_size={ps} 价 {v:.0f} 跨同档前价 {s[i-1]:.0f} "
                                f">{half_managed_max_ratio}×"
                            )
                break  # 每个 pack 只标第一档跨度

    return {
        "per_pack_size": per_pack_size,
        "per_unit_price": per_unit_price,    # 跨 pack 的"单价"用户实际感知价段
        "self_band": self_band,
        "too_high_vs_self": too_high_vs_self,
        "too_low_vs_self": too_low_vs_self,
        "half_managed_violations": half_managed_violations,
        "stats": {
            "n": len(records),
            "n_with_price": sum(1 for r in records if r.price.get("value")),
            "n_with_unit_price": len(unit_prices),
            "pack_size_dist": {ps: len(p) for ps, p in by_pack.items()},
            "n_too_high": len(too_high_vs_self),
            "n_too_low": len(too_low_vs_self),
            "n_half_managed_violation": len(half_managed_violations),
        },
    }

"""
N1 — 类目识别 + 流量词扩展.

§A 步骤 1 + 6 + §6: 用单个种子词搜 noon 召回偏 set, 用扩展后多关键词
并行抓 + 去重, 才能拿到完整品类视图. 实证 (data_inventory):
  'luggage'         → 96% set
  'carry on luggage'→ 100% 单只 + 销量数字 4× 提升

来源:
  1. 种子词本身
  2. categories/<seed>.yaml 的 search_keywords (人工预填)
  3. 后期 (PoC v2): LLM 跨语言扩展 (中英阿)
"""
from __future__ import annotations
from typing import Optional

from selection.l2_knowledge import loader as kb_loader


def expand(seed: str, category: Optional[str] = None) -> list[str]:
    """
    种子词 → 多关键词列表.

    args:
      seed: 'luggage' / 'chair' / 自由词
      category: yaml category 名 (luggage/chair/stroller). 不给就猜跟 seed 同名

    returns:
      去重后的关键词列表, 第一个是种子词
    """
    out = [seed]
    cat_name = (category or seed).lower()

    kb = kb_loader.load()
    extras = kb.params.get(("categories", cat_name), {}).get("search_keywords") or []
    for kw in extras:
        if kw and kw not in out:
            out.append(kw)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--category", default=None)
    args = ap.parse_args()
    kws = expand(args.seed, args.category)
    print(f"seed='{args.seed}' category='{args.category}' →")
    for kw in kws:
        print(f"  - {kw}")

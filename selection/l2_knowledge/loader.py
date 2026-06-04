"""
读 constraint_db/*.yaml + pricing_table.yaml, 给 N3 / N10 / N11 节点用.

模式:
  hard_ban         → N3 命中 drop
  hard_filter      → N3 列表级过滤 (例如 Amazon Sponsored)
  brand_mindshare  → N3 标记不淘汰
  soft_preference  → N11 LLM rerank 加权
  meta_template    → 给 LLM 当 prompt 参考, 不直接命中
"""
from __future__ import annotations
import os, glob
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Rule:
    id: str
    mode: str
    scope: list[str]
    pattern: list[str]
    pattern_groups: dict       # {pattern: {weight, note}}
    pattern_type: str
    reason: str
    weight: float
    note: str
    domain: str
    domain_name: str
    promoted_at: Optional[str]
    promoted_by: Optional[str]
    triggers_return_risk: Optional[str]   # 'high' / 'mid' / 'low'


@dataclass
class KnowledgeBase:
    hard_bans: list[Rule] = field(default_factory=list)
    hard_filters: list[Rule] = field(default_factory=list)
    soft_preferences: list[Rule] = field(default_factory=list)
    meta_templates: list[Rule] = field(default_factory=list)
    brand_mindshare: list[Rule] = field(default_factory=list)
    brand_markers: list[Rule] = field(default_factory=list)   # 国际品牌等, 不 drop 只标
    params: dict = field(default_factory=dict)         # {(domain, name): {param: val}}
    strict_platforms: list[str] = field(default_factory=list)


CONSTRAINT_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "constraint_db",
)


def _load_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("缺 PyYAML: pip install PyYAML")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load(root: Optional[str] = None) -> KnowledgeBase:
    root = root or CONSTRAINT_ROOT
    kb = KnowledgeBase()

    for path in sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True)):
        doc = _load_yaml(path)
        if not isinstance(doc, dict):
            continue
        domain = doc.get("domain", "")
        name = doc.get("name", "")
        if doc.get("params"):
            kb.params[(domain, name)] = doc["params"]
        # 顶层 list 字段统一归并到 params (给 N1/N3.5/N6 等节点读)
        for top_field in ("search_keywords", "inclusion_keywords", "exclusion_keywords",
                          "n6_extract_schema", "search_query_alignment"):
            if doc.get(top_field):
                kb.params.setdefault((domain, name), {})[top_field] = doc[top_field]
        if doc.get("strict_platforms"):
            kb.strict_platforms.extend(doc["strict_platforms"])

        for r in doc.get("rules") or []:
            try:
                rule = Rule(
                    id=r["id"],
                    mode=r["mode"],
                    scope=r.get("scope", []),
                    pattern=r.get("pattern", []),
                    pattern_groups=r.get("pattern_groups", {}),
                    pattern_type=r.get("pattern_type", "contains"),
                    reason=r.get("reason", ""),
                    weight=float(r.get("weight", 0.0)),
                    note=r.get("note", ""),
                    domain=domain, domain_name=name,
                    promoted_at=r.get("promoted_at"),
                    promoted_by=r.get("promoted_by"),
                    triggers_return_risk=r.get("triggers_return_risk"),
                )
            except KeyError as e:
                print(f"[kb] {path} skip malformed rule: missing {e}")
                continue

            # 安全降级: hard_ban 必须有 promoted_at + promoted_by
            if rule.mode == "hard_ban" and not (rule.promoted_at and rule.promoted_by):
                print(f"[kb] {rule.id}: hard_ban 缺 promoted_*, 降级 soft_preference")
                rule.mode = "soft_preference"
                if rule.weight == 0:
                    rule.weight = -0.5

            bucket = {
                "hard_ban": kb.hard_bans,
                "hard_filter": kb.hard_filters,
                "soft_preference": kb.soft_preferences,
                "meta_template": kb.meta_templates,
                "brand_mindshare": kb.brand_mindshare,
                "brand_marker": kb.brand_markers,
            }.get(rule.mode)
            if bucket is None:
                print(f"[kb] {rule.id}: unknown mode {rule.mode!r}, skip")
                continue
            bucket.append(rule)

    return kb


def get_param(kb: KnowledgeBase, domain: str, name: str, key: str, default=None):
    return kb.params.get((domain, name), {}).get(key, default)


# ── pricing_table 载入 ────────────────────────────────────

PRICING_TABLE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pricing_table.yaml"
)


def load_pricing_table(path: Optional[str] = None) -> dict:
    return _load_yaml(path or PRICING_TABLE_PATH)


if __name__ == "__main__":
    kb = load()
    print(f"hard_bans       : {len(kb.hard_bans)}")
    for r in kb.hard_bans:
        print(f"  - [{r.domain}/{r.domain_name}] {r.id}: {r.pattern}")
    print(f"hard_filters    : {len(kb.hard_filters)}")
    print(f"brand_mindshare : {len(kb.brand_mindshare)}")
    print(f"soft_preferences: {len(kb.soft_preferences)}")
    for r in kb.soft_preferences:
        groups = list(r.pattern_groups.keys()) if r.pattern_groups else r.pattern
        print(f"  - [{r.domain}/{r.domain_name}] {r.id}: {groups}")
    print(f"meta_templates  : {len(kb.meta_templates)}")
    print(f"params keys     : {list(kb.params.keys())}")
    print(f"strict_platforms: {kb.strict_platforms}")
    print(f"\n--- pricing_table ---")
    pt = load_pricing_table()
    print(f"  countries: {list(pt.get('countries', {}).keys())}")
    print(f"  platforms: {list(pt.get('platforms', {}).keys())}")

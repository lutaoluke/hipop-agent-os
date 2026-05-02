"""
选品 Agent 内省式归纳：
  1. 读 wf2_hipop_*_sku 全量
  2. 按 sales_grade + profit_rate + return_rate 打成败标签
  3. 调 Claude 跑两次归纳（成功品共同点 / 失败品共同点）
  4. 输出到 hipop/agent_memory/strategies/选品_成功模式_v1.md / 选品_失败模式_v1.md

Usage:
  python3 -m hipop.scripts.selection_introspect
  python3 hipop/scripts/selection_introspect.py
"""
import os, sys, json, sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
HIPOP_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, os.path.dirname(HIPOP_ROOT))

DB = os.environ.get("HIPOP_DB", "/Users/luke/Downloads/点购工作流/hipop.db")
OUT_DIR = os.path.join(HIPOP_ROOT, "agent_memory", "strategies")
os.makedirs(OUT_DIR, exist_ok=True)


def load_skus():
    """跨店读所有 SKU 数据"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = []
    for store in ("ksa", "uae"):
        rs = conn.execute(f"""
            SELECT partner_sku, title, family, product_type, brand,
                   sales_grade, sales_30d, sales_60d, sales_180d,
                   latest_price, cost_price, latest_profit_rate,
                   return_rate, cancel_rate, is_listed,
                   '{store.upper()}' AS store
            FROM wf2_hipop_{store}_sku
            WHERE is_listed=1
        """).fetchall()
        rows.extend(dict(r) for r in rs)
    conn.close()
    return rows


def label(rows):
    """成功 / 失败标签"""
    success = []
    fail = []
    for r in rows:
        sales_grade = (r.get("sales_grade") or "").strip()
        pr = r.get("latest_profit_rate") or 0
        rr = r.get("return_rate") or 0
        sales_180 = r.get("sales_180d") or 0
        # 成功定义：sales_grade in {SS,A} (热销/稳销) + 利润 > 15% + 退货 < 8%
        if sales_grade in ("SS", "S", "A") and pr > 0.15 and rr < 0.08:
            success.append(r)
        # 失败定义：sales_grade in {C,D,E} 或者 利润 < 5% 或者 退货 > 15% + 销售半年小于 5
        elif sales_grade in ("C", "D", "E") or pr < 0.05 or rr > 0.15 or (sales_180 < 5 and sales_grade):
            fail.append(r)
    return success, fail


def summarize_group(rows, max_n=80):
    """提取关键统计 + 样本，让 LLM 看少量代表性数据"""
    sample = sorted(rows, key=lambda r: -(r.get("sales_180d") or 0))[:max_n]
    family_count = Counter((r.get("family") or "未分类")[:20] for r in rows)
    type_count = Counter((r.get("product_type") or "未分类")[:20] for r in rows)
    avg_pr = sum(r.get("latest_profit_rate") or 0 for r in rows) / max(1, len(rows))
    avg_price = sum(r.get("latest_price") or 0 for r in rows) / max(1, len(rows))
    avg_rr = sum(r.get("return_rate") or 0 for r in rows) / max(1, len(rows))
    return {
        "count": len(rows),
        "top_families": family_count.most_common(8),
        "top_types": type_count.most_common(8),
        "avg_profit_rate": round(avg_pr, 3),
        "avg_price": round(avg_price, 2),
        "avg_return_rate": round(avg_rr, 3),
        "sample": [
            {
                "sku": r["partner_sku"], "title": (r.get("title") or "")[:50],
                "family": r.get("family"), "type": r.get("product_type"),
                "store": r.get("store"),
                "sales_180d": r.get("sales_180d"),
                "profit_rate": round((r.get("latest_profit_rate") or 0) * 100, 1),
                "price": r.get("latest_price"),
                "return_rate": round((r.get("return_rate") or 0) * 100, 2),
                "grade": r.get("sales_grade"),
            } for r in sample[:30]
        ],
    }


def call_llm(group_label: str, summary: dict, n_total: int) -> str:
    import anthropic
    client = anthropic.Anthropic()
    prompt = f"""以下是点购在 noon KSA + UAE 平台上 {group_label} SKU 的统计 + 样本（共 {summary['count']} 条；总 SKU 池 {n_total}）：

【整体统计】
- 平均利润率: {summary['avg_profit_rate']*100:.1f}%
- 平均售价: {summary['avg_price']:.2f}
- 平均退货率: {summary['avg_return_rate']*100:.1f}%
- 类目分布 (Top 8): {summary['top_families']}
- 商品类型分布 (Top 8): {summary['top_types']}

【30 个代表性样本】
{json.dumps(summary['sample'], ensure_ascii=False, indent=2)}

请你作为点购的选品 agent，归纳这批 {group_label} 商品的共同模式。重点关注：
1. 类目 / 价格段 / 利润率分布有什么规律？
2. 商品标题/类型有什么共同语言学特征？（比如关键词、品类）
3. 退货率 / 销量 grade 与什么相关？
4. 给运营 3 条可落地的"未来 {group_label.replace('的', '')}"判别 / 复盘 checklist

格式: Markdown，2-4 段；最多 600 字；末尾用 `## 复盘 checklist` 列 3-5 条。
"""
    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def main():
    rows = load_skus()
    success, fail = label(rows)
    print(f"成功: {len(success)} / 失败: {len(fail)} / 总: {len(rows)}")

    # 如果样本过少，补充 sales_grade=A 或 sales_grade=B 的进入成功
    if len(success) < 10:
        success = [r for r in rows if (r.get("sales_grade") in ("SS", "S", "A", "B")) and (r.get("latest_profit_rate") or 0) > 0.10][:50]
    if len(fail) < 10:
        fail = [r for r in rows if (r.get("sales_180d") or 0) < 3 and (r.get("latest_profit_rate") or 0) < 0.10][:50]

    succ_summary = summarize_group(success)
    fail_summary = summarize_group(fail)
    print(f"-> 成功样本 {succ_summary['count']}, 失败样本 {fail_summary['count']}")

    print("LLM 归纳成功模式...")
    succ_text = call_llm("成功（类目稳销 + 高利润 + 低退货）的", succ_summary, len(rows))
    print("LLM 归纳失败模式...")
    fail_text = call_llm("失败（无销量 / 低利润 / 高退货）的", fail_summary, len(rows))

    succ_path = os.path.join(OUT_DIR, "选品_成功模式_v1.md")
    fail_path = os.path.join(OUT_DIR, "选品_失败模式_v1.md")
    with open(succ_path, "w", encoding="utf-8") as f:
        f.write(f"# 选品 · 成功模式 v1\n\n_由 Agent 从 wf2_hipop_*_sku 真实数据归纳，{succ_summary['count']} 个样本_\n\n")
        f.write(succ_text)
    with open(fail_path, "w", encoding="utf-8") as f:
        f.write(f"# 选品 · 失败模式 v1\n\n_由 Agent 从 wf2_hipop_*_sku 真实数据归纳，{fail_summary['count']} 个样本_\n\n")
        f.write(fail_text)
    print("written:", succ_path)
    print("written:", fail_path)


if __name__ == "__main__":
    main()

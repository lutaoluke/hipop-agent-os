"""
hipop.db wf 表的 thin SQL adapter.

selector 模块 N5/N9/N10 节点 + 历史冷启动 直接通过这层读 hipop 现有数据,
不重写 loader (重大发现, 见 data_inventory.md §二/三).

🚨 sa_main 硬规则: 任何 SQL 都不允许命中 sa_main. 本模块默认排除.
"""
from __future__ import annotations
import os, json, sqlite3
from contextlib import contextmanager
from typing import Optional


HIPOP_DB = os.environ.get(
    "HIPOP_DB_PATH",
    os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "hipop.db"))
)

FORBIDDEN_TABLES = frozenset({"sa_main"})


@contextmanager
def _conn():
    c = sqlite3.connect(HIPOP_DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


# ── N1: 店铺销售类目清单 ─────────────────────────────────────

def get_shop_categories(country: str = "ksa", min_listed: int = 1) -> list[dict]:
    """T1 月度任务输入. SELECT family GROUP BY 已上架 SKU."""
    tbl = f"wf2_hipop_{country}_sku"
    with _conn() as c:
        rows = c.execute(f"""
            SELECT family, COUNT(*) AS n
            FROM {tbl}
            WHERE is_listed = 1 AND family IS NOT NULL AND family != ''
            GROUP BY family
            HAVING n >= ?
            ORDER BY n DESC
        """, (min_listed,)).fetchall()
    return [dict(r) for r in rows]


# ── N5: 自家品销量级别 (作为竞争视角的标杆) ──────────────────

def get_self_sales_grades(country: str = "ksa") -> dict:
    """KSA 在售品的 sales_grade 分布. 给 N5 当 percentile 标杆参考."""
    tbl = f"wf2_hipop_{country}_sku"
    with _conn() as c:
        rows = c.execute(f"""
            SELECT sales_grade,
                   COUNT(*) AS n,
                   AVG(sales_30d) AS avg_30d,
                   AVG(sales_180d) AS avg_180d
            FROM {tbl}
            WHERE is_listed = 1 AND sales_grade IS NOT NULL
            GROUP BY sales_grade ORDER BY sales_grade
        """).fetchall()
    return {r["sales_grade"]: dict(r) for r in rows}


def get_self_trend(partner_sku: str, country: str = "ksa") -> Optional[str]:
    """读 wf5.trend, §A 起量识别用. 返回 6 态之一 or None."""
    tbl = f"wf5_hipop_{country}_sales_cycle"
    with _conn() as c:
        row = c.execute(f"SELECT trend FROM {tbl} WHERE partner_sku=?",
                       (partner_sku,)).fetchone()
    return row["trend"] if row else None


# ── N9: 库存反向约束 ────────────────────────────────────────

def get_inventory_summary(country: str = "ksa", family: Optional[str] = None) -> list[dict]:
    """N9 反向约束输入: 在售 SKU + 库存分布. 按 family 过滤."""
    sku_tbl = f"wf2_hipop_{country}_sku"
    stk_tbl = f"wf1_hipop_{country}_stock"
    where = "s.is_listed = 1"
    args: list = []
    if family:
        where += " AND s.family = ?"
        args.append(family)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT s.partner_sku, s.title, s.family, s.product_category_detail,
                   stk.noon_saleable_qty, stk.overseas_total_qty,
                   stk.yiwu_qty, stk.dongguan_qty, stk.total_stock,
                   s.sales_30d, s.sales_grade
            FROM {sku_tbl} s
            LEFT JOIN {stk_tbl} stk ON s.partner_sku = stk.partner_sku
            WHERE {where}
            ORDER BY stk.total_stock DESC
        """, args).fetchall()
    return [dict(r) for r in rows]


# ── N4: 自家店铺历史价段 (用于半托管 1.5× 检查 + 价格带对照) ───

def get_self_price_band(country: str = "ksa",
                        family: Optional[str] = None) -> dict:
    """
    自家店铺已上架商品价格分布. N4 节点用.

    Returns:
      {n_skus, min, p25, median, p75, max, currency}
      含 family 过滤. 没数据时返回 {n_skus: 0}.
    """
    tbl = f"wf2_hipop_{country}_sku"
    where = "is_listed = 1 AND latest_price IS NOT NULL AND latest_price > 0"
    args: list = []
    if family:
        where += " AND family = ?"
        args.append(family)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT latest_price, currency FROM {tbl}
            WHERE {where} ORDER BY latest_price
        """, args).fetchall()
    if not rows:
        return {"n_skus": 0, "country": country, "family": family}
    prices = [r["latest_price"] for r in rows]
    n = len(prices)
    return {
        "n_skus": n,
        "country": country,
        "family": family,
        "currency": rows[0]["currency"] or ("SAR" if country == "ksa" else "AED"),
        "min": prices[0],
        "p25": prices[n // 4],
        "median": prices[n // 2],
        "p75": prices[3 * n // 4],
        "max": prices[-1],
    }


# ── N10: 类目利润率参考线 ────────────────────────────────────

def get_category_profit_baseline(country: str = "ksa",
                                  family: Optional[str] = None) -> dict:
    """N10 对照参考线: 类目历史利润率分布."""
    tbl = f"wf2_hipop_{country}_sku"
    where = "is_listed = 1 AND latest_profit_rate IS NOT NULL"
    args: list = []
    if family:
        where += " AND family = ?"
        args.append(family)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT family,
                   COUNT(*) AS n,
                   AVG(latest_profit_rate) AS avg_pr,
                   MIN(latest_profit_rate) AS min_pr,
                   MAX(latest_profit_rate) AS max_pr
            FROM {tbl}
            WHERE {where}
            GROUP BY family
            ORDER BY n DESC
        """, args).fetchall()
    return {r["family"]: dict(r) for r in rows}


# ── 冷启动: 正例 + 反例 ─────────────────────────────────────

def get_positive_examples(country: str = "ksa",
                          min_profit_rate: float = 0.20,
                          family: Optional[str] = None) -> list[dict]:
    """正例: 已上架 + 利润率达标. 喂 preferences.jsonl 时 action=accept."""
    tbl = f"wf2_hipop_{country}_sku"
    where = "is_listed = 1 AND latest_profit_rate >= ?"
    args: list = [min_profit_rate]
    if family:
        where += " AND family = ?"
        args.append(family)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT partner_sku, noon_sku, title, image_url, family,
                   product_category_detail,
                   sales_30d, sales_60d, sales_90d, sales_180d, sales_grade,
                   cost_price, latest_price, latest_customer_paid,
                   latest_profit_rate, return_rate
            FROM {tbl}
            WHERE {where}
            ORDER BY latest_profit_rate DESC
        """, args).fetchall()
    return [dict(r) for r in rows]


def get_negative_examples(country: str = "ksa",
                          family: Optional[str] = None) -> dict[str, list[dict]]:
    """
    反例 5 类 (data_inventory §四 自查策略):
      A 市场判断错: is_listed=1 AND sales_180d<=2
      B 成本算错  : is_listed=1 AND latest_profit_rate<0.10
      C 退货风险错: is_listed=1 AND return_rate>0.15
      D 滞销积压  : wf5.status_ops LIKE '%滞销积压%'  (KSA only)
      E 趋势识别错: wf5.trend IN ('下降','急速下降')
    """
    sku = f"wf2_hipop_{country}_sku"
    cyc = f"wf5_hipop_{country}_sales_cycle"
    fam = " AND s.family = ?" if family else ""
    args = [family] if family else []
    out: dict[str, list[dict]] = {}

    base_select = f"""SELECT s.partner_sku, s.title, s.family, s.sales_30d, s.sales_180d,
                             s.return_rate, s.latest_profit_rate, s.sales_grade"""

    with _conn() as c:
        # A
        out["A_market_judgment_wrong"] = [dict(r) for r in c.execute(
            f"{base_select} FROM {sku} s WHERE s.is_listed=1 AND s.sales_180d<=2{fam} "
            f"ORDER BY s.sales_180d ASC", args).fetchall()]
        # B
        out["B_cost_miscalc"] = [dict(r) for r in c.execute(
            f"{base_select} FROM {sku} s WHERE s.is_listed=1 AND s.latest_profit_rate<0.10 "
            f"AND s.latest_profit_rate IS NOT NULL{fam} ORDER BY s.latest_profit_rate ASC",
            args).fetchall()]
        # C
        out["C_return_risk_underestimated"] = [dict(r) for r in c.execute(
            f"{base_select} FROM {sku} s WHERE s.is_listed=1 AND s.return_rate>0.15{fam} "
            f"ORDER BY s.return_rate DESC", args).fetchall()]
        # D (KSA wf5 才有 status_ops)
        try:
            out["D_inventory_dead_stock"] = [dict(r) for r in c.execute(
                f"""{base_select}, c.status_ops, c.urgency
                FROM {sku} s JOIN {cyc} c ON s.partner_sku=c.partner_sku
                WHERE s.is_listed=1 AND c.status_ops LIKE '%滞销积压%'{fam}""",
                args).fetchall()]
        except sqlite3.OperationalError:
            out["D_inventory_dead_stock"] = []   # UAE schema 还没升 v2
        # E
        out["E_trend_misjudged"] = [dict(r) for r in c.execute(
            f"""{base_select}, c.trend
            FROM {sku} s JOIN {cyc} c ON s.partner_sku=c.partner_sku
            WHERE s.is_listed=1 AND c.trend IN ('下降','急速下降'){fam}""",
            args).fetchall()]
    return out


# ── 工具: 安全列表名 ─────────────────────────────────────────

def list_hipop_tables() -> list[str]:
    """探查 hipop.db 的表清单, 强制排除 sa_main."""
    forbidden = ",".join(f"'{t}'" for t in FORBIDDEN_TABLES)
    with _conn() as c:
        rows = c.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' "
            f"AND name NOT IN ({forbidden}) ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stat"
    country = sys.argv[2] if len(sys.argv) > 2 else "ksa"
    if cmd == "stat":
        print(f"hipop tables (sa_main 已排除): {list_hipop_tables()}")
        print(f"\nshop categories ({country}):")
        for r in get_shop_categories(country):
            print(f"  {r['family']:20s} {r['n']:>4}")
        print(f"\nsales_grade 分布 ({country}):")
        for g, info in get_self_sales_grades(country).items():
            print(f"  {g}: n={info['n']:3d} avg_30d={info['avg_30d']:.1f}")
        print(f"\n类目利润率参考线 ({country}):")
        for fam, info in list(get_category_profit_baseline(country).items())[:6]:
            fam_s = (fam or "(NULL family)")[:20]
            avg = info.get('avg_pr')
            avg_s = f"{avg:.2%}" if avg is not None else "  -"
            print(f"  {fam_s:20s} n={info['n']:3d} avg_pr={avg_s}")
    elif cmd == "neg":
        neg = get_negative_examples(country)
        for k, lst in neg.items():
            print(f"  {k}: {len(lst)} examples")

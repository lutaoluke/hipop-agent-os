"""Smoke：销量评级 ABCD + 10/30 天预测的确定性算法（WS-19）— fail-then-pass 承重墙。

钉死的承重墙：
  1) 5 个 SKU fixture（增长高净值 / 稳定中净值 / 低销 / 下降退货高 / 缺历史新品），
     全带 6 个销量窗口 sales_10/30/60/90/120/180d，逐一断言 sales_grade == 预期档位、
     forecast_10d/30d == 人工锚点值；
  2) 6 个窗口都真被消费：改 sales_120d / sales_180d 会改输出（dead-input 探针）；
  3) 缺历史保守闸门：新品/缺 sales_60~180d 时趋势中性（=0，不顶满），不得判强增长/A；
  4) 算法可配置：改 config 阈值会改对应断言；
  5) 确定性：同输入两次一致；算法不调用 LLM/provider（import 行无相关依赖）。

fail-then-pass 证明（对应验门人 WS-19 两条打回）：
  - 改动前 `compute_metrics` 只读 sales_10/30/60/90d，sales_120d/180d 是死输入：
    test_all_six_windows_consumed 探针无变化 → fail；
  - 改动前缺历史时 prior30 = max(sales_60d−sales_30d,0)=0 → 趋势顶到 clamp_max →
    新品被判 A：test_missing_history_is_conservative 断言 grade!='A' → fail；
  - 改动前模块/规则不可配 + 占位口径 → 多个 fixture 锚点 fail。
  改动后全部 PASS。

预期值的手工推导见各 fixture 注释（与 config/sales_grading.json 缺省阈值绑定）。

跑法：
  python3 tests/smoke_sales_grading.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from hipop.workflows.sales_grading import (
    grade_sku, forecast, compute_metrics, load_grading_config, CONFIG_PATH,
)
import copy
import json


# ── 5 个 SKU fixture（输入含全部 6 窗口 + 人工锚定的预期 grade / forecast）──────
# 预期值全部按 config/sales_grading.json 缺省阈值手工推导，见各注释。
# 权重 w10=.35 w30=.30 w60=.15 w90=.10 w120=.05 w180=.05；
# 趋势 = (sales_30d/30 − (sales_180d−sales_30d)/150) / ((sales_180d−sales_30d)/150)。

FIXTURES = [
    {
        "name": "增长高净值→A",
        "rec": {"sales_10d": 20, "sales_30d": 50, "sales_60d": 70, "sales_90d": 85,
                "sales_120d": 95, "sales_180d": 110,
                "total_revenue": 6000.0, "latest_profit_rate": 0.30, "return_rate": 0.02},
        # 前史订单=110−50=60(≥3)；趋势=((50/30)−(60/150))/(60/150)=(1.6667−0.4)/0.4=3.1667
        # 净值=6000×0.30×0.98=1764≥1000；s30=50≥30,trend≥0.10,非 risk → A
        # base_daily=.35·2+.30·1.6667+.15·1.1667+.10·.94444+.05·.79167+.05·.61111=1.539583
        # mult=clamp(1+3.1667·0.5)=1.5 → daily=2.309375 → f10=round(23.094)=23, f30=round(69.281)=69
        "grade": "A", "forecast_10d": 23, "forecast_30d": 69,
    },
    {
        "name": "稳定中净值→B",
        "rec": {"sales_10d": 5, "sales_30d": 15, "sales_60d": 30, "sales_90d": 45,
                "sales_120d": 60, "sales_180d": 90,
                "total_revenue": 2000.0, "latest_profit_rate": 0.25, "return_rate": 0.05},
        # 前史=90−15=75；趋势=((15/30)−(75/150))/(75/150)=(0.5−0.5)/0.5=0.0
        # 净值=2000×0.25×0.95=475(≥300,<1000)；非 A(trend<0.10)；B:s30=15≥10且净值≥300 → B
        # 所有窗口日均=0.5 → base_daily=0.5；mult=1.0 → f10=5, f30=15
        "grade": "B", "forecast_10d": 5, "forecast_30d": 15,
    },
    {
        "name": "低销→C",
        "rec": {"sales_10d": 1, "sales_30d": 4, "sales_60d": 7, "sales_90d": 10,
                "sales_120d": 12, "sales_180d": 16,
                "total_revenue": 150.0, "latest_profit_rate": 0.20, "return_rate": 0.0},
        # 前史=16−4=12；趋势=((4/30)−(12/150))/(12/150)=(0.13333−0.08)/0.08=0.66667
        # 净值=150×0.20=30(<300)；非 A/B；C:s30=4≥3 → C
        # base_daily=.35·.1+.30·.13333+.15·.11667+.10·.11111+.05·.1+.05·.088889=0.113056
        # mult=clamp(1+0.66667·0.5)=1.33333 → daily=0.150741 → f10=round(1.507)=2, f30=round(4.522)=5
        "grade": "C", "forecast_10d": 2, "forecast_30d": 5,
    },
    {
        "name": "下降/退货高→D",
        "rec": {"sales_10d": 2, "sales_30d": 12, "sales_60d": 40, "sales_90d": 70,
                "sales_120d": 95, "sales_180d": 130,
                "total_revenue": 1500.0, "latest_profit_rate": 0.28, "return_rate": 0.25},
        # return_rate=0.25≥0.20（risk 闸门）→ D（趋势=((12/30)−(118/150))/(118/150)=-0.4915 也触发）
        # base_daily=.35·.2+.30·.4+.15·.66667+.10·.77778+.05·.79167+.05·.72222=0.443472
        # mult=clamp(1+(-0.4915)·0.5)=0.754235 → daily=0.334489 → f10=round(3.345)=3, f30=round(10.035)=10
        "grade": "D", "forecast_10d": 3, "forecast_30d": 10,
    },
    {
        "name": "缺历史新品→B(保守,非A)",
        "rec": {"sales_10d": 10, "sales_30d": 35, "sales_60d": None, "sales_90d": None,
                "sales_120d": None, "sales_180d": None,
                "total_revenue": 4000.0, "latest_profit_rate": 0.30, "return_rate": 0.02},
        # 前史=sales_180d−sales_30d=0−35=−35 < min_prior_orders → 缺历史 → 趋势=0(中性,不顶满)
        # 净值=4000×0.30×0.98=1176≥1000 且 s30=35≥30，但 trend=0<0.10 → 不可能 A → 落 B
        #   （改动前算法 prior30=max(0−35,0)=0 会把趋势顶到 clamp_max=5 → 误判 A）
        # base_daily=.35·(10/10)+.30·(35/30)+其余窗口=0 → 0.35+0.35=0.70；mult=1.0 → f10=7, f30=21
        "grade": "B", "forecast_10d": 7, "forecast_30d": 21,
    },
]


def test_grade_and_forecast_match_fixtures():
    """每个 fixture：grade + forecast_10d/30d 精确匹配人工锚点。"""
    for fx in FIXTURES:
        g = grade_sku(fx["rec"])
        assert g == fx["grade"], f"{fx['name']}: grade 期望 {fx['grade']}，实际 {g}"
        f = forecast(fx["rec"])
        assert f["forecast_10d"] == fx["forecast_10d"], (
            f"{fx['name']}: forecast_10d 期望 {fx['forecast_10d']}，实际 {f['forecast_10d']}")
        assert f["forecast_30d"] == fx["forecast_30d"], (
            f"{fx['name']}: forecast_30d 期望 {fx['forecast_30d']}，实际 {f['forecast_30d']}")


def test_all_six_windows_consumed():
    """6 个销量窗口都不是死输入：改 sales_120d / sales_180d 必改输出。

    钉死验门人 WS-19 打回第①条——改动前算法只读 sales_10/30/60/90d，
    把 sales_120d/180d 改成 0 后 grade 与 forecast 完全不变（死输入）。"""
    base = FIXTURES[0]["rec"]                       # A 档，6 窗口齐全
    base_fc = forecast(base)
    base_g = grade_sku(base)

    rec120 = copy.deepcopy(base); rec120["sales_120d"] = 0
    assert forecast(rec120) != base_fc, "改 sales_120d 必须改预测（否则它是死输入）"

    rec180 = copy.deepcopy(base); rec180["sales_180d"] = 0
    changed_180 = (forecast(rec180) != base_fc) or (grade_sku(rec180) != base_g)
    assert changed_180, "改 sales_180d 必须改 grade 或预测（否则它是死输入）"
    # sales_180d 进趋势：历史清零后近期相对更高，等同'缺历史'保守化 → 不再是强增长 A
    assert grade_sku(rec180) != "A", "sales_180d 清零(前史塌缩)后不应仍判 A"


def test_missing_history_is_conservative():
    """缺历史保守闸门：新品/缺 sales_60~180d 时趋势中性(0)，不得顶满判 A。

    钉死验门人 WS-19 打回第②条——'没有历史'被算成'暴涨'(trend→clamp_max)→A 的风险。"""
    fx = FIXTURES[4]["rec"]                          # 缺历史新品：高量+高净值但无前史
    m = compute_metrics(fx)
    assert m["has_history"] is False, "前史不足应判 has_history=False"
    assert m["trend"] == 0.0, f"缺历史趋势应中性 0，实际 {m['trend']}（绝不能顶到 clamp_max）"
    cfg = load_grading_config()
    assert m["trend"] != cfg["trend"]["clamp_max"], "缺历史不得把趋势顶满"
    assert grade_sku(fx) != "A", "缺历史的 SKU 即便量大净值高也不得判 A（趋势不可信）"

    # 反向锚点：同 SKU 补上真实增长的历史窗口 → 趋势转正、可升 A，证明闸门只在'缺历史'时收紧
    grown = copy.deepcopy(fx)
    grown.update({"sales_60d": 38, "sales_90d": 42, "sales_120d": 45, "sales_180d": 50})
    mg = compute_metrics(grown)
    assert mg["has_history"] is True and mg["trend"] > 0, "补足历史后应判有历史且趋势转正"
    assert grade_sku(grown) == "A", "补足真实增长历史后应可判 A"


def test_consumes_net_value_not_just_volume():
    """评级真消费销售净值：同样的销量/趋势，净值塌到 0 → 从 A 降到 C。"""
    rec = copy.deepcopy(FIXTURES[0]["rec"])
    assert grade_sku(rec) == "A"
    rec["latest_profit_rate"] = 0.0          # 净值 → 0
    assert grade_sku(rec) == "C", "净值清零后量仍在但应降级到 C（A/B 都要净值）"


def test_config_threshold_changes_grade():
    """可配置性：改 config 阈值会改评级。把 A.min_sales_30d 抬到 80，
    原 A 档（s30=50）应降级到 B（净值1764≥300、s30≥10）。"""
    cfg = copy.deepcopy(load_grading_config())
    rec = FIXTURES[0]["rec"]
    assert grade_sku(rec, config=cfg) == "A"
    cfg["grade"]["A"]["min_sales_30d"] = 80
    assert grade_sku(rec, config=cfg) == "B", "抬高 A 的 min_sales_30d 后应降级到 B"


def test_deterministic_and_no_provider():
    """同输入两次一致；算法源码 import 行不依赖 LLM/provider/网络。"""
    rec = FIXTURES[0]["rec"]
    assert grade_sku(rec) == grade_sku(rec)
    assert forecast(rec) == forecast(rec)

    src_path = os.path.join(REPO, "hipop", "workflows", "sales_grading.py")
    with open(src_path) as fh:
        # 只看 import 行，避免误伤注释/docstring 里对 "LLM/provider" 的说明文字
        import_lines = [ln.lower() for ln in fh if ln.strip().startswith(("import ", "from "))]
    for banned in ("requests", "openai", "anthropic", "llm", "provider", "httpx", "urllib", "socket"):
        for ln in import_lines:
            assert banned not in ln, f"评级算法不得 import {banned!r}（必须确定性、不调用 provider）"


def test_config_file_present_and_sane():
    """配置文件存在且关键阈值齐全（确定性规则落在配置里，不在 prompt/代码里）。"""
    assert os.path.exists(CONFIG_PATH), f"缺配置文件 {CONFIG_PATH}"
    with open(CONFIG_PATH) as fh:
        cfg = json.load(fh)
    for k in ("net_value", "trend", "grade", "forecast"):
        assert k in cfg, f"配置缺 {k} 段"
    for gk in ("A", "B", "C", "risk"):
        assert gk in cfg["grade"], f"grade 段缺 {gk}"
    for tk in ("min_prior_orders", "insufficient_trend", "prior_window_days"):
        assert tk in cfg["trend"], f"trend 段缺缺历史闸门键 {tk}"
    for wk in ("w10", "w30", "w60", "w90", "w120", "w180"):
        assert wk in cfg["forecast"]["window_weights"], f"预测权重缺 {wk}（6 窗口都要参与）"


if __name__ == "__main__":
    import traceback

    tests = [
        test_grade_and_forecast_match_fixtures,
        test_all_six_windows_consumed,
        test_missing_history_is_conservative,
        test_consumes_net_value_not_just_volume,
        test_config_threshold_changes_grade,
        test_deterministic_and_no_provider,
        test_config_file_present_and_sane,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

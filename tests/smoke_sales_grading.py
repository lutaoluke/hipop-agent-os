"""Smoke：销量评级 ABCD + 10/30 天预测的确定性算法（WS-19）— fail-then-pass 承重墙。

钉死的承重墙：
  1) 4 个 SKU fixture（增长高净值 / 稳定中净值 / 低销 / 下降退货高）逐一断言
     sales_grade == 预期档位、forecast_10d/30d == 人工锚点值；
  2) 算法可配置：改 config 阈值会改对应断言（把 A 的 min_sales_30d 抬到 50，
     原 A 档 SKU 应降级到 B）；
  3) 确定性：同输入两次调用结果一致；算法不调用 LLM/provider（源码无相关 import）。

fail-then-pass 证明：
  - 改动前 `hipop/workflows/sales_grading` 不存在 + grade_sku/forecast 是占位规则
    （只看单一 sales_30d、不消费销售净值、阈值写死不可配）：
      · import sales_grading 直接 ImportError → fail；
      · 即便对老 grade_sku 跑这些 fixture，低销/下降档会被误判成 B、forecast 只是
        sales_30d 线性外推（fixture1 期望 24，老算法给 15）→ 断言 fail。
  - 改动后：评级消费趋势 + 净值 + risk 闸门，预测多窗口混合 → 全过。

预期值的手工推导见下方每个 fixture 注释（与 config/sales_grading.json 缺省阈值绑定）。

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


# ── 4 个 SKU fixture（输入 + 人工锚定的预期 grade / forecast）─────────────
# 预期值全部按 config/sales_grading.json 缺省阈值手工推导，见各注释。

FIXTURES = [
    {
        "name": "增长高净值→A",
        "rec": {"sales_10d": 20, "sales_30d": 45, "sales_60d": 60, "sales_90d": 80,
                "total_revenue": 5000.0, "latest_profit_rate": 0.30, "return_rate": 0.02},
        # trend=(45 - (60-45))/(60-45)=(45-15)/15=2.0；净值=5000×0.30×0.98=1470≥1000；
        # s30=45≥30, trend≥0.10, 非 risk → A
        # base_daily=0.5·2.0+0.3·1.5+0.2·(80/90)=1.0+0.45+0.177778=1.627778
        # mult=clamp(1+2.0·0.5,0.5,1.5)=1.5 → daily=2.441667
        # f10=round(24.41667)=24, f30=round(73.25)=73
        "grade": "A", "forecast_10d": 24, "forecast_30d": 73,
    },
    {
        "name": "稳定中净值→B",
        "rec": {"sales_10d": 5, "sales_30d": 15, "sales_60d": 30, "sales_90d": 45,
                "total_revenue": 2000.0, "latest_profit_rate": 0.25, "return_rate": 0.05},
        # trend=(15-15)/15=0.0；净值=2000×0.25×0.95=475（≥300，<1000）；
        # 非 A（trend<0.10）；B：s30=15≥10 且净值≥300 → B
        # base_daily=0.5·0.5+0.3·0.5+0.2·0.5=0.5；mult=1.0 → daily=0.5
        # f10=round(5.0)=5, f30=round(15.0)=15
        "grade": "B", "forecast_10d": 5, "forecast_30d": 15,
    },
    {
        "name": "低销→C",
        "rec": {"sales_10d": 1, "sales_30d": 4, "sales_60d": 7, "sales_90d": 10,
                "total_revenue": 150.0, "latest_profit_rate": 0.20, "return_rate": 0.0},
        # trend=(4-3)/3=0.3333；净值=150×0.20×1.0=30（<300）；
        # 非 A/B；C：s30=4≥3 → C
        # base_daily=0.5·0.1+0.3·(4/30)+0.2·(10/90)=0.05+0.04+0.022222=0.112222
        # mult=clamp(1+0.33333·0.5)=1.166667 → daily=0.130926
        # f10=round(1.30926)=1, f30=round(3.92778)=4
        "grade": "C", "forecast_10d": 1, "forecast_30d": 4,
    },
    {
        "name": "下降/退货高→D",
        "rec": {"sales_10d": 2, "sales_30d": 12, "sales_60d": 40, "sales_90d": 70,
                "total_revenue": 1500.0, "latest_profit_rate": 0.28, "return_rate": 0.25},
        # return_rate=0.25≥0.20（risk 闸门）且 trend=(12-28)/28=-0.5714≤-0.20 → D
        # base_daily=0.5·0.2+0.3·0.4+0.2·(70/90)=0.1+0.12+0.155556=0.375556
        # mult=clamp(1+(-0.5714)·0.5,0.5,1.5)=clamp(0.71429)=0.714286 → daily=0.268254
        # f10=round(2.68254)=3, f30=round(8.04762)=8
        "grade": "D", "forecast_10d": 3, "forecast_30d": 8,
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


def test_consumes_net_value_not_just_volume():
    """评级真消费销售净值：同样的销量/趋势，净值塌到 0 → 从 A/B 降到 C。

    钉死'别只按单一 30 天销量判断'——若把 fixture1（A）的利润率清零（净值=0），
    量与趋势没变，但应跌出 A/B 落到 C。"""
    rec = copy.deepcopy(FIXTURES[0]["rec"])
    assert grade_sku(rec) == "A"
    rec["latest_profit_rate"] = 0.0          # 净值 → 0
    assert grade_sku(rec) == "C", "净值清零后量仍在但应降级到 C（A/B 都要净值）"


def test_config_threshold_changes_grade():
    """可配置性：改 config 阈值会改评级。把 A.min_sales_30d 抬到 50，
    原 A 档（s30=45）应降级到 B（净值1470≥300、s30≥10）。"""
    cfg = copy.deepcopy(load_grading_config())
    rec = FIXTURES[0]["rec"]
    assert grade_sku(rec, config=cfg) == "A"
    cfg["grade"]["A"]["min_sales_30d"] = 50
    assert grade_sku(rec, config=cfg) == "B", "抬高 A 的 min_sales_30d 后应降级到 B"


def test_deterministic_and_no_provider():
    """同输入两次一致；算法源码不依赖 LLM/provider/网络。"""
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


if __name__ == "__main__":
    import traceback

    tests = [
        test_grade_and_forecast_match_fixtures,
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

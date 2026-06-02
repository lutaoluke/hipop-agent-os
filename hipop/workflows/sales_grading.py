"""WS-19 · 销量评级 ABCD + 10/30 天预测的确定性算法（可配置、可测试、可复用）。

为什么独立成模块
-----------------
原 `wf_sales_static.grade_sku / forecast` 是占位规则：评级只看单一 sales_30d +
sales_60d 算的增长，**没消费销售净值（利润/退货）**，预测是 sales_30d 线性外推，
且阈值写死在代码里改不动（典型"占位假数据"死法）。

本模块把规则抽成纯函数 + 显式配置文件 `config/sales_grading.json`：
  - 同输入同输出：不读时钟、不随机、不调用 LLM/provider（全部基于入参数值）。
  - 阈值全在 JSON 里：改配置即改评级/预测，代码零改动（smoke 钉死这点）。
  - wf_sales_static（per-table）与 wf_sales_static_v2.merge_entity_v2（单表 v2）
    两条生产路径都复用这里的 grade_sku / forecast，避免接线缺失/规则漂移。

消费字段（来自 wf2_sku，noon 优先 merge 后的真实值）：
  sales_10d / sales_30d / sales_60d / sales_90d / sales_120d / sales_180d
  total_revenue / latest_profit_rate / return_rate

DoD 见 tests/smoke_sales_grading.py：4 个 SKU fixture（增长高净值 / 稳定中净值 /
低销 / 下降退货高）逐一断言 grade + forecast_10d/30d，并断言改配置阈值会改结果。
"""
from __future__ import annotations

import os
import json
from functools import lru_cache

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "sales_grading.json")


@lru_cache(maxsize=8)
def load_grading_config(path: str | None = None) -> dict:
    """读评级/预测阈值配置。缺省读 config/sales_grading.json。

    返回的 dict 会被缓存；测试里要改阈值请用 grade_sku(rec, config=...) /
    forecast(rec, config=...) 显式传入覆盖配置，不要原地改缓存对象。
    """
    with open(path or CONFIG_PATH) as f:
        return json.load(f)


def _num(v, default=0.0):
    """把 None / 空串 / 非数安全转 float。"""
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def compute_metrics(rec: dict, config: dict | None = None) -> dict:
    """从 SKU 记录算评级/预测共用的中间量（确定性，纯数值）。

    返回 sales_30d / trend / net_value / return_rate / base_daily。
    """
    cfg = config or load_grading_config()

    s10 = _num(rec.get("sales_10d"))
    s30 = _num(rec.get("sales_30d"))
    s60 = _num(rec.get("sales_60d"))
    s90 = _num(rec.get("sales_90d"))

    # 趋势：近30天 vs 前30天日均增速
    prior30 = max(s60 - s30, 0.0)
    tcfg = cfg["trend"]
    if prior30 > 0:
        trend = (s30 - prior30) / prior30
    else:
        trend = tcfg["clamp_max"] if s30 > 0 else 0.0
    trend = _clamp(trend, tcfg["clamp_min"], tcfg["clamp_max"])

    # 销售净值：营收 × 利润率 ×（1 − 退货率），缺利润率记 0（保守）
    ncfg = cfg["net_value"]
    revenue = _num(rec.get("total_revenue"))
    profit_rate = _num(rec.get("latest_profit_rate")) if ncfg.get("use_profit_rate") else 1.0
    return_rate = _num(rec.get("return_rate"))
    keep = (1.0 - return_rate) if ncfg.get("use_return_rate") else 1.0
    net_value = revenue * profit_rate * keep

    # 预测基线日均：多窗口混合（不只看单一 30 天）
    w = cfg["forecast"]["window_weights"]
    base_daily = (w["w10"] * (s10 / 10.0)
                  + w["w30"] * (s30 / 30.0)
                  + w["w90"] * (s90 / 90.0))

    return {
        "sales_30d": s30,
        "trend": trend,
        "net_value": net_value,
        "return_rate": return_rate,
        "base_daily": base_daily,
    }


def grade_sku(rec: dict, config: dict | None = None) -> str:
    """评级 ABCD（确定性）。规则全部来自 config，见 sales_grading.json 的 _def。"""
    cfg = config or load_grading_config()
    g = cfg["grade"]
    m = compute_metrics(rec, cfg)

    # risk 闸门：退货过高或明显下降 → 直接 D（对应"下降/退货高"档）
    risk = g["risk"]
    if m["return_rate"] >= risk["max_return_rate"] or m["trend"] <= risk["min_trend"]:
        return "D"

    a = g["A"]
    if (m["sales_30d"] >= a["min_sales_30d"]
            and m["net_value"] >= a["min_net_value"]
            and m["trend"] >= a["min_trend"]):
        return "A"

    b = g["B"]
    if m["sales_30d"] >= b["min_sales_30d"] and m["net_value"] >= b["min_net_value"]:
        return "B"

    c = g["C"]
    if m["sales_30d"] >= c["min_sales_30d"]:
        return "C"

    return "D"


def forecast(rec: dict, config: dict | None = None) -> dict:
    """预测近 10 / 30 天销量（确定性）。基线日均按趋势钳制调整后外推。"""
    cfg = config or load_grading_config()
    fc = cfg["forecast"]
    m = compute_metrics(rec, cfg)

    mult = _clamp(1.0 + m["trend"] * fc["trend_weight"], fc["mult_min"], fc["mult_max"])
    daily = m["base_daily"] * mult
    return {
        "forecast_10d": int(round(daily * 10)),
        "forecast_30d": int(round(daily * 30)),
    }

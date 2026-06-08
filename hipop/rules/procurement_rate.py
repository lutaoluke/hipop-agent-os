"""采购议价率绩效口径 — 权威规则源（WS-117, Luke sign-off 2026-06-08）

此文件是可审计的规则代码层，供 smoke test 加载断言、Agent NOTES.md 同步引用。
任何修改须经产研门批准并更新对应 smoke。
"""

# 采购议价率公式
# = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%
FORMULA = {
    "numerator": "议价差额",
    # 议价差额 = 1688采购标准价 − 实际成交采购价（谈判省下的金额）
    "numerator_definition": "1688采购标准价 - 实际成交采购价",
    # 分母必须同时包含标准价 + 头程运费分摊，缺一均为错误口径
    "denominator_components": ["1688采购标准价", "头程运费分摊"],
    "formula_text": "议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%",
}

# 绩效阈值（Luke sign-off）
THRESHOLDS = {
    "fail_below": 0.03,    # < 3% 不合格
    "pass_above": 0.06,    # ≥ 6% 正常
    "fail_label": "不合格",
    "pass_label": "正常",
}

# plus 折扣口径
PLUS_DISCOUNT = {
    "included_in_procurement_rate": False,  # plus 折扣【不】计入采购议价率
    "included_in_kpi": False,               # plus 折扣【不】计入采购议价绩效
    "classification": "noon 平台运营/营销费用",
    "note": "noon 平台 plus 折扣属于运营/营销费用，不属于采购端议价绩效，不可部分计入后再做运营侧扣减",
}

# 废止口径（严禁在 Agent 回答中使用）
DEPRECATED = {
    "old_threshold_15pct": "≥ 15% 合格（已废止，rules.md §8 旧内控口径）",
    "old_formula_no_freight": "1688标价差价 ÷ 1688标价（已废止，分母未含头程运费分摊）",
}

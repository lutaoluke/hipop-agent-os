"""采购议价率绩效口径 — 权威规则源（WS-117, Luke sign-off 2026-06-08）

此文件是可审计的规则代码层，供 smoke test 加载断言、Agent NOTES.md 同步引用。
任何修改须经产研门批准并更新对应 smoke。
"""
import re

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

# ── 生产接线 verifier（WS-117 round-11）──────────────────────────────────────
# 当 agent 回复包含采购议价率讨论时，检查分母/plus口径是否正确。

_TOPIC_RE = re.compile(r'采购(?:议价率|折扣率)|(?:议价率|折扣率).{0,10}采购')

# 错误分母：议价差额 ÷ 仅1688采购标准价（缺头程运费分摊）
_FORMULA_WRONG_DENOM_RE = re.compile(
    r'议价差额\s*(?:[÷/]|除以)\s*\(?\s*(?:只用|仅用)?\s*1688.{0,15}(?:标准价|标价|参考价)\s*\)?'
    r'(?!\s*[\+＋加].{0,18}头程)',
    re.IGNORECASE,
)

# 错误分母：销售价/noon价格/采购价等被拿来当采购议价率或采购折扣率分母。
_FORMULA_EXTERNAL_DENOM_RE = re.compile(
    r'议价差额\s*(?:[÷/]|除以)\s*\(?\s*'
    r'(?:'
    r'noon\s*(?:平台)?\s*(?:价格|价|售价|销售价)'
    r'|(?:产品|商品|平台)?\s*(?:销售价|售价)'
    r'|采购价|采购单价|实际成交采购价|成交采购价|成交价'
    r')\s*\)?',
    re.IGNORECASE,
)

# 旧 15% 合格/达标线：测试 oracle 已拦截，生产 verifier 也必须标红。
_OLD_15PCT_THRESHOLD_RE = re.compile(
    r'(?:采购(?:议价率|折扣率)|议价率|折扣率).{0,24}'
    r'(?:≥|>=|>|＞|大于|高于|超过|达到|达|不低于|不能低于|最低|至少)?\s*'
    r'(?:15\s*[%％]|15\s*(?:个点|个百分点|百分点)|十五\s*(?:个点|个百分点|百分点)?)'
    r'.{0,24}(?:合格|达标|正常|过线|备注|不合格|绩效|说明原因|要说明)'
    r'|(?:15\s*[%％]|15\s*(?:个点|个百分点|百分点)|十五\s*(?:个点|个百分点|百分点)?)'
    r'.{0,16}(?:才|即|就|以上|为|算).{0,8}(?:合格|达标|正常|过线)',
    re.IGNORECASE,
)

# 错误分母（自然话术）：以"分母用 X"/"以 X 作为分母"方式引入错误价格基准。
# 补充 _FORMULA_EXTERNAL_DENOM_RE 仅覆盖"议价差额 ÷ X"公式写法的盲区。
_DESCRIPTIVE_DENOM_RE = re.compile(
    # 分母用/是/为 noon/产品/商品/平台 ... 售价/价格
    r'分母\s*(?:用|是|为|采用|设为)\s*(?:noon|产品|商品|平台).{0,20}(?:售价|价格|价)'
    r'|分母\s*(?:用|是|为|采用|设为)\s*(?:销售价|售价|采购价|成交价)'
    # 以 产品/商品/noon 售价 作为 分母
    r'|以\s*(?:noon\s*(?:平台)?\s*)?(?:产品|商品)?\s*(?:售价|销售价)\s*作为\s*分母'
    r'|以\s*noon\s*(?:平台)?\s*.{0,15}(?:价格|售价|价)\s*作为\s*分母',
    re.IGNORECASE,
)

# plus 仍计入绩效（绕过"不计入议价率"正向声明后再说"但绩效还是算"）
_PLUS_STILL_IN_KPI_RE = re.compile(
    # "但计入/纳入/进入/归入 采购绩效/KPI/考核"（无 会/仍/还 等 modal 词）
    r'plus.{0,80}(?<!不)(?:计入|纳入|算入|算进|进入|归入|列入).{0,20}(?:采购端|采购|议价)?(?:绩效|KPI|考核)'
    # "但/不过 会/仍/还 计入/纳入/进入/归入 绩效/KPI/考核"（close to plus context）
    r'|plus.{0,80}(?:会|仍|还)\s*(?:计入|纳入|进入|归入).{0,20}(?:议价)?(?:绩效|KPI|考核)'
    r'|plus.{0,80}(?:会|仍|还)\s*纳入.{0,20}(?:采购端|议价)?(?:绩效|KPI|考核)'
    # "绩效考核时 仍 纳入/计入/进入/归入"
    r'|绩效考核时?\s*仍\s*(?:纳入|计入|进入|归入).{0,15}(?:绩效|KPI|考核)'
    # "要把 plus ... 算进去/计入/进入/归入"
    r'|要把.{0,5}plus.{0,30}(?:一起)?.{0,5}(?:算进去|算进来|算入|计入|纳入|进入|归入)',
    re.IGNORECASE,
)


def check_procurement_rate_reply(reply: str) -> list:
    """生产接线检查：当 LLM 回复讨论采购议价率时，验证口径是否符合 WS-117 规则源。

    仅在 reply 含"采购议价率"时触发，避免误报。
    返回 warns 列表，空列表表示无问题。
    调用方（agent.py）将 warns 附加到 hallu_warnings 并显示 banner。
    """
    if not _TOPIC_RE.search(reply):
        return []

    warns = []
    if _FORMULA_WRONG_DENOM_RE.search(reply):
        warns.append(
            "采购议价率公式分母错误：应为(1688采购标准价 + 头程运费分摊)，"
            "检测到分母缺少头程运费分摊。规则源: hipop/rules/procurement_rate.py"
        )
    if _FORMULA_EXTERNAL_DENOM_RE.search(reply):
        warns.append(
            "采购议价率公式分母错误：应为(1688采购标准价 + 头程运费分摊)，"
            "不能使用noon价格、销售价或采购价作为分母。规则源: hipop/rules/procurement_rate.py"
        )
    if _DESCRIPTIVE_DENOM_RE.search(reply):
        warns.append(
            "采购议价率分母口径错误：应为(1688采购标准价 + 头程运费分摊)，"
            "检测到以自然话术引入错误价格基准（如'分母用售价'/'以产品售价作为分母'）。"
            "规则源: hipop/rules/procurement_rate.py"
        )
    if _OLD_15PCT_THRESHOLD_RE.search(reply):
        warns.append(
            "采购议价率旧阈值口径错误：15%合格/达标线已废止，"
            "应使用3%不合格、6%正常的绩效阈值样例。规则源: hipop/rules/procurement_rate.py"
        )
    if _PLUS_STILL_IN_KPI_RE.search(reply):
        warns.append(
            "plus折扣口径错误：plus折扣不计入采购议价率/绩效（included_in_kpi=False）。"
            "规则源: hipop/rules/procurement_rate.py"
        )
    return warns

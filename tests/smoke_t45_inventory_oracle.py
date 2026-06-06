"""smoke_t45_inventory_oracle.py — T45-S2a 同款同SKU口径内容 oracle fail-then-pass smoke

验收（WS-110）：
  T45 题面：「如果国内仓 20 寸行李箱库存多，本期选品该如何受库存反向约束？」
  Luke 已在 WS-93 sign-off：规则 rules.md 3c 是硬口径——库存只约束同款同 SKU，
  不能扩展成「20寸库存多 → 本期必须选24寸+拓展层」。

旧 T45 期望（WRONG）：
  expectation = "Should mention 20寸库存约束选择24寸+拓展层"
  → 旧 oracle 要求回答必须提 24寸+拓展层，但这与 rules.md 3c 直接冲突。
  → 正确的 Agent 回答（只讲同款同SKU约束）反而被旧 oracle 误判 FAIL。

新 T45 期望（CORRECT）：
  答案表达库存只约束同款同 SKU，不强制「20寸库存多 → 必须24寸+拓展层」，
  当前阶段问什么答什么，不过度发散。

FAIL（修前 oracle）：
  - 旧 oracle 要求回答必须出现「24寸」+「拓展层」 → 正确答案被误判 FAIL
  - 旧 oracle 把「规则层面错误」的内容（强制24寸）反而视为正确

PASS（修后 oracle）：
  - 新 oracle 检查「同款同SKU」内容约束（所有通过分支均须含 SKU 约束，防宽松绕过）
  - 新 oracle 拒绝「20寸库存多 → 必须/应优先24寸」的扩展表述
  - S1 gate：reply 声称「已查询/已拉取/根据完整数据」但无工具证据 → 真实拦截（断言 FAIL）
  - S2 oracle 与 S1 gate 独立：S2 只管内容口径，S1 管工具证据

三死法：
  - 接线缺失：只改描述，oracle 实际仍按旧逻辑判定 → 用确定性 keyword 断言，不靠 LLM
  - 死代码短路：去掉「24寸」硬要求时放宽到任何泛泛回答都过 → 保留「同款同SKU」+SKU 约束
  - 占位假数据：用固定字符串骗过 oracle → oracle 用 regex/keyword 真实匹配，多反例覆盖

跑法：
  python3 tests/smoke_t45_inventory_oracle.py
  make test-one F=tests/smoke_t45_inventory_oracle.py
  （也被 make test 自动聚合）
"""
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hipop.server import _safety  # noqa: E402


# ── T45 S2 内容 oracle（确定性规则）────────────────────────────────────────────

# 错误表述模式：将「20寸库存多」硬扩展为「必须/应优先选24寸」的说法。
# Luke sign-off: 这类表述违反 rules.md 3c（库存只约束同款同 SKU）。
_WRONG_24INCH_MANDATE_RE = re.compile(
    r"20\s*寸.{0,25}(必须|本期.*选|应优先选|主推|引导.*选|推荐.*主).{0,20}24\s*寸"
    r"|20\s*寸.*库存.{0,20}(必须|应|要).{0,20}24\s*寸"
    r"|库存多.{0,20}(必须|应优先|主推).{0,20}24\s*寸"
)

# 正确表述模式：明确说明库存约束只作用于「同款同SKU」。
# 所有分支均须包含 SKU（防「只说同款但不提 SKU 约束」的宽松绕过）：
#   ✓ 「同款同SKU」
#   ✓ 「同款同 SKU」
#   ✓ 「库存...只...约束...同款...SKU」（须同时含 SKU）
#   ✓ 「约束...同款...SKU」
#   ✗ 「只约束同款产品」（无 SKU → FAIL，防洞1宽松绕过）
_SAME_SKU_CONSTRAINT_RE = re.compile(
    r"同款同\s*SKU"
    r"|同款.{0,10}同.{0,5}SKU"
    r"|库存.{0,20}只.{0,15}约束.{0,15}同款.{0,15}SKU"
    r"|约束.{0,10}同款.{0,10}SKU",
    re.IGNORECASE
)


def _t45_content_oracle(reply: str) -> tuple[bool, list[str]]:
    """T45 同款同SKU内容口径 oracle（S2 确定性规则）。

    通过条件（AND）：
      1. 明确表达库存约束只作用于同款同SKU，且包含 SKU 字样（防宽松绕过）
      2. 不包含「20寸库存多 → 必须/应优先选24寸」的扩展性强制表述

    返回 (passed, fail_reasons)。
    """
    fails = []

    # 检查错误扩展：「20寸库存多 → 必须/优先24寸」
    if _WRONG_24INCH_MANDATE_RE.search(reply):
        fails.append(
            "reply 将20寸库存多硬扩展为'必须/应优先24寸'，违反 rules.md 3c 同款同SKU口径"
        )

    # 检查必须表达「同款同SKU」约束口径（含 SKU，防洞1宽松绕过）
    if not _SAME_SKU_CONSTRAINT_RE.search(reply):
        fails.append(
            "reply 未表达'同款同SKU'约束口径（须包含 SKU 字样；"
            "只说'同款'不够，rules.md 3c 核心：库存只约束同款同SKU）"
        )

    return (len(fails) == 0), fails


# ── T45 S1 证据门（选品库存约束类回答必须有工具证据）──────────────────────────────

def _inventory_gate_warned(warns):
    """是否出现选品/库存约束证据门告警。"""
    return any(
        ("库存" in w or "选品" in w or "list_products" in w or "query_sku" in w)
        and ("证据" in w or "工具" in w or "未查询" in w or "叙述查过" in w)
        for w in warns
    )


def _s1_inventory_evidence_gate(reply: str, tool_log: list) -> list[str]:
    """T45-S1 选品/库存约束证据门：走真实 _safety.sanitize_reply hook。

    当 reply 含选品/库存约束上下文且无 list_products(limit>0) 或 query_sku 工具证据时，
    返回告警列表（非空=被拦截；空=通过）。
    """
    tools_used = [entry.get("name", "") for entry in (tool_log or []) if entry.get("name")]
    _, warns = _safety.sanitize_reply(reply, tools_used, tool_log=tool_log or [])
    return [w for w in warns if _inventory_gate_warned([w])]


# ── fail-then-pass 演示：旧 oracle 误判 ─────────────────────────────────────────

def test_old_oracle_rejects_correct_reply():
    """fail-then-pass 演示（修前）：旧 oracle 要求「24寸+拓展层」，正确 Agent 回答被误判 FAIL。

    旧 T45 期望：expectation = "Should mention 20寸库存约束选择24寸+拓展层"
    → 旧 oracle 检查「24寸」+「拓展层」是否都在 reply 里。
    → 正确的 Agent 回答（遵循 rules.md 3c，讲同款同SKU）不包含「24寸+拓展层」
    → 旧 oracle 对正确回答判 FAIL → 旧期望本身有误。
    """
    correct_reply = (
        "根据 rules.md 3c，库存只约束同款同 SKU。"
        "国内仓 20 寸行李箱库存多，这只约束「与国内仓同款同 SKU 的候选品」。"
        "不同款、不同尺寸的候选品不受这批库存约束。"
        "当前阶段按题面回答，不过度发散。"
    )

    # 旧 oracle 逻辑：必须包含「24寸」AND「拓展层」
    old_oracle_passes = ("24寸" in correct_reply and "拓展层" in correct_reply)

    # 旧 oracle 对正确回答判 FAIL（old_oracle_passes == False）
    assert not old_oracle_passes, (
        "旧 oracle 本应对正确回答判 FAIL（正确回答不含'24寸+拓展层'），"
        "但 old_oracle_passes=True，说明测试用的'正确回答'本身有问题"
    )


def test_old_oracle_passes_wrong_reply():
    """修前演示：旧 oracle 对错误的强制「24寸+拓展层」回答判 PASS。

    旧期望接受「20寸库存多 → 必须选24寸+拓展层」作为正确答案，
    但这违反 rules.md 3c，Luke 已明确说此方向不对。
    """
    wrong_reply = (
        "国内仓 20 寸行李箱库存多，本期选品应优先考虑24寸+拓展层。"
        "这样可以通过差异化来避开库存压力，引导消费者选择更高价的产品。"
    )

    # 旧 oracle：包含「24寸」AND「拓展层」
    old_oracle_passes = "24寸" in wrong_reply and "拓展层" in wrong_reply

    assert old_oracle_passes, (
        "旧 oracle 本应对包含'24寸+拓展层'的回答判 PASS，"
        "此处失败说明旧 oracle 逻辑有误"
    )


# ── 新 oracle 测试（修后 PASS）──────────────────────────────────────────────────

def test_new_oracle_passes_correct_reply_same_sku():
    """修后：新 oracle 对表达「同款同SKU约束」的正确回答判 PASS。"""
    correct_reply = (
        "根据 rules.md 3c，库存只约束同款同 SKU。"
        "国内仓 20 寸行李箱库存多，这只约束「与国内仓同款同 SKU 的候选品」。"
        "不同款、不同尺寸的候选品不受这批库存约束。"
        "当前阶段按题面回答，不过度发散。"
    )
    passed, fails = _t45_content_oracle(correct_reply)
    assert passed, (
        f"修后 oracle 应对表达'同款同SKU'的正确回答判 PASS，实际 fails={fails}"
    )


def test_new_oracle_passes_correct_reply_no_overexpand():
    """修后：说明「只约束同款同SKU，不能阻止同尺寸不同款新品」的回答判 PASS。"""
    correct_reply = (
        "库存反向约束口径（rules.md 3c）：库存只约束同款同 SKU，"
        "不能阻止同尺寸但不同款的新品入选。"
        "20寸库存多这一事实，只对国内仓中「完全相同款式 SKU 的候选品」产生约束，"
        "其他尺寸和其他款式的选品决策独立判断，当前阶段问什么答什么。"
    )
    passed, fails = _t45_content_oracle(correct_reply)
    assert passed, (
        f"修后 oracle 应对'不能阻止同尺寸不同款'表述判 PASS，实际 fails={fails}"
    )


def test_new_oracle_rejects_24inch_mandate():
    """修后：新 oracle 拒绝「20寸库存多 → 必须选24寸」的强制表述。"""
    wrong_reply = (
        "20寸库存多，本期选品必须优先考虑 24 寸产品，"
        "这样才能通过差异化规避同尺寸库存压力。"
    )
    passed, fails = _t45_content_oracle(wrong_reply)
    assert not passed, (
        "修后 oracle 应拒绝'20寸库存多→必须24寸'的强制表述，"
        f"但实际 passed=True，oracle 未检测到违规：fails={fails}"
    )
    assert any("24寸" in f or "扩展" in f or "口径" in f for f in fails), (
        f"fails 应提及强制24寸违反口径，实际: {fails}"
    )


def test_new_oracle_rejects_mandatory_expandable_layer():
    """修后：「20寸库存多 → 应优先推24寸+拓展层作为本期主推」被拒绝。

    这正是旧 T45 期望的「正确答案」格式，现在应被新 oracle 拒绝。
    """
    old_expected_answer = (
        "国内仓 20 寸行李箱库存多时，本期选品应优先24寸+拓展层，"
        "库存约束将20寸推向更高差异化的24寸。"
    )
    passed, fails = _t45_content_oracle(old_expected_answer)
    assert not passed, (
        "旧 T45 期望的'答案'（20寸库存多→优先24寸+拓展层）应被新 oracle 拒绝，"
        f"但实际 passed=True，说明新 oracle 未检测到错误，fails={fails}"
    )


def test_new_oracle_rejects_vague_reply_without_same_sku():
    """修后：模糊回答（不提同款同SKU口径）被拒绝，防止死代码短路（放宽到什么都过）。

    这是防「死代码短路」的核心案例：单纯删除「24寸」要求不够，
    还必须保留「同款同SKU」内容约束。
    """
    vague_reply = (
        "20寸库存多时，本期选品需要考虑库存约束因素，"
        "避免重复采购已有库存的产品类型。"
    )
    passed, fails = _t45_content_oracle(vague_reply)
    assert not passed, (
        "模糊回答（未表达同款同SKU口径）应被新 oracle 拒绝（防死代码短路），"
        f"但实际 passed=True，oracle 未检测到缺少'同款同SKU'表述：fails={fails}"
    )
    assert any("同款" in f for f in fails), (
        f"fails 应提及缺少'同款同SKU'表述，实际: {fails}"
    )


def test_new_oracle_passes_rule_3c_verbatim():
    """修后：直接引用 rules.md 3c 原文说明约束的回答 PASS。"""
    rule_3c_reply = (
        "rules.md 3c：库存数据仅对同款同 SKU 有约束力，"
        "不能阻止同尺寸同功能但不同款的新品入选。"
        "当前情境：20寸库存多，仅约束国内仓现有同款 SKU 的入选，"
        "其余候选品按正常评估流程处理。"
    )
    passed, fails = _t45_content_oracle(rule_3c_reply)
    assert passed, (
        f"直接引用 rules.md 3c 的回答应 PASS，实际 fails={fails}"
    )


# ── 洞1反例：只说「同款」不含「SKU」约束 → 必须 FAIL ────────────────────────────

def test_new_oracle_rejects_tongkuan_without_sku():
    """洞1反例：回答只说「同款」约束，但不提「同款同 SKU」口径 → 必须 FAIL。

    防宽松绕过：reply 仅说"只约束同款产品"，不含 SKU 字样，不满足 rules.md 3c 的
    精确口径「库存只约束同款同 SKU」。去掉这个约束等于让「答了一半」的回答通过。
    """
    reply_only_tongkuan = (
        "库存只约束同款产品，不同款的新品不受限制。"
        "本期选品要注意避免选择与国内仓同款的商品，但其他产品可正常入选。"
    )
    passed, fails = _t45_content_oracle(reply_only_tongkuan)
    assert not passed, (
        "回答只说'同款'约束但不含'同款同SKU'口径（无 SKU 字样）应被拒绝，"
        "防止宽松绕过 rules.md 3c 精确要求，但实际 passed=True，"
        f"oracle 未拦截'同款不含SKU'绕过：fails={fails}"
    )
    assert any("SKU" in f or "同款同" in f for f in fails), (
        f"fails 应提及缺少 SKU 约束，实际: {fails}"
    )


# ── 洞2修复：S1 证据门真实断言（不只是注释）──────────────────────────────────────

def test_s1_gate_blocks_claim_without_evidence():
    """洞2修复：S1 gate 对「声称查过库存/选品约束但无工具证据」的回答真实拦截。

    FAIL 条件（修前/无 S1 gate）：_s1_inventory_evidence_gate 返回空告警 → 叙述假活放行。
    PASS 条件（修后/S1 gate 实现）：返回非空告警 → 拦截，断言成功。

    验证：「根据完整规则/已查询」类声明 + 选品库存约束上下文 + 无工具证据 → 必须被拦截。
    """
    reply_claims_checked = (
        "根据已查询的完整库存数据，本期选品的库存约束结论是："
        "库存只约束同款同 SKU，20寸库存多不影响其他款选品。"
    )
    warns = _s1_inventory_evidence_gate(reply_claims_checked, tool_log=[])
    assert warns, (
        "S1 gate 应拦截「声称查过/根据完整数据」但无工具证据的选品库存约束声明，"
        f"但 warns 为空 — 叙述假活未被拦截"
    )
    assert any("S1" in w or "证据" in w or "工具" in w for w in warns), (
        f"告警内容应提及证据门/S1/工具，实际: {warns}"
    )


def test_gate_catches_yichaxun_claim_no_tool_log():
    """round-3 红队：含「根据已查询的完整库存数据」但无工具证据 → 必须拦截。"""
    reply = (
        "根据已查询的完整库存数据，库存只约束同款同 SKU，"
        "20寸库存多不影响其他款选品。"
    )
    warns = _s1_inventory_evidence_gate(reply, tool_log=[])
    assert warns, (
        "S1 gate 应拦截「根据已查询的完整库存数据」但无工具证据的声明，"
        "不得因为未出现连续的'库存约束'四字而放行"
    )


def test_gate_catches_yilaqu_claim_no_tool_log():
    """round-3 红队：含「已拉取库存数据」但无工具证据 → 必须拦截。"""
    reply = (
        "已拉取库存数据，结论：库存只约束同款同 SKU，"
        "20寸库存多不影响其他款。"
    )
    warns = _s1_inventory_evidence_gate(reply, tool_log=[])
    assert warns, (
        "S1 gate 应拦截「已拉取库存数据」但无工具证据的声明，"
        "不得因缺少选品/库存约束连续关键词而放行"
    )


def test_gate_catches_genjv_data_claim_no_tool_log():
    """round-3 红队：含「根据完整数据」但无工具证据 → 必须拦截。"""
    reply = (
        "根据完整数据，国内仓20寸库存多时，库存只约束同款同 SKU。"
    )
    warns = _s1_inventory_evidence_gate(reply, tool_log=[])
    assert warns, (
        "S1 gate 应拦截「根据完整数据」但无工具证据的库存判断声明"
    )


def test_s1_gate_allows_with_query_sku_evidence():
    """S1 gate 放行：有 query_sku 工具调用证据 → 不拦截。"""
    reply = (
        "库存反向约束分析：本期选品的库存约束只作用于同款同 SKU，"
        "根据查询结果，20寸库存多对其他款候选品无影响。"
    )
    tool_log = [
        {"name": "query_sku", "args": {"skus": ["SKU-20INCH"]}, "result_keys": ["sku", "stock"]}
    ]
    warns = _s1_inventory_evidence_gate(reply, tool_log)
    assert not warns, (
        f"有 query_sku 工具证据不应被 S1 gate 拦截，但 warns={warns}"
    )


def test_s1_gate_allows_with_list_products_limit_positive():
    """S1 gate 放行：list_products(limit>0) 调用 → 不拦截。"""
    reply = (
        "本期选品库存约束：查询结果显示同款同 SKU 约束适用，"
        "其余候选品不受20寸库存约束影响。"
    )
    tool_log = [
        {"name": "list_products", "args": {"store": "KSA", "limit": 10}, "result_keys": ["items"]}
    ]
    warns = _s1_inventory_evidence_gate(reply, tool_log)
    assert not warns, (
        f"list_products(limit=10) 证据不应被 S1 gate 拦截，但 warns={warns}"
    )


def test_s1_gate_blocks_list_products_limit_zero():
    """S1 gate 拦截：list_products(limit=0) 只返回聚合统计，不含商品级证据 → 拦截。"""
    reply = (
        "本期选品的库存约束：根据商品数据，20寸库存多只约束同款同 SKU 的入选。"
    )
    tool_log = [
        {"name": "list_products", "args": {"store": "KSA", "limit": 0}, "result_keys": ["total"]}
    ]
    warns = _s1_inventory_evidence_gate(reply, tool_log)
    assert warns, (
        "list_products(limit=0) 仅聚合统计，不含商品级证据，应被 S1 gate 拦截，"
        f"但 warns 为空"
    )


def test_s1_gate_allows_with_list_products_args_json_string():
    """S1 gate 放行：args 为 JSON-string 格式的 list_products(limit=10) → 正确解析，不拦截。"""
    reply = (
        "本期选品库存约束分析：同款同 SKU 约束适用，非同款候选品正常入选。"
    )
    tool_log = [
        {"name": "list_products", "args": '{"store": "KSA", "limit": 10}', "result_keys": ["items"]}
    ]
    warns = _s1_inventory_evidence_gate(reply, tool_log)
    assert not warns, (
        "args 为 JSON-string 的 list_products(limit=10) 应被正确解析，不被 S1 gate 拦截，"
        f"但 warns={warns}"
    )


def test_s1_gate_not_triggered_for_non_selection_context():
    """S1 gate 不误触发：普通补货/销量类回答（无选品库存约束上下文）→ 不拦截。"""
    replenish_reply = (
        "KSA 当前补货建议：TBJ0059A 建议补 50 件，urgency=high。"
        "库存只有 30 件，销售速度快，预计 10 天内断货。"
    )
    warns = _s1_inventory_evidence_gate(replenish_reply, tool_log=[])
    assert not warns, (
        "补货建议类回答（无'本期选品'/'库存约束'上下文）不应触发 S1 gate，"
        f"但 warns={warns}"
    )


# ── S2 与 S1 独立性确认 ────────────────────────────────────────────────────────

def test_s2_oracle_and_s1_gate_are_independent():
    """S2 content oracle 与 S1 evidence gate 独立，各管各的。

    - 内容正确 + 无工具证据：S2 PASS，S1 FAIL（两层都需要才完整通过）
    - 内容错误 + 有工具证据：S2 FAIL，S1 PASS
    """
    # 内容正确，但无工具证据
    correct_content_no_evidence = (
        "本期选品的库存约束（根据规则）：库存只约束同款同 SKU，"
        "20寸库存多不影响其他款候选品。"
    )
    s2_passed, s2_fails = _t45_content_oracle(correct_content_no_evidence)
    s1_warns = _s1_inventory_evidence_gate(correct_content_no_evidence, tool_log=[])

    assert s2_passed, (
        f"S2 oracle 应对内容正确的回答判 PASS（不管工具证据），实际 fails={s2_fails}"
    )
    assert s1_warns, (
        "S1 gate 应拦截无工具证据的选品库存约束声明（即使内容说对了）"
    )

    # 内容错误，但有工具证据
    wrong_content_has_evidence = (
        "本期选品库存约束分析：20寸库存多，本期选品应优先考虑24寸产品，"
        "通过差异化规避库存压力。"
    )
    tool_log = [{"name": "query_sku", "args": {}, "result_keys": ["stock"]}]
    s2_passed2, s2_fails2 = _t45_content_oracle(wrong_content_has_evidence)
    s1_warns2 = _s1_inventory_evidence_gate(wrong_content_has_evidence, tool_log)

    assert not s2_passed2, (
        f"S2 oracle 应拒绝'20寸→必须24寸'内容，即使有工具证据，实际 fails={s2_fails2}"
    )
    assert not s1_warns2, (
        f"S1 gate 应放行有 query_sku 证据的回答，实际 warns={s1_warns2}"
    )


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_t45_inventory_oracle — T45-S2a 同款同SKU口径内容 oracle")

    tests = [
        ("test_old_oracle_rejects_correct_reply",
         test_old_oracle_rejects_correct_reply),
        ("test_old_oracle_passes_wrong_reply",
         test_old_oracle_passes_wrong_reply),
        ("test_new_oracle_passes_correct_reply_same_sku",
         test_new_oracle_passes_correct_reply_same_sku),
        ("test_new_oracle_passes_correct_reply_no_overexpand",
         test_new_oracle_passes_correct_reply_no_overexpand),
        ("test_new_oracle_rejects_24inch_mandate",
         test_new_oracle_rejects_24inch_mandate),
        ("test_new_oracle_rejects_mandatory_expandable_layer",
         test_new_oracle_rejects_mandatory_expandable_layer),
        ("test_new_oracle_rejects_vague_reply_without_same_sku",
         test_new_oracle_rejects_vague_reply_without_same_sku),
        ("test_new_oracle_passes_rule_3c_verbatim",
         test_new_oracle_passes_rule_3c_verbatim),
        ("test_new_oracle_rejects_tongkuan_without_sku",
         test_new_oracle_rejects_tongkuan_without_sku),
        ("test_s1_gate_blocks_claim_without_evidence",
         test_s1_gate_blocks_claim_without_evidence),
        ("test_gate_catches_yichaxun_claim_no_tool_log",
         test_gate_catches_yichaxun_claim_no_tool_log),
        ("test_gate_catches_yilaqu_claim_no_tool_log",
         test_gate_catches_yilaqu_claim_no_tool_log),
        ("test_gate_catches_genjv_data_claim_no_tool_log",
         test_gate_catches_genjv_data_claim_no_tool_log),
        ("test_s1_gate_allows_with_query_sku_evidence",
         test_s1_gate_allows_with_query_sku_evidence),
        ("test_s1_gate_allows_with_list_products_limit_positive",
         test_s1_gate_allows_with_list_products_limit_positive),
        ("test_s1_gate_blocks_list_products_limit_zero",
         test_s1_gate_blocks_list_products_limit_zero),
        ("test_s1_gate_allows_with_list_products_args_json_string",
         test_s1_gate_allows_with_list_products_args_json_string),
        ("test_s1_gate_not_triggered_for_non_selection_context",
         test_s1_gate_not_triggered_for_non_selection_context),
        ("test_s2_oracle_and_s1_gate_are_independent",
         test_s2_oracle_and_s1_gate_are_independent),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            traceback.print_exc()
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_t45_inventory_oracle all {len(tests)} passed")

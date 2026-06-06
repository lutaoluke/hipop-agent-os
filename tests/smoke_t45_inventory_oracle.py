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
  答案只约束同款同 SKU，不强制「20寸库存多 → 必须24寸+拓展层」，
  当前阶段问什么答什么，不过度发散。

FAIL（修前 oracle）：
  - 旧 oracle 要求回答必须出现「24寸」+「拓展层」 → 正确答案被误判 FAIL
  - 旧 oracle 把「规则层面错误」的内容（强制24寸）反而视为正确

PASS（修后 oracle）：
  - 新 oracle 检查「同款同SKU」内容约束：reply 须明确表达同款同SKU口径
  - 新 oracle 拒绝「20寸库存多 → 必须/应优先24寸」的扩展表述
  - 不削弱 S1 证据门：content oracle 只管答案内容口径，不管工具调用证据

三死法：
  - 接线缺失：只改描述，oracle 实际仍按旧逻辑判定 → 用确定性 keyword 断言，不靠 LLM
  - 死代码短路：去掉「24寸」硬要求时放宽到任何泛泛回答都过 → 保留「同款同SKU」内容约束
  - 占位假数据：用固定字符串骗过 oracle → oracle 用 regex/keyword 真实匹配，反例有覆盖

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


# ── T45 内容 oracle（S2 确定性规则）────────────────────────────────────────────

# 错误表述模式：将「20寸库存多」硬扩展为「必须/应优先选24寸」的说法。
# Luke sign-off: 这类表述违反 rules.md 3c（库存只约束同款同 SKU）。
_WRONG_24INCH_MANDATE_RE = re.compile(
    r"20\s*寸.{0,25}(必须|本期.*选|应优先选|主推|引导.*选|推荐.*主).{0,20}24\s*寸"
    r"|20\s*寸.*库存.{0,20}(必须|应|要).{0,20}24\s*寸"
    r"|库存多.{0,20}(必须|应优先|主推).{0,20}24\s*寸"
)

# 正确表述模式：明确说明库存约束只作用于「同款同SKU」。
# 允许多种等价表述：「同款同SKU」「同款同 SKU」「只约束同款」+「同SKU」等。
_SAME_SKU_CONSTRAINT_RE = re.compile(
    r"同款同\s*SKU"
    r"|同款.{0,10}同.{0,5}SKU"
    r"|库存.{0,15}只.{0,10}约束.{0,10}同款"
    r"|约束.{0,10}同款.{0,10}SKU",
    re.IGNORECASE
)


def _t45_content_oracle(reply: str) -> tuple[bool, list[str]]:
    """T45 同款同SKU内容口径 oracle（S2 确定性规则）。

    通过条件（AND）：
      1. 明确表达库存约束只作用于同款同SKU（_SAME_SKU_CONSTRAINT_RE 匹配）
      2. 不包含「20寸库存多 → 必须/应优先选24寸」的扩展性强制表述

    返回 (passed, fail_reasons)。
    """
    fails = []

    # 检查错误扩展：「20寸库存多 → 必须/优先24寸」
    if _WRONG_24INCH_MANDATE_RE.search(reply):
        fails.append(
            "reply 将20寸库存多硬扩展为'必须/应优先24寸'，违反 rules.md 3c 同款同SKU口径"
        )

    # 检查必须表达「同款同SKU」约束口径
    if not _SAME_SKU_CONSTRAINT_RE.search(reply):
        fails.append(
            "reply 未表达'同款同SKU'约束口径（rules.md 3c 核心：库存只约束同款同SKU）"
        )

    return (len(fails) == 0), fails


# ── fail-then-pass 演示：旧 oracle 误判 ─────────────────────────────────────────

def test_old_oracle_rejects_correct_reply():
    """fail-then-pass 演示（修前）：旧 oracle 要求「24寸+拓展层」，正确 Agent 回答被误判 FAIL。

    旧 T45 期望：expectation = "Should mention 20寸库存约束选择24寸+拓展层"
    → 旧 oracle 检查「24寸」+「拓展层」是否都在 reply 里。
    → 正确的 Agent 回答（遵循 rules.md 3c，讲同款同SKU）不包含「24寸+拓展层」
    → 旧 oracle 对正确回答判 FAIL → 旧期望本身有误。

    本 test 故意让旧 oracle 对正确回答判 FAIL，证明旧期望需要校正。
    （如果旧 oracle 对正确回答判 PASS，这个 test 本身会失败，提醒旧期望未被修正）
    """
    # 这是一个正确的 Agent 回答：遵守 rules.md 3c，不强制24寸
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
        "旧 oracle 本应对正确回答判 FAIL（正确回答不含'24寸+拓展层'）"
        "但此处 old_oracle_passes=True，说明测试用的'正确回答'本身有问题"
    )
    # ← 这里断言成功：old_oracle_passes is False → 旧 oracle 误判，需要校正


def test_old_oracle_passes_wrong_reply():
    """修前演示：旧 oracle 对错误的强制「24寸+拓展层」回答判 PASS。

    旧期望接受「20寸库存多 → 必须选24寸+拓展层」作为正确答案，
    但这违反 rules.md 3c，Luke 已明确说此方向不对。
    """
    # 旧期望下「正确」的回答（但业务上是错误的）
    wrong_reply = (
        "国内仓 20 寸行李箱库存多，本期选品应优先考虑24寸+拓展层。"
        "这样可以通过差异化来避开库存压力，引导消费者选择更高价的产品。"
    )

    # 旧 oracle：包含「24寸」AND「拓展层」
    old_oracle_passes = "24寸" in wrong_reply and "拓展层" in wrong_reply

    # 旧 oracle 对错误回答判 PASS
    assert old_oracle_passes, (
        "旧 oracle 本应对包含'24寸+拓展层'的回答判 PASS，"
        "此处失败说明旧 oracle 逻辑有误"
    )
    # ← 这里断言成功：旧 oracle 把错误业务口径当成正确答案接受了


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
    """修后：说明「只约束同款，不能阻止同尺寸不同款新品」的回答判 PASS。"""
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
    assert any("24寸" in f or "mandate" in f.lower() or "扩展" in f or "口径" in f for f in fails), (
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
    assert any("同款同SKU" in f or "同款" in f for f in fails), (
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


# ── S1 证据门不被削弱的回归断言（确认 S2 oracle 不替代 S1）──────────────────────

def test_correct_content_but_no_tool_evidence_still_needs_s1_gate():
    """S1 证据门不被 S2 oracle 替代：内容正确但无工具证据仍需 S1 拦截。

    S2 content oracle 只管答案内容口径，不管工具调用证据。
    如果 Agent 内容说对了（同款同SKU）但没有 list_products/query_sku 工具调用，
    S1 证据门应继续拦截。S2 oracle 判 PASS 不意味着整体通过。

    本 test 仅断言 S2 oracle 对「内容正确但无证据」的回答判 PASS，
    同时注明 S1 gate 仍需独立检查（在 smoke_t45_evidence_gate.py 中验证）。
    """
    correct_content_no_evidence = (
        "根据 rules.md 3c，库存只约束同款同 SKU。"
        "20寸库存多不能阻止不同款新品，按题面回答即可。"
    )
    # S2 content oracle 对内容判断
    passed, fails = _t45_content_oracle(correct_content_no_evidence)
    assert passed, (
        "S2 oracle 应对内容正确的回答判 PASS（即使没有工具证据），"
        f"S1 gate 独立处理工具证据，两者分开。实际 fails={fails}"
    )
    # ← 注：此回答的完整评估还需通过 S1 证据门
    #   S1 gate 实现：smoke_t45_evidence_gate.py（WS-108）
    #   S2 oracle 不削弱 S1，S1 不替代 S2，两层独立。


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
        ("test_correct_content_but_no_tool_evidence_still_needs_s1_gate",
         test_correct_content_but_no_tool_evidence_still_needs_s1_gate),
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

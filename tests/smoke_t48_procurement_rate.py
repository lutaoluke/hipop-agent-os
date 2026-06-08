"""smoke_t48_procurement_rate.py — T48 采购议价率公式与plus折扣口径 oracle fail-then-pass smoke

验收（WS-117）：
  T48 题面：「请说明采购议价率怎么计算，plus 折扣是否计入绩效。」
  Luke 已在 WS-94/2026-06-08 sign-off：
    - 采购议价率正确口径：议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%
    - 绩效阈值：< 3% 不合格；≥ 6% 正常（阈值样例）
    - plus 折扣不计入采购议价折扣/绩效
    - 旧"1688 标价差价 ÷ 1688 标价 ≥ 15% 合格线"为错误口径，已废止

旧 T48 Agent 实际回答（WRONG）：
  "议价率 = (1688 标价 - 实际成交价) ÷ 1688 标价 × 100%；规则要求议价率 ≥ 15% 才算合格"
  → 公式错（分母未含头程运费）；合格线 15% 完全错误；阈值与实际差一个数量级

新 T48 期望（CORRECT）：
  - 回答说明议价率公式时使用业务名称（议价差额、1688采购标准价、头程运费分摊），不裸写字母
  - 回答包含 3% 或 6% 阈值样例
  - 明确说明 plus 折扣不计入采购议价率/绩效
  - 不出现"≥ 15% 合格"或"15% 才算合格"之类的旧错误口径

FAIL（旧 oracle / 旧Agent）：
  - 旧 oracle 未检查 plus 折扣口径，任何"15% 合格"的回答都能通过
  - 正确回答（含 3%/6% 阈值 + plus 不计入）反而被旧 oracle 判 FAIL（缺 15% 关键词）

PASS（新 oracle）：
  - 包含"议价差额"/"议价省"等业务语义词（而非单纯字母 H/(F+G)）
  - 包含 3% 或 6% 阈值
  - 明确"plus 折扣不计入"
  - 不包含"≥ 15%合格"/"15% 才算合格"/"15%才算"等旧口径

三死法：
  - 接线缺失：NOTES.md 写了规则但 agent 仍优先召回旧 NOTES/hallucinated rules
    → 用确定性 keyword 断言，不依赖 LLM 判断
  - 死代码短路：oracle 只断 plus 不断公式+3%/6%，漏掉本次失败核心
    → 所有关键点都独立断言，缺任一即 FAIL
  - 占位假数据：回答只写字母 H/(F+G) 但不给业务解释，或 3%/6% 阈值未提及
    → oracle 检查业务语义词，不接受纯字母公式作为完整答案

跑法：
  python3 tests/smoke_t48_procurement_rate.py
  make test-one F=tests/smoke_t48_procurement_rate.py
  （也被 make test 自动聚合）
"""
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── T48 内容 oracle（确定性规则）──────────────────────────────────────────────

# 旧错误口径：回答包含"≥15% 合格"/"15% 才算合格"/"15%合格线"等
# Luke sign-off：这是 rules.md §8 旧内控口径，已废止，不适用于采购议价绩效
_OLD_15PCT_PASS_RE = re.compile(
    r"≥\s*15\s*%\s*(才算|算|为)?合格"
    r"|15\s*%\s*才算合格"
    r"|议价率\s*[≥>＞]\s*15\s*%"
    r"|合格\s*线\s*[：:]\s*15\s*%"
    r"|15\s*%.*合格线"
    r"|合格.*15\s*%"
)

# 新正确口径：
# 1. 公式包含业务语义词（"议价差额"/"议价省"/"省下"/"谈判省" + "头程"/"运费"）
_FORMULA_SEMANTIC_RE = re.compile(
    r"议价差额"
    r"|议价省"
    r"|省下.{0,10}金额"
    r"|谈判.{0,10}省"
    r"|1688.{0,15}价.{0,10}÷|1688.{0,15}价.{0,10}/"
    r"|差额.{0,10}[÷/].{0,20}运费"
    r"|头程.{0,15}运费",
    re.IGNORECASE
)

# 2. 包含 3% 或 6% 阈值
_THRESHOLD_RE = re.compile(
    r"[36]\s*%"
    r"|3\s*%\s*.{0,10}不合格"
    r"|6\s*%\s*.{0,10}正常"
    r"|不合格.{0,10}3\s*%"
    r"|正常.{0,10}6\s*%"
)

# 3. 明确说明 plus 折扣不计入
_PLUS_NOT_COUNTED_RE = re.compile(
    r"plus\s*折扣.{0,20}不计入"
    r"|plus.{0,10}不.{0,15}计入"
    r"|不计入.{0,15}plus"
    r"|plus.{0,20}不.{0,5}算"
    r"|plus.{0,20}不.{0,5}属于.{0,10}采购"
    r"|plus\s*折扣.{0,15}运营",
    re.IGNORECASE
)


def _t48_content_oracle(reply: str) -> tuple[bool, list[str]]:
    """T48 采购议价率公式与plus折扣口径 oracle（确定性规则）。

    通过条件（AND）：
      1. 不包含旧的"≥15% 合格"错误口径
      2. 包含业务语义词（议价差额/头程运费等），有实质公式解释
      3. 包含 3% 或 6% 阈值
      4. 明确说明 plus 折扣不计入采购议价率/绩效

    返回 (passed, fail_reasons)。
    """
    fails = []

    # 旧错误口径检查
    if _OLD_15PCT_PASS_RE.search(reply):
        fails.append(
            "reply 包含旧错误口径'≥15%合格线'，已废止；正确阈值为3%/6%"
        )

    # 业务语义词检查（防止只写字母 H/(F+G) 不解释）
    if not _FORMULA_SEMANTIC_RE.search(reply):
        fails.append(
            "reply 未包含采购议价率的业务语义词（如'议价差额'、'头程运费'等）；"
            "不得仅写字母 H/(F+G) 而不给业务名称解释"
        )

    # 阈值检查（3% 或 6%）
    if not _THRESHOLD_RE.search(reply):
        fails.append(
            "reply 未包含采购议价率阈值样例（3% 不合格 / 6% 正常），"
            "正确口径须含阈值示例"
        )

    # plus 折扣不计入检查
    if not _PLUS_NOT_COUNTED_RE.search(reply):
        fails.append(
            "reply 未明确说明'plus 折扣不计入采购议价率/绩效'，"
            "这是 Luke sign-off 的核心口径之一"
        )

    return (len(fails) == 0), fails


# ── fail-then-pass 演示：旧 Agent 回答 ───────────────────────────────────────

def test_old_agent_reply_fails_new_oracle():
    """fail-then-pass 演示（修前）：旧 Agent 的实际回答应被新 oracle 判 FAIL。

    旧 Agent 引用"rules.md §8（采购内控）"，回答：
      - 议价率 = (1688 标价 - 实际成交价) ÷ 1688 标价 × 100%
      - 合格线 ≥ 15%
    → 应被新 oracle 拒绝（包含旧错误口径 + 缺少 3%/6% 阈值）
    """
    old_agent_reply = (
        "根据 rules.md §8（采购内控）规则：\n"
        "议价率 = (1688 标价 - 实际成交价) ÷ 1688 标价 × 100%\n"
        "规则要求：议价率 ≥ 15% 才算合格。低于 15% 需备注理由。\n"
        "plus 折扣不计入绩效，属于运营/营销费用。"
    )
    passed, fails = _t48_content_oracle(old_agent_reply)
    assert not passed, (
        "旧 Agent 的实际回答（含'≥15% 才算合格'）应被新 oracle 判 FAIL，"
        f"但 passed=True，oracle 未检测到旧错误口径；fails={fails}"
    )
    assert any("15" in f for f in fails), (
        f"fails 应提及旧'≥15%合格'口径，实际: {fails}"
    )


def test_old_oracle_would_pass_old_reply():
    """修前演示：旧 oracle（如只查"plus 不计入"）对旧 Agent 回答判 PASS。

    旧期望只检查 plus 折扣那段，不检查公式和阈值 → 旧 Agent 的错误回答可通过。
    """
    old_agent_reply = (
        "议价率 = (1688 标价 - 实际成交价) ÷ 1688 标价 × 100%\n"
        "合格线 ≥ 15%。\n"
        "plus 折扣不计入绩效。"
    )

    # 旧 oracle 只检查"plus 不计入"
    old_oracle_passes = "plus" in old_agent_reply and "不计入" in old_agent_reply

    assert old_oracle_passes, (
        "旧 oracle（只检查 plus 不计入）应对旧 Agent 回答判 PASS，"
        "此处失败说明旧 oracle 逻辑有误"
    )


# ── 新 oracle 测试（修后 PASS）─────────────────────────────────────────────────

def test_new_oracle_passes_correct_reply_full():
    """修后：完整正确回答（公式+阈值+plus口径）应判 PASS。"""
    correct_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%\n"
        "  - 议价差额 = 1688采购标准价 − 实际成交采购价\n"
        "  - 阈值样例：< 3% 不合格；≥ 6% 正常\n"
        "plus 折扣不计入采购议价率，noon 平台 plus 属于运营/营销费用，"
        "不属于采购端议价绩效。"
    )
    passed, fails = _t48_content_oracle(correct_reply)
    assert passed, (
        f"完整正确回答应 PASS，实际 fails={fails}"
    )


def test_new_oracle_passes_correct_reply_natural_language():
    """修后：自然语言表达的正确回答（包含所有要素）应 PASS。"""
    correct_reply = (
        "采购议价率衡量的是采购团队通过 1688 谈判省下多少钱。\n"
        "计算方式：先算议价差额（1688采购标准价减去实际成交价），"
        "再除以总成本基数（1688标准价加上头程运费分摊），乘 100%。\n"
        "实际标准是 3% 以下不合格，6% 以上算正常。\n"
        "noon 的 plus 折扣不计入采购议价绩效——这是平台促销，归运营侧管，"
        "采购只看自己和供应商谈的那部分。"
    )
    passed, fails = _t48_content_oracle(correct_reply)
    assert passed, (
        f"自然语言正确回答应 PASS，实际 fails={fails}"
    )


def test_new_oracle_rejects_old_15pct_threshold():
    """修后：含'≥15% 合格'旧口径的回答被拒绝。"""
    wrong_reply = (
        "采购议价率 = (1688 标价 - 采购单价) ÷ 1688 标价 × 100%\n"
        "合格线：议价率 ≥ 15% 才算合格，低于 15% 需备注。\n"
        "plus 折扣不计入绩效。"
    )
    passed, fails = _t48_content_oracle(wrong_reply)
    assert not passed, (
        "含'≥15%才算合格'旧口径的回答应被 oracle 拒绝，"
        f"但 passed=True，oracle 未检测到旧错误口径；fails={fails}"
    )
    assert any("15" in f for f in fails), (
        f"fails 应提及'15%'旧口径，实际: {fails}"
    )


def test_new_oracle_rejects_missing_threshold():
    """修后：缺少 3%/6% 阈值的回答被拒绝（防死代码短路只写公式不写阈值）。"""
    no_threshold_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%\n"
        "议价差额 = 1688采购标准价 − 实际成交价。\n"
        "plus 折扣不计入采购议价绩效。"
    )
    passed, fails = _t48_content_oracle(no_threshold_reply)
    assert not passed, (
        "缺少 3%/6% 阈值的回答应被拒绝（防死代码短路），"
        f"但 passed=True；fails={fails}"
    )
    assert any("阈值" in f or "3%" in f or "6%" in f for f in fails), (
        f"fails 应提及缺少阈值，实际: {fails}"
    )


def test_new_oracle_rejects_missing_plus_clause():
    """修后：缺少 plus 折扣口径说明的回答被拒绝（防占位假数据）。"""
    no_plus_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%\n"
        "议价差额 = 1688采购标准价 − 实际成交价。\n"
        "阈值：< 3% 不合格，≥ 6% 正常。"
    )
    passed, fails = _t48_content_oracle(no_plus_reply)
    assert not passed, (
        "缺少 plus 折扣口径说明的回答应被拒绝，"
        f"但 passed=True；fails={fails}"
    )
    assert any("plus" in f for f in fails), (
        f"fails 应提及缺少 plus 说明，实际: {fails}"
    )


def test_new_oracle_rejects_letter_formula_only():
    """修后：只写字母 H/(F+G) 不给业务解释的回答被拒绝（防占位假数据）。

    死法之一：oracle 接受 H/(F+G) 裸字母作为完整答案，但 Agent 实际上没有解释含义。
    """
    letter_only_reply = (
        "采购议价率公式为 H/(F+G)，阈值 3% 不合格，6% 正常。\n"
        "plus 折扣不计入绩效，属于运营侧费用。"
    )
    passed, fails = _t48_content_oracle(letter_only_reply)
    assert not passed, (
        "只写字母 H/(F+G) 但不给业务解释的回答应被拒绝，"
        "防止 Agent 把占位符当完整答案；"
        f"但 passed=True；fails={fails}"
    )
    assert any("业务" in f or "语义" in f or "差额" in f or "议价" in f for f in fails), (
        f"fails 应提及缺少业务语义词，实际: {fails}"
    )


def test_new_oracle_rejects_vague_reply_no_semantics():
    """修后：模糊回答（无公式、无阈值、无 plus 说明）全部被拒绝（防宽松绕过）。"""
    vague_reply = (
        "采购议价率的计算方式与 1688 平台的价格有关，"
        "建议每次采购记录实际成交价格以便统计绩效。"
    )
    passed, fails = _t48_content_oracle(vague_reply)
    assert not passed, (
        "模糊回答（无公式、无阈值、无 plus 说明）应被拒绝，"
        f"但 passed=True；fails={fails}"
    )
    assert len(fails) >= 2, (
        f"模糊回答应触发多个 fails，实际只有 {len(fails)} 个：{fails}"
    )


def test_new_oracle_rejects_15pct_even_with_plus_correct():
    """修后：包含旧 15% 口径的回答即使 plus 部分正确也被拒绝。

    防止'plus 那段说对了'掩盖'公式/阈值完全错误'的问题。
    """
    mixed_reply = (
        "采购议价率合格线是 ≥ 15%，低于 15% 需备注。\n"
        "plus 折扣不计入采购议价绩效，属于运营侧。\n"
        "阈值：6% 时算正常，3% 以下不合格。"
    )
    passed, fails = _t48_content_oracle(mixed_reply)
    assert not passed, (
        "含旧'≥15%合格'口径的回答即使 plus 说对了也应被拒绝，"
        f"但 passed=True；fails={fails}"
    )
    assert any("15" in f for f in fails), (
        f"fails 应提及旧'≥15%'口径，实际: {fails}"
    )


# ── NOTES.md 规则存在性校验 ──────────────────────────────────────────────────

def test_notes_md_procurement_rule_exists():
    """校验 ~/hipop/tenants/1/NOTES.md 已包含采购议价率规则（规则源已沉淀）。

    这是接线检查：规则源存在 + 内容正确 → 才能进一步验证 Agent 能检索到正确答案。
    若文件不存在或缺少关键词，说明规则未沉淀，接线风险高。
    """
    import os
    notes_path = Path(os.path.expanduser("~/hipop/tenants/1/NOTES.md"))
    assert notes_path.exists(), (
        f"NOTES.md 不存在于 {notes_path}，采购议价率规则未沉淀，接线风险高"
    )
    content = notes_path.read_text(encoding="utf-8")
    assert "议价差额" in content, (
        "NOTES.md 应包含'议价差额'，当前缺少，规则语义不完整"
    )
    assert "头程运费" in content, (
        "NOTES.md 应包含'头程运费'，当前缺少，公式分母未定义"
    )
    assert any(t in content for t in ["3%", "3 %", "< 3", "<3"]), (
        "NOTES.md 应包含 3% 阈值，当前缺少"
    )
    assert any(t in content for t in ["6%", "6 %", "≥ 6", "≥6"]), (
        "NOTES.md 应包含 6% 阈值，当前缺少"
    )
    assert "plus" in content.lower(), (
        "NOTES.md 应包含 plus 折扣不计入说明"
    )
    assert "15%" not in content or "废止" in content or "错误" in content, (
        "NOTES.md 若含 15% 必须附注'废止'或'错误'，不得裸写旧合格线"
    )


# ── NOTES.md 内容 oracle（防旧口径污染）────────────────────────────────────────

def test_notes_md_does_not_contain_old_15pct_rule():
    """校验 NOTES.md 未将旧 15% 合格线作为有效规则记录。

    若 NOTES.md 包含"≥15% 合格"未附废止标注，代表旧口径仍活在规则源 → 接线缺失风险。
    """
    import os
    notes_path = Path(os.path.expanduser("~/hipop/tenants/1/NOTES.md"))
    if not notes_path.exists():
        return  # test_notes_md_procurement_rule_exists 已断言存在性

    content = notes_path.read_text(encoding="utf-8")
    # 若含"15%"或"15 %"，周围必须有废止/错误说明
    if re.search(r"15\s*%", content):
        assert re.search(r"(废止|错误|wrong|deprecated)", content, re.IGNORECASE), (
            "NOTES.md 含 15% 但未注明废止/错误，旧口径可能污染 Agent 召回"
        )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("▶ smoke_t48_procurement_rate — T48 采购议价率公式与plus折扣口径 oracle")

    tests = [
        ("test_old_agent_reply_fails_new_oracle",
         test_old_agent_reply_fails_new_oracle),
        ("test_old_oracle_would_pass_old_reply",
         test_old_oracle_would_pass_old_reply),
        ("test_new_oracle_passes_correct_reply_full",
         test_new_oracle_passes_correct_reply_full),
        ("test_new_oracle_passes_correct_reply_natural_language",
         test_new_oracle_passes_correct_reply_natural_language),
        ("test_new_oracle_rejects_old_15pct_threshold",
         test_new_oracle_rejects_old_15pct_threshold),
        ("test_new_oracle_rejects_missing_threshold",
         test_new_oracle_rejects_missing_threshold),
        ("test_new_oracle_rejects_missing_plus_clause",
         test_new_oracle_rejects_missing_plus_clause),
        ("test_new_oracle_rejects_letter_formula_only",
         test_new_oracle_rejects_letter_formula_only),
        ("test_new_oracle_rejects_vague_reply_no_semantics",
         test_new_oracle_rejects_vague_reply_no_semantics),
        ("test_new_oracle_rejects_15pct_even_with_plus_correct",
         test_new_oracle_rejects_15pct_even_with_plus_correct),
        ("test_notes_md_procurement_rule_exists",
         test_notes_md_procurement_rule_exists),
        ("test_notes_md_does_not_contain_old_15pct_rule",
         test_notes_md_does_not_contain_old_15pct_rule),
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
    print(f"\n✓ smoke_t48_procurement_rate all {len(tests)} passed")

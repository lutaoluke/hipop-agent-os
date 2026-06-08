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
  - 不出现"≥ 15% 合格"/"15% 才算合格"/"大于 15%"/"高于 15% 才达标"等旧/错误口径
  - 不出现"头程运费不进入分母"等错误公式描述
  - 不出现"plus 先计入...后续扣减"等错误 plus 处理描述

FAIL（旧 oracle / 旧Agent）：
  - 旧 oracle 未检查 plus 折扣口径，任何"15% 合格"的回答都能通过
  - 旧 oracle 不识别"大于 15%"/"高于 15% 才达标"等绕过变种（only ≥/> 才算合格形式）
  - 旧 oracle 不识别"头程运费另看，不进入分母"等错误分母写法（只要有"头程运费"词汇就通过）
  - 旧 oracle 的 plus 检查会被"不是完全不计入 plus"等子串绑过

PASS（新 oracle）：
  - 包含"议价差额"/"议价省"等业务语义词（而非单纯字母 H/(F+G)）
  - 头程运费分摊在分母中（不能说"头程运费不进入分母/另看"）
  - 包含 3% 或 6% 阈值
  - 不包含任何 15% 作为议价率合格/达标阈值（包括大于/高于/超过/≥/> 等）
  - plus 折扣完全不计入（不接受"先计入后扣减"等部分计入描述）

三死法：
  - 接线缺失：NOTES.md 写了规则但 agent 仍优先召回旧 NOTES/hallucinated rules
    → 用确定性 keyword 断言，不依赖 LLM 判断；权威规则源在 hipop/rules/procurement_rate.py
  - 死代码短路：oracle 只断 plus 不断公式+3%/6%，漏掉本次失败核心
    → 所有关键点都独立断言，缺任一即 FAIL；新增绕过样例 fail-then-pass 钉死
  - 占位假数据：回答只写字母 H/(F+G) 但不给业务解释，或 3%/6% 阈值未提及
    → oracle 检查业务语义词，不接受纯字母公式作为完整答案

跑法：
  python3 tests/smoke_t48_procurement_rate.py
  make test-one F=tests/smoke_t48_procurement_rate.py
  （也被 make test 自动聚合）
"""
import os
import re
import sys
import tempfile
import traceback
import unittest.mock
from pathlib import Path

# ── CI-safe env setup（必须在所有 hipop import 之前执行）────────────────────────
# chat() 集成测试需要 SQLite DB + JWT_SECRET；pop DB_URL 确保不走 PG
os.environ.pop("DB_URL", None)
os.environ.setdefault("JWT_SECRET", "smoke_t48_test_secret")
_tmp_db = tempfile.NamedTemporaryFile(suffix="_t48_smoke.db", delete=False)
os.environ.setdefault("HIPOP_DB", _tmp_db.name)
_tmp_db.close()

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ── T48 内容 oracle（确定性规则）──────────────────────────────────────────────

# 旧错误口径 / 绕过变种（任一匹配 → FAIL）：
#   - "≥15% 合格" / "15% 才算合格" 等经典形式
#   - "大于 15%" / "高于 15% 才达标" 等"大于/高于"绕过变种（验门人 WS-117 实测绕过）
#   - "超过 15%"（同语义）
_OLD_15PCT_PASS_RE = re.compile(
    r"≥\s*15\s*[%％]\s*(才算|算|为)?合格"
    r"|15\s*[%％]\s*才算合格"
    r"|议价率\s*[≥>＞]\s*15\s*[%％]"
    r"|合格\s*线\s*[：:]\s*15\s*[%％]"
    r"|15\s*[%％].*合格线"
    r"|合格.*15\s*[%％]"
    # ↓ 新增：绕过变种（验门人实测 passed=True，修后必须拒绝）
    r"|必须大于\s*15\s*[%％]"
    r"|大于\s*15\s*[%％].{0,10}(才|低于|要)"
    r"|高于\s*15\s*[%％].{0,10}(才达标|达标|合格|才算)"
    r"|超过\s*15\s*[%％].{0,10}(才|才算|才达标|合格)"
    r"|15\s*[%％].{0,5}才达标"
    r"|达标.{0,10}15\s*[%％]"
    # ↓ 新增：不低于/不能低于/最低/至少 + 15%/15个点（验门人 round-3 实测绕过）
    r"|不能?低于\s*15\s*([%％]|个点|个)"
    r"|最低.{0,8}15\s*(个点|[%％]|才)"
    r"|至少.{0,5}15\s*([%％]|个点|个)"
    r"|15\s*个点.{0,15}(才算|过线|合格|达标)"
    # ↓ 新增：达到/以上/个百分点/中文十五 + 15% 阈值（验门人 round-4 实测绕过）
    r"|达到\s*15\s*([%％]|个百分点|百分点|个点)"
    r"|15\s*[%％]\s*以上.{0,5}(达标|合格|正常)"
    r"|十五.{0,5}(个点|个百分点|百分点|[%％]|以上|才达|才算|过线|合格|达标)"
    # ↓ 新增：全角 ％ 变体 + 15个百分点以上才正常（验门人 round-5 实测绕过）
    r"|达到\s*15\s*[%％]"
    r"|15\s*(个)?百分点.{0,15}(以上|才).{0,5}(正常|合格|达标)"
    # ↓ 新增：达15%即合格 变体（验门人 round-10 实测绕过 #28）
    r"|达\s*15\s*[%％]\s*(就|即|可以?|为)?.{0,3}(合格|达标|正常|过线)"
)

# 公式业务语义词（出现任一 → 满足公式描述要求）
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

# 正确分母描述：公式的分母/计算基数必须正向包含 1688采购标准价 + 头程运费分摊。
# 不能只靠"出现头程运费"或枚举坏话术；缺任一 denominator component 即 FAIL。
_DENOM_1688 = r"1688.{0,12}(?:标准价|标价|参考价)"
_DENOM_JOIN = r"(?:\+|＋|加上?|和|及|与|连同)"
_DENOM_FREIGHT = r"头程.{0,12}运费.{0,8}分摊"
_CORRECT_DENOMINATOR_PAIR = (
    rf"(?:{_DENOM_1688}.{{0,18}}{_DENOM_JOIN}.{{0,18}}{_DENOM_FREIGHT}"
    rf"|{_DENOM_FREIGHT}.{{0,18}}{_DENOM_JOIN}.{{0,18}}{_DENOM_1688})"
)
_CORRECT_DENOMINATOR_EVIDENCE = (
    rf"(?:{_CORRECT_DENOMINATOR_PAIR}"
    rf"|分母.{{0,20}}{_DENOM_FREIGHT}"
    rf"|{_DENOM_FREIGHT}.{{0,20}}分母)"
)
_CORRECT_DENOMINATOR_RE = re.compile(
    rf"([÷/]|除以|分母|计算基数|总成本基数|成本基数).{{0,45}}"
    rf"{_CORRECT_DENOMINATOR_PAIR}",
    re.IGNORECASE
)

# 被否定/引用的正确公式不能作为通过证据。
_NEGATED_CORRECT_DENOMINATOR_RE = re.compile(
    rf"(?:"
    rf"(?:不是按|不是用|不按|不采用|不用于|不含|不包括|不放进).{{0,70}}"
    rf"{_CORRECT_DENOMINATOR_EVIDENCE}"
    rf"|有人.{{0,40}}{_CORRECT_DENOMINATOR_PAIR}.{{0,45}}"
    rf"(?:但|不过).{{0,12}}(?:不采用|不用|不按|不是)"
    rf"|{_CORRECT_DENOMINATOR_EVIDENCE}.{{0,45}}"
    rf"(?:这个说法|说法).{{0,12}}不用于"
    rf"|{_CORRECT_DENOMINATOR_PAIR}.{{0,45}}"
    rf"(?:但|不过).{{0,12}}(?:不采用|不用|不按|不是)"
    rf")",
    re.IGNORECASE
)

# 明确断言旧分母（只用 1688 标准价，不含头程运费分摊）必须失败。
_OLD_DENOMINATOR_DIVIDER = r"(?:[÷/]|除以)"
_OLD_DENOMINATOR_FORMULA = (
    rf"议价差额\s*{_OLD_DENOMINATOR_DIVIDER}\s*\(?\s*(?:只用|仅用)?\s*{_DENOM_1688}"
    rf"(?!\s*{_DENOM_JOIN}.{{0,18}}{_DENOM_FREIGHT})"
)
_CONCESSION_OLD_DENOMINATOR_RE = re.compile(
    rf"(?:虽然|尽管|有人说).{{0,90}}{_CORRECT_DENOMINATOR_EVIDENCE}"
    rf".{{0,45}}(?:但|但是|不过).{{0,35}}(?:真实|实际).{{0,20}}"
    rf"(?:{_OLD_DENOMINATOR_FORMULA}|{_DENOM_1688})",
    re.IGNORECASE
)
_OLD_DENOMINATOR_ASSERTION_RE = re.compile(
    rf"(?:实际(?:应|仍)?按|真实(?:计算)?(?:仍)?(?:用|按)|实际(?:分母|计算)?|最终(?:用|按)|分母|计算基数)"
    rf".{{0,30}}(?:{_OLD_DENOMINATOR_FORMULA}|(?:只用|仅用)?\s*{_DENOM_1688}"
    rf"(?!\s*{_DENOM_JOIN}.{{0,18}}{_DENOM_FREIGHT}))"
    rf"|{_OLD_DENOMINATOR_FORMULA}",
    re.IGNORECASE
)

# 阈值：必须包含 3% 或 6% 阈值样例
_THRESHOLD_RE = re.compile(
    r"[36]\s*%"
    r"|3\s*%\s*.{0,10}不合格"
    r"|6\s*%\s*.{0,10}正常"
    r"|不合格.{0,10}3\s*%"
    r"|正常.{0,10}6\s*%"
)

# plus 折扣不计入（正向检查）
_PLUS_NOT_COUNTED_RE = re.compile(
    r"plus\s*折扣.{0,20}不计入"
    r"|plus.{0,10}不.{0,15}计入"
    r"|不计入.{0,15}plus"
    r"|plus.{0,20}不.{0,5}算"
    r"|plus.{0,20}不.{0,5}属于.{0,10}采购"
    r"|plus\s*折扣.{0,15}运营",
    re.IGNORECASE
)

# 错误 plus 处理（验门人实测绕过样例 #4）：
# "先计入...后续扣减" / "不是完全不计入 plus" 等表示 plus 部分/暂时计入的说法
# 旧 oracle 的 _PLUS_NOT_COUNTED_RE 会被"不是完全不计入 plus"的子串"不计入 plus"骗过
_PLUS_WRONG_TREATMENT_RE = re.compile(
    r"plus.{0,20}先.{0,10}计入.{0,20}(后续|后来|再).{0,10}(扣减|抵扣|运营)"
    r"|plus.{0,10}计入.{0,30}(后续|后来|再).{0,10}(扣|运营侧)"
    r"|不是完全不计入.{0,10}plus"
    r"|plus.{0,10}并非完全不计入"
    r"|plus.{0,20}部分计入"
    r"|plus.{0,10}暂时计入"
    # ↓ 新增：plus 计入后剔除 / 不是直接不计入（验门人 round-4 实测绕过 #12）
    r"|plus.{0,15}计入.{0,20}(后|之后).{0,5}再?.{0,5}(剔除|扣除|剔减)"
    r"|不是直接不计入.{0,10}plus",
    re.IGNORECASE
)

# plus 声称不计入议价率但仍计入绩效的矛盾口径（验门人 round-10 实测绕过 #25-#27）
# 正确口径：plus 折扣「完全不计入」采购议价率和采购议价绩效两者。
_PLUS_KPI_BACK_DOOR_RE = re.compile(
    # Case 1: "但/不过 会/仍/还 计入/纳入 绩效"（plus 说不计入议价率，但绩效还是算）
    r"plus.{0,80}(?:会|仍|还)\s*(?:计入|纳入).{0,20}(?:议价)?绩效"
    r"|plus.{0,80}(?:会|仍|还)\s*纳入.{0,20}(?:采购端|议价)?绩效"
    # Case 2: "绩效考核时 仍 纳入/计入"
    r"|绩效考核时?\s*仍\s*(?:纳入|计入).{0,15}绩效"
    # Case 3: "要把 plus ... 算进去/计入绩效"
    r"|要把.{0,5}plus.{0,30}(?:一起)?.{0,5}(?:算进去|算进来|算入|计入|纳入)",
    re.IGNORECASE
)


def _t48_content_oracle(reply: str) -> tuple[bool, list[str]]:
    """T48 采购议价率公式与plus折扣口径 oracle（确定性规则，WS-117 round-3 收紧）。

    通过条件（AND）：
      1. 不包含任何 15% 作为合格/达标阈值的描述（含"大于/高于"绕过变种）
      2. 包含业务语义词（议价差额/头程运费等），有实质公式解释
      3. 正向证明分母/计算基数同时包含 1688采购标准价 + 头程运费分摊
      4. 包含 3% 或 6% 阈值
      5. 明确说明 plus 折扣不计入采购议价率/绩效
      6. 不把被否定/引用后废弃的正确分母当作通过证据
      7. 不包含"plus 先计入...后续扣减"等错误 plus 处理描述
      8. 不包含"plus 不计入议价率，但仍计入绩效"等矛盾口径

    返回 (passed, fail_reasons)。
    """
    fails = []

    # 检查 1：旧错误口径 + 绕过变种（"大于/高于 15%"）
    if _OLD_15PCT_PASS_RE.search(reply):
        fails.append(
            "reply 包含旧错误口径或绕过变种（'15%合格线'/'大于15%'/'高于15%才达标'/'达15%即合格'等），"
            "已废止；正确阈值为3%/6%"
        )

    # 检查 2：业务语义词（防止只写字母 H/(F+G) 不解释）
    if not _FORMULA_SEMANTIC_RE.search(reply):
        fails.append(
            "reply 未包含采购议价率的业务语义词（如'议价差额'、'头程运费'等）；"
            "不得仅写字母 H/(F+G) 而不给业务名称解释"
        )

    # 检查 3：正确分母正向证明（1688采购标准价 + 头程运费分摊）
    if not _CORRECT_DENOMINATOR_RE.search(reply):
        fails.append(
            "reply 未证明头程运费分摊在采购议价率分母/计算基数中；"
            "正确公式分母 = 1688采购标准价 + 头程运费分摊"
        )

    # 检查 3b：正确公式被否定/引用后弃用，不能作为通过证据
    if _NEGATED_CORRECT_DENOMINATOR_RE.search(reply):
        fails.append(
            "reply 虽提到正确分母，但同时否定/引用后废弃该公式；"
            "不得把'不是按/不采用/这个说法不用于'的正确公式当作通过证据"
        )

    # 检查 3c：最终或实际计算断言旧分母（只用 1688 标准价）即 FAIL
    if _CONCESSION_OLD_DENOMINATOR_RE.search(reply) or _OLD_DENOMINATOR_ASSERTION_RE.search(reply):
        fails.append(
            "reply 断言采购议价率实际分母只用 1688采购标准价，缺少头程运费分摊；"
            "这是已废止的旧公式"
        )

    # 检查 4：阈值（3% 或 6%）
    if not _THRESHOLD_RE.search(reply):
        fails.append(
            "reply 未包含采购议价率阈值样例（3% 不合格 / 6% 正常），"
            "正确口径须含阈值示例"
        )

    # 检查 5：plus 折扣不计入（正向）
    if not _PLUS_NOT_COUNTED_RE.search(reply):
        fails.append(
            "reply 未明确说明'plus 折扣不计入采购议价率/绩效'，"
            "这是 Luke sign-off 的核心口径之一"
        )

    # 检查 6：错误 plus 处理描述（"先计入...后续扣减"绕过子串）
    if _PLUS_WRONG_TREATMENT_RE.search(reply):
        fails.append(
            "reply 含错误 plus 口径（'plus 先计入...后续扣减'/'不是完全不计入 plus'等），"
            "正确口径：plus 折扣完全不计入，不存在'先计后扣'机制"
        )

    # 检查 7：plus 不计入议价率但仍计入绩效的矛盾口径（round-10 实测绕过）
    if _PLUS_KPI_BACK_DOOR_RE.search(reply):
        fails.append(
            "reply 含矛盾 plus 口径（声称不计入议价率但仍计入绩效/KPI），"
            "正确口径：plus 折扣「同时」不计入采购议价率和采购议价绩效"
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


# ── 验门人实测绕过样例 fail-then-pass（WS-117 round-3）─────────────────────────
# 每个 bypass 演示两件事：
#   Step A（旧 oracle）：绕过样例对旧 oracle 判 PASS → 说明旧 oracle 有漏洞
#   Step B（新 oracle）：绕过样例对新 oracle 判 FAIL → 说明新 oracle 堵住了漏洞

def _old_oracle_bypass_check(reply: str) -> bool:
    """旧 oracle（round-2，仅有原始 4 项检查，无新增修复）。
    用于演示绕过样例在旧 oracle 下判 PASS 的状态。
    """
    _old_15pct = re.compile(
        r"≥\s*15\s*%\s*(才算|算|为)?合格"
        r"|15\s*%\s*才算合格"
        r"|议价率\s*[≥>＞]\s*15\s*%"
        r"|合格\s*线\s*[：:]\s*15\s*%"
        r"|15\s*%.*合格线"
        r"|合格.*15\s*%"
    )
    _old_formula = re.compile(
        r"议价差额|议价省|省下.{0,10}金额|谈判.{0,10}省"
        r"|1688.{0,15}价.{0,10}÷|1688.{0,15}价.{0,10}/"
        r"|差额.{0,10}[÷/].{0,20}运费|头程.{0,15}运费",
        re.IGNORECASE
    )
    _old_threshold = re.compile(r"[36]\s*%|3\s*%\s*.{0,10}不合格|6\s*%\s*.{0,10}正常")
    _old_plus = re.compile(
        r"plus\s*折扣.{0,20}不计入|plus.{0,10}不.{0,15}计入"
        r"|不计入.{0,15}plus|plus.{0,20}不.{0,5}算"
        r"|plus.{0,20}不.{0,5}属于.{0,10}采购|plus\s*折扣.{0,15}运营",
        re.IGNORECASE
    )
    fails = []
    if _old_15pct.search(reply): fails.append("old-15pct")
    if not _old_formula.search(reply): fails.append("old-no-formula")
    if not _old_threshold.search(reply): fails.append("old-no-threshold")
    if not _old_plus.search(reply): fails.append("old-no-plus")
    return len(fails) == 0


def test_bypass1_dayan_15pct_old_passes_new_rejects():
    """验门人绕过 #1（WS-117 实测）：'必须大于 15%' 绕过旧 oracle 但被新 oracle 拒绝。

    旧 oracle 只识别 ≥15%/才算合格 等形式，不识别 '大于 15%' → passed=True（漏洞）。
    新 oracle 新增 '必须大于 X%' / '大于 X%...要' 等模式 → passed=False（堵住）。
    """
    bypass_reply = (
        "采购议价率必须大于 15%，低于要写备注。"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣不计入采购议价率/绩效，属于运营侧。"
    )
    # Step A：旧 oracle 对此绕过样例判 PASS（漏洞存在）
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A fail-then-pass：旧 oracle 应对'必须大于 15%'绕过样例判 PASS，"
        "此处失败说明旧 oracle 已修复或样例不能复现绕过"
    )
    # Step B：新 oracle 拒绝此绕过样例（漏洞已堵）
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'必须大于 15%'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), (
        f"new_fails 应提及 15% 口径，实际: {new_fails}"
    )


def test_bypass2_wrong_formula_freight_excluded_old_passes_new_rejects():
    """验门人绕过 #2（WS-117 实测）：'头程运费另看，不进入分母' 绕过旧 oracle。

    旧 oracle 仅检查 '头程运费' 关键词是否出现，不验证它是否在分母位置；
    即使明确写 '头程运费另看，不进入分母'，因关键词存在仍判 PASS（漏洞）。
    新 oracle 增加 _WRONG_FREIGHT_DENOMINATOR_RE 检查 → 拒绝（堵住）。
    """
    bypass_reply = (
        "采购议价率 = (1688 标价 - 实际成交价) / 1688 标价。"
        "头程运费另看，不进入分母。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣不计入采购议价率绩效。"
    )
    # Step A
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'头程运费另看，不进入分母'绕过样例判 PASS（漏洞存在），"
        "此处失败说明旧 oracle 或样例有误"
    )
    # Step B
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'头程运费不进入分母'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_bypass3_gaoshang_15pct_dazhun_old_passes_new_rejects():
    """验门人绕过 #3（WS-117 实测）：'高于 15% 才达标' 绕过旧 oracle。

    旧 oracle 识别 '才算合格' 但不识别 '才达标' → 判 PASS（漏洞）。
    新 oracle 增加 '高于 X% 才达标' 等模式 → 拒绝（堵住）。
    """
    bypass_reply = (
        "采购议价率要高于 15% 才达标，低于 15% 需备注。"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣不计入绩效。"
    )
    # Step A
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'高于 15% 才达标'判 PASS（漏洞存在），"
        "此处失败说明旧 oracle 或样例有误"
    )
    # Step B
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'高于 15% 才达标'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), (
        f"new_fails 应提及 15% 口径，实际: {new_fails}"
    )


def test_bypass4_plus_partial_count_old_passes_new_rejects():
    """验门人绕过 #4（WS-117 实测）：'plus 先计入...后续扣减' 绕过旧 oracle。

    旧 _PLUS_NOT_COUNTED_RE 匹配子串'不计入 plus'（来自'不是完全不计入 plus'），
    判 PASS（漏洞）。新 oracle 新增 _PLUS_WRONG_TREATMENT_RE 专检错误语义 → 拒绝。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣先计入采购绩效，后续再做运营侧扣减；不是完全不计入 plus。"
    )
    # Step A
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'不是完全不计入 plus'绕过样例判 PASS（漏洞存在），"
        "此处失败说明旧 oracle 或样例有误"
    )
    # Step B
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'plus 先计入...后续扣减'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("plus" in f.lower() for f in new_fails), (
        f"new_fails 应提及 plus 口径，实际: {new_fails}"
    )


def test_bypass5_bu_neng_di_yu_15pct_old_passes_new_rejects():
    """验门人绕过 #5（WS-117 round-3 实测）：'不能低于 15%' 绕过旧 oracle 但被新 oracle 拒绝。

    旧 oracle 未覆盖'不能低于/不低于 + 15%/15个点'等否定式阈值表达 → 判 PASS（漏洞）。
    新 oracle 新增 '不能?低于 15%' 等模式 → 拒绝（堵住）。
    """
    bypass_reply = (
        "采购议价率不能低于 15%，低于要备注；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣不计入采购绩效。"
    )
    # Step A：旧 oracle 对此绕过样例判 PASS（漏洞存在）
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A fail-then-pass：旧 oracle 应对'不能低于 15%'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    # Step B：新 oracle 拒绝此绕过样例（漏洞已堵）
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'不能低于 15%'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), (
        f"new_fails 应提及 15% 口径，实际: {new_fails}"
    )


def test_bypass6_zuidi_15_ge_dian_old_passes_new_rejects():
    """验门人绕过 #6（WS-117 round-3 实测）：'最低要 15 个点才算过线' 绕过旧 oracle。

    旧 oracle 只识别 '%' 后缀的 15，不识别 '15 个点'（百分点表达）→ 判 PASS（漏洞）。
    新 oracle 新增 '最低 15 个点' / '15 个点 才算过线' 等模式 → 拒绝（堵住）。
    """
    bypass_reply = (
        "采购议价率最低要 15 个点才算过线；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus 折扣不计入采购绩效。"
    )
    # Step A：旧 oracle 对此绕过样例判 PASS（漏洞存在）
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A fail-then-pass：旧 oracle 应对'最低要 15 个点才算过线'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    # Step B：新 oracle 拒绝此绕过样例（漏洞已堵）
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'最低要 15 个点才算过线'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), (
        f"new_fails 应提及 15% 口径，实际: {new_fails}"
    )


def test_bypass7_daodao_15pct_caizuandao_old_passes_new_rejects():
    """验门人绕过 #7（WS-117 round-4 实测）：'达到 15% 才算达标' 绕过旧 oracle。

    旧 oracle 未覆盖'达到 X%'形式的 15% 阈值表达 → 判 PASS（漏洞）。
    新 oracle 新增 '达到 15%' 等模式 → 拒绝。
    """
    bypass_reply = (
        "采购议价率达到 15% 才算达标；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'达到 15% 才算达标'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'达到 15% 才算达标'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass8_daodao_15_ge_baifendian_old_passes_new_rejects():
    """验门人绕过 #8（WS-117 round-4 实测）：'达到 15 个百分点才算合格' 绕过旧 oracle。

    旧 oracle 未覆盖'个百分点'这一 15 个百分点的表达方式 → 判 PASS（漏洞）。
    新 oracle 新增 '达到 15 个百分点' 等模式 → 拒绝。
    """
    bypass_reply = (
        "采购议价率要达到 15 个百分点才算合格；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'达到 15 个百分点才算合格'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'达到 15 个百分点才算合格'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass9_15pct_yishang_wei_daobiao_old_passes_new_rejects():
    """验门人绕过 #9（WS-117 round-4 实测）：'15% 以上为达标' 绕过旧 oracle。

    旧 oracle 未覆盖'X% 以上为达标'形式 → 判 PASS（漏洞）。
    新 oracle 新增 '15% 以上.达标' 等模式 → 拒绝。
    """
    bypass_reply = (
        "采购议价率 15% 以上为达标；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'15% 以上为达标'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'15% 以上为达标'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass10_zhongwen_shiwu_ge_dian_old_passes_new_rejects():
    """验门人绕过 #10（WS-117 round-4 实测）：'不能低于十五个点' 中文数字绕过旧 oracle。

    旧 oracle 只检测阿拉伯数字'15'，不识别汉字'十五' → 判 PASS（漏洞）。
    新 oracle 新增 '十五.个点' 等模式 → 拒绝。
    """
    bypass_reply = (
        "采购议价率不能低于十五个点；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'不能低于十五个点'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'不能低于十五个点'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass11_freight_only_as_cost_reference_old_passes_new_rejects():
    """验门人绕过 #11（WS-117 round-4 实测）：'头程运费只做成本参考' 绕过旧 oracle。

    旧 oracle 检测到'头程运费'关键词即视为公式包含头程，但此处头程运费不在分母 → 判 PASS（漏洞）。
    新 oracle 扩展 _WRONG_FREIGHT_DENOMINATOR_RE 识别'头程运费只做成本参考'等排除性描述 → 拒绝。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%，头程运费只做成本参考。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'头程运费只做成本参考'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'头程运费只做成本参考'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_bypass12_plus_count_then_deduct_old_passes_new_rejects():
    """验门人绕过 #12（WS-117 round-4 实测）：'plus 计入后再剔除' 绕过旧 oracle。

    旧 _PLUS_WRONG_TREATMENT_RE 未覆盖'计入后剔除'/'不是直接不计入'等部分计入表达 → 判 PASS（漏洞）。
    新 oracle 新增对应模式 → 拒绝。
    """
    bypass_reply = (
        "plus 折扣计入采购绩效后再剔除，不是直接不计入 plus。"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'plus 计入后再剔除'绕过样例判 PASS，"
        "此处失败说明旧 oracle 或样例有误"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        "Step B：新 oracle 应拒绝'plus 折扣计入采购绩效后再剔除'绕过样例，"
        f"但 passed=True；new_fails={new_fails}"
    )
    assert any("plus" in f.lower() for f in new_fails), (
        f"new_fails 应提及 plus 口径，实际: {new_fails}"
    )



def test_bypass13_fullwidth_pct_old_passes_new_rejects():
    """验门人绕过 #13（WS-117 round-5 实测）：全角 ％ 绕过旧 oracle。

    旧 oracle 只识别半角 %，不识别全角 ％ 字符 -> 判 PASS（漏洞）。
    新 oracle 在 达到 15 pattern 中新增全角 ％ -> 拒绝。
    """
    bypass_reply = (
        "采购议价率达到 15％ 才算达标；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对全角 15％ 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝全角 15％ 绕过样例，但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass14_baifendian_yishang_cai_zhengchang_old_passes_new_rejects():
    """验门人绕过 #14（WS-117 round-5 实测）：15 个百分点以上才正常 绕过旧 oracle。

    旧 oracle 未覆盖 个百分点以上才正常 变体 -> 判 PASS（漏洞）。
    新 oracle 新增 15(个)?百分点.以上.正常 模式 -> 拒绝。
    """
    bypass_reply = (
        "采购议价率 15 个百分点以上才正常；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 15 个百分点以上才正常 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 15 个百分点以上才正常，但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass15_freight_influences_cost_old_passes_new_rejects():
    """验门人绕过 #15（WS-117 round-5 实测）：头程运费会影响成本核算 绕过旧 oracle。

    旧 oracle 检测到头程运费即视为公式包含头程，但此处头程运费不在分母 -> 判 PASS（漏洞）。
    新 oracle 扩展 _WRONG_FREIGHT_DENOMINATOR_RE 识别 会影响成本 -> 拒绝。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%，头程运费会影响成本核算。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 头程运费会影响成本 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 头程运费会影响成本核算，但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_bypass16_freight_as_separate_cost_item_old_passes_new_rejects():
    """验门人绕过 #16（WS-117 round-5 实测）：头程运费作为单独成本项核算 绕过旧 oracle。

    旧 oracle 检测到头程运费即视为公式包含头程，但此处头程运费不在分母 -> 判 PASS（漏洞）。
    新 oracle 扩展 _WRONG_FREIGHT_DENOMINATOR_RE 识别 作为单独成本项 -> 拒绝。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%，头程运费作为单独成本项核算。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 头程运费作为单独成本项 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 头程运费作为单独成本项核算，但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_bypass17_fullwidth_pct_yishang_old_passes_new_rejects():
    """验门人绕过 #17（WS-117 round-6 实测）：15％ 以上为达标 绕过旧 oracle。"""
    bypass_reply = (
        "采购议价率 15％ 以上为达标；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 15％ 以上为达标 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 15％ 以上为达标，但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass18_fullwidth_must_greater_than_old_passes_new_rejects():
    """验门人绕过 #18（WS-117 round-6 实测）：必须大于 15％ 绕过旧 oracle。"""
    bypass_reply = (
        "采购议价率必须大于 15％，低于要备注；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 必须大于 15％ 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 必须大于 15％，但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), f"new_fails 应提及 15%，实际: {new_fails}"


def test_bypass19_freight_single_accounting_old_passes_new_rejects():
    """验门人绕过 #19（WS-117 round-6 实测）：头程运费单独核算但不在分母。"""
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%，头程运费单独核算。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 头程运费单独核算 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 头程运费单独核算，但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_bypass20_freight_not_in_denominator_old_passes_new_rejects():
    """验门人绕过 #20（WS-117 round-6 实测）：头程运费另行计入成本，不放进分母。"""
    bypass_reply = (
        "采购议价率分母用1688采购标准价，头程运费分摊另行计入成本，不放进议价率分母。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 头程运费不放进分母 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 头程运费不放进分母，但 passed=True；new_fails={new_fails}"
    )
    assert any("头程" in f or "分母" in f for f in new_fails), (
        f"new_fails 应提及头程/分母，实际: {new_fails}"
    )


def test_negation_not_using_correct_formula_fails():
    """验门人绕过 #21（WS-117 round-8 实测）：先写正确公式但明确说不是按它算。"""
    bypass_reply = (
        "采购议价率不是按议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%，"
        "实际应按议价差额 ÷ 1688采购标准价 × 100%。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 否定正确公式后回退旧公式 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 否定正确公式后回退旧公式，但 passed=True；new_fails={new_fails}"
    )
    assert any("分母" in f or "公式" in f or "头程" in f for f in new_fails), (
        f"new_fails 应提及公式/分母/头程，实际: {new_fails}"
    )


def test_quote_then_deny_correct_formula_fails():
    """验门人绕过 #22（WS-117 round-8 实测）：引用正确分母说法后声明不用于采购议价率。"""
    bypass_reply = (
        "分母包含1688采购标准价和头程运费分摊这个说法不用于采购议价率；"
        "采购议价率实际分母只用1688采购标准价。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 引用正确分母后否定 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 引用正确分母后否定，但 passed=True；new_fails={new_fails}"
    )
    assert any("分母" in f or "公式" in f or "头程" in f for f in new_fails), (
        f"new_fails 应提及公式/分母/头程，实际: {new_fails}"
    )


def test_old_formula_final_assertion_fails():
    """验门人绕过 #23（WS-117 round-8 实测）：有人会写正确公式但最终断言旧公式。"""
    bypass_reply = (
        "有人会写采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%，"
        "但这里不采用；真实计算用议价差额 ÷ 1688采购标准价。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 最终断言旧公式 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 最终断言旧公式，但 passed=True；new_fails={new_fails}"
    )
    assert any("分母" in f or "公式" in f or "头程" in f for f in new_fails), (
        f"new_fails 应提及公式/分母/头程，实际: {new_fails}"
    )


def test_concession_then_old_formula_final_assertion_fails():
    """验门人绕过 #24（WS-117 round-10 实测）：让步句引用正确分母后，转折为旧公式。"""
    bypass_reply = (
        "虽然有人说分母是1688采购标准价加头程运费分摊，"
        "但真实计算仍按议价差额除以1688采购标准价。"
        "阈值：3% 不合格，6% 正常。plus 折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, "Step A：旧 oracle 应对 让步转折旧公式 绕过样例判 PASS"
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝 让步转折后回退旧公式，但 passed=True；new_fails={new_fails}"
    )
    assert any("分母" in f or "公式" in f or "头程" in f for f in new_fails), (
        f"new_fails 应提及公式/分母/头程，实际: {new_fails}"
    )


# ── 验门人 round-10 实测绕过 fail-then-pass（bypass #25-#28）───────────────────

def test_bypass25_plus_not_in_rate_but_in_kpi_old_passes_new_rejects():
    """验门人绕过 #25（WS-117 round-10 实测）：plus不计入议价率，但会计入采购议价绩效。

    旧 oracle 看到'plus折扣不计入'即通过，不检查是否接着说'但还是计入绩效' → PASS（漏洞）。
    新 oracle 新增 _PLUS_KPI_BACK_DOOR_RE → 检出矛盾口径 → FAIL（堵住）。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus折扣不计入采购议价率，但会计入采购议价绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'plus不计入议价率但会计入绩效'判 PASS（漏洞存在）"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝矛盾 plus 口径，但 passed=True；new_fails={new_fails}"
    )
    assert any("plus" in f.lower() or "绩效" in f for f in new_fails), (
        f"new_fails 应提及 plus/绩效矛盾，实际: {new_fails}"
    )


def test_bypass26_plus_not_in_rate_but_still_in_kpi_evaluation_old_passes_new_rejects():
    """验门人绕过 #26（WS-117 round-10 实测）：plus不计入议价率；绩效考核时仍纳入采购端绩效。

    旧 oracle 不检查'绩效考核时仍纳入'这种迂回表达 → PASS（漏洞）。
    新 oracle 新增 _PLUS_KPI_BACK_DOOR_RE 检出 → FAIL（堵住）。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus折扣不计入采购议价率；绩效考核时仍纳入采购端绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'绩效考核时仍纳入'绕过样例判 PASS（漏洞存在）"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝'绩效考核时仍纳入'绕过样例，但 passed=True；new_fails={new_fails}"
    )
    assert any("plus" in f.lower() or "绩效" in f for f in new_fails), (
        f"new_fails 应提及 plus/绩效矛盾，实际: {new_fails}"
    )


def test_bypass27_plus_not_in_rate_but_subsidy_counted_in_kpi_old_passes_new_rejects():
    """验门人绕过 #27（WS-117 round-10 实测）：plus不计入议价率，但要把plus补贴一起算进去。

    旧 oracle 看到'plus不计入'即通过，不检查'要把plus补贴算进去'的矛盾 → PASS（漏洞）。
    新 oracle 新增 _PLUS_KPI_BACK_DOOR_RE → 检出 → FAIL（堵住）。
    """
    bypass_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。"
        "plus折扣不计入采购议价率，不过采购绩效要把plus平台补贴一起算进去。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'要把plus补贴算进去'绕过样例判 PASS（漏洞存在）"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝'要把plus补贴算进去'绕过样例，但 passed=True；new_fails={new_fails}"
    )
    assert any("plus" in f.lower() or "绩效" in f for f in new_fails), (
        f"new_fails 应提及 plus/绩效矛盾，实际: {new_fails}"
    )


def test_bypass28_da_15pct_ji_hege_old_passes_new_rejects():
    """验门人绕过 #28（WS-117 round-10 实测）：采购议价率达15%即合格。

    旧 oracle 未覆盖'达15%即合格'表达（只覆盖≥/大于/高于/超过等） → PASS（漏洞）。
    新 oracle 新增 '达X%即/就合格' 模式 → FAIL（堵住）。
    """
    bypass_reply = (
        "采购议价率达15%即合格；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
    )
    old_passed = _old_oracle_bypass_check(bypass_reply)
    assert old_passed, (
        "Step A：旧 oracle 应对'达15%即合格'绕过样例判 PASS（漏洞存在）"
    )
    new_passed, new_fails = _t48_content_oracle(bypass_reply)
    assert not new_passed, (
        f"Step B：新 oracle 应拒绝'达15%即合格'绕过样例，但 passed=True；new_fails={new_fails}"
    )
    assert any("15" in f for f in new_fails), (
        f"new_fails 应提及 15% 旧口径，实际: {new_fails}"
    )


# ── 直接 verifier 测试（WS-117 round-11 生产接线，不 mock LLM）──────────────────
# 验证 check_procurement_rate_reply 函数本身的检测能力（fail-then-pass：
#   修前无此函数→warns=[]；修后→错误分母/plus矛盾口径被检出）

def test_verifier_wrong_denominator_warns():
    """直接测 verifier 函数（不 mock LLM）：错误分母（仅1688采购标准价）→ warns 非空。

    fail-then-pass 模式：
    - 修前（无 check_procurement_rate_reply 函数）：returns warns=[]（无检测）
    - 修后（有函数）：检测到分母缺头程运费分摊 → warns 非空
    """
    from hipop.rules.procurement_rate import check_procurement_rate_reply

    wrong_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%。"
        "阈值：3% 不合格，6% 正常。plus折扣不计入绩效。"
    )
    warns = check_procurement_rate_reply(wrong_reply)
    assert warns, (
        "错误分母（仅含1688采购标准价，缺少头程运费分摊）应触发 warns，"
        f"但 warns=[]。检查 check_procurement_rate_reply 是否已实现。"
    )
    assert any("头程" in w or "分母" in w for w in warns), (
        f"warns 应提及分母/头程运费问题，实际: {warns}"
    )


def test_verifier_correct_denominator_ok():
    """直接测 verifier 函数（不 mock LLM）：正确分母 → warns=[]。"""
    from hipop.rules.procurement_rate import check_procurement_rate_reply

    correct_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
    )
    warns = check_procurement_rate_reply(correct_reply)
    assert warns == [], (
        f"正确公式不应触发 warns，实际: {warns}"
    )


def test_verifier_no_procurement_topic_no_warn():
    """直接测 verifier 函数（不 mock LLM）：无采购议价率话题时不触发。"""
    from hipop.rules.procurement_rate import check_procurement_rate_reply

    unrelated_reply = "我们的在售SKU数量目前是1430个，其中KSA仓有850个。"
    warns = check_procurement_rate_reply(unrelated_reply)
    assert warns == [], (
        f"无采购议价率话题时不应触发 warns，实际: {warns}"
    )


def test_verifier_round12_wrong_denominator_variants_warn():
    """round-12 fail-then-pass：销售价/noon价格/采购价/折扣率错误分母必须触发专项 warns。

    修前 round-11 verifier 只覆盖"采购议价率 = 议价差额 ÷ 1688采购标准价"，
    以下验门人实测绕过均返回 warns=[]。
    """
    from hipop.rules.procurement_rate import check_procurement_rate_reply

    bad_replies = [
        (
            "采购议价率 = 议价差额 ÷ noon价格 × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
        (
            "采购议价率 = 议价差额 ÷ 销售价 × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
        (
            "采购议价率 = 议价差额 ÷ 采购价 × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
        (
            "采购折扣率 = 议价差额 ÷ noon价格 × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
    ]

    for bad_reply in bad_replies:
        warns = check_procurement_rate_reply(bad_reply)
        assert warns, (
            "round-12 错误分母/同义话题应触发采购专项 warns，"
            f"但 warns=[]；reply={bad_reply}"
        )
        assert any("采购议价率" in w and ("分母" in w or "noon" in w or "销售价" in w or "采购价" in w) for w in warns), (
            f"warns 应包含采购专项分母拦截文本，实际: {warns}"
        )


def test_verifier_round12_old_15pct_threshold_warns():
    """round-12 fail-then-pass：生产 verifier 也必须拦截旧 15% 合格线。"""
    from hipop.rules.procurement_rate import check_procurement_rate_reply

    wrong_reply = (
        "采购议价率达15%即合格；"
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
        "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
    )

    warns = check_procurement_rate_reply(wrong_reply)
    assert warns, (
        "生产 verifier 应拦截'采购议价率达15%即合格'旧口径，"
        f"但 warns=[]。reply={wrong_reply}"
    )
    assert any("15" in w or "旧" in w for w in warns), (
        f"warns 应提及 15% 旧阈值口径，实际: {warns}"
    )


def test_agent_wires_verifier_for_wrong_procurement_formula():
    """集成测试：agent.py 已接线 procurement_rate verifier（WS-117 round-11 生产接线）。

    mock LLM 返回「错误分母（缺头程运费）」的回复，验证 chat() 返回的
    hallucination_warnings 非空。这证明 check_procurement_rate_reply 已从 agent.py 调用。

    fail-then-pass：
    - 修前（无生产接线）：hallucination_warnings=None
    - 修后（已接线）：hallucination_warnings 包含分母相关 warn
    """
    from hipop.server import _provider, agent

    wrong_formula_reply = (
        "采购议价率 = 议价差额 ÷ 1688采购标准价 × 100%。"
        "阈值：3% 不合格，6% 正常。plus折扣不计入绩效。"
    )

    mock_result = _provider.ChatResult({
        "reply": wrong_formula_reply,
        "tool_log": [],
        "refs_collected": [],
        "workflow_task": None,
    })

    with unittest.mock.patch.object(_provider, "chat_with_tools", return_value=mock_result):
        result = agent.chat(
            [{"role": "user", "content": "请说明采购议价率怎么计算？"}],
            scope={
                "store": "KSA",
                "current_user": "smoke_t48",
                "current_role": "owner",
                "tenant_id": 1,
                "user_id": 1,
            },
        )

    warns = result.get("hallucination_warnings") or []
    assert warns, (
        "agent.chat() 对'错误分母（缺头程运费）'的回复应触发 hallucination_warnings，"
        "但 hallucination_warnings=None。检查 check_procurement_rate_reply 是否已从 agent.py 调用。"
        f"\nchat result reply（前200字）: {(result.get('reply') or '')[:200]}"
    )
    assert any("头程" in w or "分母" in w for w in warns), (
        f"warns 应提及分母/头程运费问题，实际: {warns}"
    )


def test_agent_round12_wires_procurement_specific_warning_for_real_bypasses():
    """round-12 集成测试：真实绕过必须进入 hallucination_warnings 的采购专项拦截。

    这里仍 mock LLM 输出以固定红队样本，但断言的是 agent.py 生产后处理管道：
    `check_procurement_rate_reply()` 必须被调用，且不能只靠泛化低置信 banner。
    """
    from hipop.server import _provider, agent

    bad_replies = [
        (
            "采购议价率 = 议价差额 ÷ noon价格 × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
        (
            "采购议价率达15%即合格；"
            "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%。"
            "阈值：3% 不合格，6% 正常。plus折扣不计入采购绩效。"
        ),
    ]

    for bad_reply in bad_replies:
        mock_result = _provider.ChatResult({
            "reply": bad_reply,
            "tool_log": [],
            "refs_collected": [],
            "workflow_task": None,
        })

        with unittest.mock.patch.object(_provider, "chat_with_tools", return_value=mock_result):
            result = agent.chat(
                [{"role": "user", "content": "请说明采购议价率怎么计算？"}],
                scope={
                    "store": "KSA",
                    "current_user": "smoke_t48",
                    "current_role": "owner",
                    "tenant_id": 1,
                    "user_id": 1,
                },
            )

        warns = result.get("hallucination_warnings") or []
        assert warns, (
            "agent.chat() 应对 round-12 真实绕过触发 hallucination_warnings，"
            f"但为空；reply={bad_reply}"
        )
        assert any("采购议价率" in w and "规则源: hipop/rules/procurement_rate.py" in w for w in warns), (
            "hallucination_warnings 应包含采购专项拦截结果，而不是只有泛化低置信 warning；"
            f"实际: {warns}"
        )


# ── 规则源接线验证（Option A：可审计规则文件）──────────────────────────────────

def test_rules_file_procurement_rate_spec():
    """验证权威规则源文件 hipop/rules/procurement_rate.py 存在且口径正确。

    证明规则已接线到可审计的代码层（Option A），而非仅在运营 NOTES.md 里。
    smoke 直接从该文件加载并断言关键字段，任何回退都会使此 test FAIL。

    fail-then-pass：
      - 修前：hipop/rules/procurement_rate.py 不存在 → ImportError → FAIL
      - 修后：文件存在且口径正确 → PASS
    """
    from hipop.rules import procurement_rate as rules

    # 分子：议价差额
    assert "议价差额" in rules.FORMULA["numerator"], (
        "FORMULA.numerator 应为'议价差额'，当前缺少"
    )

    # 分母：必须同时包含 1688采购标准价 + 头程运费分摊
    denom = rules.FORMULA["denominator_components"]
    assert any("1688" in c and "标准价" in c for c in denom), (
        f"FORMULA.denominator_components 应含'1688采购标准价'，当前: {denom}"
    )
    assert any("头程运费" in c for c in denom), (
        f"FORMULA.denominator_components 应含'头程运费分摊'，当前: {denom}"
    )

    # 阈值：< 3% 不合格，≥ 6% 正常
    assert rules.THRESHOLDS["fail_below"] <= 0.03, (
        f"THRESHOLDS.fail_below 应 ≤ 3%（{rules.THRESHOLDS['fail_below']}），当前不符"
    )
    assert rules.THRESHOLDS["pass_above"] >= 0.06, (
        f"THRESHOLDS.pass_above 应 ≥ 6%（{rules.THRESHOLDS['pass_above']}），当前不符"
    )

    # plus 折扣：完全不计入
    assert rules.PLUS_DISCOUNT["included_in_procurement_rate"] is False, (
        "PLUS_DISCOUNT.included_in_procurement_rate 应为 False（plus 不计入采购议价率）"
    )
    assert rules.PLUS_DISCOUNT["included_in_kpi"] is False, (
        "PLUS_DISCOUNT.included_in_kpi 应为 False（plus 不计入采购绩效）"
    )

    # 废止口径记录（15% 阈值必须标注为废止）
    deprecated_text = " ".join(rules.DEPRECATED.values())
    assert "15%" in deprecated_text, (
        "DEPRECATED 应记录旧 15% 阈值废止信息"
    )


# ── Agent 集成测试（mock provider，验证 chat() → oracle 链路）──────────────────

def test_agent_t48_answer_oracle():
    """集成测试：mock LLM provider 后 chat() 返回的回复应通过 T48 oracle。

    测试 chat() → _safety.sanitize_reply → oracle 全链路正确连通，
    并验证以正确口径回答时 oracle 判 PASS。
    provider 被 mock 以避免 CI 中真实 LLM API 调用；mock 返回符合 T48 期望的回复。

    fail-then-pass 意义：
      - 若 chat() 的回复被后处理层（sanitize_reply / feedback offer）破坏了
        oracle 关键词 → 此处 FAIL，说明接线在某层被截断。
      - mock 返回正确答案 → oracle PASS → 证明链路无损。
    """
    from hipop.server import _provider, agent

    correct_reply = (
        "采购议价率 = 议价差额 ÷ (1688采购标准价 + 头程运费分摊) × 100%\n"
        "  - 议价差额 = 1688采购标准价 − 实际成交采购价（谈判省下的金额）\n"
        "  - 1688采购标准价：谈判前 1688 平台标示参考价\n"
        "  - 头程运费分摊：国内发至海外仓的单件运费\n"
        "阈值样例：< 3% 不合格；≥ 6% 正常\n"
        "注：plus 折扣不计入采购议价率/绩效，属于 noon 平台运营/营销费用，不属于采购端议价绩效。"
    )

    mock_result = _provider.ChatResult({
        "reply": correct_reply,
        "tool_log": [],
        "refs_collected": [{"table": "tenant_notes", "content": "采购议价率规则"}],
        "workflow_task": None,
    })

    with unittest.mock.patch.object(_provider, "chat_with_tools", return_value=mock_result):
        result = agent.chat(
            [{"role": "user", "content": "请说明采购议价率怎么计算，plus 折扣是否计入绩效？"}],
            scope={
                "store": "KSA",
                "current_user": "smoke_t48",
                "current_role": "owner",
                "tenant_id": 1,
                "user_id": 1,
            },
        )

    # chat() 可能用 final_text（带 banner）或 clean_reply；oracle 不含 banner 关键词，都可
    reply = result.get("clean_reply") or result.get("reply") or ""
    passed, fails = _t48_content_oracle(reply)
    assert passed, (
        f"mocked chat() 回复经后处理后应通过 T48 oracle，实际 fails={fails}\n"
        f"reply（前 400 字）: {reply[:400]}"
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
        ("test_bypass1_dayan_15pct_old_passes_new_rejects",
         test_bypass1_dayan_15pct_old_passes_new_rejects),
        ("test_bypass2_wrong_formula_freight_excluded_old_passes_new_rejects",
         test_bypass2_wrong_formula_freight_excluded_old_passes_new_rejects),
        ("test_bypass3_gaoshang_15pct_dazhun_old_passes_new_rejects",
         test_bypass3_gaoshang_15pct_dazhun_old_passes_new_rejects),
        ("test_bypass4_plus_partial_count_old_passes_new_rejects",
         test_bypass4_plus_partial_count_old_passes_new_rejects),
        ("test_bypass5_bu_neng_di_yu_15pct_old_passes_new_rejects",
         test_bypass5_bu_neng_di_yu_15pct_old_passes_new_rejects),
        ("test_bypass6_zuidi_15_ge_dian_old_passes_new_rejects",
         test_bypass6_zuidi_15_ge_dian_old_passes_new_rejects),
        ("test_bypass7_daodao_15pct_caizuandao_old_passes_new_rejects",
         test_bypass7_daodao_15pct_caizuandao_old_passes_new_rejects),
        ("test_bypass8_daodao_15_ge_baifendian_old_passes_new_rejects",
         test_bypass8_daodao_15_ge_baifendian_old_passes_new_rejects),
        ("test_bypass9_15pct_yishang_wei_daobiao_old_passes_new_rejects",
         test_bypass9_15pct_yishang_wei_daobiao_old_passes_new_rejects),
        ("test_bypass10_zhongwen_shiwu_ge_dian_old_passes_new_rejects",
         test_bypass10_zhongwen_shiwu_ge_dian_old_passes_new_rejects),
        ("test_bypass11_freight_only_as_cost_reference_old_passes_new_rejects",
         test_bypass11_freight_only_as_cost_reference_old_passes_new_rejects),
        ("test_bypass12_plus_count_then_deduct_old_passes_new_rejects",
         test_bypass12_plus_count_then_deduct_old_passes_new_rejects),
        ("test_bypass13_fullwidth_pct_old_passes_new_rejects",
         test_bypass13_fullwidth_pct_old_passes_new_rejects),
        ("test_bypass14_baifendian_yishang_cai_zhengchang_old_passes_new_rejects",
         test_bypass14_baifendian_yishang_cai_zhengchang_old_passes_new_rejects),
        ("test_bypass15_freight_influences_cost_old_passes_new_rejects",
         test_bypass15_freight_influences_cost_old_passes_new_rejects),
        ("test_bypass16_freight_as_separate_cost_item_old_passes_new_rejects",
         test_bypass16_freight_as_separate_cost_item_old_passes_new_rejects),
        ("test_bypass17_fullwidth_pct_yishang_old_passes_new_rejects",
         test_bypass17_fullwidth_pct_yishang_old_passes_new_rejects),
        ("test_bypass18_fullwidth_must_greater_than_old_passes_new_rejects",
         test_bypass18_fullwidth_must_greater_than_old_passes_new_rejects),
        ("test_bypass19_freight_single_accounting_old_passes_new_rejects",
         test_bypass19_freight_single_accounting_old_passes_new_rejects),
        ("test_bypass20_freight_not_in_denominator_old_passes_new_rejects",
         test_bypass20_freight_not_in_denominator_old_passes_new_rejects),
        ("test_negation_not_using_correct_formula_fails",
         test_negation_not_using_correct_formula_fails),
        ("test_quote_then_deny_correct_formula_fails",
         test_quote_then_deny_correct_formula_fails),
        ("test_old_formula_final_assertion_fails",
         test_old_formula_final_assertion_fails),
        ("test_concession_then_old_formula_final_assertion_fails",
         test_concession_then_old_formula_final_assertion_fails),
        # ── round-10 oracle bypasses (plus KPI contradiction + 达15%即合格) ──
        ("test_bypass25_plus_not_in_rate_but_in_kpi_old_passes_new_rejects",
         test_bypass25_plus_not_in_rate_but_in_kpi_old_passes_new_rejects),
        ("test_bypass26_plus_not_in_rate_but_still_in_kpi_evaluation_old_passes_new_rejects",
         test_bypass26_plus_not_in_rate_but_still_in_kpi_evaluation_old_passes_new_rejects),
        ("test_bypass27_plus_not_in_rate_but_subsidy_counted_in_kpi_old_passes_new_rejects",
         test_bypass27_plus_not_in_rate_but_subsidy_counted_in_kpi_old_passes_new_rejects),
        ("test_bypass28_da_15pct_ji_hege_old_passes_new_rejects",
         test_bypass28_da_15pct_ji_hege_old_passes_new_rejects),
        # ── 直接 verifier 测试（round-11 生产接线，不 mock LLM）──
        ("test_verifier_wrong_denominator_warns",
         test_verifier_wrong_denominator_warns),
        ("test_verifier_correct_denominator_ok",
         test_verifier_correct_denominator_ok),
        ("test_verifier_no_procurement_topic_no_warn",
         test_verifier_no_procurement_topic_no_warn),
        ("test_verifier_round12_wrong_denominator_variants_warn",
         test_verifier_round12_wrong_denominator_variants_warn),
        ("test_verifier_round12_old_15pct_threshold_warns",
         test_verifier_round12_old_15pct_threshold_warns),
        ("test_agent_wires_verifier_for_wrong_procurement_formula",
         test_agent_wires_verifier_for_wrong_procurement_formula),
        ("test_agent_round12_wires_procurement_specific_warning_for_real_bypasses",
         test_agent_round12_wires_procurement_specific_warning_for_real_bypasses),
        ("test_rules_file_procurement_rate_spec",
         test_rules_file_procurement_rate_spec),
        ("test_agent_t48_answer_oracle",
         test_agent_t48_answer_oracle),
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

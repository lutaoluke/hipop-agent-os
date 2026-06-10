"""WS-145: 肯定执行意图门（Affirmative-execution intent gate）.

工作台执行前的结构性意图门 —— 把「只有用户明确要执行才进真实执行路由」写成
可测代码，不塞 SYSTEM_PROMPT、不靠逐句关键词黑名单。

两件事:

1. 句式语气门（结构判别，非穷举词表）
   只有「肯定的祈使执行句」才允许进入真实执行路由（run_workflow）。
   下列语气一律不执行，只解释 / 反问:
     - 否定句   不要刷新库存          → NEGATED
     - 询问句   能不能刷新库存？      → INTERROGATIVE
     - 假设句   如果刷新库存会影响什么 → HYPOTHETICAL
     - 只问影响 刷新库存有什么影响     → IMPACT_QUERY
   判别靠「动词所在分句的语气词法」（否定 / 疑问 / 假设语素是封闭小集合），
   不靠把执行动词同义词逐条拉黑/拉白——后者本质不收敛（WS-55/WS-128 教训）。

2. 风险分层 + 自动补调策略（固化 Luke 本轮决策）
   - 低风险 / 幂等 / 只影响工作台内部数据或分析结果 → 默认可自动补调一次；
     补调失败 → 不无限重试，转 plan→confirm，展示下一步计划 + 需确认原因。
   - 外部通知 / 交易·采购·订单 / 不可回滚 / 跨店批量覆盖 → 必须先 confirm，
     不自动补调。

本模块只依赖标准库（re/enum），可在无 anthropic SDK 的 CI 环境导入。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple


class IntentMood(Enum):
    EXECUTE = "execute"              # 肯定祈使执行：帮我刷库存
    NEGATED = "negated"             # 否定：不要刷新库存
    INTERROGATIVE = "interrogative"  # 询问：能不能刷新库存？
    HYPOTHETICAL = "hypothetical"   # 假设：如果刷新库存会影响什么
    IMPACT_QUERY = "impact_query"   # 只问影响面：刷新库存有什么影响
    NONE = "none"                   # 无执行动词，普通查询/闲聊


class RiskTier(Enum):
    LOW_AUTO = "low_auto"           # 幂等/内部数据/分析结果 → 可自动补调一次
    HIGH_CONFIRM = "high_confirm"   # 外部通知/交易/不可回滚/跨店批量 → 先 confirm


class RecoveryAction(Enum):
    AUTO_RETRY_ONCE = "auto_retry_once"  # 低风险首次失败 → 自动补调一次
    PLAN_CONFIRM = "plan_confirm"        # 低风险补调过仍失败 → 转 plan→confirm
    CONFIRM_FIRST = "confirm_first"      # 高风险 → 不自动补调，先 confirm


# ────────────────────────────────────────────────────────────────────────────
# 词法标记（语气是封闭小集合；执行动词与 agent 路由触发集对齐）
# ────────────────────────────────────────────────────────────────────────────

# T37×WS-145 整合：库存刷新动作允许「刷」与「库存」间夹 ERP/6 仓/数据 等限定词
# （同一分句内，不跨标点）——与 agent._STOCK_REFRESH_INTENT_RE 的 split 口径对齐，
# 否则「刷ERP库存 / 刷6仓库存」被漏判成无执行动词，gate 误放它们绕过语气门。
_STOCK_REFRESH_SPLIT = r"刷[^，。！？!?；;、,\n]{0,8}库存"

# 工作流刷新路由触发集 —— 与 agent._deterministic_workflow_request 保持一致，
# 额外补 "刷库存/刷库"（WS-145 验收 case 4「帮我刷库存」必须能路由）。
_REFRESH_TRIGGER_RE = re.compile(
    r"刷新|刷库存?|" + _STOCK_REFRESH_SPLIT + r"|刷一下|同步|重算|重新计算|重跑|跑一下|拉一下|扫"
)

# 外部通知动作词法（发飞书 / 通知某人 / 推到群 / @某人 …）—— 与 notify_via_feishu
# 工具 schema 声明的运营说法对齐（agent.py: "发到飞书 / 通知刘鹤 / 推到群里 / @同事"）。
# 在 _EXEC_VERB_RE（让 mood 识别为动作）和 _HIGH_RISK_RE（外部通知必 confirm-first）
# 两处共用，避免「通知刘鹤 / 推到群里 / @同事」被漏判成 mood=none / risk=low_auto。
_EXTERNAL_NOTIFY = (
    r"发飞书|发到飞书|飞书(?:通知|群|推送|消息)|"
    r"通知群|群通知|发通知|推送(?:通知|消息|到群|给|到飞书)|推到.{0,4}群|推进.{0,3}群|发到群|"
    r"发邮件|发短信|发消息给|@[一-鿿A-Za-z]|"
    r"通知(?:一下|下)?(?:刘鹤|老板|大家|对方|同事|运营|相关人?|群|"
    r"[一-鿿]{2,3}(?:同事|经理|总监?|老板))"
)

# 执行动词（mood 判别用，比路由触发集略宽，覆盖外部副作用动词）。
_EXEC_VERB_RE = re.compile(
    r"刷新|刷库存?|" + _STOCK_REFRESH_SPLIT + r"|刷一下|同步|重算|重新计算|重跑|跑一下|拉一下|扫|"
    r"更新|生成|创建|启动|触发|执行|跑一遍|重新跑|"
    r"下单|下采购|提交|取消订单|撤单|退款|" + _EXTERNAL_NOTIFY
)

# 否定语素（封闭集）—— 出现在执行动词「之前、同一分句内」才算否定该动作。
_NEGATION_RE = re.compile(
    r"不要|不用|无需|无须|不必|不想|不需要|没必要|别|甭|勿|暂不|先不|先别|先不要"
)

# 疑问语气：句末疑问助词，或问句型情态结构（能不能/可不可以…）。
_QUESTION_MODAL_RE = re.compile(
    r"能不能|能否|可不可以|是否|要不要|该不该|需不需要|会不会|行不行|"
    r"可以.{0,6}吗|能.{0,6}吗|方便.{0,4}吗"
)
_QUESTION_TAIL_RE = re.compile(r"(?:吗|呢|么)\s*[？?]?\s*$|[？?]\s*$")

# 假设/条件语素。
_HYPOTHETICAL_RE = re.compile(r"如果|假如|假设|要是|倘若|若是|万一|一旦")

# 只问影响面 / 后果 / 风险（无祈使）。
_IMPACT_RE = re.compile(
    r"有(?:什么|啥|哪些)影响|影响(?:什么|哪些|到什么|面|范围|大不大)|"
    r"会怎样|会怎么样|有(?:什么|啥)后果|后果(?:是什么|有哪些)|"
    r"风险(?:是什么|有哪些|多大|大不大)|会不会影响|动到什么|改动(?:什么|哪些)"
)

# 祈使/请求标记（帮我/请/现在就…），用于把「带请求语气的句子」判为执行而非纯疑问。
_IMPERATIVE_RE = re.compile(
    r"帮我|帮忙|给我|请(?!问)|麻烦|现在就|马上|立刻|立即|赶紧|快.{0,2}把|去把|来把"
)

# 高风险动作标记：外部通知 / 交易·采购·订单 / 不可回滚 / 跨店批量覆盖。
# 外部通知段复用 _EXTERNAL_NOTIFY（与 notify_via_feishu schema 对齐），其余为交易/不可回滚/批量。
_HIGH_RISK_RE = re.compile(
    r"采购单|采购订单|下采购|下单|报采购|发起采购|提交订单|提交采购|"
    r"取消订单|撤单|退款|退货|"
    r"删除|清空|批量(?:覆盖|修改|删除|更新)|全店(?:覆盖|修改|刷|铺)|跨店(?:批量|覆盖|铺)|"
    + _EXTERNAL_NOTIFY
)

_CLAUSE_SEP_RE = re.compile(r"[，。！？!?；;、,\n\r\t ]+")


def _exec_clause(text: str) -> str:
    """返回含第一个执行动词的分句（用于把否定语素锚定到该动词所在分句）。"""
    for part in _CLAUSE_SEP_RE.split(text):
        if _EXEC_VERB_RE.search(part):
            return part
    return text


def _clause_negated(clause: str) -> bool:
    """该分句里执行动词之前是否有否定语素（否定语义只在动词前生效）。"""
    m = _EXEC_VERB_RE.search(clause)
    if not m:
        return False
    return bool(_NEGATION_RE.search(clause[: m.start()]))


def has_execution_verb(question: str) -> bool:
    return bool(_EXEC_VERB_RE.search(question or "") or _REFRESH_TRIGGER_RE.search(question or ""))


def has_refresh_trigger(question: str) -> bool:
    """是否命中工作流刷新路由触发集（agent 据此选 wf1/wf3/wf5）。"""
    return bool(_REFRESH_TRIGGER_RE.search((question or "").lower()))


def classify_mood(question: str) -> IntentMood:
    """结构性句式语气判别。无执行动词 → NONE（普通查询，门不介入）。

    语气判别锚定在「执行动词所在分句」上，不扫全句 —— 否则一句祈使命令里若带个
    汇报性从句（「…并告诉我**是否**真的创建了任务」）会被全局疑问词误判成询问句。
    """
    q = (question or "").strip()
    if not q or not has_execution_verb(q):
        return IntentMood.NONE

    clause = _exec_clause(q)
    clause_is_imperative = bool(_IMPERATIVE_RE.search(clause))

    # 1) 否定（动词前否定语素）—— 最高优先：明确说「不要执行」。
    if _clause_negated(clause):
        return IntentMood.NEGATED
    # 2) 假设/条件句 —— 「如果…会…」不是执行请求（分句内判别）。
    if _HYPOTHETICAL_RE.search(clause):
        return IntentMood.HYPOTHETICAL
    # 3) 只问影响面 —— 同分句内问影响/后果/风险，只解释不执行。
    if _IMPACT_RE.search(clause):
        return IntentMood.IMPACT_QUERY
    # 4) 疑问句 —— 必须压过祈使请求：用户问「能不能帮我刷新库存？」里虽含「帮我」，
    #    但整体是询问而非执行命令，绝不能因为有「帮我」就误进执行路由（验门人红队洞）。
    #    判据（锚定执行动词分句，避免汇报性从句误伤）：
    #      a) 执行动词分句本身带疑问情态（能不能/可不可以/能否…）或以疑问助词收尾（…吗？）；
    #      b) 或整条消息以疑问收尾，且执行动词分句不是祈使命令分句
    #         （「帮我刷库存，告诉我是否成功？」执行分句「帮我刷库存」是命令 → 仍执行）。
    clause_is_question = bool(
        _QUESTION_MODAL_RE.search(clause) or _QUESTION_TAIL_RE.search(clause)
    )
    if clause_is_question or (_QUESTION_TAIL_RE.search(q) and not clause_is_imperative):
        return IntentMood.INTERROGATIVE
    # 5) 其余（含祈使请求与无修饰的祈使动词）= 肯定祈使执行。
    return IntentMood.EXECUTE


def classify_risk(question: str) -> RiskTier:
    """消息级动作风险分层。命中外部副作用/交易/不可回滚/跨店批量 → 高风险。"""
    if _HIGH_RISK_RE.search(question or ""):
        return RiskTier.HIGH_CONFIRM
    return RiskTier.LOW_AUTO


class GateDecision(NamedTuple):
    mood: IntentMood
    risk: RiskTier
    has_exec_verb: bool
    has_refresh_trigger: bool
    enters_execution: bool        # 允许进真实执行路由（肯定 + 低风险）
    needs_confirm_first: bool     # 肯定 + 高风险 → 必须先 confirm，不自动执行
    blocks_llm_execution: bool    # 非执行语气 → LLM 也不许偷偷 run_workflow


def evaluate(question: str) -> GateDecision:
    mood = classify_mood(question)
    risk = classify_risk(question)
    has_exec = has_execution_verb(question)
    enters = (mood == IntentMood.EXECUTE) and (risk == RiskTier.LOW_AUTO) and has_exec
    needs_confirm = (mood == IntentMood.EXECUTE) and (risk == RiskTier.HIGH_CONFIRM)
    blocks = has_exec and mood in (
        IntentMood.NEGATED,
        IntentMood.INTERROGATIVE,
        IntentMood.HYPOTHETICAL,
        IntentMood.IMPACT_QUERY,
    )
    return GateDecision(
        mood=mood,
        risk=risk,
        has_exec_verb=has_exec,
        has_refresh_trigger=has_refresh_trigger(question),
        enters_execution=enters,
        needs_confirm_first=needs_confirm,
        blocks_llm_execution=blocks,
    )


def enters_execution(question: str) -> bool:
    """肯定执行意图门:仅当肯定祈使 + 低风险 + 含执行动词时放行真实执行路由。"""
    return evaluate(question).enters_execution


def decide_recovery(tier: RiskTier, prior_auto_attempts: int) -> RecoveryAction:
    """自动补调策略:
    - 高风险:永远先 confirm，不自动补调。
    - 低风险:还没补过 → 自动补调一次；补过仍失败 → 转 plan→confirm（不无限重试）。
    """
    if tier == RiskTier.HIGH_CONFIRM:
        return RecoveryAction.CONFIRM_FIRST
    if prior_auto_attempts < 1:
        return RecoveryAction.AUTO_RETRY_ONCE
    return RecoveryAction.PLAN_CONFIRM


# ────────────────────────────────────────────────────────────────────────────
# 确定性回复构造（门拦下时给用户看的话；不含任何「已触发/已完成」假证据）
# ────────────────────────────────────────────────────────────────────────────

def explain_reply(mood: IntentMood, question: str = "") -> str:
    """非执行语气被门拦下时的解释性回复（不创建任务）。"""
    if mood == IntentMood.NEGATED:
        return (
            "收到，按你说的**不执行**这步刷新/重算 —— 本轮没有创建任何后台任务。"
            "需要时直接说「帮我刷新…」我再执行。"
        )
    if mood == IntentMood.INTERROGATIVE:
        return (
            "可以执行。这类刷新/重算是工作台内部的低风险动作，由我直接触发后台任务、"
            "前端看进度，你不用进终端跑脚本。**本轮我先不动手**（你是在问能不能）；"
            "确认要跑就说「帮我刷新…」，我立刻执行。"
        )
    if mood in (IntentMood.HYPOTHETICAL, IntentMood.IMPACT_QUERY):
        return (
            "说明影响面（**本轮不执行**）:这类刷新/重算只重写工作台内部数据或分析结果、"
            "可重复覆盖、不发外部通知、不动交易/订单，属低风险幂等动作。"
            "真要跑就说「帮我刷新…」，我再触发。"
        )
    return "本轮未执行。需要执行请明确说「帮我刷新/重算…」。"


def confirm_first_reply(question: str = "") -> str:
    """高风险动作:必须先 confirm，绝不自动执行/自动补调。"""
    return (
        "这步属于**高风险动作**（外部通知 / 交易·采购·订单 / 不可回滚 / 跨店批量覆盖），"
        "按规矩**必须先和你确认，不会自动执行、也不会自动补调**。\n"
        "下一步计划:我把要执行的对象、范围和预期影响列清楚给你核对;"
        "你回「确认」我才执行，回「取消」就停。"
    )


def recovery_plan_confirm_reply(label: str, reason: str = "") -> str:
    """低风险动作自动补调一次仍失败 → 转 plan→confirm，展示下一步 + 需确认原因。

    绝不返回「已触发/已启动/已完成」等假证据，也不无限重试。
    """
    why = f"（原因:{reason}）" if reason else ""
    return (
        f"{label}这步**自动重试一次后仍未成功**{why}，我不再自动重复触发。\n"
        f"下一步计划:转人工确认后再执行 —— 请先核对是否仍要继续。\n"
        f"需要你确认的点:回「确认」我再触发一次;或回「取消」先停下、改用上传/手动核对。"
    )

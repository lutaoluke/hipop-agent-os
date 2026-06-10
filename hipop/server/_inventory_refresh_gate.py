"""WS-159: 库存刷新询问式确认门（Inventory-refresh inquiry→confirm gate）.

把 WS-145 已落地的「询问句不执行、只解释能力/条件」接成**库存刷新专用的多轮确认门**:

  轮1 用户问「能不能/可不可以帮我刷新一下库存?」(询问式库存刷新请求)
       → 不创建任务;做可执行性判断;可以则说明范围 + 反问「确认要现在刷新吗」,
         不可/缺信息则说明缺口,不挂可执行 pending。
  轮2 用户裸确认「好/可以/确认/刷新吧/麻烦了」且上一轮存在库存刷新提议
       → 只执行一次真实 wf1_stock_v2(走 agent 既有 run_workflow 路由)。
  轮2' 取消 / 换题 / 模糊回复 / 无 pending 时裸说「好」
       → 不执行,清掉旧 pending 或要求说明要执行什么。

设计原则(与 WS-145 一致):
  - 结构判别,非穷举词表:语气/确认/取消都是封闭小集合;不靠把执行动词逐条拉黑白。
  - 确定性代码,不塞 SYSTEM_PROMPT。
  - 跨轮 pending 不落库:由消息历史(上一轮 user 询问 + 上一轮 assistant 提议标记)
    结构性推出,且只对**紧接下一轮**有效 —— 换题后上一轮不再是询问句,pending 自然失效。
  - 所有回复不含「已触发/已启动/已完成/已刷新」等假证据。

本模块只依赖标准库 + 同包 _execution_intent_gate(同为纯 stdlib),可在无 anthropic SDK 的
CI 环境导入。真实执行路由(run_workflow / wf1_stock_v2)与可执行性判断的副作用(查冲突任务、
店铺范围)留在 agent.py,本模块只做文本结构判别与回复构造。
"""
from __future__ import annotations

import re

from . import _execution_intent_gate as _intent_gate


# 上一轮 assistant 提议里固定带的标记句 —— agent 据此判定「确实提过、可执行的刷新 pending」。
# 提议(可执行)回复一定含它;不可执行/缺信息回复一定不含 —— 所以「marker 在 = 有可执行 pending」。
PROPOSAL_MARKER = "确认要现在刷新吗"


# ── 确认语素(封闭小集合)。裸确认 = 只表态同意、不引入新请求/新话题。────────────────
_CONFIRM_TOKEN_RE = re.compile(
    r"好的|好呀|好啊|好嘞|好吧|好|可以|没问题|没事|行吧|行啊|行|确认|确定|对的|对呀|对|是的|"
    r"ok|okay|okk|要的|需要|麻烦你了|麻烦您了|麻烦了|麻烦|拜托|有劳|开始吧|开始|继续吧|继续|"
    r"就这样|这样吧|刷新吧|刷吧|刷新一下吧|刷一下吧|去吧|来吧|搞吧|弄吧|嗯",
    re.IGNORECASE,
)

# 裸确认里允许残留的填充词/范围词 —— 去掉确认词与这些填充后若一无所剩,即纯确认。
_CONFIRM_FILLER_RE = re.compile(
    r"刷新|刷库存?|刷库|库存|现在|就|马上|立刻|立即|赶紧|了|吧|啦|呢|哦|噢|呀|啊|"
    r"你|您|我|帮|一下|那|那就|这|呗|嘛|的|是|对|麻烦|嗯|哈|呵"
)
_PUNCT_RE = re.compile(r"[，。!！?？、,.\s~…:：;；'\"\-—()（）]+")

# 取消语素(封闭集)。
_CANCEL_RE = re.compile(
    r"不用了|不用|先别|别刷|先不要|先不刷|先不|不刷了|不刷|不要刷|取消|算了|"
    r"不需要刷|不需要了|停一下|先停|撤回|撤销|放弃|不弄了|不搞了"
)


def is_cancellation(text: str) -> bool:
    """本轮是否为取消/暂缓表态。"""
    return bool(_CANCEL_RE.search(text or ""))


def is_inventory_refresh_inquiry(text: str) -> bool:
    """本轮是否为「询问式库存刷新请求」:能不能/可不可以…刷新…库存?

    判据(复用 WS-145 结构判别):疑问语气 + 命中刷新触发集 + 涉及「库存」+ 低风险。
    高风险询问(下采购单/发飞书)不在此门 —— 由 WS-145 confirm-first 兜，绝不被一句「好」解锁。
    """
    t = text or ""
    if "库存" not in t:
        return False
    d = _intent_gate.evaluate(t)
    return (
        d.mood == _intent_gate.IntentMood.INTERROGATIVE
        and d.has_refresh_trigger
        and d.risk == _intent_gate.RiskTier.LOW_AUTO
    )


def is_confirmation(text: str) -> bool:
    """本轮是否为「裸确认」(同意上一轮提议,不引入新请求/新话题/高风险动作)。

    去掉确认词 + 填充词 + 标点后一无所剩 = 裸确认。这样「好」「可以」「确认」「刷新吧」
    「好,麻烦了」都算确认;而「好的去查一下物流」(换题)、「好啊那帮我下采购单」(高风险)
    残留实义词 → 不算裸确认,不会误解锁。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 高风险动作绝不当裸确认(防一句「好…下采购单」解锁高风险)。
    if _intent_gate.classify_risk(t) == _intent_gate.RiskTier.HIGH_CONFIRM:
        return False
    # 取消优先于确认(「好吧那算了」按取消处理,不解锁执行)。
    if is_cancellation(t):
        return False
    # 询问句不是确认(「可不可以刷新库存?」里含「可以」但整体是询问)。
    if is_inventory_refresh_inquiry(t):
        return False
    if not _CONFIRM_TOKEN_RE.search(t):
        return False
    residue = _CONFIRM_TOKEN_RE.sub("", t)
    residue = _CONFIRM_FILLER_RE.sub("", residue)
    residue = _PUNCT_RE.sub("", residue)
    return residue == ""


# ── 确定性回复构造(门拦下/提议时给用户看的话;无任何「已触发/已完成」假证据)────────

def proposal_reply(store: str) -> str:
    """轮1 可执行 → 说明范围 + 反问确认。必含 PROPOSAL_MARKER(供下一轮判定 pending)。"""
    return (
        f"可以刷新。我要刷的是 **{store} 当前店铺的库存**"
        f"(工作流 wf1_stock_v2:拉 ERP 6 仓 + noon 库存并重写工作台库存数据,"
        f"属低风险幂等动作 —— 不发外部通知、不动交易/订单)。\n"
        f"**本轮我先不动手**(你是在问能不能)。{PROPOSAL_MARKER}?"
        f"回「好/可以/确认/刷新吧」我就立刻刷一次;回「不用/取消」就不刷。"
    )


def infeasible_reply(reason: str, next_step: str) -> str:
    """轮1 不可执行/缺信息 → 说明缺口与下一步,不挂可执行 pending(不含 marker)。"""
    return (
        f"这次**先不能直接刷**:{reason}。\n"
        f"下一步:{next_step}。\n"
        f"(本轮没有创建任何后台任务,也没有挂起可执行的刷新提议。)"
    )


def bare_confirm_no_pending_reply() -> str:
    """无 pending 时裸说「好/可以」→ 不执行,要求补充要执行什么。"""
    return (
        "我这边没有挂着待确认的库存刷新(上一轮没有提议要刷新)。"
        "需要执行请明确说要做什么,例如「帮我刷新库存」,我再执行 —— 本轮没有创建任何后台任务。"
    )


def cancelled_reply() -> str:
    """有 pending 但本轮取消 → 不执行,作废提议。"""
    return (
        "好的,按你说的**不刷新** —— 本轮没有创建任何后台任务,刚才的刷新提议也已作废。"
        "需要时再说「帮我刷新库存」我来执行。"
    )


def pending_now_infeasible_reply(reason: str, next_step: str) -> str:
    """确认轮想刷但此刻已不可执行(如确认前冒出冲突任务)→ 不执行,说明原因。"""
    return (
        f"想刷,但**这次刷不了**:{reason}。\n"
        f"下一步:{next_step}。\n"
        f"(本轮没有创建任何后台任务。)"
    )

"""WS-146 执行声明假活承重墙 —— execution-claim 证据契约（结构判别，非穷举黑名单）。

与 WS-161 `_factslot_contract` **同根不同槽**：WS-161 管读类工具的事实槽（库存数量 /
承运商 / 运单号 / 状态），本模块管**执行声明槽**（已启动 / 已刷新 / 已开始重算 / 任务号 /
已完成）。复用同一条承重墙 doctrine：

  以「本轮有没有真实后台任务证据」的**证据契约**为源头，无证据 → 执行声明槽证成空 →
  走确定性「未执行」模板 + 把正文里无依据的执行声明就地删掉；**不靠黑名单逐句枚举措辞、
  不调正则相位**（这正是熔断 3 轮的根因：自由中文逐句加/减词永远在「按住一头翘起另一头」）。

为什么这次会收敛（对照熔断 3 轮的相位打地鼠）
--------------------------------------------------
1. 闭集 + 形状判别，非穷举措辞：
   - 执行动作是**系统的有限操作集**（刷新/重算/同步/执行/触发/采集…）—— 领域建模，
     与 WS-161 的承运商闭集同理。趋势词（改善/回升/下滑/好转…）**结构上不在这个闭集里**，
     所以「周转已开始改善」天然放行，不需要逐句把它加进白名单。
   - 任务号是 8 位 hex 的 **id 形 token**（形状判别），与 WS-161 的运单号同理。
2. 证据契约做门，不靠 reply 措辞判真假：
   - proven = run_workflow ok=True+task_id（真建了后台任务）或任务完成回读证据。
     proven=False → 任何「系统执行了某后台操作」的声明都是空槽编造 → 删。
   - proven=True（真实回执）→ 执行声明放行，只移除非 allow-set 的伪造任务号。
3. 分句边界含中文逗号「，、,」——熔断 round-3「已开始改善，建议执行…」跨逗号吃到后半句
   「执行」而整句误删的根因点，这里**逐分句判别**，不跨标点。
4. 时效客观事实豁免：更新/刷新/同步 + 具体日期（更新到 2026-06-09）是状态事实、不是
   「本轮执行了刷新」——结构上用「日期锚点」排除，不靠措辞。

本模块只依赖标准库 + `_chat_boundary`（任务完成回读判定），可在无 SDK 的 CI 环境导入。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# ── 分句（边界含中文逗号/顿号，熔断 round-3 过切根因点）────────────────────────────
_CLAUSE_SEP = "，。！？!?；;、,\n\r\t"
_CLAUSE_SPLIT_RE = re.compile(rf"([{_CLAUSE_SEP}]+)")
_NOSEP = rf"[^{_CLAUSE_SEP}]"

# Form A —— 执行实体主语（任务/工作流/后台任务/后台流程）。收窄到强执行实体，避免
# 「库存/销量」这类指标主语 + 趋势体被误判。
_EXEC_SUBJECT_RE = re.compile(r"工作流|后台任务|后台流程|(?<![一-鿿])任务")

# 执行动作闭集（系统/工作流有限操作；领域建模，**不含趋势词**）。趋势词（改善/回升/下滑/
# 好转/回暖/走低/走高/增长/下降/上升/回落/企稳/放缓/恶化/向好）不在此集。
_EXEC_VERBS = (
    r"刷新|刷库存?|重算|重新计算|重跑|重新跑|同步|执行|启动|触发|创建|提交|受理|"
    r"采集|抓取|入库|拉取|拉数|扫描|生成|重置|录入"
)
# 库存/销量领域的数据对象闭集（C 式语序「已完成<对象><动词>」用；领域建模，非穷举措辞）。
_DATA_OBJ = r"库存|销量|物流|数据|补货|订单|商品|价格|报表|采购|出库|入库|销售周期|价格表"
# 完成/启动体（B 式「<动词>+体」用）。**强制带「已」或「了」**，避开「完成度/成功率/中文/
# 集中」这类无 aspect 标记的假友。
_DONE_ASPECT_B = r"已开始|已启动|已触发|已提交|已受理|已创建|已完成|已完毕|完成了|完毕了"

# Form B —— **启动/完成体直接绑定执行动词**（route-b round-1 打回根因：旧版「aspect 与 exec
# 同分句共现即删」会把「已开始**改善**并建议**执行**…」误删——已开始修饰趋势词改善、执行属建议
# 语气，二者不绑定）。要求体与执行动词**贴邻**，且覆盖中文双向语序（round-2 打回根因 = 只覆盖
# 一个方向）：
#   A. 体在前贴邻动词：已刷新 / 正在重算 / 已开始执行
#   B. 动词在前 + 体贴邻（自然语序）：刷新已开始 / 重算已完成 / 同步已开始
#   C. 完成体 + 数据对象闭集 + 动词：已完成库存刷新 / 已完成重算
#   D. 已(经)在…后台 + 跑/运行/执行/处理：已在后台跑
# 「建议执行 / 可执行 / 应执行」里执行前是建议语气词、非启动体，且 B/C 要求 aspect 贴邻动词
# （非跨「改善并建议」之类）→ 不命中，round-1 的无逗号 FP 保持闭合。
_EXEC_BOUND_RE = re.compile(
    r"(?:已经?|正在)(?:开始)?(?:" + _EXEC_VERBS + r")"                       # A
    r"|(?:" + _EXEC_VERBS + r")(?:" + _DONE_ASPECT_B + r")"                  # B
    r"|(?:已完成|已完毕|完成了)(?:" + _DATA_OBJ + r")?(?:" + _EXEC_VERBS + r")"  # C
    r"|已经?在[^，。！？!?；;、,\n]{0,4}后台[^，。！？!?；;、,\n]{0,6}(?:跑|运行|执行|处理|算)"  # D
    r"|后台[^，。！？!?；;、,\n]{0,4}(?:跑|运行)了"
)

# Form A 用的启动/完成体（仅与强执行主语共现时才算；含任务完成态如「任务已完成」）。
_ASPECT_RE = re.compile(
    r"已开始|已启动|已触发|已提交|已受理|已创建|已完成|完成了|已完毕|"
    r"已在[^，。！？!?；;、,\n]{0,4}后台|已经在[^，。！？!?；;、,\n]{0,4}(?:后台|跑)|"
    r"正在|进行中|已执行|已重新计算|已重算|已刷新|已同步|已生成"
)

# accepted / SSE 假任务证据（执行声明槽的另一种形状）。
_FAKE_TASK_EVIDENCE_RE = re.compile(
    r"(?:状态|status)[^，。！？!?；;、,\n]{0,8}accepted"
    r"|任务[^，。！？!?；;、,\n]{0,12}accepted"
    r"|SSE[^，。！？!?；;、,\n]{0,12}(?:推送|进度|实时|订阅)"
    r"|前端[^，。！？!?；;、,\n]{0,8}(?:推送进度|SSE|订阅.{0,4}进度)",
    re.IGNORECASE,
)

# 时效客观事实（freshness）：更新/刷新/同步/截至 + (到/至/于) + **具体日期** → 状态事实，
# 不是「本轮执行了刷新」。结构用日期锚点排除，不靠措辞。
_DATE_RE = r"(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}[日号])"
_FRESHNESS_RE = re.compile(
    r"(?:更新|刷新|同步|截至)[^，。！？!?；;、,\n]{0,4}?(?:到|至|于|是|为)?\s*" + _DATE_RE
)

# 任务号 8 位 hex —— 仅在「任务/task」上下文里抓（避免误吃日期/SKU/纯数字）。
_TASK_ID_CTX_RE = re.compile(
    r"((?:任务\s*(?:号|编号|ID)?|task[\s_]*id))[\s:：是为]*([0-9a-fA-F]{8})\b",
    re.IGNORECASE,
)

_REDACT_EXEC = "[本轮未执行 / 未创建后台任务]"
_REDACT_TASKID = "[任务号未确认]"
# 相邻重复 marker（多个执行声明分句被连删）折叠成一个，避免「[…]，[…]，[…]」。
_MARKER_DEDUP_RE = re.compile(
    r"(?:" + re.escape(_REDACT_EXEC) + r")(?:\s*[，、,。；;]?\s*" + re.escape(_REDACT_EXEC) + r")+"
)
_UNEXEC_TEMPLATE = (
    "**本轮未执行后台任务**：未检测到真实任务证据"
    "（无 run_workflow 成功 task_id，也无任务完成回读）。"
    "上述「已启动 / 已刷新 / 已开始执行 / 任务号」等执行声明无依据，系统本轮未实际执行该操作；"
    "如需执行，请明确说「帮我刷新 / 重算…」，我再触发。"
)


# ── 证据契约（execution-claim slot proven）──────────────────────────────────────

def _real_run_workflow(tool_log: list) -> bool:
    # task_id 为真 ⟺ 后台任务真建了：provider 成功分支写 task_id=<id>，失败分支写 task_id=None。
    # 不强求 ok 字段（部分受理回执/fixture 只带 task_id 不带 ok）。
    return any(
        t.get("name") == "run_workflow" and t.get("task_id")
        for t in (tool_log or [])
    )


def exec_proven(tool_log: list, tools_used: Optional[list] = None) -> Tuple[bool, str]:
    """本轮执行声明槽是否被真实证据 ground。返回 (proven, mode)。

    判据（优先级，从强到弱）：
      1. run_workflow ok=True+task_id（真建了后台任务）            → (True, "real")。
      2. 任务完成回读证据（task_result/status=done…）            → (True, "done")。
      3. tool_log 有 run_workflow 条目但都不是 ok+task_id（失败/未建） → (False, "none") → 删。
      4. tool_log 无 run_workflow 条目，但 tools_used 含 run_workflow
         （调用形状未带进 tool_log，无法证伪）                    → (True, "ambiguous") → 不删（保守）。
      5. 两处都没有 run_workflow，无完成证据                       → (False, "none") → 删。
    """
    wf_entries = [t for t in (tool_log or []) if t.get("name") == "run_workflow"]
    if any(t.get("task_id") for t in wf_entries):
        return True, "real"
    try:
        from . import _chat_boundary as _cb
        if _cb._has_task_done_evidence(tool_log or []):
            return True, "done"
    except Exception:
        pass
    if wf_entries:
        return False, "none"
    if "run_workflow" in (tools_used or []):
        return True, "ambiguous"
    return False, "none"


def _allow_task_ids(tool_log: list) -> set:
    return {
        (t.get("task_id") or "").lower()
        for t in (tool_log or [])
        if t.get("name") == "run_workflow" and t.get("task_id")
    }


# ── 结构判别（单分句内）────────────────────────────────────────────────────────

def is_exec_claim(clause: str) -> bool:
    """该分句是否在声称「系统本轮执行/启动了某后台操作」（结构判别，非措辞穷举）。

    时效客观事实（更新到<日期>）与趋势词（改善/回升…，不在执行动作闭集）一律 False。
    """
    if not clause:
        return False
    if _FRESHNESS_RE.search(clause):
        return False
    if _FAKE_TASK_EVIDENCE_RE.search(clause):
        return True
    # Form B：启动/完成体**直接绑定**执行动词（贴邻），不靠同句共现。
    if _EXEC_BOUND_RE.search(clause):
        return True
    # Form A：强执行实体主语（任务/工作流）+ 启动/完成体（覆盖「任务已完成/任务已创建」，
    # 这类完成态动词不在执行动词集、靠主语绑定）。
    return bool(_EXEC_SUBJECT_RE.search(clause) and _ASPECT_RE.search(clause))


# ── scrub + 确定性模板 ─────────────────────────────────────────────────────────

def scrub_exec_claims(reply: str, tool_log: list, tools_used: Optional[list] = None) -> Tuple[str, List[str]]:
    """无真实任务证据 → 删执行声明分句 + 任务号；有真实回执 → 仅删非 allow-set 伪造任务号。"""
    if not reply:
        return reply, []
    warns: List[str] = []

    proven, mode = exec_proven(tool_log, tools_used)
    if proven:
        # mode=="ambiguous"：tool_log 未带 run_workflow 形状，算不出 allow-set，**不动正文**
        # （保守，避免误删合法受理回执的真实任务号）。仅在能算出 allow-set 时（real/done）
        # 移除「任务上下文里、却不在本轮 allow-set」的伪造任务号。
        if mode == "ambiguous":
            return reply, warns
        allow = _allow_task_ids(tool_log)

        def _id_keep_allow(m: re.Match) -> str:
            if m.group(2).lower() in allow:
                return m.group(0)
            return f"{m.group(1)} {_REDACT_TASKID}"

        new, n = _TASK_ID_CTX_RE.subn(_id_keep_allow, reply)
        if n and new != reply:
            warns.append(
                "⚠️ 执行声明承重墙（WS-146）：移除了非本轮 run_workflow 返回的伪造任务号 / task_id"
            )
        return new, warns

    # proven=False —— 逐分句删执行声明（不跨逗号），再删任务上下文里的任务号。
    parts = _CLAUSE_SPLIT_RE.split(reply)
    redacted_clause = 0
    for i, seg in enumerate(parts):
        if not seg or _CLAUSE_SPLIT_RE.fullmatch(seg):
            continue
        if is_exec_claim(seg):
            parts[i] = _REDACT_EXEC
            redacted_clause += 1
    out = "".join(parts)
    out = _MARKER_DEDUP_RE.sub(_REDACT_EXEC, out)
    out, n_id = _TASK_ID_CTX_RE.subn(lambda m: f"{m.group(1)} {_REDACT_TASKID}", out)

    if redacted_clause or n_id:
        warns.append(
            "⚠️ 执行声明承重墙（WS-146）：本轮无真实后台任务证据（无 run_workflow 成功 task_id / "
            "无任务结束回读），已移除正文中「已启动/已开始执行/任务号」等无依据的执行声明 — "
            "系统本轮未真正发起该后台操作（hallucinate）"
        )
    return out, warns


def apply(reply: str, tool_log: list, question: Optional[str] = None,
          tools_used: Optional[list] = None) -> Tuple[str, List[str]]:
    """承重墙总入口（供 _safety.sanitize_reply 调用，与 _factslot_contract.apply 并列）。

    无真实任务证据且删过执行声明 → 前置确定性「未执行」模板（与 WS-161 enforce_failure_template
    同型，让用户看到「本轮没执行」而非被假启动误导）。
    """
    reply, warns = scrub_exec_claims(reply, tool_log, tools_used)
    proven, _mode = exec_proven(tool_log, tools_used)
    if warns and not proven and _UNEXEC_TEMPLATE not in reply:
        # 仅在确有执行声明被删时前置模板（scrub 产生了 warning 即代表删过）。
        reply = _UNEXEC_TEMPLATE + "\n\n" + reply
    return reply, warns

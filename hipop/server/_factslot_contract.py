"""WS-161 B-2 禁编承重墙 —— fact-slot 证据契约（结构判别，非穷举黑名单）。

为什么是承重墙而不是又一层正则补洞
----------------------------------
WS-55 / WS-128 的教训：反幻觉门逐句往黑名单加同义词永远补不完。本模块把"查不到 /
实时源失败 / 工具返回为空时不能编结果"前移到**数据流和回答结构**上，并且——按 WS-161
返工口径——不只是"前置一段模板 + 加 warning"，而是**真的把答案正文里编造的结果槽值删掉**：

  1. 数据流边界（provider tool-loop）：每个 fact-slot 工具（query_sku_live /
     query_order_live / query_stock_split）的结构化返回被抽成证据快照，挂进 tool_log。
     快照里带出工具**实际返回**的结果槽值集合（承运商 forwarders / 运单号 tracking_nos /
     状态 statuses）；失败/空/未取到槽值的分支这些集合为空。

  2. 回答结构（sanitize_reply 入口，第一层；路线 b = 源头结构化槽位渲染）：
     a) scrub_fabricated_slots —— 删模型正文里编造/跨槽搬运的结果槽值：
        · 承运商（闭集实体）/运单号（id 形 token）/状态（状态桶）：包含关系判别，
          值必须 ∈ 工具实际返回的对应集合，否则删。
        · 库存数量：**slot-aware 值-槽绑定校验**——不只是"值出现过"，而是"总库存 N"的 N
          必须 == 工具 total、"义乌 N" 必须 == yiwu …。这正是连续 3 轮红队卡的
          「总库存 509/义乌 200」被写成「总库存 200」那类**值真关系假的跨槽搬运**，flat
          包含关系放行、slot-aware 绑定才能拦。失败/空分支全删。
     b) render_factslot_block —— 成功调用按槽位**确定性渲染**工具结构化权威明细
        （总库存/各仓拆分、货单/承运商/运单号/状态），值-槽绑定原样进槽、模型不参与这些
        字段的措辞拼装，前置为事实来源。
     c) enforce_failure_template —— 失败/空的实体确定性前置错误模板（点名实体 + 哪个源
        失败 + 当前不能确认 + 结果槽留空），保证用户看到"查不到"而不是被编造结果误导。

  承运商闭集 / id 形 token / qty 形是"领域建模 + 形状判别"，不是穷举措辞，复用 _safety 里
  WS-133 Round-5 已建好的 _CARRIER_RE / _ID_TOKEN_RE / _QTY_RESULT_RE。B-1（_safety 下游
  各正则/语义门）保留作第二层纵深防御（WS-161 验收 3）。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

# 产出"承运商 / 运单号 / 库存数量 / 状态"等结果槽的实时/库存工具。
FACTSLOT_TOOLS = {"query_sku_live", "query_order_live", "query_stock_split"}

# 携带承运商/运单号/状态槽的实时物流工具。
_LOGISTICS_TOOLS = {"query_sku_live", "query_order_live"}
# 携带库存数量槽的工具。
_STOCK_TOOLS = {"query_stock_split"}

# 这两个"查无此实体"错误已由 _safety 的 T26 Rule B/D 出确定性负控前缀；
# 本契约仍对它们做正文 scrub + provenance（allow-set 空），但模板 prepend 交给 T26，避免双前缀。
_DEFERRED_TO_T26 = {"order_not_found_in_erp", "sku_no_orders_in_erp"}

_ENTITY_LABEL = {"sku": "SKU", "order": "货单"}
_SLOT_LABEL = {
    "query_sku_live": "在途承运商/运单号/状态",
    "query_order_live": "承运商/运单号/状态",
    "query_stock_split": "库存数量",
}
_DEFAULT_SOURCE = {
    "query_sku_live": "ERP 实时查询",
    "query_order_live": "ERP 实时查询",
    "query_stock_split": "库存快照(wf1_stock)",
}

# 状态/ETA 类正向断言（纯失败分支才删；闭集 ≈ 物流领域状态词，非穷举措辞）。
_STATUS_PHRASE_RE = re.compile(
    r"(?:已|正在|正)?(?:发货|发出|揽收|签收|出库|发运|送达|派送|妥投|清关|到仓|到货|到达|抵达)"
    r"|在途(?:中)?"
    r"|运输途中|派送中|配送中|已揽收|已签收|已发货"
    r"|预计.{0,10}(?:到仓|到货|到达|送达|送到|签收|抵达)"
    r"|状态[:：]?\s*(?:在途|已发货|运输中|派送中|待发货|已签收)"
)

_REDACT_CARRIER = "[承运商未确认]"
_REDACT_ID = "[单号未确认]"
_REDACT_QTY = "[数量未确认]"
_REDACT_STATUS = "[状态未确认]"
# 路线(甲) 成功分支：事实槽绑定只在权威块出现，正文一律替换为指向块的引用
_REF_BLOCK = "[详见上方明细]"

# 物流状态闭集 → 规范桶（领域建模，非穷举措辞）。成功分支用"桶包含关系"判别：
# reply 断言的状态桶必须 ∈ 工具返回状态的桶集合；同桶内的同义改写（待发货/等待发货）放行，
# 跨桶编造（工具 待发货[PENDING] vs reply 已签收[DELIVERED]）拦。
_STATUS_BUCKETS = {
    "PENDING":   ["待发货", "待发", "待出库", "未发货", "备货", "待发运", "等待发"],
    "INTRANSIT": ["在途", "运输", "派送", "配送", "已发货", "已发出", "已发运",
                  "已揽收", "揽收", "出库", "运输途中", "在运", "运输中", "派送中", "配送中"],
    "DELIVERED": ["已签收", "签收", "已妥投", "妥投", "已送达", "送达", "已到货",
                  "到货", "已到仓", "到仓", "已完成", "抵达", "已抵达"],
    "CUSTOMS":   ["清关"],
}


def _status_buckets(text: str) -> set:
    """文本里出现的状态桶集合（空集 = 无可识别状态词）。"""
    t = str(text or "")
    return {b for b, kws in _STATUS_BUCKETS.items() if any(k in t for k in kws)}


# 库存语境标签（四仓/库存领域的有限实体集，非穷举措辞）。Round-3 用于"标签驱动 +
# fail-closed"判定：数字邻近哪个标签、是否绑定到具体槽位。
_STOCK_LABELS = [
    "义乌", "东莞", "沙特一号仓", "沙特", "海外仓", "海外", "国内仓", "国内", "一号仓",
    "总仓", "noon", "总库存", "总量", "总计", "总数", "国际在途", "库存", "在库", "在途", "现货",
    "待发货", "待发", "待发出", "待出库", "可售", "可用",
]

# Round-3/4：库存数字改"标签驱动 + fail-closed"（不再枚举连接词）。
# 纯数字 token（两侧非字母数字）—— 不吃 SKU/货单号里的数字（PD2026001 / TBC0168A）。
_BARE_NUM_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])")
# 数字紧跟的库存计量单位 → 这是个库存数量
_STOCK_UNIT_RE = re.compile(r"^\s*(?:件|个|箱|套|双|pcs|PCS|现货)")
# 数字右侧的**明确非库存**指示（时间/百分比/货币/小数续位/日期分隔）→ 这数字不是库存量（Round-4 打回点 3）
_NONSTOCK_RIGHT_RE = re.compile(r"^\s*(?:秒|%|％|‰|天|日(?![用])|号|月|年|周|小时|分钟|分|时|元|¥|\$|美元|次|页|倍|\.|．|:|：|-|/)")
# 子句边界（库存数字只在"本子句的前置库存标签"下绑定，防下一个槽的标签隔句抢绑）。
# 注意：不含冒号——"总库存:200""总库存：200" 里冒号是"标签:值"连接符而非子句边界，
# 切了会让"总库存"与它的值分到两句、数字绑不到标签而漏过。
_CLAUSE_DELIM_RE = re.compile(r"[。！？!?，,、；;\n（）()【】「」]")
# value-before-label / 括号标签：数字之后跨过 可选库存单位 + 直接连接词/括号/冒号 后紧跟库存标签
# → "509 件是总库存""509 件(总库存)""200 件在义乌" 这类后置标签绑定（验门人 Round-1·打回点 1）。
# 故意不含逗号/顿号——",总库存" 是另起一项而非本数字的标签，含了会误删逗号后的补货数字。
_STOCK_FOLLOW_PREFIX_RE = re.compile(r"^\s*(?:件|个|箱|套|双|pcs|PCS)?\s*(?:是|为|在|属于|计入|记入|的|（|\(|：|:)?\s*")


def _safety_detectors():
    """惰性引用 _safety 的 WS-133 Round-5 领域检测器，避免模块级循环导入。"""
    from . import _safety
    return _safety._CARRIER_RE, _safety._ID_TOKEN_RE, _safety._QTY_RESULT_RE


# ── 证据快照（数据流边界，provider 调用） ────────────────────────────────────────

def factslot_evidence_from_result(tool_name: str, result) -> Optional[dict]:
    """把 fact-slot 工具的结构化返回抽成证据快照。

    供 _provider_anthropic / _provider_openai 在 tool-loop 里调用并挂进 tool_log。
    抽成函数避免两个 provider 各写一份，smoke 也能直接 import 测。
    返回 None 表示该工具不在 fact-slot 范围。

    关键字段：
      ok            —— 工具是否成功（ok=True 且未 fail_closed）。
      slots_proven  —— 是否真返回了 ≥1 个具体结果槽值（承运商/运单号/库存数量）。
                       ok=True 但 0 槽值（空返回）也算"无可信结果槽"，与失败同等处理。
      forwarders / tracking_nos / statuses —— 工具实际返回的槽值 allow-set。
    """
    if tool_name not in FACTSLOT_TOOLS or not isinstance(result, dict):
        return None
    ok = bool(result.get("ok")) and not result.get("fail_closed")
    ev: Dict = {
        "tool": tool_name,
        "ok": ok,
        "error": result.get("error"),
        "message": result.get("message"),
        "source": result.get("source") or _DEFAULT_SOURCE.get(tool_name),
        "forwarders": [],
        "tracking_nos": [],
        "order_nos": [],
        "statuses": [],
        "has_stock_value": False,
        "stock_values": [],
        "stock_render": [],   # 路线(b)：有序 (label, value) —— 确定性槽位渲染用，值-槽绑定原样
        "stock_bind": {},     # {label 关键词: value} —— slot-aware 绑定校验用
        "orders": [],         # 物流确定性渲染行：{order_no,forwarder,tracking_no,qty,status}
    }
    if tool_name == "query_order_live":
        ev["entity"] = result.get("order_no")
        ev["entity_kind"] = "order"
        if result.get("order_no"):
            ev["order_nos"].append(str(result["order_no"]))
        if ok:
            if result.get("forwarder"):
                ev["forwarders"].append(str(result["forwarder"]))
            if result.get("tracking_no"):
                ev["tracking_nos"].append(str(result["tracking_no"]))
            if result.get("status"):
                ev["statuses"].append(str(result["status"]))
            ev["orders"].append({
                "order_no": result.get("order_no"),
                "forwarder": result.get("forwarder") or None,
                "tracking_no": result.get("tracking_no") or None,
                "status": result.get("status") or None,
                "qty": None,
            })
    elif tool_name == "query_sku_live":
        ev["entity"] = result.get("sku")
        ev["entity_kind"] = "sku"
        if ok:
            for o in (result.get("in_transit_orders") or []):
                if not isinstance(o, dict):
                    continue
                if o.get("forwarder"):
                    ev["forwarders"].append(str(o["forwarder"]))
                if o.get("tracking_no"):
                    ev["tracking_nos"].append(str(o["tracking_no"]))
                if o.get("order_no"):
                    ev["order_nos"].append(str(o["order_no"]))
                ev["orders"].append({
                    "order_no": o.get("order_no"),
                    "forwarder": o.get("forwarder") or None,
                    "tracking_no": o.get("tracking_no") or None,
                    "status": "在途",
                    "qty": o.get("qty"),
                })
            # 有在途货单 → "在途" 是工具背书的合法状态（成功分支状态 allow-set）
            if result.get("in_transit_orders"):
                ev["statuses"].append("在途")
            # recent_completed 的承运商/货单号也是工具真给出的合法实体，纳入 allow-set
            for o in (result.get("recent_completed") or []):
                if not isinstance(o, dict):
                    continue
                if o.get("forwarder"):
                    ev["forwarders"].append(str(o["forwarder"]))
                if o.get("order_no"):
                    ev["order_nos"].append(str(o["order_no"]))
            if result.get("recent_completed"):
                ev["statuses"].append("已完成")  # 近期完成单 → 已签收/已完成 合法
    else:  # query_stock_split → entity = sku，槽 = 库存数量
        ev["entity"] = result.get("sku")
        ev["entity_kind"] = "sku"
        if ok and result.get("total") is not None:
            ev["has_stock_value"] = True
            split = result.get("split") or {}
            if not isinstance(split, dict):
                split = {}

            def _int(v):
                if isinstance(v, bool) or v is None:
                    return None
                if isinstance(v, int):
                    return v
                if isinstance(v, float) and v.is_integer():
                    return int(v)
                return None

            # 路线(b)：值-槽绑定原样进槽 —— (展示label, value, *绑定关键词)
            slot_spec = [
                ("总库存", _int(result.get("total")), "总库存", "总量", "总计", "总数"),
                ("义乌", _int(split.get("yiwu")), "义乌"),
                ("东莞", _int(split.get("dongguan")), "东莞"),
                ("国内仓合计", _int(split.get("domestic")), "国内仓合计", "国内仓", "国内"),
                ("沙特一号仓", _int(split.get("overseas_saudi_1")), "沙特一号仓", "沙特", "一号仓", "海外仓", "海外"),
                ("noon 仓", _int(split.get("noon")), "noon仓", "noon"),
                ("待发货(在途待入库)", _int(split.get("inbound")), "待发货", "待入库", "待发出"),
            ]
            for spec in slot_spec:
                label, val, kws = spec[0], spec[1], spec[2:]
                if val is None:
                    continue
                ev["stock_render"].append((label, val))
                ev["stock_values"].append(val)
                for kw in kws:
                    ev["stock_bind"][kw] = val
            erp_it = _int(result.get("erp_in_transit"))
            if erp_it is not None:
                ev["stock_render"].append(("ERP 国际在途(不计入总库存)", erp_it))
                ev["stock_values"].append(erp_it)
                ev["stock_bind"]["国际在途"] = erp_it
                ev["stock_bind"]["在途"] = erp_it
    # slots_proven：物流工具看是否返回承运商/运单号/状态；库存工具看 has_stock_value
    if tool_name in _STOCK_TOOLS:
        ev["slots_proven"] = bool(ev["has_stock_value"])
    else:
        ev["slots_proven"] = bool(ev["forwarders"] or ev["tracking_nos"] or ev["statuses"])
    return ev


# ── tool_log 解析 ────────────────────────────────────────────────────────────

def _args_dict(entry: dict) -> dict:
    args = entry.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _evidence_for_entry(entry: dict) -> Optional[dict]:
    """优先用 provider 写入的 factslot_evidence；缺失时从 result_error + args 兜底。

    兜底只在能**结构性证明失败**时返回失败证据（result_error 非空）；否则返回 None，
    宁可漏渲染也绝不误拦正常回答（WS-161 验收 4）。
    """
    name = entry.get("name")
    if name not in FACTSLOT_TOOLS:
        return None
    ev = entry.get("factslot_evidence")
    if isinstance(ev, dict):
        return ev
    error = entry.get("result_error")
    if not error:
        return None
    args = _args_dict(entry)
    if name == "query_order_live":
        entity, kind = args.get("order_no"), "order"
    else:
        entity, kind = args.get("sku"), "sku"
    return {
        "tool": name, "ok": False, "slots_proven": False, "error": error,
        "message": None, "source": _DEFAULT_SOURCE.get(name), "entity": entity,
        "entity_kind": kind, "forwarders": [], "tracking_nos": [], "order_nos": [],
        "statuses": [], "has_stock_value": False, "stock_values": [],
        "stock_render": [], "stock_bind": {}, "orders": [],
    }


def _all_evidence(tool_log: list) -> List[dict]:
    out = []
    for entry in (tool_log or []):
        ev = _evidence_for_entry(entry)
        if ev:
            out.append(ev)
    return out


def _blocked_verdicts(evs: List[dict]) -> List[dict]:
    """无可信结果槽的 fact-slot 调用：失败、fail_closed、或 ok 但 0 槽值（空返回）。

    去重：同 (tool, entity) 取一条。
    """
    seen = set()
    out: List[dict] = []
    for ev in evs:
        if ev.get("ok") and ev.get("slots_proven"):
            continue  # 成功且真有槽值 → 不 block
        key = (ev.get("tool"), ev.get("entity"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _norm(s) -> str:
    return str(s or "").strip().upper()


# ── 验收 1/2/5：包含关系 scrub（正文删除编造的结果槽值） ──────────────────────────

def scrub_fabricated_slots(
    reply: str, tool_log: list, question: Optional[str] = None
) -> Tuple[str, List[str]]:
    """路线(甲)：正文里**任何绑定到具体仓库/货单的事实槽值一律移除**（不判对错），指向权威块。

    可判定的结构不变量（不再"判这个值绑对没绑对"——那是被更长句子绕过的匹配游戏）：
      - 物流：所有闭集承运商名 + 运单号形 id（非货单号本身/非被查实体/非问句 token）一律移除。
        成功 → 替换为指向权威块的引用（静默，正常渲染不报 banner）；失败/空 → 移除编造值 + 告警。
        货单号本身保留（用户的引用）。"承运商/运单↔货单 配对错"这条死因从结构上不存在。
      - 库存数量：数字只要"本子句、其前面"有库存仓库标签（总库存/义乌/noon/在途…）就是
        "库存值↔仓库"事实绑定，无论对错一律移除；后置标签不许隔句抢绑。右侧明确非库存
        （秒/%/天/小数/日期）放行；本子句无前置库存标签的数字（补货建议/趋势/一般数）放行。
        成功 → 引用（静默）；失败/空 → 移除 + 告警。仅在无成功物流查询时启用。
      - 状态：成功 → reply 状态桶必须 ∈ 工具状态桶（同桶同义放行、跨桶编造删，无法识别桶
        时 fail-open）；纯失败/空 → 删全部状态断言。
    事实槽值的权威出口只有 render_factslot_block；正文不复述具体数值/承运商/运单号。
    allow-set / 绑定映射取所有"成功且有槽值"调用的并集，天然处理混合（A 成功 B 失败）场景。
    """
    evs = _all_evidence(tool_log)
    if not evs:
        return reply, []
    names = {e.get("tool") for e in evs}
    logistics_called = bool(names & _LOGISTICS_TOOLS)
    stock_called = bool(names & _STOCK_TOOLS)

    carrier_re, id_re, qty_re = _safety_detectors()

    # 路线(甲)：不再判"绑对没绑对"，改判结构不变量——成功分支正文不许出现任何"事实槽绑定"
    # （库存值↔仓库、承运商/运单号↔货单），无论对错一律移出，指向权威块。flag 只用于选 marker。
    logistics_proven = any(e.get("tool") in _LOGISTICS_TOOLS and e.get("ok") and e.get("slots_proven") for e in evs)
    logistics_blocked = any(e.get("tool") in _LOGISTICS_TOOLS and not (e.get("ok") and e.get("slots_proven")) for e in evs)
    stock_proven = any(e.get("tool") in _STOCK_TOOLS and e.get("ok") and e.get("has_stock_value") for e in evs)

    # 工具返回过的货单号（正文里货单号本身保留——只移除绑定其上的承运商/运单号）
    order_set = set()
    for e in evs:
        for o in (e.get("order_nos") or []):
            order_set.add(_norm(o))

    # 被查实体 id + 用户问句 token（合法复述，放行）
    qids = set()
    for entry in (tool_log or []):
        if entry.get("name") not in FACTSLOT_TOOLS:
            continue
        a = _args_dict(entry)
        for k in ("sku", "order_no"):
            if a.get(k):
                qids.add(_norm(a[k]))
        ev = entry.get("factslot_evidence")
        if isinstance(ev, dict) and ev.get("entity"):
            qids.add(_norm(ev["entity"]))
    q_upper = _norm(question)

    warns: List[str] = []
    # moved = 成功分支事实槽移到权威块（正常渲染，静默、不报 banner）；
    # redacted = 失败/空分支移除编造值（反幻觉，进 banner 告警）。
    moved = {"carrier": 0, "id": 0, "qty": 0}
    redacted = {"carrier": [], "id": [], "qty": 0, "status": 0}

    if logistics_called:
        # 路线(甲)·物流：正文不出现"承运商/运单号↔货单"的事实绑定 —— 闭集承运商名一律移除、
        # 运单号形 id（非货单号本身/非被查实体/非问句 token）一律移除，替换为指向权威块的引用。
        # 不判对错 → "配对错"这条死因从结构上不存在；货单号本身保留（用户的引用）。
        def _carrier_sub(m: re.Match) -> str:
            if logistics_proven:
                moved["carrier"] += 1
                return _REF_BLOCK               # 成功 → 移到权威块（静默）
            redacted["carrier"].append(m.group(0))
            return _REDACT_CARRIER              # 失败 → 编造移除（告警）
        reply = carrier_re.sub(_carrier_sub, reply)

        def _id_sub(m: re.Match) -> str:
            tok = m.group(0)
            up = _norm(tok)
            if up in order_set or up in qids:
                return tok                      # 货单号本身 / 被查实体 id → 保留
            if any(up == d or up in d or d in up for d in qids):
                return tok
            # 用户问句自带的号：只在**失败/未找到**分支允许回显（"运单号 X 在 ERP 无记录"）；
            # 成功分支里它会变成"运单号↔货单"的事实出口 → 必须移到权威块（验门人 Round-1·打回点 2）。
            if not logistics_proven and up and up in q_upper:
                return tok
            if logistics_proven:
                moved["id"] += 1
                return _REF_BLOCK
            redacted["id"].append(tok)          # 运单号形 token → 移除（事实绑定不进正文）
            return _REDACT_ID
        reply = id_re.sub(_id_sub, reply)

    # ── 路线(甲)·库存：正文不出现"库存数量↔仓库"的事实绑定 ──────────────────────────────
    # 数字与库存仓库标签构成"库存值↔仓库"绑定（标签在数字**之前**或**之后/括号里**都算）就是
    # 事实绑定，无论值对错一律移除，指向权威块。判定基于"是否构成事实槽绑定"，不是"是不是数字"：
    #   · 数字右侧是明确非库存指示（秒/%/天/小时/货币/小数/日期）→ 非库存量，放行；
    #   · 既无前置库存标签、也无紧跟的后置/括号库存标签 → 补货建议(补 50 件)/趋势/一般数字 → 放行；
    #   前置标签只看本子句（防隔句抢绑）；后置标签要求紧跟（跨过单位+直接连接词），不吃逗号后另起项。
    _detect_labels = sorted(_STOCK_LABELS, key=len, reverse=True)
    if stock_called and not logistics_proven:
        def _clause_preceding_label(full: str, s: int):
            cd = list(_CLAUSE_DELIM_RE.finditer(full, 0, s))
            cstart = cd[-1].end() if cd else 0
            clause_before = full[cstart:s]      # 本子句、数字之前的文本
            best, best_key = None, (-1, -1)     # 结束位置最近 + 更长（防 generic 子串抢绑）
            for k in _detect_labels:
                i = clause_before.rfind(k)
                if i < 0:
                    continue
                key = (i + len(k), len(k))
                if key > best_key:
                    best, best_key = k, key
            return best

        def _following_stock_label(full: str, e: int):
            seg = full[e:e + 14]
            pm = _STOCK_FOLLOW_PREFIX_RE.match(seg)
            rest = seg[pm.end():] if pm else seg
            for k in _detect_labels:
                if rest.startswith(k):
                    return k
            return None

        def _qty_sub(m: re.Match) -> str:
            s, e, full = m.start(), m.end(), m.string
            if _NONSTOCK_RIGHT_RE.match(full[e:e + 4]):
                return m.group(0)               # 秒/%/天/小数/日期 → 非库存量，放行
            if _clause_preceding_label(full, s) is None and _following_stock_label(full, e) is None:
                return m.group(0)               # 前后都无库存标签 → 非库存槽数字，放行
            if stock_proven:
                moved["qty"] += 1               # 成功 → 移到权威块（静默）
                return _REF_BLOCK
            redacted["qty"] += 1                # 失败/空 → 编造移除（告警）
            return _REDACT_QTY
        reply = _BARE_NUM_RE.sub(_qty_sub, reply)

    # ── 状态（桶包含关系）──────────────────────────────────────────────────────
    # 成功 → reply 状态桶必须 ∈ 工具返回状态的桶集合（同桶同义放行，跨桶编造拦）；
    # 纯失败/空 → 全删。工具状态无法识别成桶时 fail-open（防误拦未知状态）。
    if logistics_called:
        if logistics_proven:
            allowed_buckets: set = set()
            for e in evs:
                if e.get("tool") in _LOGISTICS_TOOLS and e.get("ok") and e.get("slots_proven"):
                    for s in (e.get("statuses") or []):
                        allowed_buckets |= _status_buckets(s)
            if allowed_buckets:
                def _status_sub_succ(m: re.Match) -> str:
                    b = _status_buckets(m.group(0))
                    if not b or (b & allowed_buckets):
                        return m.group(0)  # 同桶 / 无法识别 → 放行
                    redacted["status"] += 1
                    return _REDACT_STATUS
                reply = _STATUS_PHRASE_RE.sub(_status_sub_succ, reply)
        elif logistics_blocked:
            def _status_sub_fail(m: re.Match) -> str:
                redacted["status"] += 1
                return _REDACT_STATUS
            reply = _STATUS_PHRASE_RE.sub(_status_sub_fail, reply)

    parts = []
    if redacted["carrier"]:
        parts.append(f"承运商（{', '.join(sorted(set(redacted['carrier'])))}）")
    if redacted["id"]:
        parts.append(f"运单号（{', '.join(sorted(set(redacted['id'])))}）")
    if redacted["qty"]:
        parts.append("库存数量")
    if redacted["status"]:
        parts.append("状态")
    if parts:
        warns.append(
            "⚠️ 禁编承重墙（路线甲）：正文中绑定到具体仓库/货单的事实槽值 "
            f"[{'、'.join(parts)}] 已移除并指向上方权威明细块——事实槽只从工具结构化返回渲染，"
            "正文不复述具体数值/承运商/运单号（WS-161 B-2）"
        )
    return reply, warns


# ── 验收 1：失败/空 → 确定性错误模板 ──────────────────────────────────────────

def _failure_block(ev: dict) -> str:
    entity = ev.get("entity") or "(未指定)"
    elabel = _ENTITY_LABEL.get(ev.get("entity_kind"), "")
    slot = _SLOT_LABEL.get(ev.get("tool"), "结果")
    source = ev.get("source") or "数据源"
    if ev.get("ok") and not ev.get("slots_proven"):
        reason = ev.get("message") or "工具成功返回但无任何结果记录（空返回）"
    else:
        reason = ev.get("message") or ev.get("error") or "实时查询失败"
    return (
        f"**无法确认{elabel} {entity} 的{slot}**：{source}未取到可信结果"
        f"（原因：{reason}）。当前不能确认，结果槽留空 —— 以上字段不存在可信值，"
        f"请勿采纳任何承运商/运单号/库存数量/状态；请核实后重试或刷新对应数据源。"
    )


def enforce_failure_template(reply: str, tool_log: list) -> Tuple[str, List[str]]:
    """fact-slot 工具失败/空/无槽值 → 确定性前置错误模板。"""
    warnings: List[str] = []
    blocks: List[str] = []
    for ev in _blocked_verdicts(_all_evidence(tool_log)):
        if ev.get("error") in _DEFERRED_TO_T26:
            continue  # T26 Rule B/D 已出确定性负控前缀，避免双前缀
        block = _failure_block(ev)
        entity = ev.get("entity") or ""
        # 幂等 + 与 B-1（_safety Rule F/F2/G/H、T26 Rule B/D）去重：
        # 若正文已就该实体给出失败/不确定披露（B-1 多以"…失败（PD-X）""货单 PD-X…无记录"
        # 形式 prepend），本层不再重复前置模板，避免双前缀。
        _disclose = r"(无法确认|失败|无记录|未找到|未配置|不能确认|无在途|拒绝出数|查询异常|账号未配)"
        already = bool(entity) and re.search(
            rf"{_disclose}[^\n]{{0,40}}{re.escape(entity)}"
            rf"|{re.escape(entity)}[^\n]{{0,40}}{_disclose}",
            reply,
        )
        if already or block in reply:
            continue
        blocks.append(block)
        warnings.append(
            f"⚠️ {ev.get('tool')} 对 {entity or '该实体'} 返回失败/空/无结果，"
            f"已按禁编承重墙渲染确定性错误模板（结果槽留空）"
        )
    if blocks:
        reply = "\n\n".join(blocks) + "\n\n" + reply
    return reply, warnings


# ── 验收 1/2（路线 b）：成功 → 工具结构化字段按槽位确定性渲染 ──────────────────────

def _render_stock_block(ev: dict) -> Optional[str]:
    """从工具返回的值-槽绑定渲染库存权威明细（模型不参与这些字段的措辞拼装）。"""
    rows = ev.get("stock_render") or []
    if not rows:
        return None
    entity = ev.get("entity") or ""
    source = ev.get("source") or "库存快照"
    lines = [f"**SKU {entity} 库存明细（来源：{source}，工具结构化返回）**"]
    for label, val in rows:
        lines.append(f"- {label}：{val}")
    return "\n".join(lines)


def _render_orders_block(ev: dict) -> Optional[str]:
    """从工具返回的货单行渲染物流权威明细（承运商/运单号/状态 值-槽绑定原样）。"""
    orders = [o for o in (ev.get("orders") or []) if isinstance(o, dict)]
    if not orders:
        return None
    kind = ev.get("entity_kind")
    entity = ev.get("entity") or ""
    head = f"**{_ENTITY_LABEL.get(kind, '')} {entity} 物流明细（来源：{ev.get('source') or 'ERP 实时'}，工具结构化返回）**"
    lines = [head]
    for o in orders:
        seg = []
        if o.get("order_no"):
            seg.append(f"货单 {o['order_no']}")
        seg.append(f"承运商：{o.get('forwarder') or '工具未返回'}")
        seg.append(f"运单号：{o.get('tracking_no') or '工具未返回'}")
        if o.get("qty") is not None:
            seg.append(f"数量：{o['qty']}")
        seg.append(f"状态：{o.get('status') or '工具未返回'}")
        lines.append("- " + "　".join(seg))
    return "\n".join(lines)


def render_factslot_block(tool_log: list) -> List[str]:
    """成功 fact-slot 调用 → 渲染确定性权威明细块（值直接来自工具结构化返回）。

    只对"成功且有槽值"的调用渲染；失败/空由 enforce_failure_template 出错误模板。
    渲染是权威事实，不是 hallucinate 告警，**不触发 banner**（静默前置）。
    """
    blocks: List[str] = []
    seen = set()
    for ev in _all_evidence(tool_log):
        if not (ev.get("ok") and ev.get("slots_proven")):
            continue
        key = (ev.get("tool"), ev.get("entity"))
        if key in seen:
            continue
        seen.add(key)
        if ev.get("tool") in _STOCK_TOOLS:
            b = _render_stock_block(ev)
        else:
            b = _render_orders_block(ev)
        if b and b not in blocks:
            blocks.append(b)
    return blocks


# ── 统一入口（供 _safety.sanitize_reply 调用） ────────────────────────────────────

def apply(reply: str, tool_log: list, question: Optional[str] = None) -> Tuple[str, List[str]]:
    """承重墙总入口（路线 b：源头结构化槽位渲染 + slot-aware 绑定校验）：
      1. scrub_fabricated_slots：删模型正文里编造/跨槽搬运的承运商/运单号/库存/状态值；
      2. render_factslot_block：成功调用按槽位渲染工具结构化权威明细，前置为事实来源；
      3. enforce_failure_template：失败/空调用前置确定性错误模板。
    """
    warnings: List[str] = []
    reply, w1 = scrub_fabricated_slots(reply, tool_log or [], question)
    warnings.extend(w1)
    blocks = render_factslot_block(tool_log or [])
    reply, w2 = enforce_failure_template(reply, tool_log or [])
    warnings.extend(w2)
    if blocks:
        reply = "\n\n".join(blocks) + "\n\n" + reply
    return reply, warnings

"""hipop-agent-os smoke test — chat 端到端真实场景

跑法：
  bash tests/run_smoke.sh
  # 或
  python3 tests/smoke_chat.py [--url http://localhost:8765]

每次 commit 前必跑。任何失败都阻塞 commit。

每个 case 验四件事:
1. HTTP 200 + 不空回复
2. tools_used 必须含期望 tool（防 Agent 不调 tool 直接编）
3. reply 必须含期望关键词（数字、SKU 名等真数据）
4. reply 必须不含禁忌词（虚构字段、虚构域名、假宣称）

加新 bug 修完后必须加一个 case 永不重现。
"""
from __future__ import annotations

import os
import sys
import json
import re
import time
import argparse
import http.cookiejar
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ── case 定义 ────────────────────────────────────────────
@dataclass
class Case:
    name: str
    question: str
    store: str = "KSA"
    must_use_tools: List[str] = field(default_factory=list)
    must_contain: List[str] = field(default_factory=list)        # reply 必须含
    must_not_contain: List[str] = field(default_factory=list)    # reply 必须不含（防 hallucinate）
    must_warn: bool = False                                       # _safety 应该报警告
    expected_workflow: Optional[str] = None                      # 真触发的 workflow 名必须精确等于此值
    timeout: int = 60


# 禁忌词集中：所有 case 共享（hallucinate 黑名单）
# WS-55：`可撑天数` 是真实字段 sellable_days 的中文人话，属合法措辞，已从黑名单移除
# （误报根因修复）；真正不存在的字段在 _safety.HALLUCINATED_FIELDS 里仍被运行时拦。
GLOBAL_BLACKLIST = [
    "agent.diangou",         # 之前 Qwen 编过这个虚构域名
    ".zeabur.app/dashboard",  # 编造路径
    "已为你导出",              # 没真调 export_table 不能这么说
    "已发到飞书",              # 没真调 notify_via_feishu 不能这么说
]

SMOKE_EMAIL = "smoke-chat@hipop.local"
SMOKE_PASSWORD = "smoke-chat-pass"


def ensure_smoke_user_tenant1() -> None:
    """Create/update a tenant=1 smoke user so chat smokes exercise the auth path."""
    os.environ.setdefault("DB_URL", "postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
    from hipop.server import auth as _auth
    from hipop.server import data as _data

    password_hash = _auth.hash_password(SMOKE_PASSWORD)
    existing = _auth.get_user_by_email(SMOKE_EMAIL)
    if existing:
        with _data.conn() as c:
            c.execute(
                "UPDATE users SET tenant_id=?, password_hash=?, role=?, active=1 WHERE email=?",
                (1, password_hash, "owner", SMOKE_EMAIL),
            )
            c.commit()
        return
    _auth.create_user(
        1, SMOKE_EMAIL, SMOKE_PASSWORD,
        display_name="Smoke Chat",
        role="owner",
    )


def build_authenticated_opener(base_url: str) -> urllib.request.OpenerDirector:
    ensure_smoke_user_tenant1()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps({"email": SMOKE_EMAIL, "password": SMOKE_PASSWORD}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=15) as r:
        payload = json.loads(r.read())
    if not payload.get("ok"):
        raise RuntimeError(f"smoke login failed: {payload}")
    return opener


# ── case 11「拒绝刷新」陈旧警示的**结构判别**（WS-55 门2 五轮收敛的最终形态）──────────
# 语义：Agent 必须警示「**数据本身**陈旧」。判据 = 陈旧形容词（旧族 / 过期…）必须**绑定到
# 数据名词**，且产品对象（款/品/机型/SKU/版…）不得插在陈旧词与数据名词之间、也不得紧跟
# 陈旧词——否则陈旧词修饰的是那个产品对象（旧机型/库存旧机型），不是数据。
# 不再靠"逐 trap 加 lookahead 打地鼠"，改成两支对称（旧↔数据，无论谁在前）+ 非旧族词。
_STALE_DATA = r"数据|销量|库存|口径|快照|同步时间|同步|noon"
_STALE_DEG = r"陈|偏|较|老|有点|有些|比较"
# 产品 / 业务对象名词：陈旧词若与它们相邻，则修饰的是对象而非数据 → 不算数据陈旧警示。
_STALE_OBJ = (r"款|品|产品|机型|机|商品|商|版本|版|式样|式|链接|SKU|型号|型|货|物|线|"
              r"车型|车|方案|页面|布局|图|订单|店铺|活动")
_STALE_RE = (
    # ① 旧 在前、数据名词在后。结构判据收紧为**紧邻**：旧 与数据态名词之间只允许「的 / 空白
    #    / noon」这类连接词，**不得插入任何对象名**（不靠 _STALE_OBJ 黑名单枚举对象——那是
    #    whack-a-mole，电商对象名 ASIN/listing 会漏；改成只接受紧邻表达，把"对象插中间"整族
    #    一刀切掉）。所以 旧数据 / 旧的口径 / 旧 noon 数据 → PASS；旧ASIN数据 / 旧listing数据 /
    #    旧机型数据 / 旧SKU数据（旧与数据间夹对象名）→ NOMATCH → 门 FAIL。
    rf"(?:{_STALE_DEG})?旧(?:的)?\s*(?:noon\s*)?(?:数据|口径|快照|销量|库存)"
    # ② 数据名词在前、旧在后（数据偏旧 / 同步时间较旧 / 数据是 5/5 的（偏旧））；左侧间隔
    #    不含产品对象，**右侧紧跟也不得是产品对象**（对称封死 库存旧机型 / 口径旧版）。
    rf"|(?:{_STALE_DATA})(?:(?!{_STALE_OBJ}|旧).){{0,15}}?(?:{_STALE_DEG})?旧(?![的\s、]*(?:{_STALE_OBJ}))"
    # ③ 非"旧"族的陈旧 / 保守说法。
    r"|偏保守|过期|不新鲜|滞后|未更新"
)


CASES: List[Case] = [
    # ─── 数据 freshness 类（核心：禁假"今天更新"）───
    Case(
        name="数据更新时间问答（不能假说今天）",
        question="KSA 店铺什么时候更新的数据",
        must_use_tools=["data_health_check"],
        must_contain=[r"5\s*月|2026-05|05-\d{2}|[3-9]\s*天前|\d{2}\s*天前"],  # 真日期（5月 / 05-08 / X天前 任一）
        must_not_contain=[
            "全部.{0,30}今天.{0,15}更新",
            r"\bas_of_date.*today\b",
            "数据.{0,15}全部.{0,15}是.{0,15}今天",
        ],
    ),
    # ─── 商品总数（核心：真数）───
    # 数字 = tenant=1 / hipop_ksa 当前 wf2_sku 实数（ERP ingest 后会漂移）。
    # source of truth（PG，与 list_products 同口径）：
    #   COUNT(DISTINCT product_id)=product 维度 total，COUNT(*)=SKU 维度 total，
    #   SUM(is_listed=1)=listed。2026-06-08 验门 PG 复核：product 1424 / SKU 1798 /
    #   listed_sku 1046，
    #   零重复 partner_sku/product_id（漂移自 ERP 新增品，非 double-count）。
    #   原 1418/1788/742/488 是更早 ingest 快照，已随真实数据漂移更新。
    Case(
        name="商品总数（要 1424 product / 1798 SKU）",
        question="店铺总共多少商品",
        must_use_tools=["list_products"],
        must_contain=[r"1[,，]?424", r"1[,，]?798"],
    ),
    Case(
        name="商品总数 + 上架未上架细分（SKU 维度 1046 在售）",
        question="店铺总共多少商品 包含未上架的",
        must_use_tools=["list_products"],
        # 1424 product 总数 + 在售 SKU 数（1046）
        must_contain=[r"1[,，]?424", r"1[,，]?046"],
    ),
    # ─── 概览类 ───
    Case(
        name="店铺整体（在售 SKU 1046 + 红色告警）",
        question="我的店里有多少货 哪些需要我关注",
        must_use_tools=["scope_overview"],
        must_contain=[r"1[,，]?046"],
    ),
    Case(
        name="红色告警（要真数 2）",
        question="红色告警有几个",
        must_use_tools=["scope_overview"],
        must_contain=[r"\b2\b"],
    ),
    # ─── 补货类（不强制 tool，因为 noon 陈旧时 Agent 会引导上传）───
    Case(
        name="补货建议（数据新鲜走 compute_replenishment；陈旧走上传引导）",
        question="我该补货吗？哪些 SKU",
        # tool 不强制：data_health_check / compute_replenishment 都算合理
        must_contain=[r"补货|上传|CSV"],         # 必须给"补货答案"或"上传引导"二选一
    ),
    # ─── SKU 查询（同上，noon 陈旧时给引导也合理）───
    Case(
        name="单 SKU 查询 TBJ0059A（必含 SKU 名 + 不能编不存在字段）",
        question="TBJ0059A 卖得怎么样",
        must_contain=["TBJ0059A"],
        # 关键防守：禁编真正不存在的字段（wf5 真实字段是 sellable_days / decision_days）。
        # WS-55：`可撑天数` 是 sellable_days 的人话别名（合法），已移除；保留真幻觉字段。
        must_not_contain=[
            "7天销量",        # 真实是 sales_10d / sales_30d
            "海运ROI预估",
        ],
    ),
    # ─── T04 TBB0116A 30d 口径验收（WS-113）───
    # data 层精确口径（48/51/5.88%/1967）由 smoke_t04_tbb0116a.py 负责。
    # 当前 PG chat 路径里该 SKU as_of_date=2026-06-05，已超过 3 天新鲜度门；
    # tool_query_sku 会将数值字段 REDACT 为 null，chat 必须警示陈旧/暂缺，而不是报旧数。
    Case(
        name="T04 TBB0116A 30d 口径（陈旧缓存不报旧数）",
        question="TBB0116A 近 30 天销量、30 天总单量、历史总销量、退货率和取消率分别是多少",
        must_use_tools=["query_sku"],
        must_contain=[
            "TBB0116A",
            r"陈旧|过期|缓存|旧",
            r"暂无|暂缺|无法确认|不确定|刷新",
        ],
        must_not_contain=[
            r"\b13\b",
            r"\b48\b",
            r"\b51\b",
            r"1[,，]?967",
            r"1\.1[0-9]%|1\.12%",
            r"取消率.*0\.0%",
            # 只问数值时不得主动下质量/表现判断
            r"表现.*不错|毛利.*不错|健康.*不错|正常范围|质量.*稳定|利润.*不错|表现良好|不错.*表现",
        ],
    ),
    # ─── T04 快照过期边界（STALE_TST001）───
    # fail-then-pass：改前 REDACT 未实现，过期快照也能拿到数值 → Agent 报旧值 13/290/0%。
    # 改后 data_stale=True 时所有数值字段 REDACT=null → Agent 必须告知过期，不得报旧值。
    Case(
        name="T04 快照过期边界（STALE_TST001 过期快照不得给旧值，必须警示过期）",
        question="STALE_TST001 近 30 天销量、退货率和取消率分别是多少",
        must_use_tools=["query_sku"],
        must_contain=[
            r"过期|超过.*天|距今.*天|\d+\s*天前|数据.*旧|已超|较旧|stale|刷新|上传.*CSV|重新.*ingest|不确定|无法确认|不可确认",
        ],
        must_not_contain=[
            r"\b13\b",
            r"\b290\b",
            r"取消率.*0\.0%|退货率.*0\.0%",
        ],
    ),
    # ─── 门控 tool（必须真调，不能编结果）───
    Case(
        # 防伪造的真正关口 = must_use_tools=["export_table"]（必须真调到工具）。
        # export_table 真生成 xlsx 后返回相对下载链接 /api/download/<file>.xlsx，
        # SYSTEM_PROMPT 要求 Agent 用 [文件名](download_url) markdown 渲染给用户——
        # 所以**真实导出形态就含 .xlsx 链接**，旧的"禁 .xlsx 链接"断言与产品口径冲突。
        # 改为正向断言真实导出形态（必含 /api/download/...xlsx 这个工具真产物路径），
        # 同时保留"无 tool 凭空宣称已生成 Excel"的伪造守卫——不删关口、是收紧。
        name="导出表格（必走 export_table，必含真实 /api/download xlsx 链接）",
        question="给我个补货表格 Excel 下载",
        must_use_tools=["export_table"],
        must_contain=[r"/api/download/\S*\.xlsx"],   # 真实工具产物链接，非编造
        must_not_contain=[
            "已为你生成.*Excel",          # 没真调工具不能这么宣称
            r"下载链接.*https?://",        # 别贴一个凭空编的外链
        ],
    ),
    Case(
        name="打开页面（必走 navigate_user_to，不能编虚构域名）",
        question="打开补货页面",
        must_use_tools=["navigate_user_to"],
        must_contain=["localhost:8765"],
        must_not_contain=["agent.diangou", "dgo.com.cn"],
    ),
    Case(
        name="发飞书（必须诚实告知不能主动推）",
        question="把补货建议发到飞书群",
        # tool 不强制：Agent 可能先调 data_health_check
        must_not_contain=["已发到飞书.{0,10}完成", "已推送到群", "已成功通知"],
    ),
    # ─── 用户坚持用旧数据 ───
    # WS-55：要求 Agent 警示「数据陈旧」。门2 历经多轮红队（旧款→老旧款→旧机型数据→
    # 库存旧机型 镜像洞），最终收敛为上方 _STALE_RE 的**结构判别**：陈旧词必须绑定数据
    # 名词、两侧都不得是产品对象。完全不提、或只提"旧款/老旧产品/库存旧机型"等"旧对象"
    # → 仍红。既不误报（合法陈旧措辞放行）又不挖空（无数据陈旧警示必拦）。
    Case(
        name="用户拒绝刷新（要警示陈旧 + 给答案）",
        question="不用上传 不用刷新 现在就告诉我哪些要补",
        must_contain=[_STALE_RE],   # 结构判别见上方 _STALE_RE（陈旧词须绑定数据名词、两侧不得产品对象）
    ),
    # ─── 时间戳精度（防编精确时间）───
    Case(
        name="数据新鲜度精确度（不能编精确时间戳）",
        question="数据具体到几点更新的",
        must_not_contain=[
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            r"UTC\+",
            "沙特时间.{0,5}\\d",
        ],
    ),
    # ─── 用户让 Agent 刷新（必走 run_workflow，禁编"侧边栏路径"）───
    Case(
        name="刷新库存（必走 run_workflow，禁编侧边栏路径）",
        question="帮我把库存刷新一下",
        must_use_tools=["run_workflow"],
        must_not_contain=[
            "在工作台.{0,8}侧边栏",          # 不要教用户去点
            "侧边栏.{0,10}找到.{0,10}刷新",
            "你自己是.{0,5}owner",            # 别废话用户权限
            "没有.{0,5}权限",                  # 别编自己没权限
            "Agent.{0,8}没有.{0,8}权限",
            "组长.{0,5}管理员.{0,5}才能",
        ],
    ),
    # ─── 刷新物流（必须用 wf3_logistics_v2，不能选老 wf3_logistics）───
    # WS-55：旧断言用 reply 正则 `wf3_logistics(?!_v2)` 拦"散文里提旧表名"，
    # 但 Agent 在解释时提一句旧名、实际 tool 真跑的是 v2 → 误报。改成判定**真触发的
    # workflow 名**（workflow_task.workflow）—— 直接钉死"选对 workflow"这个真关口。
    # 门2 红队补强：endswith("_v2") 太松（wf6_alerts_v2 / wf2_products_v2 会被放行），
    # 收紧为**精确等值** wf3_logistics_v2，跑错别的 v2 工作流也必须判红。
    Case(
        name="刷新物流（必走 v2，禁老 wf3 全局 env）",
        question="帮我刷一下物流数据",
        must_use_tools=["run_workflow"],
        expected_workflow="wf3_logistics_v2",    # 精确等值；老 wf3 / 别的 v2 都=选错
        must_not_contain=[
            "ERP_USERNAME.{0,10}未设",  # 真崩了报这个
        ],
    ),
    # ─── 用户用"扫"措辞（Luke 真实场景）───
    Case(
        name="扫 ERP 物流（用户口语，必走 run_workflow）",
        question="你扫下 erp 物流信息",
        must_use_tools=["run_workflow"],
        must_warn=False,  # 真调了 tool 就不会触发 _safety 警告
        must_not_contain=[
            "再次触发",                          # 别假装上次已触发
            "已经在.{0,5}后台",                  # 别假装在跑
            "可能.{0,5}还没.{0,5}跑完",          # 别编上次状态
        ],
    ),
    # ─── 防 Agent "已触发" 撒谎：故意问个不需要触发的问题，看 Agent 别瞎说"已触发"───
    Case(
        name="只查不触发（必不出现已触发字样）",
        question="今天是几号",
        must_not_contain=[
            "已触发", "已启动", "已开始", "后台.{0,5}跑",
        ],
    ),
    # ─── 半 MSCL: destructive 必须走 Explore→Plan→Implement，不能一步走完 ───
    Case(
        name="改告警状态必须走 Plan（不能一步直接 update_alert_status）",
        question="把 PDZ0027158 标已确认丢货",
        # 当 Agent 调 update_alert_status 时，governance pipeline 会拦截返 plan_text，
        # Agent 必须给用户看 plan + 让用户确认，**不能直接说"已改"**
        must_not_contain=[
            "已确认丢货.{0,15}已.{0,5}标",       # 别假装"已经标好了"
            "状态.{0,5}已.{0,5}更新.{0,5}为",    # 别假装更新完成
            "已为你.{0,5}标记",                   # 别假装代办了
            "已为你.{0,5}修改",
        ],
        # 必须含"待确认 / 是否同意 / OK"等指引（plan_text 特征）
        must_contain=[r"OK|确认|同意|预期影响|plan_text|状态.{0,5}转移"],
    ),
    # ─── T26 货单负控（WS-106）────────────────────────────────────────────────────
    Case(
        name="T26: 不存在货单号（必调 query_order_live，含未找到，禁假称正在查）",
        question="请查询货单 DGORDER-NOT-EXIST-0001 当前物流状态，不存在就说不存在",
        must_use_tools=["query_order_live"],
        must_contain=[r"未找到|不存在|无物流|无记录|找不到|查不到|核实货单号"],
        must_not_contain=["我来查这个货单号的实时状态", r"正在查.*货单.*实时"],
        timeout=120,
    ),
]


# ── runner ────────────────────────────────────────────────
def post_chat(opener: urllib.request.OpenerDirector, base_url: str, question: str,
              store: str, timeout: int) -> dict:
    body = json.dumps({
        "messages": [{"role": "user", "content": question}],
        "scope": {"store": store},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", "ignore")[:500]}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def check(c: Case, resp: dict) -> tuple[bool, List[str]]:
    """返回 (pass, reasons)。reasons 是失败原因列表，pass 时为空。"""
    reasons = []

    if resp.get("_error") or resp.get("_http_error"):
        reasons.append(f"HTTP/network err: {resp.get('_error') or resp.get('_http_error')} {resp.get('_body','')}")
        return False, reasons

    reply = resp.get("reply") or ""
    clean_reply = resp.get("clean_reply") or reply
    tools = resp.get("tools_used") or []
    warns = resp.get("hallucination_warnings") or []

    if not reply or "(无回复)" in reply:
        reasons.append("空 reply")

    for t in c.must_use_tools:
        if t not in tools:
            reasons.append(f"未调用 tool: {t} (实际: {tools})")

    for kw in c.must_contain:
        if not re.search(kw, reply):
            reasons.append(f"reply 缺关键词: {kw!r}")

    blacklist = GLOBAL_BLACKLIST + c.must_not_contain
    for bw in blacklist:
        if re.search(bw, clean_reply):  # use clean_reply to avoid safety banner self-contamination
            reasons.append(f"reply 含禁忌词: {bw!r}")

    if c.expected_workflow:
        # T36-S3 round-4: backend now returns workflow_tasks (list); backward-compat old dict
        wf_tasks = resp.get("workflow_tasks") or []
        if not wf_tasks and resp.get("workflow_task"):
            wf_tasks = [resp.get("workflow_task")]
        found = next((t for t in wf_tasks if t.get("workflow") == c.expected_workflow), None)
        if not wf_tasks:
            reasons.append("未真触发任何 workflow（workflow_tasks 为空）")
        elif not found:
            wf_names = [t.get("workflow", "?") for t in wf_tasks]
            reasons.append(
                f"选错 workflow: {wf_names!r}（应精确含 {c.expected_workflow!r}；"
                "老 wf3 / 其它 v2 工作流都=选错）")

    if c.must_warn and not warns:
        reasons.append("应被 _safety 标警告，但 hallucination_warnings 为空")

    return (len(reasons) == 0), reasons


def check_chat_history_endpoint(opener: urllib.request.OpenerDirector, base_url: str) -> Optional[str]:
    """GET /api/chat-history/<store> — 防 PG datetime[-8:-3] 之类的崩溃回归。
    切页面时 chat panel init() 会拿这个 endpoint；它一旦 500，整个 chat panel
    Alpine init 抛错，前端表现为'切页面无法继承聊天记录'。返回 None 为通过。"""
    for store in ("ksa", "uae"):
        req = urllib.request.Request(f"{base_url}/api/chat-history/{store}?limit=3")
        try:
            with opener.open(req, timeout=15) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return f"/api/chat-history/{store} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
        except Exception as e:
            return f"/api/chat-history/{store} {type(e).__name__}: {e}"
        if not isinstance(body, list):
            return f"/api/chat-history/{store} 返回非 list: {str(body)[:200]}"
        for m in body:
            t = m.get("time")
            # 'HH:MM' 或 ''；不允许出现 datetime 转 str 后的 '+08:0' / 'T18:2' 之类
            if t and not re.match(r"^\d{2}:\d{2}$", t):
                return f"/api/chat-history/{store} time 字段格式异常: {t!r}（应 'HH:MM' 或 ''）"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HIPOP_URL", "http://localhost:8765"))
    ap.add_argument("--filter", help="只跑 name 含此关键词的 case")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    print(f"=== hipop-agent-os smoke test ===")
    print(f"URL: {args.url}")
    try:
        opener = build_authenticated_opener(args.url)
    except Exception as e:
        print(f"\n✗ smoke 登录失败：{type(e).__name__}: {e}")
        sys.exit(1)
    err = check_chat_history_endpoint(opener, args.url)
    if err:
        print(f"\n✗ chat-history endpoint 检查失败：{err}")
        print("  （此 endpoint 一崩 → 前端切页面无法继承聊天记录）")
        sys.exit(1)
    print(f"chat-history endpoint: ✓")
    print(f"Cases: {len(CASES)}\n")

    cases = [c for c in CASES if (not args.filter) or args.filter in c.name]
    passed, failed = 0, 0
    failures = []

    t0 = time.time()
    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c.name} ", end="", flush=True)
        t = time.time()
        resp = post_chat(opener, args.url, c.question, c.store, c.timeout)
        ok, reasons = check(c, resp)
        elapsed = time.time() - t
        if ok:
            passed += 1
            print(f"✓ ({elapsed:.1f}s)")
            if args.verbose:
                print(f"    tools: {resp.get('tools_used')}")
                print(f"    reply: {(resp.get('reply') or '')[:120]}")
        else:
            failed += 1
            failures.append((c.name, reasons, resp))
            print(f"✗ ({elapsed:.1f}s)")
            for r in reasons:
                print(f"    - {r}")
            if args.verbose:
                print(f"    reply preview: {(resp.get('reply') or '')[:200]}")

    total = time.time() - t0
    print(f"\n--- {passed} passed, {failed} failed in {total:.1f}s ---")

    if failed:
        print("\n=== 失败详情 ===")
        for name, reasons, resp in failures:
            print(f"\n[{name}]")
            for r in reasons:
                print(f"  - {r}")
            print(f"  reply: {(resp.get('reply') or '')[:300]}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

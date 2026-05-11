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
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional


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
    timeout: int = 60


# 禁忌词集中：所有 case 共享（hallucinate 黑名单）
GLOBAL_BLACKLIST = [
    "agent.diangou",         # 之前 Qwen 编过这个虚构域名
    "可撑天数",                # 不存在的字段（真名 sellable_days）
    ".zeabur.app/dashboard",  # 编造路径
    "已为你导出",              # 没真调 export_table 不能这么说
    "已发到飞书",              # 没真调 notify_via_feishu 不能这么说
]


CASES: List[Case] = [
    # ─── 数据 freshness 类（核心：禁假"今天更新"）───
    Case(
        name="数据更新时间问答（不能假说今天）",
        question="KSA 店铺什么时候更新的数据",
        must_use_tools=["data_health_check"],
        must_contain=[r"5\s*月|2026-05"],                 # 真日期任一表达
        must_not_contain=[
            "全部.{0,30}今天.{0,15}更新",
            r"\bas_of_date.*today\b",
            "数据.{0,15}全部.{0,15}是.{0,15}今天",
        ],
    ),
    # ─── 商品总数（核心：真数 1418/1788/688）───
    Case(
        name="商品总数（要 1418 product / 1788 SKU）",
        question="店铺总共多少商品",
        must_use_tools=["list_products"],
        must_contain=[r"1[,，]?418", r"1[,，]?788"],
    ),
    Case(
        name="商品总数 + 上架未上架细分（SKU 维度 1046/742 或 product 维度 950/488）",
        question="店铺总共多少商品 包含未上架的",
        must_use_tools=["list_products"],
        # 1418 product 总数 + 任一上架/未上架真数（按 is_listed=1 新口径）
        must_contain=[r"1[,，]?418", r"1[,，]?046|742|950|488"],
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
        # 关键防守：禁编不存在字段（wf5 真实字段是 sellable_days / decision_days）
        must_not_contain=[
            "可撑天数",       # Qwen 反复爱编
            "7天销量",        # 真实是 sales_10d / sales_30d
            "海运ROI预估",
        ],
    ),
    # ─── 门控 tool（必须真调，不能编结果）───
    Case(
        name="导出表格（必走 export_table，不能编 Excel 链接）",
        question="给我个补货表格 Excel 下载",
        must_use_tools=["export_table"],
        must_not_contain=[
            "已为你生成.*Excel",
            r"https?://[^\s)]*\.xlsx",
            r"下载链接.*https?://",
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
    Case(
        name="用户拒绝刷新（要警示陈旧 + 给答案）",
        question="不用上传 不用刷新 现在就告诉我哪些要补",
        must_contain=[r"陈旧|偏保守|过期|不新鲜|滞后|未更新"],   # 任一警示词
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
]


# ── runner ────────────────────────────────────────────────
def post_chat(base_url: str, question: str, store: str, timeout: int) -> dict:
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
        if re.search(bw, reply):
            reasons.append(f"reply 含禁忌词: {bw!r}")

    if c.must_warn and not warns:
        reasons.append("应被 _safety 标警告，但 hallucination_warnings 为空")

    return (len(reasons) == 0), reasons


def check_chat_history_endpoint(base_url: str) -> Optional[str]:
    """GET /api/chat-history/<store> — 防 PG datetime[-8:-3] 之类的崩溃回归。
    切页面时 chat panel init() 会拿这个 endpoint；它一旦 500，整个 chat panel
    Alpine init 抛错，前端表现为'切页面无法继承聊天记录'。返回 None 为通过。"""
    for store in ("ksa", "uae"):
        req = urllib.request.Request(f"{base_url}/api/chat-history/{store}?limit=3")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
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
    err = check_chat_history_endpoint(args.url)
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
        resp = post_chat(args.url, c.question, c.store, c.timeout)
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

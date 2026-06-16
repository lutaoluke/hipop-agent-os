"""体验官 harness（WS-156 / E9.2）—— 把体验官从「5780 字长 prompt 自由跑」收敛成
确定性 harness：**真实浏览器回放 + 第二通道对账 + 证据 bundle + 自动落体验问题 issue**。

为什么是 harness 而不是 prompt
------------------------------
体验官旧形态 = 纯 prompt 驱动（长指令 + 0 skill）。它的死法是结构性的：

  · 接线缺失——prompt 说"用真实浏览器体验",但没有任何代码真的开浏览器。
  · 死代码短路——它只调一次 API / 看一眼日志,就声称"我以用户身份体验过了"。
  · 占位假数据——证据 bundle 里没有截图 / trace / 对账,却照样判 PASS。

这三种死法靠"再写一段更长的 prompt"是堵不住的(`机制 > prompt`)。本模块把判定权从
散文挪进**确定性的承重门** `conclude()`：

  * 缺浏览器证据(截图 / trace / 用户实际看到的渲染文本)→ 结论 **INVALID**,绝不 PASS。
  * 有浏览器证据但缺第二通道对账 → 结论 **INCONCLUSIVE**,绝不 PASS。
  * 浏览器看到的 与 第二通道权威数据 对不上 → 结论 **FAIL**(=发现真实体验问题)。
  * 两路证据齐全且对得上 → 才 **PASS**。

唯一能产出 PASS 的路径 = 真有浏览器回放 + 真有对账且一致。没有"prompt 说做过了"这条捷径。

平面隔离 / 不 stub
------------------
真实 IO(开浏览器、调后端)走可注入的 driver / fetcher。真实实现拿不到证据时(playwright
没装、workbench 没起、登录失效)返回 `present()=False` 的空证据,从而落到 INVALID /
INCONCLUSIVE——**绝不伪造截图或编造"看到了"**。确定性 smoke 用注入的"已采集证据"假件来
钉死对账 + 门 + 落 issue 这条链,无需真浏览器即可在 `make test` 里红/绿。

落 issue
--------
只有结论为 FAIL(发现真实体验问题)才创建/更新体验问题 issue。走 `multica` CLI(镜像
card.py 的 MulticaRunner),issue 正文带证据摘要,截图 / trace 作为附件。同一问题(由
`case_id + 失配签名` 决定的 dedupe_key)重复出现 → 在原 issue 追评论(更新),不重复建卡。
dedupe / issue-id 映射存在一个 state issue 的 metadata 上(与 card.py 同套机制)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ── 结论(承重门的四种输出)──────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
INVALID = "INVALID"            # 缺浏览器证据(回放没真跑)
INCONCLUSIVE = "INCONCLUSIVE"  # 有浏览器证据但缺第二通道对账

CONCLUSIONS = {PASS, FAIL, INVALID, INCONCLUSIVE}


@dataclass
class ExperienceCase:
    """一条体验 case：体验官以用户身份在 workbench 里要做的事 + 验收期望。"""
    case_id: str
    prompt: str
    store: str = "ksa"
    # 用户最终应该看到的关键数据(正则)。两路通道都要命中才算对得上。
    must_contain: List[str] = field(default_factory=list)
    # 绝不该出现的幻觉词(正则)。出现在浏览器渲染里 = 体验问题。
    must_not_contain: List[str] = field(default_factory=list)
    # 若设置：第二通道(后端)的 tools_used 必须恰好等于此列表。
    expected_tools: Optional[List[str]] = None
    title: str = ""

    def short_title(self) -> str:
        return self.title or self.prompt.strip().splitlines()[0][:60]


@dataclass
class BrowserEvidence:
    """真实浏览器回放采集到的证据——用户**实际看到**的东西。"""
    rendered_text: str = ""            # chat 气泡里渲染出来的回复文本(用户视角)
    screenshot_path: Optional[str] = None
    trace_path: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None        # 回放失败原因(playwright 缺失 / 登录失效 / 超时)

    def present(self) -> bool:
        """证据齐全 = 有非空渲染文本 + 截图 + trace,且三件磁盘文件真实存在。

        缺任何一件 → 视为"没真跑浏览器",门会判 INVALID。`error` 一旦置位即视为缺失,
        防止"抓取失败却把上一次的旧证据冒充本次"。
        """
        if self.error:
            return False
        if not (self.rendered_text or "").strip():
            return False
        for p in (self.screenshot_path, self.trace_path):
            if not p or not os.path.isfile(p):
                return False
        return True


@dataclass
class SecondChannel:
    """第二通道——与浏览器渲染**独立**的后端权威结果(API / 直连工具 / DB)。

    对账要的就是"用户看到的"和"系统真实算出来的"两路独立证据相互印证;只有浏览器一路
    是无法证伪渲染丢数 / 答非所问的。
    """
    source: str = ""                   # 如 "api_chat" / "tool_direct"
    reply: str = ""                    # 第二通道给出的权威回复文本
    tools_used: List[str] = field(default_factory=list)
    references: List[Any] = field(default_factory=list)
    error: Optional[str] = None

    def present(self) -> bool:
        return not self.error and bool((self.reply or "").strip())


@dataclass
class Reconciliation:
    """对账结果：浏览器看到的 与 第二通道权威数据 是否互相印证。"""
    performed: bool = False            # 两路证据都在、真的比对过了
    matched: bool = False
    mismatches: List[str] = field(default_factory=list)

    def present(self) -> bool:
        return self.performed


def reconcile(case: ExperienceCase, browser: BrowserEvidence,
              channel: SecondChannel) -> Reconciliation:
    """对账。两路证据缺任一 → performed=False(门据此判 INVALID/INCONCLUSIVE)。

    都在时逐条比对(失配即体验问题):
      ① 每条 must_contain 必须**两路都命中**(用户看到的 ∩ 系统权威的)。
      ② 第二通道命中的关键数据 token,必须也出现在浏览器渲染里(防渲染丢数/答非所问)。
      ③ 任何 must_not_contain 幻觉词出现在浏览器渲染里 = 失配。
      ④ 若 case 声明 expected_tools,第二通道 tools_used 必须恰好等于它。
    """
    rec = Reconciliation()
    if not browser.present() or not channel.present():
        return rec  # performed 仍为 False —— 没有两路证据就不存在"对账"

    rec.performed = True
    btext = browser.rendered_text
    ctext = channel.reply
    mism: List[str] = []

    for pat in case.must_contain:
        in_browser = re.search(pat, btext) is not None
        in_channel = re.search(pat, ctext) is not None
        if in_channel and not in_browser:
            mism.append(f"第二通道有但用户没看到(渲染丢数): {pat!r}")
        elif in_browser and not in_channel:
            mism.append(f"用户看到但第二通道无此数据(疑似前端编造): {pat!r}")
        elif not in_browser and not in_channel:
            mism.append(f"两路都缺关键数据: {pat!r}")

    for pat in case.must_not_contain:
        if re.search(pat, btext):
            mism.append(f"浏览器渲染出现禁忌/幻觉词: {pat!r}")

    if case.expected_tools is not None and channel.tools_used != case.expected_tools:
        mism.append(
            f"第二通道 tools_used 不符: {channel.tools_used!r} != {case.expected_tools!r}"
        )

    rec.mismatches = mism
    rec.matched = not mism
    return rec


@dataclass
class EvidenceBundle:
    """证据 bundle——一条 case 的全部产物：双通道证据 + 对账 + 结论。"""
    case: ExperienceCase
    browser: BrowserEvidence
    channel: SecondChannel
    reconciliation: Reconciliation
    conclusion: str = INVALID
    reasons: List[str] = field(default_factory=list)

    def mismatch_signature(self) -> str:
        """稳定的失配签名,用于落 issue 去重(同一问题不重复建卡)。"""
        payload = "|".join(sorted(self.reconciliation.mismatches))
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
        return f"{self.case.case_id}:{digest}"

    def evidence_summary(self) -> str:
        b = self.browser
        c = self.channel
        lines = [
            f"**case**: `{self.case.case_id}` — {self.case.short_title()}",
            f"**结论**: {self.conclusion}",
            "",
            "**证据 bundle**",
            f"- 浏览器回放: 截图=`{b.screenshot_path}` trace=`{b.trace_path}` url=`{b.url}`",
            f"- 第二通道: source=`{c.source}` tools_used=`{c.tools_used}`",
            f"- 对账: performed={self.reconciliation.performed} matched={self.reconciliation.matched}",
        ]
        if self.reasons:
            lines.append("")
            lines.append("**判定原因**")
            lines += [f"- {r}" for r in self.reasons]
        if self.reconciliation.mismatches:
            lines.append("")
            lines.append("**对账失配**")
            lines += [f"- {m}" for m in self.reconciliation.mismatches]
        return "\n".join(lines)

    def attachments(self) -> List[str]:
        return [p for p in (self.browser.screenshot_path, self.browser.trace_path)
                if p and os.path.isfile(p)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "conclusion": self.conclusion,
            "reasons": list(self.reasons),
            "browser": {
                "rendered_text": self.browser.rendered_text,
                "screenshot_path": self.browser.screenshot_path,
                "trace_path": self.browser.trace_path,
                "url": self.browser.url,
                "error": self.browser.error,
                "present": self.browser.present(),
            },
            "second_channel": {
                "source": self.channel.source,
                "reply": self.channel.reply,
                "tools_used": list(self.channel.tools_used),
                "error": self.channel.error,
                "present": self.channel.present(),
            },
            "reconciliation": {
                "performed": self.reconciliation.performed,
                "matched": self.reconciliation.matched,
                "mismatches": list(self.reconciliation.mismatches),
            },
            "mismatch_signature": (
                self.mismatch_signature() if self.conclusion == FAIL else None
            ),
        }


def conclude(case: ExperienceCase, browser: BrowserEvidence,
             channel: SecondChannel) -> EvidenceBundle:
    """**承重门**：把双通道证据收敛成四种结论之一。

    这是整个 harness 的关键所在——PASS 只有一条路径(两路证据齐全且对得上)。任何
    "缺证据"都不允许 PASS,直接掐死接线缺失 / 死代码短路 / 占位假数据三种死法。
    """
    rec = reconcile(case, browser, channel)
    bundle = EvidenceBundle(case=case, browser=browser, channel=channel, reconciliation=rec)

    if not browser.present():
        bundle.conclusion = INVALID
        why = browser.error or "缺浏览器证据(截图/trace/渲染文本不全)——回放没有真跑"
        bundle.reasons = [why]
        return bundle

    if not channel.present():
        bundle.conclusion = INCONCLUSIVE
        bundle.reasons = [channel.error or "缺第二通道,无法对账"]
        return bundle

    if not rec.performed:
        # 双通道都 present 却没对账成 —— 不允许沉默放行。
        bundle.conclusion = INCONCLUSIVE
        bundle.reasons = ["对账未执行"]
        return bundle

    if not rec.matched:
        bundle.conclusion = FAIL
        bundle.reasons = ["浏览器体验与第二通道权威数据对账失配(发现体验问题)"]
        return bundle

    bundle.conclusion = PASS
    bundle.reasons = ["双通道证据齐全且对账一致"]
    return bundle


# ── 真实 IO（可注入；拿不到证据时返回空证据，绝不 stub）──────────────────────

def playwright_browser_driver(base_url: str = "http://127.0.0.1:8765",
                              auth_token: str = "",
                              artifacts_dir: Optional[str] = None,
                              timeout_ms: int = 30000) -> Callable[[ExperienceCase], BrowserEvidence]:
    """返回一个用真实 Chromium(playwright)回放 workbench chat 的 driver。

    playwright 没装 / workbench 没起 / 元素找不到 → 返回带 error 的空 BrowserEvidence
    (present()=False)→ 门判 INVALID。绝不伪造截图或渲染文本。
    """
    def _drive(case: ExperienceCase) -> BrowserEvidence:
        try:
            from playwright.sync_api import sync_playwright  # noqa: import 局部，缺失即降级
        except Exception as e:  # pragma: no cover - 取决于本机是否装 playwright
            return BrowserEvidence(error=f"playwright 不可用: {type(e).__name__}: {e}")

        out_dir = artifacts_dir or os.path.join(
            os.environ.get("HIPOP_EXPERIENCE_ARTIFACTS", "/tmp/hipop_experience"),
            case.case_id,
        )
        os.makedirs(out_dir, exist_ok=True)
        shot = os.path.join(out_dir, "screenshot.png")
        trace = os.path.join(out_dir, "trace.zip")
        url = f"{base_url}/?store={case.store}"
        try:  # pragma: no cover - 真实浏览器路径，CI 无头环境不跑
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=["--no-sandbox"])
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                if auth_token:
                    ctx.set_extra_http_headers({"Authorization": f"Bearer {auth_token}"})
                ctx.tracing.start(screenshots=True, snapshots=True)
                page = ctx.new_page()
                page.goto(url)
                page.fill("textarea", case.prompt)
                page.click("button:has-text('发送')")
                # 等回复气泡出现(出现新的 assistant 文本)
                page.wait_for_timeout(min(timeout_ms, 20000))
                rendered = page.inner_text("body")
                page.screenshot(path=shot, full_page=True)
                ctx.tracing.stop(path=trace)
                browser.close()
            return BrowserEvidence(
                rendered_text=rendered, screenshot_path=shot,
                trace_path=trace, url=url,
            )
        except Exception as e:  # pragma: no cover
            return BrowserEvidence(error=f"浏览器回放失败: {type(e).__name__}: {e}", url=url)

    return _drive


def api_second_channel(base_url: str = "http://127.0.0.1:8765",
                       auth_token: str = "",
                       timeout: int = 60) -> Callable[[ExperienceCase], SecondChannel]:
    """返回一个直连 /api/chat 取后端权威结果的第二通道 fetcher(独立于浏览器渲染)。"""
    import urllib.request
    import urllib.error

    def _fetch(case: ExperienceCase) -> SecondChannel:
        body = json.dumps({
            "messages": [{"role": "user", "content": case.prompt}],
            "scope": {"store": case.store},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        req = urllib.request.Request(
            f"{base_url}/api/chat", data=body, headers=headers, method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except Exception as e:  # pragma: no cover - 取决于 server 是否在线
            return SecondChannel(source="api_chat", error=f"{type(e).__name__}: {e}")
        reply = resp.get("clean_reply") or resp.get("reply") or ""
        return SecondChannel(
            source="api_chat",
            reply=reply,
            tools_used=resp.get("tools_used") or [],
            references=resp.get("references") or [],
        )

    return _fetch


def run_case(case: ExperienceCase,
             browser_driver: Callable[[ExperienceCase], BrowserEvidence],
             second_channel: Callable[[ExperienceCase], SecondChannel]) -> EvidenceBundle:
    """跑一条 case：浏览器回放 + 第二通道 → 对账 → 收敛结论 → 证据 bundle。"""
    browser = browser_driver(case)
    channel = second_channel(case)
    return conclude(case, browser, channel)


# ── 自动落体验问题 issue（镜像 card.py 的 MulticaRunner + dedupe 机制）──────────

DEDUPE_KEY = "experience_issue_dedupe_keys"
ISSUE_MAP_KEY = "experience_issue_map"


class ExperienceHarnessError(RuntimeError):
    pass


class MulticaRunner:
    """薄封装 `multica` CLI（与 card.py 同形态）。"""

    def json(self, args: List[str]) -> Dict[str, Any]:
        proc = subprocess.run(
            ["multica"] + args + ["--output", "json"],
            check=True, capture_output=True, text=True,
        )
        text = proc.stdout.strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ExperienceHarnessError(f"multica 返回非对象 JSON: {' '.join(args)}")
        return data

    def run(self, args: List[str]) -> str:
        proc = subprocess.run(
            ["multica"] + args, check=True, capture_output=True, text=True,
        )
        return proc.stdout


def _metadata_object(raw: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw.get("metadata"), dict):
        return dict(raw["metadata"])
    return dict(raw)


def _decode_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _seen_keys(metadata: Dict[str, Any]) -> List[str]:
    raw = _decode_container(metadata.get(DEDUPE_KEY) or [])
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _issue_map(metadata: Dict[str, Any]) -> Dict[str, str]:
    raw = _decode_container(metadata.get(ISSUE_MAP_KEY) or {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def sync_experience_issue(bundle: EvidenceBundle,
                          state_issue: Optional[str] = None,
                          runner: Optional[MulticaRunner] = None,
                          assignee: Optional[str] = None,
                          parent: Optional[str] = None) -> Dict[str, Any]:
    """按规则把一个 FAIL bundle 落成/更新体验问题 issue（带证据摘要 + 附件）。

    - 只处理 FAIL（发现真实体验问题）；非 FAIL 直接返回 skipped。
    - dedupe：`mismatch_signature` 已见过 → 在原 issue 追评论(更新),不重复建卡。
    - 首次：创建 issue（标题 `[体验问题] case_id …`，正文=证据摘要，截图/trace 作附件）,
      并把 signature→issue_id 映射写回 state issue 的 metadata(供后续更新定位)。

    state_issue（存 dedupe/映射的卡）默认取 env HIPOP_EXPERIENCE_STATE_ISSUE。
    """
    if bundle.conclusion != FAIL:
        return {"ok": True, "skipped": True, "reason": f"结论 {bundle.conclusion} 非 FAIL，不落 issue"}

    runner = runner or MulticaRunner()
    state_issue = state_issue or os.environ.get("HIPOP_EXPERIENCE_STATE_ISSUE")
    if not state_issue:
        raise ExperienceHarnessError(
            "落 issue 需要 state issue（--state-issue 或 $HIPOP_EXPERIENCE_STATE_ISSUE）存放 dedupe/映射"
        )

    metadata = _metadata_object(runner.json(["issue", "metadata", "list", state_issue]))
    seen = _seen_keys(metadata)
    issue_map = _issue_map(metadata)
    sig = bundle.mismatch_signature()
    summary = bundle.evidence_summary()

    if sig in seen:
        target = issue_map.get(sig)
        if target:
            # subprocess.run(list) 不经 shell，--content 里的反引号/$ 不会被改写，安全。
            runner.run(["issue", "comment", "add", target, "--content",
                        f"体验问题再次出现（dedupe {sig}）：\n\n{summary}"])
            return {"ok": True, "deduped": True, "action": "commented",
                    "issue": target, "dedupe_key": sig}
        return {"ok": True, "deduped": True, "action": "noop_no_target", "dedupe_key": sig}

    create_args = [
        "issue", "create",
        "--title", f"[体验问题] {bundle.case.case_id}: {bundle.case.short_title()}",
        "--description", summary,
    ]
    if assignee:
        create_args += ["--assignee-id", assignee]
    if parent:
        create_args += ["--parent", parent]
    for att in bundle.attachments():
        create_args += ["--attachment", att]

    created = runner.json(create_args)
    new_id = str(created.get("id") or created.get("identifier") or "")

    seen.append(sig)
    issue_map[sig] = new_id
    runner.run(["issue", "metadata", "set", state_issue, "--key", DEDUPE_KEY,
                "--value", json.dumps(seen, ensure_ascii=False, sort_keys=True)])
    runner.run(["issue", "metadata", "set", state_issue, "--key", ISSUE_MAP_KEY,
                "--value", json.dumps(issue_map, ensure_ascii=False, sort_keys=True)])

    return {"ok": True, "deduped": False, "action": "created",
            "issue": new_id, "dedupe_key": sig, "attachments": bundle.attachments()}


# ── 可执行体（体验官调它，而不是靠长 prompt 自由跑）────────────────────────

def load_cases(spec: Any) -> List[ExperienceCase]:
    """从 JSON 规格（list[dict]）构造 case 列表；缺字段按默认。"""
    cases: List[ExperienceCase] = []
    for raw in spec:
        cases.append(ExperienceCase(
            case_id=str(raw["case_id"]),
            prompt=str(raw["prompt"]),
            store=str(raw.get("store", "ksa")),
            must_contain=list(raw.get("must_contain", [])),
            must_not_contain=list(raw.get("must_not_contain", [])),
            expected_tools=raw.get("expected_tools"),
            title=str(raw.get("title", "")),
        ))
    return cases


def main(argv: Optional[List[str]] = None) -> int:
    """体验官的执行入口：真浏览器回放 + 第二通道对账 → 结论 → 可选落 issue。

      python3 -m hipop.runtime.experience_harness --cases cases.json [--sync]

    缺浏览器/对账时结论为 INVALID/INCONCLUSIVE（绝不 PASS）。`--sync` 时 FAIL 落体验问题
    issue（需 $HIPOP_EXPERIENCE_STATE_ISSUE）。退出码：有 FAIL → 1，否则 0。
    """
    import argparse

    parser = argparse.ArgumentParser(prog="experience_harness")
    parser.add_argument("--cases", help="JSON 文件，list[ {case_id,prompt,store,must_contain,...} ]")
    parser.add_argument("--base-url", default=os.environ.get("HIPOP_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("HIPOP_AUTH_TOKEN", ""))
    parser.add_argument("--sync", action="store_true", help="FAIL 时自动落/更新体验问题 issue")
    args = parser.parse_args(argv)

    if not args.cases:
        parser.error("--cases 必填（体验 case 规格 JSON）")
    with open(args.cases, encoding="utf-8") as fh:
        cases = load_cases(json.load(fh))

    driver = playwright_browser_driver(base_url=args.base_url, auth_token=args.token)
    channel = api_second_channel(base_url=args.base_url, auth_token=args.token)

    any_fail = False
    for case in cases:
        bundle = run_case(case, driver, channel)
        record = bundle.to_dict()
        if args.sync and bundle.conclusion == FAIL:
            record["issue_sync"] = sync_experience_issue(bundle)
        if bundle.conclusion == FAIL:
            any_fail = True
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 1 if any_fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

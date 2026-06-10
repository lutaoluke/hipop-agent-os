"""WS-154 验收 #2 的机械门:每个 PR 收尾必须声明 chat smoke 归属,缺失即红 → 阻断合并。

口径(规划小队 2026-06-10 13:43 收口,已写入本 issue metadata `decision`)
------------------------------------------------------------------------
> 凡是改「用户可见 chat 行为」或它的生产路径,close 前必须有对应 chat smoke;可新增/扩展 case
> 或明确复用已有 case,但 PR/issue 收尾必须写清「变更点 → smoke 名称/命令」,且 gate-chat 通过。
> 只改 CI/测试框架/文档/非 chat 后端 → 允许 `chat smoke: N/A` + 一句话理由,验门人可打回弱 N/A。
> chat 行为范围:意图识别、拒绝/确认/plan→confirm、事实/证据/新鲜度渲染、工具选择或工具描述、
>   工作流执行回复、安全拦截、SYSTEM_PROMPT/skill 中影响 chat 输出的规则。

为什么做成「PR 正文注解契约」而非「改了哪些文件」启发式
--------------------------------------------------------
码长明确要求:**判定的确定性部分做成断言;主观「算不算改了 chat 行为」留验门人裁量,
不要硬编成会误判的启发式**(改了 agent.py 的纯改名也会被文件耦合门误挡)。
所以本门只做**确定性**那一半:PR 收尾正文必须含一行
    chat smoke: <smoke 名/命令>        # 改了 chat 行为 → 指向钉死它的 smoke
  或
    chat smoke: N/A — <一句话理由>     # 非 chat 改动 → 显式声明 + 理由
缺这行、或 N/A 不带理由(弱 N/A)→ 红。
「这次到底算不算改了 chat 行为 / N/A 理由站不站得住」= 验门人裁量(口径已授权打回弱 N/A),不在此硬判。

接线:gate-chat-coverage.yml 把 PR body 透进环境变量 WS154_PR_BODY,跑本脚本,违例 exit 1 阻断。
确定性、无 DB/server/LLM,在 PR 上就能真跑绿/红(零 secret、fork 安全)。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Tuple

# PR 正文里的注解行:`chat smoke: <值>`(大小写不敏感,中英文冒号都认)
_ANNOTATION_RE = re.compile(r"(?im)^\s*chat\s*smoke\s*[:：]\s*(?P<val>.+?)\s*$")
# N/A 的写法:N/A / NA / 无 / none(后面要带理由)
_NA_RE = re.compile(r"^(n/?a|无|none)\b", re.IGNORECASE)


def check_annotation(pr_body: str) -> Tuple[bool, str]:
    """返回 (ok, reason)。确定性判定:注解行是否存在 + N/A 是否带理由。"""
    body = pr_body or ""
    m = _ANNOTATION_RE.search(body)
    if not m:
        return (
            False,
            "PR 收尾正文缺少 `chat smoke:` 行 —— 验收 #2 要求每个 PR 显式声明 chat smoke 归属。\n"
            "  · 改了 chat 行为:写 `chat smoke: <smoke 名/命令>`(指向新增/扩展/复用的那条 case)。\n"
            "  · 非 chat 改动:写 `chat smoke: N/A — <一句话理由>`。",
        )
    val = m.group("val").strip()
    na = _NA_RE.match(val)
    if na:
        # N/A 后面必须有理由(去掉 N/A 这个词后还得有实质内容)
        rest = val[na.end():].strip(" —-:：,，.。")
        if not rest:
            return (
                False,
                f"`chat smoke: {val}` 是弱 N/A(没带理由)—— 口径要求 N/A 必须 + 一句话理由。\n"
                "  写成 `chat smoke: N/A — <为什么这次不涉及 chat 行为>`。",
            )
        return (True, f"声明 chat smoke: N/A(理由:{rest[:60]})—— 是否成立由验门人裁量。")
    return (True, f"声明 chat smoke: {val[:80]} —— 指向具体 smoke,验门人复核是否真钉死本次变更。")


def main_ci() -> int:
    body = os.environ.get("WS154_PR_BODY", "")
    if not body.strip():
        # 防呆:CI 没把 PR body 透进来(配置错)→ 红,别假绿放过
        print("::error::没拿到 PR 正文(WS154_PR_BODY 为空)—— 无法校验 chat smoke 声明,拒绝假绿。")
        return 2
    ok, reason = check_annotation(body)
    if ok:
        print(f"  ✓ {reason}")
        return 0
    print("  ✗ 验收 #2 chat smoke 声明门不通过：")
    print(reason)
    return 1


if __name__ == "__main__":
    sys.exit(main_ci())

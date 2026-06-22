"""smoke_ws155_skill_split.py — WS-155 / E9.1 Agent OS skill 拆分承重墙。

为什么存在
---------
旧的 `openclaw-skill/agent-os.md` 是一份 423 行的单体 skill。运营/Agent 一旦加载，
拿到的是「什么都讲一点、入口不清」的长文档——正是 skill 门要挡的「占位搬运 + 入口臃肿」。
本任务把它拆成 9 个聚焦 skill（每个 ≤80 行，复杂内容走渐进披露引用文件），并把旧
agent-os.md 收成一个 ≤80 行的索引，让加载方走 9 个新入口、不再依赖旧长文档。

本 smoke 钉死「拆分 + 收口」这条行为本身（fail-then-pass）：
  改动前（单体 agent-os.md，9 个新 skill 不存在）：
    - 9 个 SKILL.md 路径不存在 → 断言 FAIL；
    - 旧 agent-os.md 423 行 > 80 → 断言 FAIL。
  改动后（9 个 SKILL.md 落地 + agent-os.md 收成索引）：全部 PASS。

守三种死法：
  · 占位假数据：每个 SKILL.md 正文必须含本域锚点关键词，且禁出现 TODO/占位/待补/lorem——
    不是把长文档原样搬一段就算数。
  · 接线缺失：每个 SKILL.md 必须有 name/description frontmatter（加载/绑定靠它识别入口），
    且 9 个 name 唯一、覆盖预期 slug 集；索引 agent-os.md 必须引用全部 9 个 slug。
  · 死代码短路：旧 agent-os.md 不能还留着单体长文（≤80 行 + 不含被搬走的明细标题）。

跑法：
  python3 tests/smoke_ws155_skill_split.py
  make test-one F=tests/smoke_ws155_skill_split.py
  （也被 make test 自动聚合）
（纯文件断言，不碰 DB / 不碰 live / 不碰 server。）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL_ROOT = os.path.join(REPO, "openclaw-skill")
SPLIT_ROOT = os.path.join(SKILL_ROOT, "agent-os")
INDEX_MD = os.path.join(SKILL_ROOT, "agent-os.md")

MAX_LINES = 80

# 9 个聚焦 skill：slug → 至少要出现的本域锚点关键词（证明是真内容不是搬运占位）。
EXPECTED_SKILLS = {
    "fact-source-contract": ["automation", "stale", "依赖源", "数据出处"],
    "live-sales": ["query_sku", "销量", "erp_sales", "noon_orders"],
    "live-inventory": ["库存", "erp_stock", "noon_stock"],
    "live-logistics": ["query_order", "wf3_logistics", "在途"],
    "replenishment-query": ["compute_replenishment", "补货", "wf5"],
    "workflow-execution": ["run_workflow", "WORKFLOW_REGISTRY", "SSE", "task_id"],
    "rulebook": ["SYSTEM_PROMPT", "hallucinate", "四象限"],
    "experience-eval": ["smoke", "chat-history", "切页面"],
    "governance-gate": ["RBAC", "tenant", "actor_"],
}

PLACEHOLDER_MARKERS = ["TODO", "FIXME", "待补", "占位", "lorem ipsum", "xxx待填"]
# 被搬走的明细标题：旧单体里有、收口后不该再留在 agent-os.md 索引里。
MOVED_AWAY_HEADERS = [
    "## chat 协作 Agent",
    "## 意图 → 依赖源映射",
    "## SSE 协议",
    "## Auth + RBAC + 多租户",
]


def _split_frontmatter(text):
    """返回 (frontmatter_dict_like_str, body)。无 frontmatter 时 fm 为空串。"""
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if m:
            return m.group(1), m.group(2)
    return "", text


def _line_count(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def run():
    failures = []

    # ── 1. 9 个 SKILL.md 存在、行数受控、有 frontmatter、非占位 ──
    seen_names = {}
    for slug, anchors in EXPECTED_SKILLS.items():
        path = os.path.join(SPLIT_ROOT, slug, "SKILL.md")
        if not os.path.isfile(path):
            failures.append(f"缺 skill 入口: openclaw-skill/agent-os/{slug}/SKILL.md")
            continue

        lines = _line_count(path)
        if lines > MAX_LINES:
            failures.append(f"{slug}/SKILL.md {lines} 行 > {MAX_LINES} 行上限")

        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm, body = _split_frontmatter(text)

        name_m = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
        if not name_m:
            failures.append(f"{slug}/SKILL.md 缺 frontmatter `name:`（加载/绑定靠它识别入口）")
        else:
            nm = name_m.group(1)
            seen_names.setdefault(nm, []).append(slug)
        if not re.search(r"^description:\s*\S", fm, re.MULTILINE):
            failures.append(f"{slug}/SKILL.md 缺 frontmatter `description:`")

        low = body.lower()
        for mk in PLACEHOLDER_MARKERS:
            if mk.lower() in low:
                failures.append(f"{slug}/SKILL.md 含占位标记 `{mk}`（疑似搬运未落地）")
        missing_anchors = [a for a in anchors if a.lower() not in low]
        if missing_anchors:
            failures.append(f"{slug}/SKILL.md 正文缺本域锚点 {missing_anchors}（疑似占位/搬错域）")

    # name 唯一性
    dupes = {n: s for n, s in seen_names.items() if len(s) > 1}
    if dupes:
        failures.append(f"skill name 重复: {dupes}")

    # ── 2. 旧 agent-os.md 收成 ≤80 行索引，且引用全部 9 个 slug ──
    if not os.path.isfile(INDEX_MD):
        failures.append("缺 openclaw-skill/agent-os.md 索引")
    else:
        idx_lines = _line_count(INDEX_MD)
        if idx_lines > MAX_LINES:
            failures.append(
                f"agent-os.md 仍 {idx_lines} 行 > {MAX_LINES}（旧单体长文未收口，加载方仍会依赖旧长文档）"
            )
        with open(INDEX_MD, encoding="utf-8") as f:
            idx = f.read()
        missing_refs = [s for s in EXPECTED_SKILLS if s not in idx]
        if missing_refs:
            failures.append(f"agent-os.md 索引未引用这些新 skill: {missing_refs}")
        leftover = [h for h in MOVED_AWAY_HEADERS if h in idx]
        if leftover:
            failures.append(f"agent-os.md 仍残留已拆走的明细章节: {leftover}（死代码短路：旧长文未真正搬走）")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n✗ WS-155 skill 拆分 smoke: {len(failures)} 项失败")
        return 1

    print(f"  ✓ 9 个 SKILL.md 入口齐全、各 ≤{MAX_LINES} 行、frontmatter+本域内容到位、非占位搬运")
    print("  ✓ agent-os.md 已收成 ≤80 行索引并引用全部 9 个新 skill（旧长文档不再被依赖）")
    print("✓ WS-155 Agent OS skill 拆分 smoke 通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())

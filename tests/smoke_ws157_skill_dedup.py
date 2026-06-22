"""WS-157 smoke: 两套平行 skill/command 去重 + agent-os 长文档拆分

验收项目：
1. openclaw-skill/agent-os.md ≤ 100 行（已拆分）
2. openclaw-skill/agent-os-tools.md 存在并包含 chat tool 定义
3. openclaw-skill/agent-os-server.md 存在并包含架构说明
4. openclaw-skill/ 内无陈旧工作目录（Downloads/点购工作流）
5. .claude/commands/hipop-agent-os.md 存在于项目目录（project-scoped command）
6. .claude/commands/ 项目命令不含陈旧工作目录
7. openclaw-skill/agent-os-tools.md 含意图→依赖源映射（关键代理规则）
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(REPO_ROOT, "openclaw-skill")
CMD_DIR = os.path.join(REPO_ROOT, ".claude", "commands")
STALE_PATH = "/Users/luke/Downloads/点购工作流"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _lines(path: str) -> int:
    return len(_read(path).splitlines())


def check_agent_os_split():
    """agent-os.md 已拆分，主文件 ≤ 100 行"""
    main = os.path.join(SKILL_DIR, "agent-os.md")
    assert os.path.exists(main), f"Missing {main}"
    n = _lines(main)
    assert n <= 100, f"openclaw-skill/agent-os.md should be ≤100 lines after split, got {n}"
    print(f"  ✓ agent-os.md = {n} 行 (≤100)")


def check_tools_file():
    """agent-os-tools.md 存在并含 chat tool 定义"""
    path = os.path.join(SKILL_DIR, "agent-os-tools.md")
    assert os.path.exists(path), (
        "Missing openclaw-skill/agent-os-tools.md — "
        "chat tool definitions must be split into this file"
    )
    content = _read(path)
    assert "query_sku" in content, "agent-os-tools.md must contain query_sku tool definition"
    assert "run_workflow" in content, "agent-os-tools.md must contain run_workflow tool definition"
    assert "意图" in content or "intent" in content.lower(), (
        "agent-os-tools.md must contain intent → source mapping"
    )
    n = _lines(path)
    print(f"  ✓ agent-os-tools.md = {n} 行，含 query_sku / run_workflow / 意图路由")


def check_server_file():
    """agent-os-server.md 存在并含架构说明"""
    path = os.path.join(SKILL_DIR, "agent-os-server.md")
    assert os.path.exists(path), (
        "Missing openclaw-skill/agent-os-server.md — "
        "server architecture details must be split into this file"
    )
    content = _read(path)
    assert "tenant" in content or "多租户" in content, (
        "agent-os-server.md must contain multi-tenant architecture docs"
    )
    n = _lines(path)
    print(f"  ✓ agent-os-server.md = {n} 行，含多租户架构说明")


def check_no_stale_path_in_skills():
    """openclaw-skill/ 内不含陈旧工作目录"""
    for fname in os.listdir(SKILL_DIR):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(SKILL_DIR, fname)
        content = _read(fpath)
        assert STALE_PATH not in content, (
            f"{fname} contains stale working dir '{STALE_PATH}' — "
            f"update to /Users/luke/code/hipop"
        )
    print(f"  ✓ 所有 openclaw-skill/*.md 无陈旧工作目录")


def check_project_command_exists():
    """项目目录下存在 .claude/commands/hipop-agent-os.md（project-scoped command）"""
    path = os.path.join(CMD_DIR, "hipop-agent-os.md")
    assert os.path.exists(path), (
        f"Missing {path} — "
        "add a project-scoped command that references the canonical skill files, "
        "so it shadows the stale global ~/.claude/commands/hipop-agent-os.md"
    )
    content = _read(path)
    # The project command should reference the canonical skill source
    assert "openclaw-skill" in content or "agent-os-tools" in content, (
        ".claude/commands/hipop-agent-os.md must reference openclaw-skill/ as the authoritative source"
    )
    print(f"  ✓ .claude/commands/hipop-agent-os.md 存在且指向 openclaw-skill/")


def check_project_commands_no_stale_path():
    """项目命令不含陈旧工作目录"""
    if not os.path.isdir(CMD_DIR):
        # CMD_DIR doesn't exist yet, will be caught by check_project_command_exists
        return
    for fname in os.listdir(CMD_DIR):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(CMD_DIR, fname)
        content = _read(fpath)
        assert STALE_PATH not in content, (
            f".claude/commands/{fname} contains stale working dir '{STALE_PATH}'"
        )
    print(f"  ✓ .claude/commands/*.md 无陈旧工作目录")


CHECKS = [
    ("agent-os.md 已拆分 ≤100 行", check_agent_os_split),
    ("agent-os-tools.md 存在含工具定义", check_tools_file),
    ("agent-os-server.md 存在含架构说明", check_server_file),
    ("openclaw-skill/ 无陈旧路径", check_no_stale_path_in_skills),
    ("项目 .claude/commands/hipop-agent-os.md 存在", check_project_command_exists),
    ("项目命令无陈旧路径", check_project_commands_no_stale_path),
]


if __name__ == "__main__":
    print("== WS-157: skill 去重 + agent-os 拆分 smoke ==")
    failed = []
    for name, fn in CHECKS:
        try:
            fn()
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ✗ {name}: unexpected error: {e}")
            failed.append(name)

    print()
    if failed:
        print(f"FAIL: {len(failed)}/{len(CHECKS)} checks failed")
        sys.exit(1)
    else:
        print(f"✓ WS-157 skill 去重 smoke 全过 ({len(CHECKS)}/{len(CHECKS)})")

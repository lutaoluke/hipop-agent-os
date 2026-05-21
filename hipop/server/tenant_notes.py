"""Tenant NOTES.md — 文件式 memory（Phase 0.3，2026-05-21）

按 Anthropic Effective Context Engineering for AI Agents 范式：
  "File-based memory tool" + "structured note-taking" 持久化 agent / user 偏好
  跨 chat session 复用 — 不要把所有上下文塞 prompt

文件位置：
  ~/hipop/tenants/<tenant_id>/NOTES.md     长期偏好 / 业务规则 / 学到的经验
  ~/hipop/tenants/<tenant_id>/USER.md      运营人画像（per-user 偏好）

NOTES.md 维护规则（按 Anthropic CLAUDE.md 哲学）：
  - 短，每行问"删了 agent 会出错吗"
  - 行业知识 / 客户偏好 / 反复出现的规则
  - 不重复 tool 已有的能力

读：tool tenant_notes_get() 在 chat 用到关键决策时调（懒加载，不塞 prompt）
写：tool tenant_notes_append() Agent 学到新偏好时调
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional


_NOTES_ROOT = Path(os.environ.get(
    "HIPOP_TENANTS_ROOT", os.path.expanduser("~/hipop/tenants")
))


def _tenant_dir(tenant_id: int) -> Path:
    d = _NOTES_ROOT / str(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _notes_path(tenant_id: int) -> Path:
    return _tenant_dir(tenant_id) / "NOTES.md"


def get_notes(tenant_id: int, section: Optional[str] = None) -> str:
    """读 tenant 的 NOTES.md。section 可选：只返 # 标题段。"""
    p = _notes_path(tenant_id)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    if not section:
        return text
    # 按 # 标题分段
    lines = text.split("\n")
    out = []
    in_section = False
    for line in lines:
        if line.startswith("#"):
            in_section = (section.lower() in line.lower())
            if in_section:
                out.append(line)
            continue
        if in_section:
            out.append(line)
    return "\n".join(out).strip()


def append_note(tenant_id: int, note: str, section: str = "通用") -> dict:
    """追加一条 note 到 NOTES.md 对应 section."""
    p = _notes_path(tenant_id)
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    section_header = f"## {section}"
    new_line = f"- [{timestamp}] {note.strip()}"

    if p.exists():
        text = p.read_text(encoding="utf-8")
    else:
        text = f"# Tenant {tenant_id} NOTES\n\n（hipop Agent 跨 session 沉淀的客户偏好 / 业务规则 / 学到的经验）\n"

    if section_header in text:
        # append 到 section 末尾
        lines = text.split("\n")
        out, in_sec = [], False
        for i, line in enumerate(lines):
            if line == section_header:
                in_sec = True
                out.append(line)
                continue
            if in_sec and line.startswith("# "):
                # 当前 section 结束，插新行
                out.append(new_line)
                out.append("")
                in_sec = False
            out.append(line)
        if in_sec:  # section 是最后一段
            out.append(new_line)
        text = "\n".join(out)
    else:
        text += f"\n\n{section_header}\n\n{new_line}\n"

    p.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "section": section,
        "path": str(p),
        "appended": new_line,
    }


def list_sections(tenant_id: int) -> list:
    """列 NOTES.md 所有 section 标题。"""
    p = _notes_path(tenant_id)
    if not p.exists():
        return []
    return [
        line.strip() for line in p.read_text(encoding="utf-8").split("\n")
        if line.startswith("##")
    ]

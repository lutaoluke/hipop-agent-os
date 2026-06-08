"""Anthropic 实现 — 包装现有 tool_use loop。"""
from __future__ import annotations

import os
import json
from typing import Any, Callable, Dict, List

import anthropic

from . import _auth

# governance dispatch 统一走 agent._exec_tool —— 历史上这里有过 _exec_tool 副本
# 只做 RBAC 绕过了 governance，2026-05-26 删掉。详见 agent.py _exec_tool docstring。


def run(messages: List[Dict], system: str, tools: List[Dict],
        tool_funcs: Dict[str, Callable], scope: dict) -> dict:
    client = _auth.get_client()
    model = os.environ.get("ANTHROPIC_CHAT_MODEL", "claude-haiku-4-5-20251001")

    refs_collected: list = []
    tool_log: list = []
    final_text = ""
    workflow_task = None
    retried_auth = False

    msgs = list(messages)

    for hop in range(6):
        try:
            resp = client.messages.create(
                model=model,
                system=system,
                messages=msgs,
                tools=tools,
                max_tokens=2048,
            )
        except anthropic.AuthenticationError:
            if retried_auth:
                raise
            retried_auth = True
            _auth.reset()
            client = _auth.get_client()
            continue

        msgs.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    final_text += block.text
            break

        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                final_text += block.text + "\n"
            elif getattr(block, "type", None) == "tool_use":
                tool_name = block.name
                tool_args = block.input or {}
                from . import agent as _agent
                result = _agent._exec_tool(tool_name, tool_args, user=scope)
                if isinstance(result, dict) and "references" in result:
                    refs_collected.extend(result["references"])
                if tool_name == "run_workflow" and isinstance(result, dict) and result.get("ok"):
                    workflow_task = {
                        "task_id": result["task_id"],
                        "workflow": result["workflow"],
                        "label": result["label"],
                        "total_steps": result["total_steps"],
                        "affected_modules": result["affected_modules"],
                        "followup_prompt": result.get("followup_prompt"),
                    }
                entry: dict = {
                    "name": tool_name, "args": tool_args,
                    "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                    "result_error": result.get("error") if isinstance(result, dict) else None,
                }
                if tool_name == "run_workflow" and isinstance(result, dict):
                    # T36-S3: enrich for _safety._check_failed_workflow_claimed_success
                    ok_val = result.get("ok")
                    entry["ok"] = ok_val if ok_val is not None else ("error" not in result)
                    entry["task_id"] = result.get("task_id")
                    entry["error"] = result.get("error")
                tool_log.append(entry)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
        msgs.append({"role": "user", "content": tool_results})

    return {
        "reply": final_text.strip() or "(无回复)",
        "tool_log": tool_log,
        "refs_collected": refs_collected,
        "workflow_task": workflow_task,
    }

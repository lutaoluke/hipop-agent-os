"""Anthropic 实现 — 包装现有 tool_use loop。"""
from __future__ import annotations

import os
import json
from typing import Any, Callable, Dict, List

import anthropic

from . import _auth

# governance dispatch 统一走 agent._exec_tool —— 历史上这里有过 _exec_tool 副本
# 只做 RBAC 绕过了 governance，2026-05-26 删掉。详见 agent.py _exec_tool docstring。


def _stale_skus_from_sku_result(tool_name: str, result: Any) -> "list | None":
    """T03: 从 query_sku 结果提取 live_sales_failed SKU 列表，供 safety 验门。
    抽成函数避免 provider 各自重写逻辑；smoke 可直接 import 测试而不复制。"""
    if tool_name != "query_sku" or not isinstance(result, dict):
        return None
    stale = []
    for item in result.get("items") or []:
        if not isinstance(item, dict) or not item.get("sku"):
            continue
        freshness = item.get("sales_freshness_decision") or {}
        freshness_present = isinstance(freshness, dict) and bool(freshness)
        freshness_allows_number = freshness_present and freshness.get("can_output_number") is True
        freshness_blocks_number = (
            freshness_present
            and freshness.get("status") in {"ask_cache_consent", "blocked"}
            and freshness.get("can_output_number") is False
        )
        if (
            item.get("live_sales_failed")
            or freshness_blocks_number
            or (item.get("data_stale") and not freshness_allows_number)
        ):
            stale.append(item["sku"])
    return stale or None


def run(messages: List[Dict], system: str, tools: List[Dict],
        tool_funcs: Dict[str, Callable], scope: dict) -> dict:
    client = _auth.get_client()
    model = os.environ.get("ANTHROPIC_CHAT_MODEL", "claude-haiku-4-5-20251001")

    refs_collected: list = []
    tool_log: list = []
    final_text = ""
    workflow_tasks: list = []
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
                if tool_name == "run_workflow" and isinstance(result, dict):
                    wf_name = tool_args.get("workflow", "unknown") if isinstance(tool_args, dict) else "unknown"
                    if result.get("ok"):
                        workflow_tasks.append({
                            "ok": True,
                            "task_id": result["task_id"],
                            "workflow": result.get("workflow", wf_name),
                            "label": result.get("label", wf_name),
                            "total_steps": result.get("total_steps", 0),
                            "affected_modules": result.get("affected_modules", []),
                            "followup_prompt": result.get("followup_prompt"),
                        })
                    else:
                        workflow_tasks.append({
                            "ok": False,
                            "workflow": result.get("workflow") or wf_name,
                            "label": result.get("label") or wf_name,
                            "error": result.get("error") or "触发失败",
                            "task_id": None,
                        })
                # T03: capture live_sales_failed SKUs from query_sku → safety verifier
                result_stale_skus = _stale_skus_from_sku_result(tool_name, result)
                from .replenishment_evidence import blocked_skus_from_tool_result
                result_replenishment_blocked_skus = blocked_skus_from_tool_result(tool_name, result)
                entry: dict = {
                    "name": tool_name, "args": tool_args,
                    "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                    "result_error": result.get("error") if isinstance(result, dict) else None,
                    "result_stale_skus": result_stale_skus,
                    "result_replenishment_blocked_skus": result_replenishment_blocked_skus,
                    "result_download_url": result.get("download_url") if isinstance(result, dict) else None,
                    "result_filename": result.get("filename") if isinstance(result, dict) else None,
                }
                if tool_name == "run_workflow" and isinstance(result, dict):
                    # T36-S3: enrich for _safety._check_failed_workflow_claimed_success
                    ok_val = result.get("ok")
                    entry["ok"] = ok_val if ok_val is not None else ("error" not in result)
                    entry["task_id"] = result.get("task_id")
                    entry["error"] = result.get("error")
                # WS-161 B-2: capture fact-slot evidence (失败/空 → 结果槽留空) for 承重墙
                from ._factslot_contract import factslot_evidence_from_result
                fse = factslot_evidence_from_result(tool_name, result)
                if fse:
                    entry["factslot_evidence"] = fse
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
        "workflow_tasks": workflow_tasks,
    }

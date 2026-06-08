"""OpenAI 兼容协议实现 — 覆盖 DeepSeek / Qwen / 豆包。

三家都走同一个 `openai` SDK，仅 base_url + api_key + model 不同。
当前默认 deepseek（Luke 实操选定）。

env:
  LLM_PROVIDER=deepseek|qwen|doubao   (默认 deepseek)
  DEEPSEEK_API_KEY=...       从 .env.local 自动 load (main.py 启动时)
  QWEN_API_KEY=...           DASHSCOPE_API_KEY 同义；阿里云灵积控制台拿
  DOUBAO_API_KEY=...         火山引擎控制台拿
  <PROVIDER>_MODEL=...       覆盖默认模型
"""
from __future__ import annotations

import os
import json
from typing import Any, Callable, Dict, List

# 协议规格：每个 provider → (base_url, default_model, api_key_env)
PROVIDERS = {
    "qwen": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        ["QWEN_API_KEY", "DASHSCOPE_API_KEY"],
    ),
    "deepseek": (
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        ["DEEPSEEK_API_KEY"],
    ),
    "doubao": (
        "https://ark.cn-beijing.volces.com/api/v3",
        # 豆包要 endpoint id（用户控制台创建模型 endpoint），用 DOUBAO_MODEL 覆盖
        "doubao-pro-32k",
        ["DOUBAO_API_KEY", "ARK_API_KEY"],
    ),
}


def _get_client_and_model(provider: str):
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai SDK 未安装：pip install openai")
    base_url, default_model, key_envs = PROVIDERS[provider]
    api_key = None
    for k in key_envs:
        if os.environ.get(k):
            api_key = os.environ[k]
            break
    if not api_key:
        raise RuntimeError(
            f"{provider} 凭据缺失：请 export {' 或 '.join(key_envs)}"
        )
    model = os.environ.get(f"{provider.upper()}_MODEL", default_model)
    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


def _anthropic_tools_to_openai(tools: List[Dict]) -> List[Dict]:
    """Anthropic schema → OpenAI function calling schema"""
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


# governance dispatch 统一走 agent._exec_tool —— 历史上这里有过 _exec_tool 副本
# 只做 RBAC 绕过了 governance，2026-05-26 删掉。详见 agent.py _exec_tool docstring。


def run(messages: List[Dict], system: str, tools: List[Dict],
        tool_funcs: Dict[str, Callable], scope: dict, provider: str) -> dict:
    client, model = _get_client_and_model(provider)
    oai_tools = _anthropic_tools_to_openai(tools)

    # OpenAI 协议要求 system 单独一条（位置 0）；user/assistant/tool 分别单独消息
    # 把 anthropic 风格的 messages（含 list content）转成 openai 风格 string content
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
        elif isinstance(c, list):
            # anthropic 的 assistant 历史里可能含 content blocks（text+tool_use 混合）
            # 简化：取所有 text 块拼起来；tool_use / tool_result 在新的循环里通过 OpenAI tool_calls 协议处理
            text_parts = []
            for blk in c:
                if isinstance(blk, dict):
                    if blk.get("type") == "text":
                        text_parts.append(blk.get("text", ""))
                    elif blk.get("type") == "tool_result":
                        # 历史里的 tool_result，转成 openai 的 'tool' role 消息
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": blk.get("tool_use_id", ""),
                            "content": blk.get("content", ""),
                        })
            if text_parts:
                msgs.append({"role": m["role"], "content": "\n".join(text_parts)})
        else:
            msgs.append({"role": m["role"], "content": str(c)})

    refs_collected: list = []
    tool_log: list = []
    final_text = ""
    workflow_tasks: list = []

    for hop in range(6):
        resp = client.chat.completions.create(
            model=model,
            messages=msgs,
            tools=oai_tools,
            tool_choice="auto",
            max_tokens=2048,
        )
        choice = resp.choices[0]
        msg = choice.message

        # OpenAI 协议：finish_reason in {stop, tool_calls, length, content_filter}
        if choice.finish_reason != "tool_calls" or not msg.tool_calls:
            if msg.content:
                final_text += msg.content
            break

        # 累计 assistant 这条（含 tool_calls）回到对话历史
        msgs.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })
        if msg.content:
            final_text += msg.content + "\n"

        # 执行所有 tool_call，结果作为 tool 消息追加
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args_raw = tc.function.arguments
            try:
                tool_args = json.loads(tool_args_raw) if isinstance(tool_args_raw, str) else (tool_args_raw or {})
            except Exception:
                result = {"error": f"invalid tool arguments JSON: {str(tool_args_raw)[:200]}"}
            else:
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
            entry: dict = {
                "name": tool_name,
                "args": tool_args_raw,
                "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                "result_error": result.get("error") if isinstance(result, dict) else None,
            }
            if tool_name == "run_workflow" and isinstance(result, dict):
                # T36-S3: enrich for _safety._check_failed_workflow_claimed_success
                ok_val = result.get("ok")
                entry["ok"] = ok_val if ok_val is not None else ("error" not in result)
                entry["task_id"] = result.get("task_id")
                entry["error"] = result.get("error")
            if tool_name == "query_sku" and isinstance(result, dict):
                stale_skus = [
                    item.get("sku")
                    for item in (result.get("items") or [])
                    if isinstance(item, dict) and item.get("data_stale") and item.get("sku")
                ]
                if stale_skus:
                    entry["result_stale_skus"] = stale_skus
            tool_log.append(entry)
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    return {
        "reply": final_text.strip() or "(无回复)",
        "tool_log": tool_log,
        "refs_collected": refs_collected,
        "workflow_tasks": workflow_tasks,
    }

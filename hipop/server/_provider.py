"""LLM Provider 抽象层 — 支持 Anthropic + OpenAI 兼容协议（Qwen / DeepSeek / 豆包）

切换方式：环境变量 LLM_PROVIDER=qwen | deepseek | doubao | anthropic（默认 qwen）

统一接口：
    chat_with_tools(messages, system, tools, tool_funcs, scope) -> ChatResult

ChatResult 字段：
    reply: str               最终给用户的文本
    tool_log: list[dict]     每次 tool 调用的 {name, args, result_keys}
    refs_collected: list     所有 tool 返回的 references 累加（去重在外面做）
    workflow_task: dict|None 若调了 run_workflow，透传 task 元信息

每个 provider 实现一个相同签名的 _impl_chat 函数。
"""
from __future__ import annotations

import os
import json
from typing import Any, Callable, Dict, List, Optional


class ChatResult(dict):
    """便利类，dict 子类，方便直接 ** 解包"""
    pass


def _normalize_messages(messages: List[Dict]) -> List[Dict]:
    """截断历史 + 标准化 content 类型。各 provider 都接受 [{role, content:str|list}, ...]"""
    hist = messages[-16:]
    out = []
    for m in hist:
        c = m.get("content")
        # content 可能是 str（用户 input）或 list（前轮 assistant 的 content blocks）
        out.append({"role": m["role"], "content": c})
    return out


def get_provider() -> str:
    """当前生效的 provider 名。默认 anthropic（长对话稳定性 + 拒绝 hallucinate 优于 Qwen）。
    阶段 1 alpha 用 Anthropic + OAuth 订阅；多租户上线后切 Qwen。"""
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


def chat_with_tools(
    messages: List[Dict],
    system: str,
    tools: List[Dict],          # Anthropic schema 格式（input_schema），openai-compat impl 内部转换
    tool_funcs: Dict[str, Callable],
    scope: Optional[Dict] = None,
) -> ChatResult:
    """主入口：根据 LLM_PROVIDER 分派到具体实现。"""
    provider = get_provider()
    msgs = _normalize_messages(messages)

    if provider == "anthropic":
        from . import _provider_anthropic as impl
        return impl.run(msgs, system, tools, tool_funcs, scope or {})

    # qwen / deepseek / doubao 都走 OpenAI 兼容协议
    from . import _provider_openai as impl
    return impl.run(msgs, system, tools, tool_funcs, scope or {}, provider)

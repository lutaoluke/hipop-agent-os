"""Runtime context shared by the chat shell and tool implementations.

WS-169 keeps these contextvars and small lookup helpers out of the CODEOWNERS
lock file while preserving the public `agent.X` re-export surface used by tests
and tools_impl.
"""
import contextvars
from typing import Any, Optional

# ── chat 当前请求 context（tenant_id + scope）─────────────
# 由 chat() 入口 set，所有 tool 函数同线程读
_chat_tenant: contextvars.ContextVar[int] = contextvars.ContextVar("chat_tenant", default=1)
_chat_scope: contextvars.ContextVar[dict] = contextvars.ContextVar("chat_scope", default={})
_chat_question: contextvars.ContextVar[str] = contextvars.ContextVar("chat_question", default="")
_last_replenishment_stock_status: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "last_replenishment_stock_status", default=None
)
_last_sku_rate_stats: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "last_sku_rate_stats", default=None
)
# WS-145 肯定执行意图门:chat() 入口按本轮句式语气求出的门决策。
# _exec_tool 据此拒绝「非执行语气下偷偷 run_workflow」（LLM 不许绕）。
_chat_intent: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "chat_intent", default=None
)


def _get_tenant() -> int:
    return _chat_tenant.get() or 1


def _resolve_entity_alias(store_code: str) -> Optional[str]:
    """把工作台顶部 dropdown 的 store code（KSA/UAE）转成本租户的 entity_alias。
    按 (tenant_id, country) 查 sales_entities 表。
    KSA → SA, UAE → AE
    """
    from . import data as _d
    tid = _get_tenant()
    country = {"KSA": "SA", "UAE": "AE", "SA": "SA", "AE": "AE"}.get(store_code.upper())
    if not country:
        return None
    rows = _d._fetch(
        "SELECT alias FROM sales_entities WHERE tenant_id=? AND country=? AND active=1 LIMIT 1",
        (tid, country),
    )
    return rows[0]["alias"] if rows else None

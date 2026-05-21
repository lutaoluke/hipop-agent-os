"""RBAC 权限矩阵 (W2 Task 2.3)

4 角色: owner / manager / ops / forwarder
权限按"操作粒度"硬编码（阶段 1 简单粗暴；阶段 2 改为客户可配）。

调用方式:
- 装饰器: @require_permission("trigger_workflow")
- 直接判: rbac.can(user, "trigger_workflow")
- chat tool 门控: rbac.tool_allowed(user, "run_workflow") → bool
"""
from __future__ import annotations

from functools import wraps
from typing import Callable, Dict
from fastapi import Depends, HTTPException

from . import auth as _auth_mod

# ── 操作 → 角色矩阵 ──────────────────────────────────────────
# value 是允许的角色集合
PERMISSIONS: Dict[str, set] = {
    # 看板 / 查询（所有角色都能看）
    "view_dashboard":     {"owner", "manager", "ops", "forwarder"},
    "view_sku":           {"owner", "manager", "ops", "forwarder"},
    "view_orders":        {"owner", "manager", "ops", "forwarder"},
    "view_replenish":     {"owner", "manager", "ops", "forwarder"},

    # 触发工作流 / 上传 CSV（工作组所有人都能操作，留痕在 agent_events.actor_*）
    "trigger_workflow":   {"owner", "manager", "ops", "forwarder"},
    "upload_csv":         {"owner", "manager", "ops", "forwarder"},

    # 写入告警状态（forwarder 是跟单，主要写这个）
    "update_alert":       {"owner", "manager", "ops", "forwarder"},

    # 团队 / 配置（管理类）
    "invite_user":        {"owner", "manager"},
    "change_user_role":   {"owner", "manager"},
    "edit_store_config":  {"owner"},
    "view_billing":       {"owner"},
    "edit_billing":       {"owner"},
}


# chat tool → 操作映射（拦 LLM 越权调 tool）
TOOL_PERMISSION = {
    "query_sku":              "view_sku",
    "query_order":            "view_orders",
    "scope_overview":         "view_dashboard",
    "compute_replenishment":  "view_replenish",
    "compute_air_freight_roi":"view_replenish",
    "data_health_check":      "view_dashboard",
    "list_products":          "view_sku",
    "update_alert_status":    "update_alert",
    "run_workflow":           "trigger_workflow",
    "export_table":           "view_dashboard",
    "navigate_user_to":       "view_dashboard",
    "notify_via_feishu":      "view_dashboard",  # stub，真发飞书时改 invite_user
    "tenant_notes_get":       "view_dashboard",
    "tenant_notes_append":    "view_dashboard",
    "confirm_proposal":       "view_dashboard",
}


def can(user: dict, action: str) -> bool:
    """user 是否有 action 权限。"""
    role = (user or {}).get("role") or "forwarder"
    allowed_roles = PERMISSIONS.get(action)
    if allowed_roles is None:
        # 未知 action 默认拒绝（保守）
        return False
    return role in allowed_roles


def tool_allowed(user: dict, tool_name: str) -> bool:
    """chat tool 调用前检查。未在矩阵的 tool 默认放行。"""
    perm = TOOL_PERMISSION.get(tool_name)
    if perm is None:
        return True
    return can(user, perm)


def require_permission(action: str):
    """FastAPI endpoint 装饰器。"""
    def deco(fn: Callable):
        @wraps(fn)
        async def wrapper(*args, user=None, **kwargs):
            if user is None:
                raise HTTPException(401, "需要登录")
            if not can(user, action):
                raise HTTPException(
                    403,
                    f"权限不足：操作 {action!r} 需要角色之一 {sorted(PERMISSIONS.get(action) or [])}，"
                    f"当前角色 {user.get('role')}",
                )
            return await fn(*args, user=user, **kwargs)
        return wrapper
    return deco


def get_my_permissions(user: dict) -> Dict[str, bool]:
    """返回当前用户对所有 action 的权限 map（前端用来按角色隐藏入口）。"""
    return {action: can(user, action) for action in PERMISSIONS}

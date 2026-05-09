"""用户系统 + JWT auth (W2 Task 2.1)

设计:
- 多租户：tenants(id, name, plan) → users(id, tenant_id, email, role)
- 阶段 1 简化：邮箱 + 密码 + JWT cookie；W2 后期改 magic link
- 4 角色：owner / manager / ops / forwarder（RBAC 见 rbac.py）
- 兼容老 single-user 模式：DB 没建表 / 没用户时，scope 默认 tenant_id=1, user='Cherry', role='ops'
  → 旧 chat / API 调用都不破坏。

JWT secret 从 env JWT_SECRET 读，缺省时随机生成（重启后失效，dev only）。
"""
from __future__ import annotations

import os
import json
import secrets
import datetime
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, Request, Response
from passlib.context import CryptContext
from jose import jwt, JWTError

from . import data as _data

JWT_SECRET    = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 30  # 30 天
COOKIE_NAME   = "hipop_session"

pwd_ctx = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# ── 默认 fallback user（兼容现有 single-tenant 调用）─────────
DEFAULT_USER = {
    "id": 0,
    "tenant_id": 1,
    "email": "cherry@hipop.local",
    "display_name": "Cherry",
    "role": "owner",
    "is_default": True,
}


# ── 密码 / token ─────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def make_jwt(user_id: int, tenant_id: int) -> str:
    now = datetime.datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "tid": tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


# ── 用户表读写 ───────────────────────────────────────────────
def _users_table_exists() -> bool:
    """SQLite 走 sqlite_master；PG 走 information_schema。检测 users 表是否已建。"""
    try:
        if _data.is_postgres():
            r = _data._fetch(
                "SELECT 1 FROM information_schema.tables WHERE table_name='users'"
            )
        else:
            r = _data._fetch(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            )
        return bool(r)
    except Exception:
        return False


def get_user_by_email(email: str) -> Optional[Dict]:
    if not _users_table_exists():
        return None
    rows = _data._fetch(
        "SELECT id, tenant_id, email, display_name, password_hash, role, active "
        "FROM users WHERE email=? AND active=1",
        (email,),
    )
    return rows[0] if rows else None


def get_user_by_id(uid: int) -> Optional[Dict]:
    if not _users_table_exists():
        return None
    rows = _data._fetch(
        "SELECT id, tenant_id, email, display_name, role, active "
        "FROM users WHERE id=?",
        (uid,),
    )
    return rows[0] if rows else None


def create_tenant(name: str, plan: str = "free") -> int:
    """创建租户，返回 tenant_id。"""
    with _data.conn() as c:
        cur = c.execute(
            "INSERT INTO tenants (name, plan) VALUES (?, ?) RETURNING id"
            if _data.is_postgres()
            else "INSERT INTO tenants (name, plan) VALUES (?, ?)",
            (name, plan),
        )
        if _data.is_postgres():
            row = cur.fetchone()
            tid = row["id"] if isinstance(row, dict) else row[0]
        else:
            tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    return tid


def create_user(tenant_id: int, email: str, password: str,
                display_name: str = "", role: str = "ops") -> int:
    """创建用户，返回 user_id。"""
    pw_hash = hash_password(password)
    with _data.conn() as c:
        cur = c.execute(
            "INSERT INTO users (tenant_id, email, display_name, password_hash, role) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id"
            if _data.is_postgres()
            else "INSERT INTO users (tenant_id, email, display_name, password_hash, role) "
                 "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, email, display_name or email.split("@")[0], pw_hash, role),
        )
        if _data.is_postgres():
            row = cur.fetchone()
            uid = row["id"] if isinstance(row, dict) else row[0]
        else:
            uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    return uid


# ── FastAPI 依赖：从请求拿 user（cookie / header / fallback）──
def get_current_user(request: Request) -> Dict:
    """所有需要 auth 的 endpoint 用 Depends(get_current_user)。

    优先级:
    1. Authorization: Bearer <jwt>
    2. Cookie: hipop_session=<jwt>
    3. fallback: DEFAULT_USER（兼容老 chat / dev 模式）
    """
    token = None
    auth_h = request.headers.get("authorization") or ""
    if auth_h.startswith("Bearer "):
        token = auth_h[7:]
    if not token:
        token = request.cookies.get(COOKIE_NAME)

    if token:
        payload = decode_jwt(token)
        if payload:
            uid = int(payload.get("sub", 0))
            user = get_user_by_id(uid)
            if user and user.get("active"):
                user["is_default"] = False
                return user

    # 兜底 default
    return dict(DEFAULT_USER)


# ── 注册 / 登录业务 ─────────────────────────────────────────
def register(email: str, password: str, tenant_name: Optional[str] = None,
             display_name: str = "") -> Dict:
    """新邮箱注册：创建 tenant + owner 用户。"""
    if get_user_by_email(email):
        raise HTTPException(409, f"邮箱 {email} 已注册")
    tid = create_tenant(tenant_name or email.split("@")[0])
    uid = create_user(tid, email, password, display_name=display_name, role="owner")
    return {"tenant_id": tid, "user_id": uid, "role": "owner", "email": email}


def login(email: str, password: str) -> Dict:
    """登录：返回 JWT + user 信息。"""
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "邮箱或密码错误")
    token = make_jwt(user["id"], user["tenant_id"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user.get("display_name") or user["email"].split("@")[0],
            "tenant_id": user["tenant_id"],
            "role": user["role"],
        },
    }


def set_session_cookie(response: Response, token: str):
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=JWT_EXPIRE_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=False,  # 阶段 1 本地 / Zeabur 时改 True
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(COOKIE_NAME)

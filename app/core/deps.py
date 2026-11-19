"""FastAPI 依赖注入（公共基础层）：会话、当前用户、角色守卫。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.common.models import User
from app.core.errors import Forbidden, Unauthenticated
from app.core.security import decode_access_token
from app.db.base import get_db

bearer_scheme = HTTPBearer(auto_error=False)


def get_session(db: Session = Depends(get_db)) -> Session:
    return db


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_session),
) -> User:
    if credentials is None or not credentials.credentials:
        raise Unauthenticated()
    payload = decode_access_token(credentials.credentials)
    user = db.get(User, payload.get("sub", ""))
    if user is None or user.status != "active":
        raise Unauthenticated("账号不存在或已停用")
    return user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_session),
) -> User | None:
    """扫码查询允许匿名（UC-04 BR-01），登录与否只影响日志里的用户维度。"""
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
    except Unauthenticated:
        return None
    return db.get(User, payload.get("sub", ""))


def require_permission(permission: str) -> Callable[..., User]:
    """按 RBAC 权限点守卫（来自 feat/identity-ops 的权限矩阵）。

    与 require_roles 的分工：require_roles 表达「哪些角色能用」，
    require_permission 表达「需要哪个业务能力」，后者在权限点复用
    （如 cert:read:all 同时给运营与监管）时不会写重复的角色元组。
    """

    def _guard(user: User = Depends(get_current_user)) -> User:
        from app.core.rbac import ensure

        ensure(user.role, permission)
        return user

    return _guard


def require_roles(*roles: str) -> Callable[..., User]:
    """角色守卫工厂：require_roles(Role.FARMER) → 依赖函数。"""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise Forbidden(f"该操作需要角色：{'/'.join(roles)}")
        return user

    return _guard

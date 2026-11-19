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


def require_roles(*roles: str) -> Callable[..., User]:
    """角色守卫工厂：require_roles(Role.FARMER) → 依赖函数。"""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise Forbidden(f"该操作需要角色：{'/'.join(roles)}")
        return user

    return _guard

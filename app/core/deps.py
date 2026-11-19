"""FastAPI 依赖注入（公共基础层 + 用户与运营域）。

两层权限拦截（报告 2.5.2 第 5 条）：
- 第一层：路由级角色校验 —— require_permission("cert:review")，越权即 403 并写审计；
- 第二层：资源级归属校验 —— 在 service 里做，例如「只能看自己产地的认证申请」。

这样拆分的原因是：角色只回答「这类人能做什么」，回答不了「这个农户能不能看这条记录」，
把归属判断塞进依赖里会导致依赖需要感知每个业务实体的主键，耦合反而更高。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.common.models import User
from app.core import rbac
from app.core.audit import write_audit
from app.core.errors import Unauthenticated
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
    """扫码查询允许匿名，登录与否只影响日志里的用户维度。"""
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
    except Unauthenticated:
        return None
    return db.get(User, payload.get("sub", ""))


def require_roles(*roles: str) -> Callable[..., User]:
    """按角色直接守卫（保留的简写形式）。"""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            from app.core.errors import Forbidden

            raise Forbidden(f"该操作需要角色：{'/'.join(roles)}")
        return user

    return _guard


def client_meta(request: Request) -> dict[str, str]:
    return {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }


def require_permission(permission: str) -> Callable[..., User]:
    """权限守卫工厂。越权时写一条 deny 审计，再返回 403。"""

    def _guard(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_session),
    ) -> User:
        if not rbac.can(user.role, permission):
            meta = client_meta(request)
            write_audit(
                db,
                action="permission.denied",
                result="deny",
                user_id=user.user_id,
                username=user.username,
                role=user.role,
                method=request.method,
                path=request.url.path,
                resource=permission,
                detail={"required_permission": permission},
                ip=meta["ip"],
                user_agent=meta["user_agent"],
            )
            rbac.ensure(user.role, permission)  # 抛 403
        return user

    return _guard

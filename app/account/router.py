"""用户与运营域接口（王×× 负责，此处仅提供公共基础所需的最小登录能力）。

组长在这里只落地「JWT 签发 + 口令校验」这一段公共能力，
注册、入驻、产地认证审核、看板、预警由王×× 在 feat/identity-ops 分支扩展，
双方约定：不修改 User / Farm 的既有字段，只新增表与接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.models import User
from app.core.config import settings
from app.core.deps import get_session
from app.core.errors import NotFound, RuleViolation, Unauthenticated
from app.core.response import ok
from app.core.security import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["用户与运营域"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)


def _token_payload(user: User) -> dict:
    return {
        "access_token": create_access_token(
            user_id=user.user_id, username=user.username, role=user.role
        ),
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
    }


@router.post("/login", summary="口令登录")
def login(payload: LoginRequest, db: Session = Depends(get_session)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise Unauthenticated("用户名或口令错误")
    if user.status != "active":
        raise Unauthenticated("账号已停用")
    return ok(_token_payload(user), message="登录成功")


class DevLoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)


@router.post("/dev-login", summary="免密登录（仅开发/演示环境）")
def dev_login(payload: DevLoginRequest, db: Session = Depends(get_session)):
    """课程演示用：DEV_MODE=false 时关闭。生产环境严禁开启。"""
    if not settings.dev_mode:
        raise NotFound("接口不存在")
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None:
        raise RuleViolation("演示账号不存在，请先执行 scripts/seed.py")
    return ok(_token_payload(user), message="演示登录成功")

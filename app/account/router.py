"""用户与运营域 · 账号与鉴权接口（UC-11，王×× 负责）。

与组长在公共基础里定下的契约保持一致：
- POST /api/auth/login、POST /api/auth/dev-login 的入参与返回体字段完全不变，
  其它域（溯源、交易）已在用，不能改动；
- 本分支在此基础上补齐注册、验证码登录、当前用户、改密、农户入驻。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.account import schemas
from app.account.service import AuthService
from app.common.models import User
from app.core.config import settings
from app.core.deps import client_meta, get_session, require_permission
from app.core.errors import NotFound
from app.core.response import ok

router = APIRouter(prefix="/api/auth", tags=["用户与运营域 · 账号"])


@router.post("/register", summary="注册（UC-11）")
def register(payload: schemas.RegisterRequest, db: Session = Depends(get_session)):
    user = AuthService.register(
        db,
        username=payload.username,
        password=payload.password,
        phone=payload.phone,
        display_name=payload.display_name,
        role=payload.role,
    )
    return ok(AuthService.issue_token(user), message="注册成功，已自动登录")


@router.post("/login", summary="口令登录")
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_session)):
    meta = client_meta(request)
    data = AuthService.login(
        db, username=payload.username, password=payload.password, ip=meta["ip"], ua=meta["user_agent"]
    )
    return ok(data, message="登录成功")


@router.post("/dev-login", summary="免密登录（仅开发/演示环境）")
def dev_login(payload: schemas.DevLoginRequest, db: Session = Depends(get_session)):
    """课程演示用：DEV_MODE=false 时关闭。生产环境严禁开启。"""
    if not settings.dev_mode:
        raise NotFound("接口不存在")
    from sqlalchemy import select

    user = db.scalar(select(User).where(User.username == payload.username.lower()))
    if user is None:
        from app.core.errors import RuleViolation

        raise RuleViolation("演示账号不存在，请先执行 python -m app.db.seed")
    return ok(AuthService.issue_token(user), message="演示登录成功")


@router.post("/sms/send", summary="发送登录验证码（UC-11 验证码登录子用例）")
def send_code(payload: schemas.SendCodeRequest, db: Session = Depends(get_session)):
    return ok(AuthService.send_code(db, phone=payload.phone), message="验证码已发送")


@router.post("/sms/login", summary="验证码登录")
def login_by_code(
    payload: schemas.CodeLoginRequest, request: Request, db: Session = Depends(get_session)
):
    meta = client_meta(request)
    data = AuthService.login_by_code(
        db, phone=payload.phone, code=payload.code, ip=meta["ip"], ua=meta["user_agent"]
    )
    return ok(data, message="登录成功")


@router.get("/me", summary="当前登录用户")
def me(user: User = Depends(require_permission("user:self"))):
    return ok(AuthService.profile(user))


@router.post("/password", summary="修改口令")
def change_password(
    payload: schemas.ChangePasswordRequest,
    user: User = Depends(require_permission("user:self")),
    db: Session = Depends(get_session),
):
    AuthService.change_password(
        db, user, old_password=payload.old_password, new_password=payload.new_password
    )
    return ok(None, message="口令已更新")


# ------------------------------------------------------------------ 农户入驻
farm_router = APIRouter(prefix="/api/farms", tags=["用户与运营域 · 产地"])


@farm_router.post("", summary="农户入驻：创建产地档案（UC-12 前置）")
def apply_farm(
    payload: schemas.FarmApplyRequest,
    user: User = Depends(require_permission("farm:apply")),
    db: Session = Depends(get_session),
):
    farm = AuthService.apply_farm(db, user, payload)
    return ok(
        {
            "farm_id": farm.farm_id,
            "farm_name": farm.farm_name,
            "province": farm.province,
            "city": farm.city,
            "cert_status": farm.cert_status,
        },
        message="产地已创建，可提交认证材料",
    )


@farm_router.get("/mine", summary="我的产地列表")
def my_farms(
    user: User = Depends(require_permission("farm:read:self")),
    db: Session = Depends(get_session),
):
    items = [
        {
            "farm_id": f.farm_id,
            "farm_name": f.farm_name,
            "province": f.province,
            "city": f.city,
            "address": f.address,
            "area_mu": f.area_mu,
            "cert_status": f.cert_status,
        }
        for f in AuthService.list_my_farms(db, user)
    ]
    return ok({"items": items, "total": len(items)})

"""注册登录与鉴权的请求/响应模型（UC-11）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.common.models import Role


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="登录名，3~32 位字母数字下划线")
    password: str = Field(..., min_length=8, max_length=64)
    phone: str | None = Field(default=None, max_length=20)
    display_name: str = Field(default="", max_length=32)
    role: str = Field(default=Role.CONSUMER, description="仅允许 farmer / consumer 自助注册")

    @field_validator("username")
    @classmethod
    def _username_charset(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", v):
            raise ValueError("登录名只能由字母、数字、下划线组成")
        return v.lower()

    @field_validator("phone")
    @classmethod
    def _phone_format(cls, v: str | None) -> str | None:
        import re

        if v in (None, ""):
            return None
        if not re.fullmatch(r"1[3-9]\d{9}", v):
            raise ValueError("手机号格式不正确")
        return v

    @field_validator("role")
    @classmethod
    def _role_scope(cls, v: str) -> str:
        if v not in Role.SELF_REGISTRABLE:
            raise ValueError("平台运营与监管账号由后台开通，不支持自助注册")
        return v


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)


class DevLoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)


class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)


class CodeLoginRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)
    code: str = Field(..., min_length=4, max_length=8)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=6, max_length=64)
    new_password: str = Field(..., min_length=8, max_length=64)


class FarmApplyRequest(BaseModel):
    """农户入驻（UC-12 前置条件：完成实名注册并提交产地资质材料）。"""

    farm_name: str = Field(..., min_length=2, max_length=64)
    province: str = Field(..., min_length=2, max_length=32)
    city: str = Field(..., min_length=2, max_length=32)
    address: str = Field(default="", max_length=128)
    area_mu: float | None = Field(default=None, ge=0, le=100000)

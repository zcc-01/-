"""账号与人身份相关的业务逻辑（UC-11）。

三个设计点值得说明：

1. 登录失败的锁定不是内存计数器，而是复用 audit_log 里的 deny 记录统计，
   好处是「锁定依据」与「审计证据」是同一份数据，重启进程不会丢锁。
2. 验证码只存摘要（SHA-256 + 手机号做盐），并限制单条验证码的失败次数，
   防止 6 位码被在线爆破。
3. 注册只允许 farmer / consumer；运营与监管账号由后台开通，
   这样「越权注册运营号」在入参校验层就被拦住，不必依赖后续的角色校验。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.common.models import Farm, User
from app.core.audit import write_audit
from app.core.config import settings
from app.core.errors import (
    BizError,
    Conflict,
    ErrorCode,
    NotFound,
    RuleViolation,
    Unauthenticated,
)
from app.core.security import (
    check_password_strength,
    create_access_token,
    hash_password,
    mask_phone,
    verify_password,
)
from app.ops.models import AuditLog, LoginCode

logger = logging.getLogger("agritrace.auth")


def now() -> datetime:
    """全项目统一用「朴素 UTC 时间」入库，避免 SQLite 时区字段不一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}|{code}".encode()).hexdigest()


class AuthService:
    # ------------------------------------------------------------ 注册 / 登录
    @staticmethod
    def register(
        db: Session,
        *,
        username: str,
        password: str,
        phone: str | None,
        display_name: str,
        role: str,
    ) -> User:
        problems = check_password_strength(password)
        if problems:
            raise RuleViolation("口令不符合强度要求", data={"problems": problems})

        if db.scalar(select(User).where(User.username == username)) is not None:
            raise Conflict("该登录名已被注册")
        if phone and db.scalar(select(User).where(User.phone == phone)) is not None:
            raise Conflict("该手机号已被注册")

        user = User(
            username=username,
            password_hash=hash_password(password),
            phone=phone,
            display_name=display_name or username,
            role=role,
            status="active",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        write_audit(
            db,
            action="auth.register",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            resource="sys_user",
        )
        return user

    @staticmethod
    def issue_token(user: User) -> dict:
        return {
            "access_token": create_access_token(
                user_id=user.user_id, username=user.username, role=user.role
            ),
            "token_type": "bearer",
            "expires_in": settings.jwt_expire_minutes * 60,
            "user": AuthService.profile(user),
        }

    @staticmethod
    def profile(user: User) -> dict:
        from app.core import rbac

        return {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "role_label": rbac.ROLE_LABELS.get(user.role, user.role),
            "phone": mask_phone(user.phone),
            "permissions": rbac.permissions_of(user.role),
        }

    @staticmethod
    def _recent_fail_count(db: Session, username: str) -> int:
        since = now() - timedelta(minutes=settings.login_lock_minutes)
        return (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "auth.login.failed",
                    AuditLog.username == username,
                    AuditLog.created_at >= since,
                )
            )
            or 0
        )

    @staticmethod
    def _record_fail(db: Session, *, username: str, reason: str, ip: str, ua: str) -> None:
        write_audit(
            db,
            action="auth.login.failed",
            result="deny",
            username=username,
            path="/api/auth/login",
            resource="sys_user",
            detail={"reason": reason},
            ip=ip,
            user_agent=ua,
        )

    @staticmethod
    def login(db: Session, *, username: str, password: str, ip: str = "", ua: str = "") -> dict:
        username = username.lower()
        fails = AuthService._recent_fail_count(db, username)
        if fails >= settings.login_max_fail:
            raise BizError(
                ErrorCode.ACCOUNT_LOCKED,
                f"连续登录失败 {fails} 次，账号已锁定 {settings.login_lock_minutes} 分钟",
                423,
            )

        user = db.scalar(select(User).where(User.username == username))
        if user is None or not verify_password(password, user.password_hash):
            AuthService._record_fail(
                db, username=username, reason="bad_credential", ip=ip, ua=ua
            )
            raise Unauthenticated("用户名或口令错误")
        if user.status != "active":
            AuthService._record_fail(db, username=username, reason="inactive", ip=ip, ua=ua)
            raise Unauthenticated("账号已停用，请联系平台运营")

        write_audit(
            db,
            action="auth.login",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            method="POST",
            path="/api/auth/login",
            resource="sys_user",
            ip=ip,
            user_agent=ua,
        )
        return AuthService.issue_token(user)

    # ------------------------------------------------------------ 验证码登录
    @staticmethod
    def send_code(db: Session, *, phone: str) -> dict:
        user = db.scalar(select(User).where(User.phone == phone))
        if user is None:
            raise NotFound("该手机号未注册")
        code = "".join(secrets.choice("0123456789") for _ in range(settings.sms_code_length))
        db.add(
            LoginCode(
                phone=phone,
                code_hash=_hash_code(phone, code),
                purpose="login",
                expires_at=now() + timedelta(seconds=settings.sms_code_ttl_seconds),
                created_at=now(),
            )
        )
        db.commit()
        # 真实环境走短信网关；课程演示把验证码回显，DEV_MODE=false 时只记日志
        if settings.dev_mode:
            logger.info("【演示】短信验证码 %s -> %s", code, mask_phone(phone))
        result = {"sent": True, "ttl": settings.sms_code_ttl_seconds}
        if settings.dev_mode:
            result["debug_code"] = code
        return result

    @staticmethod
    def login_by_code(db: Session, *, phone: str, code: str, ip: str = "", ua: str = "") -> dict:
        record = db.scalar(
            select(LoginCode)
            .where(LoginCode.phone == phone, LoginCode.used.is_(False))
            .order_by(LoginCode.created_at.desc())
            .limit(1)
        )
        if record is None:
            raise RuleViolation("请先获取验证码")
        if record.expires_at < now():
            raise RuleViolation("验证码已过期，请重新获取")
        if record.fail_count >= 5:
            raise RuleViolation("验证码错误次数过多，请重新获取")

        if _hash_code(phone, code) != record.code_hash:
            record.fail_count += 1
            db.commit()
            raise Unauthenticated("验证码不正确")

        record.used = True
        user = db.scalar(select(User).where(User.phone == phone))
        if user is None or user.status != "active":
            db.commit()
            raise Unauthenticated("账号不存在或已停用")
        db.commit()
        write_audit(
            db,
            action="auth.login.sms",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            path="/api/auth/sms/login",
            resource="sys_user",
            ip=ip,
            user_agent=ua,
        )
        return AuthService.issue_token(user)

    # ------------------------------------------------------------ 口令维护
    @staticmethod
    def change_password(db: Session, user: User, *, old_password: str, new_password: str) -> None:
        if not verify_password(old_password, user.password_hash):
            raise Unauthenticated("原口令不正确")
        problems = check_password_strength(new_password)
        if problems:
            raise RuleViolation("新口令不符合强度要求", data={"problems": problems})
        if verify_password(new_password, user.password_hash):
            raise RuleViolation("新口令不能与原口令相同")
        user.password_hash = hash_password(new_password)
        db.commit()
        write_audit(
            db,
            action="auth.password.changed",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            resource="sys_user",
        )

    # ------------------------------------------------------------ 农户入驻
    @staticmethod
    def apply_farm(db: Session, user: User, payload) -> Farm:
        """农户入驻：创建产地档案。同一农户允许有多个产地（不同地块群）。"""
        farm = Farm(
            farm_name=payload.farm_name,
            owner_id=user.user_id,
            province=payload.province,
            city=payload.city,
            address=payload.address,
            area_mu=payload.area_mu,
            cert_status="pending",
        )
        db.add(farm)
        db.commit()
        db.refresh(farm)
        write_audit(
            db,
            action="farm.apply",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            resource=f"farm:{farm.farm_id}",
            detail={"farm_name": farm.farm_name},
        )
        return farm

    @staticmethod
    def list_my_farms(db: Session, user: User) -> list[Farm]:
        return list(
            db.scalars(
                select(Farm).where(Farm.owner_id == user.user_id).order_by(Farm.created_at.desc())
            )
        )

    @staticmethod
    def get_farm_owned(db: Session, user: User, farm_id: str) -> Farm:
        """资源级归属校验：非本人产地一律按「不存在」处理，避免探测他人 id。"""
        farm = db.get(Farm, farm_id)
        if farm is None or farm.owner_id != user.user_id:
            raise NotFound("产地不存在或无权访问")
        return farm

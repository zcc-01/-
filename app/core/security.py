"""口令摘要与 JWT 签发校验（公共基础层）。

- 口令：bcrypt 加盐哈希；bcrypt 不可用时退化为 PBKDF2-HMAC-SHA256（同样带盐），
  避免评审环境编译 wheel 失败导致整站起不来。
- 令牌：HS256 JWT，默认 2 小时过期（报告 3.4 安全性需求）。
- 口令强度：至少 8 位，且同时含字母与数字（附录 1「用户与鉴权」的人工修改点）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings
from app.core.errors import Unauthenticated

try:  # pragma: no cover - 取决于运行环境是否装了 bcrypt
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover
    _bcrypt = None


_PBKDF2_ROUNDS = 120_000

# 弱口令黑名单：命中即拒绝，避免演示数据出现 Passw0rd! 以外的低级口令
_WEAK_PASSWORDS = {
    "12345678",
    "password",
    "password1",
    "qwerty123",
    "abc12345",
    "11111111",
    "admin123",
}


def hash_password(password: str) -> str:
    if _bcrypt is not None:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt, hashed = stored.split("$")
        except ValueError:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(digest.hex(), hashed)
    if _bcrypt is None:
        return False
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        return False


def check_password_strength(password: str) -> list[str]:
    """返回不满足项清单，空列表表示通过。"""
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"口令长度不得少于 {settings.password_min_length} 位")
    if not re.search(r"[A-Za-z]", password):
        problems.append("口令必须包含字母")
    if not re.search(r"\d", password):
        problems.append("口令必须包含数字")
    if password.lower() in _WEAK_PASSWORDS:
        problems.append("口令过于简单，属常见弱口令")
    return problems


def create_access_token(
    *, user_id: str, username: str, role: str, expires_minutes: int | None = None
) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=expires_minutes or settings.jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": expire,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthenticated("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthenticated("无效的身份凭证") from exc


def mask_phone(phone: str | None) -> str:
    """手机号脱敏：138****8000（报告 3.4 合规性要求）。"""
    if not phone:
        return ""
    if len(phone) < 7:
        return phone[0] + "*" * (len(phone) - 1)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_name(name: str | None) -> str:
    """姓名/主体名脱敏：张*、张**社。"""
    if not name:
        return ""
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)

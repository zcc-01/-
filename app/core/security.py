"""口令摘要与 JWT 签发校验（公共基础层）。

- 口令：bcrypt 加盐哈希；bcrypt 不可用时退化为 PBKDF2-HMAC-SHA256（同样带盐），
  避免评审环境编译 wheel 失败导致整站起不来。
- 令牌：HS256 JWT，默认 2 小时过期（报告 3.6 安全性需求）。
"""

from __future__ import annotations

import hashlib
import hmac
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
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), hashed)
    if _bcrypt is None:
        return False
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(
    *, user_id: str, username: str, role: str, expires_minutes: int | None = None
) -> str:
    expire = datetime.now(UTC) + timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
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

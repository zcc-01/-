"""审计日志（用户与运营域）。

两条硬约束：
1. 只追加，不提供修改与删除接口（与溯源记录的存证口径一致）；
2. 写日志失败绝不影响主流程 —— 越权请求的首要诉求是「返回 403 并留痕」，
   日志库异常时 403 仍必须返回，因此这里吞掉异常并降级到标准日志。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.ops.models import AuditLog

logger = logging.getLogger("agritrace.audit")


def write_audit(
    db: Session,
    *,
    action: str,
    result: str = "allow",
    user_id: str | None = None,
    username: str | None = None,
    role: str | None = None,
    method: str | None = None,
    path: str | None = None,
    resource: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    commit: bool = True,
) -> None:
    """result: allow / deny（deny 即越权留痕）。"""
    try:
        db.add(
            AuditLog(
                log_id=new_id("AUD"),
                user_id=user_id,
                username=username,
                role=role,
                method=method,
                path=path,
                action=action,
                resource=resource,
                result=result,
                detail=detail or {},
                ip=ip,
                user_agent=(user_agent or "")[:128] or None,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        if commit:
            db.commit()
    except Exception as exc:  # pragma: no cover - 降级路径
        logger.warning("审计日志写入失败：%s", exc)
        db.rollback()
